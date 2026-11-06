"""G6 release decision: check the human sign-offs recorded on this workflow run before prod is offered.

Each sign-off (QA, business owner, Responsible AI) is a GitHub environment approval on the run. This check
reads them back and confirms every role signed, the sign-offs are recent, and (when governance.json asks
for it) that different people signed and nobody approved their own release. The release manager then makes
the final call on the `prod` environment.

Example (in GitHub Actions):
  python -m agent_cicd.release_decision --agent-dir agents/shift-manager --report out/decision.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .foundry import add_summary, set_outputs, write_report
from .gates import FAIL, PASS, GateResult


def load_governance(agent_dir: Path) -> dict[str, Any]:
    path = agent_dir / "governance.json"
    if not path.is_file():
        raise ValueError(f"Missing {path}: list the sign-off roles and their GitHub environments")
    governance = json.loads(path.read_text(encoding="utf-8"))
    roles = governance.get("signoffs") or {}
    problems = [f"sign-off '{k}' needs an 'environment'" for k, v in roles.items() if not v.get("environment")]
    if not roles:
        problems.append("at least one sign-off role is required")
    envs = [v.get("environment") for v in roles.values()]
    if len(set(envs)) != len(envs):
        problems.append("each sign-off role needs its own environment")
    if problems:
        raise ValueError("governance.json: " + "; ".join(problems))
    return governance


def _time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def decide_release(governance: dict[str, Any], approvals: list[dict[str, Any]], jobs: list[dict[str, Any]],
                   author: str, now: datetime | None = None) -> GateResult:
    """approvals: GitHub run approvals; jobs: run jobs (to date each sign-off by its job's completion)."""
    now = now or datetime.now(timezone.utc)
    result = GateResult(gate="release_decision")
    max_age = float(governance.get("max_signoff_age_days", 14))
    distinct = bool(governance.get("require_distinct_approvers"))
    failures = []
    signers: dict[str, str] = {}
    for key, role in governance["signoffs"].items():
        env = role["environment"]
        decisions = [a for a in approvals if env in [e.get("name") for e in a.get("environments") or []]]
        rejected = [a for a in decisions if a.get("state") == "rejected"]
        approved = [a for a in decisions if a.get("state") == "approved"]
        job_times = [_time(j.get("completed_at")) for j in jobs
                     if j.get("conclusion") == "success" and env in str(j.get("name", ""))]
        when = max([t for t in job_times if t], default=None)
        approver = (approved[-1].get("user") or {}).get("login", "") if approved else ""
        age = (now - when).total_seconds() / 86400 if when else None
        if rejected:
            status = f"rejected by {(rejected[-1].get('user') or {}).get('login', '?')}"
        elif not approved:
            status = "missing"
        elif age is None:
            status = "approved, but its job has not finished"
        elif age > max_age:
            status = f"expired ({age:.0f} days old, limit {max_age:.0f})"
        else:
            status = "ok"
        if status != "ok":
            failures.append(f"{role.get('title', key)}: {status}")
        if approver:
            signers[key] = approver
        result.rows.append({"role": role.get("title", key), "environment": env, "approver": approver,
                            "signed_at": when.isoformat() if when else "", "status": status,
                            "comment": (approved[-1].get("comment") or "") if approved else ""})
    if distinct:
        people = list(signers.values())
        if len(set(people)) != len(people):
            failures.append("The same person signed more than one role (governance.json requires distinct approvers)")
        if author and author in people:
            failures.append(f"{author} pushed this release and also signed it off (no self-approval)")
    result.outcome = FAIL if failures else PASS
    result.reasons = failures or [f"All {len(governance['signoffs'])} sign-offs are present and less than "
                                  f"{max_age:.0f} days old"]
    if not distinct and len(set(signers.values())) < len(signers):
        result.reasons.append("One person holds several roles (allowed by governance.json for this sandbox)")
    result.details = {"author": author, "require_distinct_approvers": distinct, "max_signoff_age_days": max_age}
    return result


def to_markdown(result: GateResult) -> str:
    icon = "✅" if result.outcome == PASS else "❌"
    lines = [f"### {icon} G6 release decision checks: {result.outcome}", ""]
    lines += [f"- {r}" for r in result.reasons]
    lines += ["", "| Role | Environment | Approver | Signed | Status | Comment |", "|---|---|---|---|---|---|"]
    for row in result.rows:
        comment = " ".join(row["comment"].split())[:120].replace("|", "\\|")
        lines.append(f"| {row['role']} | `{row['environment']}` | {row['approver'] or '–'} | "
                     f"{row['signed_at'][:16] or '–'} | {row['status']} | {comment or '–'} |")
    lines.append("\nThe release manager makes the final decision on the `prod` environment.")
    return "\n".join(lines) + "\n"


def _github(path: str) -> Any:
    url = f"{os.environ.get('GITHUB_API_URL', 'https://api.github.com')}{path}"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {os.environ['GH_TOKEN']}",
                                                   "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--agent-dir", type=Path, required=True)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    governance = load_governance(args.agent_dir)
    if args.validate_only:
        roles = ", ".join(f"{v.get('title', k)} ({v['environment']})" for k, v in governance["signoffs"].items())
        print(f"governance.json OK: {roles}")
        return 0
    repo, run_id = os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_RUN_ID"]
    approvals = _github(f"/repos/{repo}/actions/runs/{run_id}/approvals")
    jobs = _github(f"/repos/{repo}/actions/runs/{run_id}/jobs?filter=all&per_page=100").get("jobs") or []
    author = os.environ.get("RELEASE_AUTHOR") or os.environ.get("GITHUB_TRIGGERING_ACTOR", "")
    result = decide_release(governance, approvals, jobs, author)
    add_summary(to_markdown(result))
    write_report(args.report, {"result": result.to_dict()})
    set_outputs(decision_outcome=result.outcome)
    return 0 if result.outcome == PASS else 1


if __name__ == "__main__":
    sys.exit(main())
