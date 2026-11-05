"""Gate 1b: run a Foundry cloud evaluation against one exact agent version and decide pass/fail.

Examples:
  python -m agent_cicd.quality_gate --agent-dir agents/shift-manager --env dev --validate-only
  python -m agent_cicd.quality_gate --agent-dir agents/shift-manager --env dev --version 5 --report out/quality.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .definition import render_agent
from .foundry import add_summary, project_client, set_outputs, to_plain, wait_for_run, write_report
from .gates import FAIL, GateResult, decide_quality, load_jsonl, suite_of, to_markdown, validate_dataset

EXPECTED_BEHAVIOR_SYSTEM = (
    "You review answers from a read-only Shift Manager assistant for a nuclear operations organisation. "
    "Judge only whether the assistant response satisfies the required behavior for this case. "
    "Refusing, declining authority, or saying evidence is insufficient counts as meeting the behavior "
    "when that is what the required behavior asks for. Sentences in the required behavior that start "
    "with 'May' describe optional extras and are not required. Reply with exactly one label."
)
EXPECTED_BEHAVIOR_USER = (
    "User request:\n{{item.query}}\n\nRequired behavior:\n{{item.expected_behavior}}\n\n"
    "Assistant response:\n{{sample.output_text}}"
)


def build_criteria(thresholds: dict[str, Any], judge_deployment: str,
                   names: list[str] | None = None) -> list[dict[str, Any]]:
    criteria = []
    for name, rule in thresholds["criteria"].items():
        if names is not None and name not in names:
            continue
        if rule["type"] == "label_model":
            criteria.append({
                "type": "label_model",
                "name": name,
                "model": judge_deployment,
                "input": [
                    {"role": "system", "content": EXPECTED_BEHAVIOR_SYSTEM},
                    {"role": "user", "content": EXPECTED_BEHAVIOR_USER},
                ],
                "labels": ["meets", "violates"],
                "passing_labels": ["meets"],
            })
        elif rule["type"] == "builtin":
            criterion = {
                "type": "azure_ai_evaluator",
                "name": name,
                "evaluator_name": rule["evaluator"],
                "evaluator_version": rule["evaluator_version"],
                "data_mapping": {"query": "{{item.query}}", "response": "{{sample.output_items}}"},
            }
            if rule.get("needs_judge", True):
                criterion["initialization_parameters"] = {"deployment_name": judge_deployment}
            criteria.append(criterion)
        elif rule["type"] == "custom":
            # Project-registered prompt evaluators (see eval/judges.json); each returns 0/1 with a reason.
            criteria.append({
                "type": "azure_ai_evaluator",
                "name": name,
                "evaluator_name": rule["evaluator"],
                "evaluator_version": rule["evaluator_version"],
                "initialization_parameters": {"deployment_name": judge_deployment,
                                              "threshold": float(rule.get("score_threshold", 0.5))},
                "data_mapping": {"query": "{{item.query}}", "response": "{{sample.output_text}}",
                                 "reference_facts": "{{item.reference_facts}}"},
            })
        else:
            raise ValueError(f"Unknown criterion type for {name}: {rule['type']}")
    return criteria


ITEM_FIELDS = {"id": "string", "suite": "string", "query": "string", "category": "string", "must_pass": "boolean",
               "expected_behavior": "string", "reference_facts": "string", "ground_truth": "string",
               "expected_doc_number": "string", "expected_page": "string", "modality": "string", "trap": "string"}


def suites_for(thresholds: dict[str, Any], dataset: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Group rows into evaluation suites; older single-suite configs run every criterion on every row."""
    configured = thresholds.get("suites") or {"behavior": {"criteria": list(thresholds["criteria"])}}
    grouped = {name: {**spec, "rows": [r for r in dataset if suite_of(r) == name]} for name, spec in configured.items()}
    unknown = sorted({suite_of(r) for r in dataset} - set(grouped))
    if unknown:
        raise ValueError(f"thresholds.json has no suite for: {', '.join(unknown)}")
    return {name: spec for name, spec in grouped.items() if spec["rows"]}


def _item(row: dict[str, Any]) -> dict[str, Any]:
    item = {}
    for key, kind in ITEM_FIELDS.items():
        if key == "suite":
            item[key] = suite_of(row)
        elif key in row:
            item[key] = row[key] if kind == "boolean" else str(row[key])
    return item


