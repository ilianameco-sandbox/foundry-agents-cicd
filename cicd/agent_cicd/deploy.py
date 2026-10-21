"""Create a new agent version from code, record it, and (optionally) roll back.

Examples:
  python -m agent_cicd.deploy --agent-dir agents/shift-manager --env dev --dry-run
  python -m agent_cicd.deploy --agent-dir agents/shift-manager --env dev
  python -m agent_cicd.deploy --agent-dir agents/shift-manager --env prod --rollback-to 7
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .definition import DefinitionError, fingerprint, render_agent
from .foundry import project_client, set_outputs


def _model_version(client, deployment_name: str) -> str:
    deployment = client.deployments.get(deployment_name).as_dict()
    version = deployment.get("modelVersion") or deployment.get("model_version")
    if not version:
        raise RuntimeError(f"Model deployment '{deployment_name}' has no model version")
    return f"{deployment.get('modelName') or deployment.get('model_name')}@{version}"


def _latest_version(client, agent_name: str) -> str:
    from azure.core.exceptions import ResourceNotFoundError
    try:
        details = client.agents.get(agent_name=agent_name).as_dict()
    except ResourceNotFoundError:
        return ""
    return str((details.get("versions") or {}).get("latest", {}).get("version", ""))


def deploy(agent_dir: Path, environment: str, dry_run: bool) -> int:
    rendered = render_agent(agent_dir, environment)
    if dry_run:
        print(json.dumps({"environment": environment, "agent_name": rendered.agent_name,
                          "definition": rendered.definition}, indent=2, ensure_ascii=False))
        if rendered.placeholders:
            print(f"WARNING: {environment} is not configured yet; placeholders in: {', '.join(rendered.placeholders)}")
        return 0
    if not rendered.deployable:
        print(f"ERROR: {environment} config still has placeholders: {', '.join(rendered.placeholders)}", file=sys.stderr)
        return 1

    from azure.ai.projects.models import PromptAgentDefinition

    with project_client(rendered.project_endpoint) as client:
        model_version = _model_version(client, rendered.definition["model"])
        agent_fingerprint = fingerprint(rendered.definition, model_version)
        previous = _latest_version(client, rendered.agent_name)
        created = client.agents.create_version(
            agent_name=rendered.agent_name,
            definition=PromptAgentDefinition(rendered.definition),
            metadata={
                "deployed_by": "agent-cicd",
                "git_sha": os.environ.get("GITHUB_SHA", "local"),
                "fingerprint": agent_fingerprint[:32],
                "model_version": model_version,
            },
        )
    set_outputs(agent_name=rendered.agent_name, agent_version=str(created.version),
                previous_version=previous, fingerprint=agent_fingerprint, model_version=model_version)
    return 0


def rollback(agent_dir: Path, environment: str, target_version: str) -> int:
    rendered = render_agent(agent_dir, environment, require_configured=True)
    with project_client(rendered.project_endpoint) as client:
        source = client.agents.get_version(agent_name=rendered.agent_name, agent_version=target_version)
        created = client.agents.create_version(
            agent_name=rendered.agent_name,
            definition=source.definition,
            metadata={"deployed_by": "agent-cicd", "rollback_of": str(target_version),
                      "git_sha": os.environ.get("GITHUB_SHA", "local")},
        )
    print(f"Rolled back: version {created.version} recreates version {target_version}")
    set_outputs(agent_version=str(created.version))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--agent-dir", type=Path, required=True)
    parser.add_argument("--env", required=True)
    parser.add_argument("--dry-run", action="store_true", help="Render the definition without calling Azure")
    parser.add_argument("--rollback-to", help="Recreate this earlier version as the newest version")
    args = parser.parse_args()
    try:
        if args.rollback_to:
            return rollback(args.agent_dir, args.env, args.rollback_to)
        return deploy(args.agent_dir, args.env, args.dry_run)
    except DefinitionError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
