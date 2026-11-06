"""G5 evidence: did real people try this exact release candidate in the test project, and did they like it?

Testers rate answers in Foundry (Human Evaluation form in the Preview web app, or 👍/👎 on a trace).
Foundry stores each rating as a `gen_ai.evaluation.result` event in the test project's Application Insights.
This check counts the ratings for the release-candidate version and applies the rules in eval/uat.json.
It produces evidence for the approvers; the sign-off itself stays a GitHub environment approval.

Examples:
  python -m agent_cicd.uat_evidence --agent-dir agents/shift-manager --validate-only
  python -m agent_cicd.uat_evidence --agent-dir agents/shift-manager --env test --version 3 --report out/uat.json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .definition import render_agent
from .foundry import add_summary, project_client, set_outputs, write_report
from .gates import FAIL, INCONCLUSIVE, PASS, GateResult
from .policy_gate import COGNITIVE_API, _arm_token, _subscription, arm_getter

DEFAULT_RULES = {"min_rated_answers": 10, "min_thumbs_up_rate": 0.8, "evaluation_names": ["task_completion"],
                 "sources": ["builder", "end_user"], "lookback_days": 30}

_KQL = """
customEvents
| where timestamp > datetime({since})
| where name == "gen_ai.evaluation.result"
| extend d = customDimensions
| extend internal = parse_json(tostring(d["internal_properties"]))
| extend source = tostring(d["microsoft.gen_ai.human_evaluation.source"]),
         actor = tostring(d["microsoft.gen_ai.evaluation.actor.type"]),
         eval_name = tostring(d["gen_ai.evaluation.name"]),
         score = todouble(d["gen_ai.evaluation.score.value"]),
         label = tostring(d["gen_ai.evaluation.score.label"]),
         explanation = tostring(d["gen_ai.evaluation.explanation"]),
         agent_name = coalesce(tostring(d["gen_ai.agent.name"]), tostring(internal["gen_ai.agent.name"])),
         agent_version = coalesce(tostring(d["gen_ai.agent.version"]), tostring(internal["gen_ai.agent.version"])),
         response_id = coalesce(tostring(d["gen_ai.response.id"]), operation_Id)
| where isnotempty(source) or actor == "human"
| project timestamp, source, actor, eval_name, score, label, explanation, agent_name, agent_version, response_id,
          trace_id = operation_Id
