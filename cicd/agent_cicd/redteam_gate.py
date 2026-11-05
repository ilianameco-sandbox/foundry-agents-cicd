"""Gate 2: red-team one exact agent version in Foundry, or reuse a recent passing scan of the same agent.

A scan is skipped only when a completed scan in the last `reuse_days` days covered the identical
fingerprint (definition + model version) under the identical policy and still passes the gate today.

Examples:
  python -m agent_cicd.redteam_gate --agent-dir agents/shift-manager --env dev --validate-only
  python -m agent_cicd.redteam_gate --agent-dir agents/shift-manager --env dev --version 5 \
      --fingerprint <sha256> --report out/redteam.json [--force]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from .definition import render_agent
from .foundry import add_summary, project_client, set_outputs, to_plain, wait_for_run, write_report
from .gates import FAIL, GateResult, decide_redteam, to_markdown


def load_policy(agent_dir: Path) -> dict[str, Any]:
    policy = json.loads((agent_dir / "eval" / "redteam.json").read_text(encoding="utf-8"))
    for key in ("eval_name", "evaluators", "categories", "attack_strategies", "num_turns", "min_attacks_per_category"):
        if key not in policy:
            raise ValueError(f"redteam.json: '{key}' is required")
    for category, rule in policy["categories"].items():
        if not 0.0 <= float(rule["max_asr"]) < 1.0:
            raise ValueError(f"redteam.json: categories.{category}.max_asr must be between 0 and 1")
        if category not in policy["evaluators"]:
            raise ValueError(f"redteam.json: category '{category}' has no evaluator, so it can never be scored")
        if rule.get("success_rule", "service") not in ("service", "evaluator_score"):
            raise ValueError(f"redteam.json: categories.{category}.success_rule must be 'service' or 'evaluator_score'")
    # The cloud agent red team only generates attacks from a prohibited-actions taxonomy; the evaluators
    # then score every conversation for each category.
    policy.setdefault("taxonomy_risks", ["prohibited_actions"])
    unsupported = set(policy["taxonomy_risks"]) - {"prohibited_actions"}
    if unsupported:
        raise ValueError(f"redteam.json: taxonomy_risks only supports prohibited_actions, not {sorted(unsupported)}")
    return policy


def policy_hash(policy: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(policy, sort_keys=True).encode("utf-8")).hexdigest()[:32]


def _criteria(policy: dict[str, Any], judge_deployment: str) -> list[dict[str, Any]]:
    criteria = []
    for name, spec in policy["evaluators"].items():
        criterion = {"type": "azure_ai_evaluator", "name": name, "evaluator_name": f"builtin.{name}",
                     "evaluator_version": spec["version"]}
        if spec.get("needs_judge"):
            criterion["initialization_parameters"] = {"deployment_name": judge_deployment}
        criteria.append(criterion)
    return criteria


def _find_or_create_eval(openai_client, policy: dict[str, Any], judge_deployment: str):
    for evaluation in openai_client.evals.list(limit=100):
        if evaluation.name == policy["eval_name"]:
            return evaluation
    return openai_client.evals.create(
        name=policy["eval_name"],
        data_source_config={"type": "azure_ai_source", "scenario": "red_team"},
        testing_criteria=_criteria(policy, judge_deployment),
    )


def _reusable_run(openai_client, eval_id: str, agent_name: str, agent_fingerprint: str, policy: dict[str, Any]):
    """Return (run, items, result) for the newest passing scan of the same fingerprint and policy, or None."""
    cutoff = time.time() - float(policy.get("reuse_days", 30)) * 86400
    wanted = {"agent_name": agent_name, "fingerprint": agent_fingerprint[:32], "policy": policy_hash(policy)}
    for run in openai_client.evals.runs.list(eval_id=eval_id, limit=50, order="desc"):
        metadata = run.metadata or {}
        if run.status != "completed" or (run.created_at or 0) < cutoff:
            continue
        if any(metadata.get(key) != value for key, value in wanted.items()):
            continue
        items = [to_plain(i) for i in openai_client.evals.runs.output_items.list(run_id=run.id, eval_id=eval_id)]
        result = decide_redteam(run.status, items, policy)
        if result.allows_promotion:
            return run, items, result
    return None


def _prepare_taxonomy(client, name: str, target, policy: dict[str, Any], version: str) -> tuple[str, list[str]]:
    """Create (or reuse) the generated taxonomy and enable the sub-categories the reviewed policy approves.

    Foundry generates every sub-category disabled; a human must confirm which ones apply. In CI that
    confirmation is `enabled_subcategories` in redteam.json ("all", or a list of sub-category names).
    """
    from azure.ai.projects.models import AgentTaxonomyInput, EvaluationTaxonomy, RiskCategory
    from azure.core.exceptions import HttpResponseError

    # `get` on a missing taxonomy returns HTTP 500 rather than 404, and `list` breaks after the first page
    # (its next link is an internal http:// URL), so try `get` and treat 404/500 as "not created yet".
    try:
        taxonomy = client.beta.evaluation_taxonomies.get(name=name)
    except HttpResponseError as error:
        if error.status_code not in (404, 500):
            raise
        taxonomy = None
    if taxonomy is None:
        try:
            taxonomy = client.beta.evaluation_taxonomies.create(
                name=name,
                taxonomy=EvaluationTaxonomy(
                    description=f"Red-team gate taxonomy for {target.name} v{version}",
                    taxonomy_input=AgentTaxonomyInput(
                        risk_categories=[RiskCategory[c.upper()] for c in policy["taxonomy_risks"]], target=target),
                ),
            )
        except HttpResponseError as error:
            if error.status_code != 409:
                raise
            taxonomy = client.beta.evaluation_taxonomies.get(name=name)
    # Sub-categories are generated asynchronously; wait for them before editing.
    deadline = time.monotonic() + 600
    body = taxonomy.as_dict()
    while not body.get("taxonomyCategories") and time.monotonic() < deadline:
        time.sleep(15)
        body = client.beta.evaluation_taxonomies.get(name=name).as_dict()
    if not body.get("taxonomyCategories"):
        raise RuntimeError(f"Taxonomy {name} produced no categories within 10 minutes")
    wanted = policy.get("enabled_subcategories", "all")
    available, enabled = set(), []
    for category in body.get("taxonomyCategories") or []:
        for sub in category.get("subCategories") or []:
            available.add(sub["name"])
            sub["enabled"] = wanted == "all" or sub["name"] in wanted
            if sub["enabled"]:
                enabled.append(sub["name"])
    if wanted != "all" and set(wanted) - available:
        raise ValueError(f"redteam.json: unknown taxonomy sub-categories {sorted(set(wanted) - available)}")
    if not enabled:
        raise RuntimeError("The generated taxonomy has no enabled sub-categories, so no attacks would run")
    # The service expects the complete taxonomy document back, including read-only fields.
    for attempt in range(1, 6):
        try:
            updated = client.beta.evaluation_taxonomies.update(name=name, taxonomy=body)
            break
        except HttpResponseError as error:
            if attempt == 5 or (error.status_code or 0) < 500:
                raise
            time.sleep(20 * attempt)
    return updated.id, sorted(enabled)


def run_redteam_gate(agent_dir: Path, environment: str, version: str, agent_fingerprint: str,
                     force: bool, report: Path | None) -> GateResult:
    from azure.ai.projects.models import AzureAIAgentTarget

    rendered = render_agent(agent_dir, environment, require_configured=True)
    policy = load_policy(agent_dir)
    with project_client(rendered.project_endpoint) as client, client.get_openai_client() as openai_client:
        evaluation = _find_or_create_eval(openai_client, policy, rendered.judge_deployment)

        if not force:
            reused = _reusable_run(openai_client, evaluation.id, rendered.agent_name, agent_fingerprint, policy)
            if reused:
                run, items, result = reused
                result.reasons.insert(0, f"Reused scan {run.id}: same agent fingerprint and policy, still passing")
                result.details.update({"eval_id": evaluation.id, "run_id": run.id, "reused": True,
                                       "report_url": getattr(run, "report_url", None)})
                write_report(report, {"result": result.to_dict(), "run": to_plain(run), "output_items": items})
                return result

        target = AzureAIAgentTarget(name=rendered.agent_name, version=str(version),
                                    tool_descriptions=list(rendered.tool_descriptions))
        taxonomy_id, enabled = _prepare_taxonomy(
            client, f"{rendered.agent_name}-{agent_fingerprint[:12]}-{policy_hash(policy)[:8]}", target, policy, version)
        print(f"[redteam] taxonomy {taxonomy_id}: {len(enabled)} sub-categories enabled", flush=True)
        metadata = {"agent_name": rendered.agent_name, "agent_version": str(version),
                    "fingerprint": agent_fingerprint[:32], "policy": policy_hash(policy),
                    "git_sha": os.environ.get("GITHUB_SHA", "local")[:40]}
        run = openai_client.evals.runs.create(
            eval_id=evaluation.id,
            name=f"{rendered.agent_name} v{version} red-team gate",
            metadata=metadata,
            data_source={
                "type": "azure_ai_red_team",
                "item_generation_params": {
                    "type": "red_team_taxonomy",
                    "attack_strategies": policy["attack_strategies"],
                    "num_turns": int(policy["num_turns"]),
                    "source": {"type": "file_id", "id": taxonomy_id},
                },
                "target": target.as_dict(),
            },
        )
        run = wait_for_run(lambda: openai_client.evals.runs.retrieve(run_id=run.id, eval_id=evaluation.id),
                           float(policy.get("timeout_minutes", 240)), "redteam")
        items = []
        if run.status == "completed":
            items = [to_plain(i) for i in openai_client.evals.runs.output_items.list(run_id=run.id, eval_id=evaluation.id)]

    result = decide_redteam(run.status, items, policy)
    result.details.update({"eval_id": evaluation.id, "run_id": run.id, "reused": False,
                           "report_url": getattr(run, "report_url", None),
                           "taxonomy_id": taxonomy_id, "enabled_subcategories": enabled})
    if run.status != "completed" and getattr(run, "error", None):
        result.reasons.append(f"Service error: {to_plain(run.error)}")
    write_report(report, {"result": result.to_dict(), "run": to_plain(run), "output_items": items})
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--agent-dir", type=Path, required=True)
    parser.add_argument("--env", required=True)
    parser.add_argument("--version")
    parser.add_argument("--fingerprint", help="Fingerprint recorded by the deploy step")
    parser.add_argument("--force", action="store_true", help="Always run a new scan")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--results-file", type=Path, help="Decide from a downloaded results .jsonl (offline)")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()

    policy = load_policy(args.agent_dir)
    if args.validate_only:
        print(f"Red-team policy is valid ({len(policy['categories'])} categories, hash {policy_hash(policy)})")
        return 0
    if args.results_file:
        items = [json.loads(line) for line in args.results_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        result = decide_redteam("completed", items, policy)
    else:
        if not args.version or not args.fingerprint:
            parser.error("--version and --fingerprint are required to run the gate")
        try:
            result = run_redteam_gate(args.agent_dir, args.env, args.version, args.fingerprint, args.force, args.report)
        except Exception as error:  # noqa: BLE001 - any unexpected error fails the gate closed
            result = GateResult(gate="redteam", outcome=FAIL,
                                reasons=[f"Gate error (fail closed): {type(error).__name__}: {error}"])
    link = result.details.get("report_url")
    add_summary(to_markdown(result, header=f"Foundry run: {link}" if link else ""))
    set_outputs(redteam_outcome=result.outcome, redteam_reused=str(result.details.get("reused", False)).lower())
    return 0 if result.allows_promotion else 1


if __name__ == "__main__":
    raise SystemExit(main())
