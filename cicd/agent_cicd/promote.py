"""Promote an agent version built in the Foundry portal (dev project) into the repo files.

Reads one agent version from the dev project, writes its instructions, reasoning and tools back into
instructions.md and agent.json (environment-specific values become ${VARIABLES} again), checks that the
files render to exactly that definition, and writes a pull-request description. Nothing is deployed:
the pull request goes through review, then the normal pipeline (dev gates, release tag, approval).

Anything it cannot map safely (a new connection or index that has no variable for prod, a different model
deployment, a definition field the pipeline does not render yet) stops the export with an explanation
instead of guessing.

Examples:
  python -m agent_cicd.promote --agent-dir agents/shift-manager --env dev --version 12 --dry-run
  python -m agent_cicd.promote --agent-dir agents/shift-manager --env dev --version latest --pr-body out/promote-pr.md
"""

from __future__ import annotations

import argparse
import copy
import difflib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from .definition import DefinitionError, VARIABLE, _read_json, _substitute, render_agent
from .foundry import add_summary, project_client, set_outputs

RENDERED_FIELDS = {"kind", "model", "instructions", "tools", "reasoning"}
# Values like these differ between dev and prod; left as literals they would point prod at dev resources.
ENV_SPECIFIC = re.compile(r"https?://|/versions/|/connections/|/subscriptions/|azureml://|azureai://|"
                          r"\.search\.windows\.net|\.services\.ai\.azure\.com|\.openai\.azure\.com")


class PromoteError(ValueError):
    """The portal version cannot be mapped to the repo files without a human decision."""


def _template(value: Any, variables: dict[str, str]) -> Any:
    """Turn environment values from config/<env>.json back into ${NAME} references."""
    if isinstance(value, str):
        for name, actual in sorted(variables.items(), key=lambda kv: -len(kv[1])):
            if actual and (value == actual or (len(actual) >= 8 and actual in value)):
                value = value.replace(actual, "${" + name + "}")
        return value
    if isinstance(value, list):
        return [_template(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: _template(item, variables) for key, item in value.items()}
    return value


def _literals(value: Any, path: str = "tools") -> list[tuple[str, str]]:
    if isinstance(value, str):
        return [] if VARIABLE.search(value) else [(path, value)]
    if isinstance(value, list):
        return [x for i, item in enumerate(value) for x in _literals(item, f"{path}[{i}]")]
    if isinstance(value, dict):
        return [x for key, item in value.items() for x in _literals(item, f"{path}.{key}")]
    return []


def portal_to_files(definition: dict[str, Any], agent: dict[str, Any], config: dict[str, Any]) -> tuple[dict, str]:
    """Return (new agent.json, new instructions) for a portal definition, or raise PromoteError."""
    problems = []
    if definition.get("kind") != "prompt":
        problems.append(f"kind is '{definition.get('kind')}'; this pipeline only handles prompt agents")
    if definition.get("model") != config.get("model_deployment"):
        problems.append(f"model deployment is '{definition.get('model')}' but config uses "
                        f"'{config.get('model_deployment')}'. Change model_deployment in config/dev.json and "
                        "config/prod.json by hand (the prod project needs a deployment with that name)")
    extra = sorted(k for k, v in definition.items() if k not in RENDERED_FIELDS and v not in (None, "", [], {}))
    if extra:
        problems.append(f"the version sets {', '.join(extra)}, which the pipeline does not deploy yet; "
                        "add support in definition.py first so prod gets the same agent")
    instructions = str(definition.get("instructions") or "")
    if not instructions.strip():
        problems.append("the version has no instructions")

    tools = _template(copy.deepcopy(definition.get("tools") or []), config.get("variables") or {})
    unmapped = [(path, value) for path, value in _literals(tools) if ENV_SPECIFIC.search(value)]
    if unmapped:
        listed = "; ".join(f"{path} = {value}" for path, value in unmapped)
        problems.append(f"environment-specific tool values have no variable for prod: {listed}. Add a variable to "
                        "config/dev.json and config/prod.json (\"variables\") with the dev and prod values, then "
                        "run the export again")
    if problems:
        raise PromoteError("\n".join(f"- {p}" for p in problems))

    new_agent = copy.deepcopy(agent)
    new_agent["tools"] = tools
    if definition.get("reasoning"):
        new_agent["reasoning"] = definition["reasoning"]
    else:
        new_agent.pop("reasoning", None)
    return new_agent, instructions.rstrip("\n") + "\n"


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _definition_only(definition: dict[str, Any]) -> dict[str, Any]:
    kept = {k: v for k, v in definition.items() if k in RENDERED_FIELDS and v not in (None, "", [], {})}
    if isinstance(kept.get("instructions"), str):
        kept["instructions"] = kept["instructions"].rstrip("\n")
    return kept


def pr_body(agent_name: str, version: dict[str, Any], old_agent: dict, new_agent: dict,
            old_instructions: str, new_instructions: str, environment: str) -> str:
    created = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(version.get("created_at") or 0))
    metadata = version.get("metadata") or {}
    source = "the pipeline" if metadata.get("deployed_by") == "agent-cicd" else "the Foundry portal or SDK"
    changes = []
    diff = list(difflib.unified_diff(old_instructions.splitlines(), new_instructions.splitlines(), lineterm="", n=1,
                                     fromfile="instructions.md (repo)", tofile=f"portal version {version.get('version')}"))
    if diff:
        added = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
        removed = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))
        changes.append(f"**Instructions**: {added} line(s) added, {removed} removed")
    if old_agent.get("reasoning") != new_agent.get("reasoning"):
        changes.append(f"**Reasoning**: `{_canonical(old_agent.get('reasoning'))}` → `{_canonical(new_agent.get('reasoning'))}`")
    if old_agent.get("tools") != new_agent.get("tools"):
        old_types = [t.get("type") for t in old_agent.get("tools") or []]
        new_types = [t.get("type") for t in new_agent.get("tools") or []]
        changes.append(f"**Tools**: {old_types} → {new_types}" if old_types != new_types else "**Tools**: settings changed")
        if old_types != new_types:
            changes.append("⚠️ The tool list changed: update `tool_descriptions` in agent.json so red teaming "
                           "describes the new tools, and add test cases for them")
    lines = [
        f"## Promote `{agent_name}` version {version.get('version')} from the {environment} project",
        "",
        f"Created {created} by {source}. The repo files below now render to exactly that definition.",
        "",
        *[f"- {c}" for c in changes],
        "",
        "### What happens next",
        "1. Review the diff (**Files changed**). Instructions are the agent's behaviour: read them like code.",
        "2. Merge → the pipeline deploys this definition to dev, runs the smoke checks and the evaluation gate.",
        "3. To ship it: create a release tag `shift-manager-vX.Y.Z` → evaluation, red teaming, approval, prod.",
        "",
        "_Pull requests opened by GitHub Actions don't start workflows; Gate 1a runs after merge, or push any "
        "commit to this branch to run it on the PR._",
    ]
    if diff:
        shown = diff[:120]
        lines += ["", "<details><summary>Instructions diff</summary>", "", "```diff", *shown,
                  *(["… (truncated, see Files changed)"] if len(diff) > len(shown) else []), "```", "", "</details>"]
    return "\n".join(lines) + "\n"


