import contextlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_cicd import promote as promote_module
from agent_cicd.definition import render_agent
from agent_cicd.promote import PromoteError, portal_to_files, pr_body

AGENT_DIR = Path(__file__).resolve().parents[1] / "agents" / "shift-manager"
ENV = {"FOUNDRY_PROJECT_ENDPOINT": "https://example.services.ai.azure.com/api/projects/dev"}


def _repo():
    agent = json.loads((AGENT_DIR / "agent.json").read_text(encoding="utf-8"))
    config = json.loads((AGENT_DIR / "config" / "dev.json").read_text(encoding="utf-8"))
    portal = render_agent(AGENT_DIR, "dev", env=ENV).definition
    return agent, config, json.loads(json.dumps(portal))


def test_unchanged_portal_version_maps_back_to_the_same_files():
    agent, config, portal = _repo()
    new_agent, instructions = portal_to_files(portal, agent, config)
    assert new_agent == agent
    assert instructions == (AGENT_DIR / "instructions.md").read_text(encoding="utf-8").rstrip("\n") + "\n"
    assert new_agent["tools"][0]["azure_ai_search"]["indexes"][0]["index_asset_id"] == "${SEARCH_INDEX_ASSET}"


def test_instruction_tool_setting_and_reasoning_changes_are_exported():
    agent, config, portal = _repo()
    portal["instructions"] += "\nNew rule."
    portal["tools"][0]["azure_ai_search"]["indexes"][0]["top_k"] = 10
    portal.pop("reasoning")
    new_agent, instructions = portal_to_files(portal, agent, config)
    assert instructions.endswith("New rule.\n")
    assert new_agent["tools"][0]["azure_ai_search"]["indexes"][0]["top_k"] == 10
    assert "reasoning" not in new_agent and new_agent["tool_descriptions"] == agent["tool_descriptions"]


def test_new_dev_only_resources_stop_the_export_instead_of_reaching_prod():
    agent, config, portal = _repo()
    portal["tools"].append({"type": "azure_ai_search", "azure_ai_search": {"indexes": [
        {"index_asset_id": "new-index/versions/2", "query_type": "simple"}]}})
    with pytest.raises(PromoteError, match=r"no variable for prod: tools\[1\].*new-index/versions/2"):
        portal_to_files(portal, agent, config)


@pytest.mark.parametrize("change, message", [
    ({"model": "gpt-6"}, "model deployment is 'gpt-6'"),
    ({"temperature": 0.2}, "sets temperature"),
    ({"kind": "hosted"}, "only handles prompt agents"),
    ({"instructions": "  "}, "no instructions"),
])
def test_unsupported_changes_need_a_human(change, message):
    agent, config, portal = _repo()
    with pytest.raises(PromoteError, match=message):
        portal_to_files({**portal, **change}, agent, config)


def test_empty_optional_fields_from_the_service_are_ignored():
    agent, config, portal = _repo()
    portal_to_files({**portal, "temperature": None, "text": {}}, agent, config)


def test_pr_body_flags_tool_list_changes():
    agent, config, portal = _repo()
    new_agent = {**agent, "tools": agent["tools"] + [{"type": "code_interpreter"}]}
    body = pr_body("a", {"version": "12", "created_at": 1_800_000_000, "metadata": {}}, agent, new_agent,
                   "old\n", "old\nnew\n", "dev")
    assert "version 12" in body and "Foundry portal" in body and "tool_descriptions" in body
    assert "+new" in body and "1 line(s) added" in body


def _fake_client(definition, version="12"):
    agents = SimpleNamespace(
        get=lambda agent_name: SimpleNamespace(as_dict=lambda: {"versions": {"latest": {"version": version}}}),
        get_version=lambda agent_name, agent_version: SimpleNamespace(as_dict=lambda: {
            "version": agent_version, "created_at": 1_800_000_000, "metadata": {}, "definition": definition}))
    return SimpleNamespace(agents=agents)


def test_promote_writes_files_that_render_to_the_portal_version(tmp_path, monkeypatch, capsys):
    agent_dir = tmp_path / "shift-manager"
    shutil.copytree(AGENT_DIR, agent_dir)
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", ENV["FOUNDRY_PROJECT_ENDPOINT"])
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    _, _, portal = _repo()
    portal["instructions"] += "\nNew rule from the portal."
    monkeypatch.setattr(promote_module, "project_client", lambda endpoint: contextlib.nullcontext(_fake_client(portal)))
    body = tmp_path / "pr.md"
    assert promote_module.promote(agent_dir, "dev", "latest", None, dry_run=False, body_path=body) == 0
    assert "changed=true" in capsys.readouterr().out
    assert render_agent(agent_dir, "dev", env=ENV).definition["instructions"].endswith("New rule from the portal.")
    assert "version 12" in body.read_text(encoding="utf-8")
    # Running it again finds nothing to promote.
    assert promote_module.promote(agent_dir, "dev", "12", None, dry_run=False, body_path=None) == 0
    assert "already matches the repo" in capsys.readouterr().out


def test_promote_refuses_unmappable_versions_and_leaves_files_untouched(tmp_path, monkeypatch, capsys):
    agent_dir = tmp_path / "shift-manager"
    shutil.copytree(AGENT_DIR, agent_dir)
    before = (agent_dir / "agent.json").read_text(encoding="utf-8")
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", ENV["FOUNDRY_PROJECT_ENDPOINT"])
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    _, _, portal = _repo()
    monkeypatch.setattr(promote_module, "project_client",
                        lambda endpoint: contextlib.nullcontext(_fake_client({**portal, "model": "gpt-6"})))
    assert promote_module.promote(agent_dir, "dev", "12", None, dry_run=False, body_path=None) == 1
    assert (agent_dir / "agent.json").read_text(encoding="utf-8") == before
    assert "Not promoted" in capsys.readouterr().out
