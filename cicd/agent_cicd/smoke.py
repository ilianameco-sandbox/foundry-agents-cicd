"""Smoke test: call one exact agent version once and require a non-empty answer.

Example:
  python -m agent_cicd.smoke --agent-dir agents/shift-manager --env dev --version 5
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .definition import render_agent
from .foundry import project_client

PROMPT = "In one sentence, what kinds of questions can you help a shift manager with?"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--agent-dir", type=Path, required=True)
    parser.add_argument("--env", required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()

    rendered = render_agent(args.agent_dir, args.env, require_configured=True)
    try:
        with project_client(rendered.project_endpoint) as client, client.get_openai_client() as openai_client:
            response = openai_client.responses.create(
                input=PROMPT,
                extra_body={"agent_reference": {"name": rendered.agent_name, "version": str(args.version),
                                                "type": "agent_reference"}},
            )
            text = (response.output_text or "").strip()
    except Exception as error:  # noqa: BLE001 - any failure to answer fails the smoke test
        print(f"SMOKE FAIL: {type(error).__name__}: {error}")
        return 1
    if not text:
        print("SMOKE FAIL: the agent returned an empty answer")
        return 1
    print(f"SMOKE PASS ({rendered.agent_name} v{args.version}): {text[:300]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