def promote(agent_dir: Path, environment: str, version: str, agent_name: str | None, dry_run: bool,
            body_path: Path | None) -> int:
    rendered = render_agent(agent_dir, environment, require_configured=True)
    raw_config = _read_json(agent_dir / "config" / f"{environment}.json")
    config = _substitute(raw_config, {}, set())
    agent_path = agent_dir / "agent.json"
    old_agent_text = agent_path.read_text(encoding="utf-8")
    old_agent = json.loads(old_agent_text)
    instructions_path = agent_dir / old_agent.get("instructions_file", "instructions.md")
    old_instructions = instructions_path.read_text(encoding="utf-8")
    name = agent_name or rendered.agent_name

    with project_client(rendered.project_endpoint) as client:
        if version == "latest":
            details = client.agents.get(agent_name=name).as_dict()
            version = str(((details.get("versions") or {}).get("latest") or {}).get("version", ""))
        source = client.agents.get_version(agent_name=name, agent_version=version).as_dict()
    definition = source.get("definition") or {}

    try:
        new_agent, new_instructions = portal_to_files(definition, old_agent, config)
    except PromoteError as error:
        message = f"Cannot promote {name} version {version} automatically:\n{error}"
        print(message, file=sys.stderr)
        add_summary(f"### ❌ Not promoted\n\n{message}\n")
        set_outputs(changed="false", source_version=version)
        return 1

    if new_agent == old_agent and new_instructions.rstrip("\n") == old_instructions.rstrip("\n"):
        print(f"{name} version {version} already matches the repo; nothing to promote")
        set_outputs(changed="false", source_version=version)
        return 0
    if dry_run:
        print(pr_body(name, source, old_agent, new_agent, old_instructions, new_instructions, environment))
        set_outputs(changed="true", source_version=version)
        return 0

    agent_path.write_text(json.dumps(new_agent, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    instructions_path.write_text(new_instructions, encoding="utf-8", newline="\n")
    try:
        check = render_agent(agent_dir, environment, require_configured=True)
        matches = _canonical(_definition_only(check.definition)) == _canonical(_definition_only(definition))
        problem = "" if matches else "the exported files do not render to the portal definition"
    except DefinitionError as error:
        problem = f"the exported files do not render: {error}"
    if problem:
        agent_path.write_text(old_agent_text, encoding="utf-8", newline="")
        instructions_path.write_text(old_instructions, encoding="utf-8", newline="")
        print(f"ERROR: {problem}; files restored, no pull request", file=sys.stderr)
        return 1
    if body_path:
        body_path.parent.mkdir(parents=True, exist_ok=True)
        body_path.write_text(pr_body(name, source, old_agent, new_agent, old_instructions, new_instructions,
                                     environment), encoding="utf-8")
    print(f"Exported {name} version {version} into {agent_path.name} and {instructions_path.name}")
    set_outputs(changed="true", source_version=version)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--agent-dir", type=Path, required=True)
    parser.add_argument("--env", default="dev", help="Project to read from (always the dev project)")
    parser.add_argument("--version", required=True, help="Agent version number, or 'latest'")
    parser.add_argument("--agent-name", help="Defaults to agent_name in config/<env>.json")
    parser.add_argument("--pr-body", type=Path, help="Write the pull request description here")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without writing files")
    args = parser.parse_args()
    if args.env == "prod":
        parser.error("promote reads from the dev project only; prod is changed by the pipeline")
    try:
        return promote(args.agent_dir, args.env, args.version, args.agent_name, args.dry_run, args.pr_body)
    except DefinitionError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
