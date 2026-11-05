import json
import shutil
from pathlib import Path

import pytest

from agent_cicd.definition import DefinitionError, fingerprint, read_dotenv, render_agent

AGENT_DIR = Path(__file__).resolve().parents[1] / "agents" / "shift-manager"
ENV = {"FOUNDRY_PROJECT_ENDPOINT": "https://example.services.ai.azure.com/api/projects/dev"}


def test_dev_renders_a_deployable_prompt_agent():
    rendered = render_agent(AGENT_DIR, "dev", env=ENV)
    assert rendered.deployable
    assert rendered.project_endpoint == ENV["FOUNDRY_PROJECT_ENDPOINT"]
    assert rendered.agent_name == "shiftmanageragent-promptv3"
    assert rendered.definition["kind"] == "prompt"
    assert rendered.definition["model"] == "gpt-5.4"
    assert rendered.definition["reasoning"] == {"effort": "low"}
    search = next(t for t in rendered.definition["tools"] if t["type"] == "azure_ai_search")
    index = search["azure_ai_search"]["indexes"][0]
    assert index["index_asset_id"] == "shift-manager-v3/versions/1"
    assert index["top_k"] == 5 and index["query_type"] == "simple"
    assert "${" not in json.dumps(rendered.definition)
    assert rendered.definition["instructions"].startswith("You are the Shift Manager assistant")
    assert not rendered.definition["instructions"].endswith("\n")


def test_configs_keep_endpoints_out_of_the_repo():
    for environment in ("dev", "prod"):
        text = (AGENT_DIR / "config" / f"{environment}.json").read_text(encoding="utf-8")
        assert "https://" not in text


def test_unset_environment_variables_render_but_block_azure_calls():
    rendered = render_agent(AGENT_DIR, "dev", env={})
    assert not rendered.deployable
    assert "${FOUNDRY_PROJECT_ENDPOINT} not set" in rendered.placeholders
    with pytest.raises(DefinitionError, match="FOUNDRY_PROJECT_ENDPOINT"):
        render_agent(AGENT_DIR, "dev", env={}, require_configured=True)
    assert render_agent(AGENT_DIR, "dev", env=ENV, require_configured=True).deployable


def test_dotenv_parsing(tmp_path):
    path = tmp_path / ".env"
    path.write_text('# comment\n\nA=1\nB = "two"\nnot a pair\n', encoding="utf-8")
    assert read_dotenv(path) == {"A": "1", "B": "two"}
    assert read_dotenv(tmp_path / "missing.env") == {}


def test_prod_deploys_the_same_definition_as_dev():
    dev = render_agent(AGENT_DIR, "dev", env=ENV)
    prod = render_agent(AGENT_DIR, "prod", env={"FOUNDRY_PROJECT_ENDPOINT": "https://example/api/projects/prod"})
    assert prod.deployable
    assert prod.definition == dev.definition and prod.agent_name == dev.agent_name


def test_placeholders_block_deploys(tmp_path):
    copy = tmp_path / "agent"
    shutil.copytree(AGENT_DIR, copy)
    config = json.loads((copy / "config" / "prod.json").read_text(encoding="utf-8"))
    config["model_deployment"] = "<prod-model-deployment>"
    (copy / "config" / "prod.json").write_text(json.dumps(config), encoding="utf-8")
    rendered = render_agent(copy, "prod", env=ENV)
    assert not rendered.deployable and "model_deployment" in rendered.placeholders


def test_missing_variable_is_an_error(tmp_path):
    copy = tmp_path / "agent"
    shutil.copytree(AGENT_DIR, copy)
    config = json.loads((copy / "config" / "dev.json").read_text(encoding="utf-8"))
    del config["variables"]["SEARCH_INDEX_ASSET"]
    (copy / "config" / "dev.json").write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(DefinitionError, match="SEARCH_INDEX_ASSET"):
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
