import json
import shutil
from pathlib import Path

import pytest

from agent_cicd.definition import DefinitionError, fingerprint, read_dotenv, render_agent

AGENT_DIR = Path(__file__).resolve().parents[1] / "agents" / "shift-manager"
ENV = {
    "FOUNDRY_PROJECT_ENDPOINT": "https://example.services.ai.azure.com/api/projects/dev",
    "KB_SERVER_LABEL": "kb-example",
    "KB_SERVER_URL": "https://example.search.windows.net/knowledgebases/example/mcp?api-version=2026-08-01-preview",
    "KB_CONNECTION_ID": "kb-example-connection",
}


def test_dev_renders_a_deployable_prompt_agent():
    rendered = render_agent(AGENT_DIR, "dev", env=ENV)
    assert rendered.deployable
    assert rendered.project_endpoint == ENV["FOUNDRY_PROJECT_ENDPOINT"]
    assert rendered.definition["kind"] == "prompt"
    assert rendered.definition["model"] == "gpt-5.4"
    assert rendered.definition["reasoning"] == {"effort": "low"}
    mcp = next(t for t in rendered.definition["tools"] if t["type"] == "mcp")
    assert mcp["project_connection_id"] == "kb-example-connection"
    assert mcp["server_url"] == ENV["KB_SERVER_URL"]
    assert "${" not in json.dumps(rendered.definition)
    assert rendered.definition["instructions"].startswith("You are the Shift Manager agent.")
    assert not rendered.definition["instructions"].endswith("\n")


def test_configs_keep_endpoints_out_of_the_repo():
    for environment in ("dev", "prod"):
        text = (AGENT_DIR / "config" / f"{environment}.json").read_text(encoding="utf-8")
        assert "https://" not in text


def test_unset_environment_variables_render_but_block_azure_calls():
    rendered = render_agent(AGENT_DIR, "dev", env={})
    assert not rendered.deployable
    assert "${FOUNDRY_PROJECT_ENDPOINT} not set" in rendered.placeholders
    assert "${KB_SERVER_URL} not set" in rendered.placeholders
    with pytest.raises(DefinitionError, match="FOUNDRY_PROJECT_ENDPOINT"):
        render_agent(AGENT_DIR, "dev", env={}, require_configured=True)
    assert render_agent(AGENT_DIR, "dev", env=ENV, require_configured=True).deployable


def test_dotenv_parsing(tmp_path):
    path = tmp_path / ".env"
    path.write_text('# comment\n\nA=1\nB = "two"\nnot a pair\n', encoding="utf-8")
    assert read_dotenv(path) == {"A": "1", "B": "two"}
    assert read_dotenv(tmp_path / "missing.env") == {}


def test_prod_renders_but_is_not_deployable_until_configured():
    rendered = render_agent(AGENT_DIR, "prod", env=ENV)
    assert not rendered.deployable
    assert "model_deployment" in rendered.placeholders


def test_missing_variable_is_an_error(tmp_path):
    copy = tmp_path / "agent"
    shutil.copytree(AGENT_DIR, copy)
    config = json.loads((copy / "config" / "dev.json").read_text(encoding="utf-8"))
    del config["variables"]["KB_SERVER_URL"]
    (copy / "config" / "dev.json").write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(DefinitionError, match="KB_SERVER_URL"):
        render_agent(copy, "dev", env=ENV)


def test_unknown_environment_is_an_error():
    with pytest.raises(DefinitionError, match="Missing file"):
        render_agent(AGENT_DIR, "staging", env=ENV)


def test_fingerprint_is_stable_and_tracks_behaviour_inputs():
    definition = render_agent(AGENT_DIR, "dev", env=ENV).definition
    base = fingerprint(definition, "gpt-5.4@2026-03-05")
    assert base == fingerprint(json.loads(json.dumps(definition)), "gpt-5.4@2026-03-05")
    assert base != fingerprint(definition, "gpt-5.4@2026-06-01")
    changed = dict(definition, instructions=definition["instructions"] + " Be brief.")
    assert base != fingerprint(changed, "gpt-5.4@2026-03-05")
