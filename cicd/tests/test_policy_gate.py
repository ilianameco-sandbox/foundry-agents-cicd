"""G2 config & residency: presets, per-customer overrides, warn vs enforce, and reading Azure facts."""
import json
from pathlib import Path

import pytest

from agent_cicd.gates import FAIL, PASS, REVIEW
from agent_cicd.policy_gate import PolicyError, collect_facts, evaluate, load_policy, to_markdown

AGENT_DIR = Path(__file__).resolve().parents[1] / "agents" / "shift-manager"


def facts(location="canadacentral", sku="Standard", rai="Microsoft.DefaultV2", shields=True, store="Canada Central",
          tracing=True, local_auth_disabled=True, public="Disabled"):
    return {
        "project": "p",
        "account": {"name": "acct", "location": location, "public_network_access": public,
                    "disable_local_auth": local_auth_disabled},
        "deployments": [{"name": "gpt", "sku": sku, "rai_policy": rai, "prompt_shields": shields}],
        "data_stores": [{"name": "search", "kind": "azure_ai_search", "location": store}],
        "tracing": tracing,
    }


def policy_file(tmp_path, preset="canada-strict", enforcement="enforce", rules=None):
    agent = tmp_path / "agent"
    agent.mkdir(exist_ok=True)
    (agent / "policy.json").write_text(json.dumps({"preset": preset, "enforcement": enforcement,
                                                   "rules": rules or {}}), encoding="utf-8")
    return load_policy(agent)


def test_shipped_policy_is_canada_strict_in_warn_mode():
    policy = load_policy(AGENT_DIR)
    assert policy["preset"] == "canada-strict" and policy["enforcement"] == "warn"
    assert policy["rules"]["model_deployment_type"]["allowed_skus"] == ["Standard", "ProvisionedManaged"]


def test_compliant_canadian_setup_passes(tmp_path):
    result = evaluate(policy_file(tmp_path), facts(), "test")
    assert result.outcome == PASS
    assert all(row["status"] == "ok" for row in result.rows)


def test_global_deployment_in_sweden_blocks_when_enforced(tmp_path):
    result = evaluate(policy_file(tmp_path), facts(location="swedencentral", sku="GlobalStandard",
                                                   store="Central US"), "dev")
    assert result.outcome == FAIL
    blocked = {row["rule"] for row in result.rows if row["status"] == "block"}
    assert blocked == {"model_region", "model_deployment_type", "data_store_region"}
    assert any("any Azure region worldwide" in r for r in result.reasons)


def test_warn_enforcement_reports_but_does_not_block(tmp_path):
    result = evaluate(policy_file(tmp_path, enforcement="warn"), facts(sku="GlobalStandard"), "dev")
    assert result.outcome == REVIEW and result.allows_promotion
    assert "would block" in result.reasons[0]


def test_customer_override_turns_one_rule_into_a_warning(tmp_path):
    policy = policy_file(tmp_path, rules={"data_store_region": {"severity": "warn"}})
    result = evaluate(policy, facts(store="eastus"), "dev")
    assert result.outcome == REVIEW


def test_eu_preset_allows_data_zone_but_not_global(tmp_path):
    policy = policy_file(tmp_path, preset="eu-data-zone")
    assert evaluate(policy, facts(location="swedencentral", sku="DataZoneStandard", store="westeurope"),
                    "dev").outcome == PASS
    assert evaluate(policy, facts(location="swedencentral", sku="GlobalStandard", store="westeurope"),
                    "dev").outcome == FAIL


def test_permissive_preset_still_requires_content_filter(tmp_path):
    policy = policy_file(tmp_path, preset="permissive")
    assert evaluate(policy, facts(location="eastus", sku="GlobalStandard", store="eastus"), "dev").outcome == PASS
    assert evaluate(policy, facts(rai=None, shields=False), "dev").outcome == FAIL


def test_tracing_is_required_only_in_test(tmp_path):
    policy = policy_file(tmp_path)
    assert evaluate(policy, facts(tracing=False), "dev").outcome == PASS
    result = evaluate(policy, facts(tracing=False), "test")
    assert result.outcome == FAIL and any("tracing_enabled" in r for r in result.reasons)


def test_unknown_facts_fail_closed_for_blocking_rules(tmp_path):
    result = evaluate(policy_file(tmp_path), facts(shields=None), "dev")
    assert result.outcome == FAIL
    assert "❔ unknown" in to_markdown(result)


def test_invalid_policy_files_are_rejected(tmp_path):
    with pytest.raises(PolicyError, match="unknown preset"):
        policy_file(tmp_path, preset="mars")
    with pytest.raises(PolicyError, match="unknown rule"):
        policy_file(tmp_path, rules={"speed_limit": {"severity": "block"}})
    with pytest.raises(PolicyError, match="severity"):
        policy_file(tmp_path, rules={"model_region": {"severity": "maybe"}})


def test_collect_facts_reads_account_deployments_and_connected_stores():
    sub, rg = "s", "rg"
    acct = f"/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.CognitiveServices/accounts/acct"
    search_id = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Search/searchServices/srch"
    ai_id = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Insights/components/ai"
    responses = {
        acct: {"location": "swedencentral", "properties": {"publicNetworkAccess": "Enabled", "disableLocalAuth": True}},
        f"{acct}/deployments/gpt": {"sku": {"name": "GlobalStandard"}, "properties": {"raiPolicyName": "custom"}},
        f"{acct}/raiPolicies/custom": {"properties": {"contentFilters": [
            {"name": "Jailbreak", "source": "Prompt", "enabled": True, "blocking": False}]}},
        f"{acct}/projects/proj/connections": {"value": [
            {"name": "c1", "properties": {"category": "CognitiveSearch", "metadata": {"ResourceId": search_id}}},
            {"name": "c2", "properties": {"category": "AppInsights", "target": ai_id}},
            {"name": "c3", "properties": {"category": "ApiKey"}}]},
        search_id: {"location": "Central US"},
        ai_id: {"location": "swedencentral"},
    }
    got = collect_facts("https://acct.services.ai.azure.com/api/projects/proj", ["gpt", "gpt"], rg, sub,
                        lambda path, api: responses.get(path))
    assert got["account"]["location"] == "swedencentral"
    assert got["deployments"] == [{"name": "gpt", "sku": "GlobalStandard", "rai_policy": "custom",
                                   "prompt_shields": False}]
    assert [(s["kind"], s["location"]) for s in got["data_stores"]] == [("azure_ai_search", "Central US"),
                                                                        ("app_insights", "swedencentral")]
    assert got["tracing"] is True
