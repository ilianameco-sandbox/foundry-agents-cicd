"""Smoke test: call one exact agent version with a few known prompts and check each answer.

Checks come from `<agent-dir>/eval/smoke.json`; without that file a single capability prompt must get a
non-empty answer. Each check may set `must_match` (regexes) and `must_not_contain` (case-insensitive text),
and is retried up to `attempts` times so one unlucky answer does not roll back a healthy deploy.

Examples:
  python -m agent_cicd.smoke --agent-dir agents/shift-manager --env dev --version 5
  python -m agent_cicd.smoke --agent-dir agents/shift-manager --env dev --validate-only
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from .definition import render_agent
from .foundry import project_client

PROMPT = "In one sentence, what kinds of questions can you help a shift manager with?"


def load_smoke(agent_dir: Path) -> dict[str, Any]:
    path = agent_dir / "eval" / "smoke.json"
    config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    checks = config.get("checks") or [{"id": "capability", "prompt": PROMPT}]
    for check in checks:
        if not str(check.get("prompt") or "").strip():
            raise ValueError(f"smoke.json: check '{check.get('id')}' has no prompt")
        for pattern in check.get("must_match") or []:
            re.compile(pattern)
    return {"attempts": max(1, int(config.get("attempts", 1))), "checks": checks}


def smoke_prompts(agent_dir: Path) -> list[str]:
    return [check["prompt"] for check in load_smoke(agent_dir)["checks"]]


def check_answer(check: dict[str, Any], text: str) -> list[str]:
    """Return the problems with one answer; an empty list means the check passed."""
    text = (text or "").strip()
    if not text:
        return ["empty answer"]
    problems = [f"answer does not match /{p}/" for p in check.get("must_match") or [] if not re.search(p, text)]
    problems += [f"answer contains '{s}'" for s in check.get("must_not_contain") or [] if s.lower() in text.lower()]
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--agent-dir", type=Path, required=True)
    parser.add_argument("--env", required=True)
    parser.add_argument("--version")
    parser.add_argument("--validate-only", action="store_true", help="Check smoke.json offline")
    args = parser.parse_args()

    smoke = load_smoke(args.agent_dir)
    if args.validate_only:
        print(f"Smoke checks are valid: {', '.join(c.get('id', '?') for c in smoke['checks'])}")
        return 0
    if not args.version:
        parser.error("--version is required unless --validate-only is set")

    rendered = render_agent(args.agent_dir, args.env, require_configured=True)
    reference = {"name": rendered.agent_name, "version": str(args.version), "type": "agent_reference"}
    failed = 0
    try:
        with project_client(rendered.project_endpoint) as client, client.get_openai_client() as openai_client:
            for check in smoke["checks"]:
                for attempt in range(1, smoke["attempts"] + 1):
                    response = openai_client.responses.create(input=check["prompt"],
                                                              extra_body={"agent_reference": reference})
                    text = (response.output_text or "").strip()
                    problems = check_answer(check, text)
                    if not problems:
                        break
                    print(f"  {check.get('id')}: attempt {attempt} failed: {'; '.join(problems)}")
                status = "PASS" if not problems else "FAIL"
                failed += bool(problems)
                print(f"SMOKE {status} [{check.get('id')}] ({rendered.agent_name} v{args.version}): {text[:300]}")
    except Exception as error:  # noqa: BLE001 - any failure to answer fails the smoke test
        print(f"SMOKE FAIL: {type(error).__name__}: {error}")
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
