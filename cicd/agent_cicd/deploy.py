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


def _latest_version(client, agent_name: str) -> tuple[str, dict]:
    from azure.core.exceptions import ResourceNotFoundError
    try:
        details = client.agents.get(agent_name=agent_name).as_dict()
    except ResourceNotFoundError:
        return "", {}
    latest = (details.get("versions") or {}).get("latest") or {}
    return str(latest.get("version", "")), latest.get("metadata") or {}


def deploy(agent_dir: Path, environment: str, dry_run: bool, force_new: bool = False) -> int:
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
        previous, previous_metadata = _latest_version(client, rendered.agent_name)
        # The latest version already runs this exact definition and model: keep it instead of adding an
        # identical version (and leave nothing to roll back to, since nothing changed).
        if (not force_new and previous and previous_metadata.get("deployed_by") == "agent-cicd"
                and previous_metadata.get("fingerprint") == agent_fingerprint[:32]):
            print(f"Unchanged: version {previous} already has fingerprint {agent_fingerprint[:12]}; not creating a new one")
            set_outputs(agent_name=rendered.agent_name, agent_version=previous, previous_version="",
                        fingerprint=agent_fingerprint, model_version=model_version, unchanged="true")
            return 0
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
                previous_version=previous, fingerprint=agent_fingerprint, model_version=model_version, unchanged="false")
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
    parser.add_argument("--force-new-version", action="store_true",
                        help="Create a version even when the latest one has the same fingerprint")
    args = parser.parse_args()
    try:
        if args.rollback_to:
            return rollback(args.agent_dir, args.env, args.rollback_to)
        return deploy(args.agent_dir, args.env, args.dry_run, args.force_new_version)
    except DefinitionError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