| order by timestamp desc
| take 5000
"""


@dataclass
class Rating:
    timestamp: str
    source: str
    eval_name: str
    score: float | None
    label: str
    explanation: str
    agent_name: str
    agent_version: str
    response_id: str
    trace_id: str = ""

    @property
    def thumbs_up(self) -> bool:
        return self.label.lower() == "pass" or (self.score is not None and self.score >= 1.0)


def load_rules(agent_dir: Path) -> dict[str, Any]:
    path = agent_dir / "eval" / "uat.json"
    rules = {**DEFAULT_RULES, **(json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {})}
    problems = []
    if int(rules["min_rated_answers"]) < 1:
        problems.append("min_rated_answers must be at least 1")
    if not 0 <= float(rules["min_thumbs_up_rate"]) <= 1:
        problems.append("min_thumbs_up_rate must be between 0 and 1")
    if problems:
        raise ValueError("eval/uat.json: " + "; ".join(problems))
    return rules


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def decide_uat(ratings: list[Rating], rules: dict[str, Any], agent_name: str, version: str,
               version_created: datetime | None) -> GateResult:
    """Keep ratings for this agent version, newest rating per answer, then apply the UAT rules."""
    result = GateResult(gate="uat")
    sources = set(rules.get("sources") or [])
    names = set(rules.get("evaluation_names") or [])
    matched: dict[str, tuple[Rating, str]] = {}
    other_versions = 0
    for rating in sorted(ratings, key=lambda r: r.timestamp, reverse=True):
        if sources and rating.source and rating.source not in sources:
            continue
        if names and rating.eval_name and rating.eval_name not in names:
            continue
        if rating.agent_name and rating.agent_name != agent_name:
            continue
        if rating.agent_version:
            if rating.agent_version != str(version):
                other_versions += 1
                continue
            how = "version"
        elif version_created and _parse_time(rating.timestamp) >= version_created:
            # Some feedback paths don't stamp the version; in the test project only release candidates are
            # deployed, so a rating given after this version was created belongs to it.
            how = "time"
        else:
            continue
        matched.setdefault(rating.response_id or rating.trace_id or rating.timestamp, (rating, how))

    rated = len(matched)
    up = sum(1 for r, _ in matched.values() if r.thumbs_up)
    rate = up / rated if rated else 0.0
    minimum, floor = int(rules["min_rated_answers"]), float(rules["min_thumbs_up_rate"])
    if rated < minimum:
        result.outcome = INCONCLUSIVE
        result.reasons.append(f"Only {rated} rated answer(s) for v{version}; UAT needs at least {minimum}. "
                              "Ask testers to rate more answers in the test project, then re-run this job.")
    elif rate < floor:
        result.outcome = FAIL
        result.reasons.append(f"{rate:.0%} thumbs up ({up}/{rated}) is below the agreed {floor:.0%}")
    else:
        result.outcome = PASS
        result.reasons.append(f"{rated} rated answers, {rate:.0%} thumbs up (rules: ≥{minimum}, ≥{floor:.0%})")
    if other_versions:
        result.reasons.append(f"{other_versions} rating(s) for other versions were ignored")
    down = [r for r, _ in matched.values() if not r.thumbs_up]
    result.rows = [{"timestamp": r.timestamp, "source": r.source, "thumbs_up": r.thumbs_up,
                    "explanation": r.explanation, "response_id": r.response_id, "trace_id": r.trace_id,
                    "matched_by": how} for r, how in matched.values()]
    result.details = {"rated": rated, "thumbs_up": up, "thumbs_down": len(down), "rate": round(rate, 4),
                      "rules": rules, "version": str(version),
                      "matched_by_time": sum(1 for _, how in matched.values() if how == "time"),
                      "by_source": {s: sum(1 for r, _ in matched.values() if r.source == s)
                                    for s in sorted({r.source for r, _ in matched.values()})}}
    return result


def to_markdown(result: GateResult, agent_name: str) -> str:
    icon = {PASS: "✅", INCONCLUSIVE: "⏳", FAIL: "❌"}.get(result.outcome, "⚠️")
    d = result.details
    lines = [f"### {icon} G5 UAT evidence: {agent_name} v{d.get('version')}: {result.outcome}", ""]
    lines += [f"- {r}" for r in result.reasons]
    if d.get("rated"):
        lines.append(f"- By source: " + ", ".join(f"{k or 'unknown'} {v}" for k, v in d["by_source"].items()))
        if d.get("matched_by_time"):
            lines.append(f"- {d['matched_by_time']} rating(s) had no version stamp and were matched by time")
    down = [r for r in result.rows if not r["thumbs_up"]]
    if down:
        lines += ["", "**👎 answers** (candidates for new test cases):", "",
                  "| When | Source | Comment | Trace |", "|---|---|---|---|"]
        for row in down[:20]:
            comment = " ".join(str(row["explanation"] or "–").split())[:160].replace("|", "\\|")
            lines.append(f"| {row['timestamp'][:16]} | {row['source']} | {comment} | `{row['trace_id']}` |")
    return "\n".join(lines) + "\n"


def _app_insights_id(endpoint: str, resource_group: str) -> str:
    from urllib.parse import urlparse
    account = (urlparse(endpoint).hostname or "").split(".")[0]
    project = endpoint.rstrip("/").split("/projects/")[-1]
    get = arm_getter(_arm_token())
    path = (f"/subscriptions/{_subscription()}/resourceGroups/{resource_group}/providers/"
            f"Microsoft.CognitiveServices/accounts/{account}/projects/{project}/connections")
    for connection in (get(path, COGNITIVE_API) or {}).get("value") or []:
        props = connection.get("properties") or {}
        if props.get("category") == "AppInsights":
            return (props.get("metadata") or {}).get("ResourceId") or props.get("target")
    raise RuntimeError(f"Project {project} has no Application Insights connection: UAT feedback is not recorded")


def query_ratings(resource_id: str, since: datetime) -> list[Rating]:
    import requests
    from azure.identity import AzureCliCredential

    token = AzureCliCredential(process_timeout=120).get_token("https://api.loganalytics.io/.default").token
    response = requests.post(f"https://api.loganalytics.io/v1{resource_id}/query",
                             headers={"Authorization": f"Bearer {token}"},
                             json={"query": _KQL.format(since=since.strftime("%Y-%m-%dT%H:%M:%SZ"))}, timeout=120)
    if response.status_code != 200:
        raise RuntimeError(f"App Insights query failed ({response.status_code}): {response.text[:400]}")
    table = response.json()["tables"][0]
    columns = [c["name"] for c in table["columns"]]
    ratings = []
    for values in table["rows"]:
        row = dict(zip(columns, values))
        ratings.append(Rating(timestamp=str(row["timestamp"]), source=row["source"] or "",
                              eval_name=row["eval_name"] or "", score=row["score"], label=row["label"] or "",
                              explanation=row["explanation"] or "", agent_name=row["agent_name"] or "",
                              agent_version=row["agent_version"] or "", response_id=row["response_id"] or "",
                              trace_id=row["trace_id"] or ""))
    return ratings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--agent-dir", type=Path, required=True)
    parser.add_argument("--env", default="test")
    parser.add_argument("--version")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    rules = load_rules(args.agent_dir)
    if args.validate_only:
        print(f"uat.json OK: ≥{rules['min_rated_answers']} rated answers, ≥{rules['min_thumbs_up_rate']:.0%} thumbs up")
        return 0
    if not args.version:
        parser.error("--version is required")

    rendered = render_agent(args.agent_dir, args.env, require_configured=True)
    raw_config = json.loads((args.agent_dir / "config" / f"{args.env}.json").read_text(encoding="utf-8"))
    with project_client(rendered.project_endpoint) as client:
        created = client.agents.get_version(agent_name=rendered.agent_name, agent_version=str(args.version)).created_at
    version_created = (datetime.fromtimestamp(created, timezone.utc) if isinstance(created, (int, float))
                       else created if isinstance(created, datetime) else None)
    since = max(version_created or datetime.min.replace(tzinfo=timezone.utc),
                datetime.now(timezone.utc) - timedelta(days=float(rules["lookback_days"])))
    resource_id = _app_insights_id(rendered.project_endpoint, raw_config["azure_resource_group"])
    result = decide_uat(query_ratings(resource_id, since), rules, rendered.agent_name, args.version, version_created)
    add_summary(to_markdown(result, rendered.agent_name))
    write_report(args.report, {"result": result.to_dict(), "app_insights": resource_id})
    set_outputs(uat_outcome=result.outcome)
    return 0 if result.outcome == PASS else 1


if __name__ == "__main__":
    sys.exit(main())
