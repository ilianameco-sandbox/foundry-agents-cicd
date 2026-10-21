"""Load an agent definition from code, render it for one environment, and fingerprint it.

Environment configs keep endpoints and resource names out of the repo: any "${NAME}" value in
config/<environment>.json is read from the process environment (GitHub Environment variables in
Actions) or, for local runs, from cicd/.env (copy .env.example).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PLACEHOLDER = re.compile(r"^<[^<>]+>$")
VARIABLE = re.compile(r"\$\{([A-Z0-9_]+)\}")
REQUIRED_ENV_KEYS = ("project_endpoint", "agent_name", "model_deployment", "judge_deployment")
DOTENV_PATH = Path(__file__).resolve().parents[1] / ".env"


class DefinitionError(ValueError):
    """Raised when an agent definition or environment config is invalid."""


@dataclass(frozen=True)
class RenderedAgent:
    environment: str
    project_endpoint: str
    agent_name: str
    judge_deployment: str
    definition: dict[str, Any]
    placeholders: tuple[str, ...]
    tool_descriptions: tuple[dict[str, str], ...] = ()

    @property
    def deployable(self) -> bool:
        return not self.placeholders


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise DefinitionError(f"Missing file: {path}") from error
    except json.JSONDecodeError as error:
        raise DefinitionError(f"Invalid JSON in {path}: {error}") from error


def _substitute(value: Any, variables: dict[str, str], missing: set[str]) -> Any:
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in variables:
                missing.add(name)
                return match.group(0)
            return variables[name]
        return VARIABLE.sub(replace, value)
    if isinstance(value, list):
        return [_substitute(item, variables, missing) for item in value]
    if isinstance(value, dict):
        return {key: _substitute(item, variables, missing) for key, item in value.items()}
    return value


def _find_placeholders(value: Any, path: str = "") -> list[str]:
    if isinstance(value, str):
        return [path] if PLACEHOLDER.match(value.strip()) else []
    if isinstance(value, list):
        return [p for i, item in enumerate(value) for p in _find_placeholders(item, f"{path}[{i}]")]
    if isinstance(value, dict):
        return [p for key, item in value.items() for p in _find_placeholders(item, f"{path}.{key}" if path else key)]
    return []


def read_dotenv(path: Path) -> dict[str, str]:
    """Parse simple KEY=VALUE lines; blank lines and # comments are ignored."""
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def default_env() -> dict[str, str]:
    """cicd/.env for local runs; real environment variables win."""
    return {**read_dotenv(DOTENV_PATH), **os.environ}


def render_agent(agent_dir: Path, environment: str, *, env: Mapping[str, str] | None = None,
                 require_configured: bool = False) -> RenderedAgent:
    """Combine agent.json, the instructions file and config/<environment>.json into one definition.

    Unset ${NAME} values are reported as placeholders. Pass require_configured=True before calling Azure.
    """
    agent = _read_json(agent_dir / "agent.json")
    raw_config = _read_json(agent_dir / "config" / f"{environment}.json")
    environment_values = {k: v for k, v in (default_env() if env is None else env).items() if v}
    unset: set[str] = set()
    config = _substitute(raw_config, environment_values, unset)

    for key in REQUIRED_ENV_KEYS:
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise DefinitionError(f"config/{environment}.json: '{key}' is required")
    if agent.get("kind") != "prompt":
        raise DefinitionError("Only prompt agents are supported by this pipeline")

    instructions_path = agent_dir / agent.get("instructions_file", "instructions.md")
    try:
        instructions = instructions_path.read_text(encoding="utf-8").rstrip("\n")
    except FileNotFoundError as error:
        raise DefinitionError(f"Missing instructions file: {instructions_path}") from error
    if not instructions.strip():
        raise DefinitionError("Instructions are empty")

    variables = config.get("variables", {})
    missing: set[str] = set()
    tools = _substitute(agent.get("tools", []), variables, missing)
    if missing:
        raise DefinitionError(f"config/{environment}.json is missing variables: {', '.join(sorted(missing))}")

    definition: dict[str, Any] = {
        "kind": "prompt",
        "model": config["model_deployment"],
        "instructions": instructions,
        "tools": tools,
    }
    if agent.get("reasoning"):
        definition["reasoning"] = agent["reasoning"]

    placeholders = tuple(_find_placeholders(config)) + tuple(f"${{{name}}} not set" for name in sorted(unset))
    if require_configured and placeholders:
        raise DefinitionError(f"{environment} is not configured: {', '.join(placeholders)}")
    return RenderedAgent(
        environment=environment,
        project_endpoint=config["project_endpoint"],
        agent_name=config["agent_name"],
        judge_deployment=config["judge_deployment"],
        definition=definition,
        placeholders=placeholders,
        tool_descriptions=tuple(agent.get("tool_descriptions", [])),
    )


def fingerprint(definition: dict[str, Any], model_version: str) -> str:
    """Hash everything that changes the agent's behaviour, including the deployed model version."""
    payload = {"definition": definition, "model_version": model_version}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