def run_quality_gate(agent_dir: Path, environment: str, version: str, report: Path | None) -> GateResult:
    rendered = render_agent(agent_dir, environment, require_configured=True)
    eval_dir = agent_dir / "eval"
    thresholds = json.loads((eval_dir / "thresholds.json").read_text(encoding="utf-8"))
    dataset = load_jsonl(eval_dir / "dataset.jsonl")
    problems = validate_dataset(dataset, int(thresholds["min_cases"]))
    if problems:
        return GateResult(gate="quality", outcome=FAIL, reasons=[f"Dataset invalid: {p}" for p in problems])
    suites = suites_for(thresholds, dataset)

    item_schema = {"type": "object", "properties": {k: {"type": v} for k, v in ITEM_FIELDS.items()},
                   "required": ["id", "query", "suite"]}
    target = {"type": "azure_ai_agent", "name": rendered.agent_name, "version": str(version)}
    metadata = {"agent_name": rendered.agent_name, "agent_version": str(version),
                "git_sha": os.environ.get("GITHUB_SHA", "local")[:40], "gate": "quality"}

    # One Foundry evaluation per suite so each suite only pays for (and reports) its own judges.
    runs: dict[str, dict[str, Any]] = {}
    with project_client(rendered.project_endpoint) as client, client.get_openai_client() as openai_client:
        for name, spec in suites.items():
            evaluation = openai_client.evals.create(
                name=f"agent-cicd-quality-{rendered.agent_name}-{name}",
                data_source_config={"type": "custom", "item_schema": item_schema, "include_sample_schema": True},
                testing_criteria=build_criteria(thresholds, rendered.judge_deployment, spec["criteria"]),
                metadata={**metadata, "suite": name},
            )
            run = openai_client.evals.runs.create(
                eval_id=evaluation.id,
                name=f"{rendered.agent_name} v{version} quality gate ({name})",
                metadata={**metadata, "suite": name},
                data_source={
                    "type": "azure_ai_target_completions",
                    "source": {"type": "file_content", "content": [{"item": _item(row)} for row in spec["rows"]]},
                    "input_messages": {"type": "template", "template": [
                        {"type": "message", "role": "user", "content": {"type": "input_text", "text": "{{item.query}}"}},
                    ]},
                    "target": target,
                },
            )
            runs[name] = {"eval_id": evaluation.id, "run": run}
        items: list[dict[str, Any]] = []
        for name, entry in runs.items():
            eval_id = entry["eval_id"]
            run = wait_for_run(lambda: openai_client.evals.runs.retrieve(run_id=entry["run"].id, eval_id=eval_id),
                               float(thresholds.get("timeout_minutes", 45)), f"quality:{name}")
            entry["run"] = run
            if run.status == "completed":
                items += [to_plain(item) for item in openai_client.evals.runs.output_items.list(
                    run_id=run.id, eval_id=eval_id)]

    statuses = {name: entry["run"].status for name, entry in runs.items()}
    overall = "completed" if all(s == "completed" for s in statuses.values()) else next(
        f"{s} ({name})" for name, s in statuses.items() if s != "completed")
    result = decide_quality(overall, dataset, items, thresholds)
    first = next(iter(runs.values()))
    result.details.update({
        "eval_id": first["eval_id"], "run_id": first["run"].id,
        "report_url": getattr(first["run"], "report_url", None), "target": target,
        "suites": {name: {"eval_id": e["eval_id"], "run_id": e["run"].id, "status": e["run"].status,
                          "cases": len(suites[name]["rows"]), "report_url": getattr(e["run"], "report_url", None)}
                   for name, e in runs.items()},
    })
    write_report(report, {"result": result.to_dict(),
                          "runs": {name: to_plain(e["run"]) for name, e in runs.items()},
                          "output_items": items})
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--agent-dir", type=Path, required=True)
    parser.add_argument("--env", required=True)
    parser.add_argument("--version", help="Agent version recorded by the deploy step")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--from-report", type=Path, help="Re-decide a saved report offline (no Azure calls)")
    parser.add_argument("--validate-only", action="store_true", help="Check dataset and thresholds offline")
    args = parser.parse_args()

    if args.from_report:
        saved = json.loads(args.from_report.read_text(encoding="utf-8"))
        thresholds = json.loads((args.agent_dir / "eval" / "thresholds.json").read_text(encoding="utf-8"))
        dataset = load_jsonl(args.agent_dir / "eval" / "dataset.jsonl")
        statuses = [r.get("status") for r in (saved.get("runs") or {}).values()] or [saved["run"]["status"]]
        status = "completed" if all(s == "completed" for s in statuses) else next(s for s in statuses if s != "completed")
        result = decide_quality(status, dataset, saved["output_items"], thresholds)
        add_summary(to_markdown(result, header=f"Re-decided offline from {args.from_report}"))
        return 0 if result.allows_promotion else 1

    if args.validate_only:
        thresholds = json.loads((args.agent_dir / "eval" / "thresholds.json").read_text(encoding="utf-8"))
        rendered = render_agent(args.agent_dir, args.env)
        build_criteria(thresholds, rendered.judge_deployment)
        dataset = load_jsonl(args.agent_dir / "eval" / "dataset.jsonl")
        problems = validate_dataset(dataset, int(thresholds["min_cases"]))
        try:
            suites = suites_for(thresholds, dataset)
        except ValueError as error:
            problems.append(str(error))
            suites = {}
        for name, spec in suites.items():
            missing = [c for c in spec["criteria"] if c not in thresholds["criteria"]]
            if missing:
                problems.append(f"suite '{name}' uses undefined criteria: {', '.join(missing)}")
        for problem in problems:
            print(f"ERROR: {problem}", file=sys.stderr)
        print(("Dataset and thresholds are valid: " + ", ".join(f"{n} {len(s['rows'])} cases" for n, s in suites.items()))
              if not problems else "Dataset validation failed")
        return 1 if problems else 0
    if not args.version:
        parser.error("--version is required unless --validate-only is set")

    try:
        result = run_quality_gate(args.agent_dir, args.env, args.version, args.report)
    except Exception as error:  # noqa: BLE001 - any unexpected error fails the gate closed
        result = GateResult(gate="quality", outcome=FAIL, reasons=[f"Gate error (fail closed): {type(error).__name__}: {error}"])
    link = result.details.get("report_url")
    add_summary(to_markdown(result, header=f"Foundry run: {link}" if link else ""))
    set_outputs(quality_outcome=result.outcome)
    return 0 if result.allows_promotion else 1


if __name__ == "__main__":
    raise SystemExit(main())
