"""G2: check the Azure configuration an agent runs on against the customer's rules (policy.json).

Every customer has different residency and security rules, so the rules live in a preset
(cicd/policies/presets/<name>.json) that policy.json picks and can override rule by rule.
Each rule is `block` or `warn`; `"enforcement": "warn"` reports would-block findings without failing,
which is how a team adopts a strict preset before its resources have moved.

Examples:
  python -m agent_cicd.policy_gate --agent-dir agents/shift-manager --validate-only
  python -m agent_cicd.policy_gate --agent-dir agents/shift-manager --env dev --report out/policy-dev.json
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .definition import render_agent
from .foundry import add_summary, set_outputs, write_report
from .gates import FAIL, PASS, REVIEW, GateResult

PRESETS_DIR = Path(__file__).resolve().parent.parent / "policies" / "presets"
ARM = "https://management.azure.com"
COGNITIVE_API = "2025-06-01"
RULES = ("model_region", "model_deployment_type", "data_store_region", "content_filter", "prompt_shields",
         "key_auth_disabled", "private_network", "tracing_enabled")
SEVERITIES = ("block", "warn", "off")
WARN = "warn"
BLOCK = "block"

# Where a deployment type may process prompts and answers (Azure OpenAI deployment types).
PROCESSING = {
    "GlobalStandard": "any Azure region worldwide",
    "GlobalProvisionedManaged": "any Azure region worldwide",
    "GlobalBatch": "any Azure region worldwide",
    "DataZoneStandard": "any region in the resource's data zone (US or EU)",
    "DataZoneProvisionedManaged": "any region in the resource's data zone (US or EU)",
    "DataZoneBatch": "any region in the resource's data zone (US or EU)",
    "Standard": "the resource's own region",
    "ProvisionedManaged": "the resource's own region",
}
# Built-in content filter policies can't be read through ARM; Microsoft documents both with Prompt Shields on.
KNOWN_POLICIES = {"Microsoft.DefaultV2": {"jailbreak": True}, "Microsoft.Default": {"jailbreak": False}}


class PolicyError(ValueError):
    """policy.json or a preset is invalid."""


# ---------------------------------------------------------------- rules

def load_policy(agent_dir: Path, presets_dir: Path = PRESETS_DIR) -> dict[str, Any]:
    path = agent_dir / "policy.json"
    if not path.is_file():
        raise PolicyError(f"Missing {path}: every agent needs customer rules (start from a preset)")
    policy = json.loads(path.read_text(encoding="utf-8"))
    preset_name = policy.get("preset")
    preset_path = presets_dir / f"{preset_name}.json"
    if not preset_name or not preset_path.is_file():
        known = sorted(p.stem for p in presets_dir.glob("*.json"))
        raise PolicyError(f"policy.json: unknown preset '{preset_name}' (known: {', '.join(known)})")
    preset = json.loads(preset_path.read_text(encoding="utf-8"))
    rules = copy.deepcopy(preset.get("rules") or {})
    for name, override in (policy.get("rules") or {}).items():
        rules[name] = {**rules.get(name, {}), **override}
    enforcement = policy.get("enforcement", "enforce")
    problems = [f"unknown rule '{n}'" for n in rules if n not in RULES]
    problems += [f"rule '{n}': severity must be one of {SEVERITIES}" for n, r in rules.items()
                 if r.get("severity", BLOCK) not in SEVERITIES]
    if enforcement not in ("enforce", "warn"):
        problems.append("enforcement must be 'enforce' or 'warn'")
    if problems:
        raise PolicyError("policy.json: " + "; ".join(problems))
    return {"preset": preset_name, "description": preset.get("description", ""), "enforcement": enforcement,
            "owner": policy.get("owner", ""), "rules": rules}


def _applies(rule: dict[str, Any], environment: str) -> bool:
    envs = rule.get("environments")
    return rule.get("severity", BLOCK) != "off" and (not envs or environment in envs)


def _region(value: Any) -> str:
    """ARM returns both 'canadacentral' and 'Canada Central' depending on the resource type."""
    return str(value or "").lower().replace(" ", "")


def evaluate(policy: dict[str, Any], facts: dict[str, Any], environment: str) -> GateResult:
    """Pure decision: one row per rule and resource, then PASS / REVIEW (warnings) / FAIL (a block)."""
    result = GateResult(gate="policy")
    rows: list[dict[str, Any]] = []
    rules = policy["rules"]
    account = facts.get("account") or {}

    def add(rule: str, subject: str, ok: bool | None, finding: str) -> None:
        severity = rules[rule].get("severity", BLOCK)
        status = "ok" if ok else "unknown" if ok is None else severity
        rows.append({"rule": rule, "subject": subject, "status": status, "severity": severity, "finding": finding})

    def regions(rule: str) -> set[str]:
        return {_region(r) for r in rules[rule].get("allowed_regions") or []}

    if _applies(rules.get("model_region", {"severity": "off"}), environment):
        allowed = regions("model_region")
        location = _region(account.get("location"))
        add("model_region", account.get("name", "account"), (location in allowed) if allowed else True,
            f"Foundry resource is in {location or 'an unknown region'}"
            + (f"; allowed: {', '.join(sorted(allowed))}" if allowed else ""))

    for deployment in facts.get("deployments") or []:
        name = deployment["name"]
        if _applies(rules.get("model_deployment_type", {"severity": "off"}), environment):
            allowed = set(rules["model_deployment_type"].get("allowed_skus") or [])
            sku = deployment.get("sku") or "unknown"
            add("model_deployment_type", name, (sku in allowed) if allowed else True,
                f"{sku}: prompts and answers are processed in {PROCESSING.get(sku, 'an unknown location')}"
                + (f"; allowed: {', '.join(sorted(allowed))}" if allowed else ""))
        if _applies(rules.get("content_filter", {"severity": "off"}), environment):
            rai = deployment.get("rai_policy")
            add("content_filter", name, bool(rai) and rai != "Microsoft.Nil",
                f"content filter policy: {rai or 'none'}")
        if _applies(rules.get("prompt_shields", {"severity": "off"}), environment):
            shields = deployment.get("prompt_shields")
            add("prompt_shields", name, shields,
                "Prompt Shields (jailbreak detection) " + {True: "on and blocking", False: "off or not blocking",
                                                           None: "could not be read"}[shields])

    if _applies(rules.get("data_store_region", {"severity": "off"}), environment):
        allowed = regions("data_store_region")
        for store in facts.get("data_stores") or []:
            location = _region(store.get("location"))
            known = bool(location)
            add("data_store_region", f"{store['kind']}: {store['name']}",
                ((location in allowed) if allowed else True) if known else None,
                f"stores data in {location or 'an unknown region'}"
                + (f"; allowed: {', '.join(sorted(allowed))}" if allowed else ""))

    if _applies(rules.get("key_auth_disabled", {"severity": "off"}), environment):
        disabled = account.get("disable_local_auth")
        add("key_auth_disabled", account.get("name", "account"), disabled,
            "API keys " + {True: "disabled (Entra ID only)", False: "enabled", None: "unknown"}[disabled])

    if _applies(rules.get("private_network", {"severity": "off"}), environment):
        access = account.get("public_network_access")
        add("private_network", account.get("name", "account"), None if access is None else access == "Disabled",
            f"public network access: {access or 'unknown'}")

    if _applies(rules.get("tracing_enabled", {"severity": "off"}), environment):
        add("tracing_enabled", facts.get("project", "project"), bool(facts.get("tracing")),
            "Application Insights " + ("connected" if facts.get("tracing") else
                                       "not connected: traces and user feedback are not recorded"))

    enforce = policy["enforcement"] == "enforce"
    blocks = [r for r in rows if r["status"] in (BLOCK, "unknown") and r["severity"] == BLOCK]
    warns = [r for r in rows if r["status"] == WARN or (r["status"] == "unknown" and r["severity"] == WARN)]
    for row in blocks + warns:
        result.reasons.append(f"{row['rule']} ({row['subject']}): {row['finding']}")
    if blocks and enforce:
        result.outcome = FAIL
    elif blocks or warns:
        result.outcome = REVIEW
        if blocks:
            result.reasons.insert(0, f"{len(blocks)} finding(s) would block, but enforcement is 'warn' in policy.json")
    else:
        result.outcome = PASS
        result.reasons.append(f"Every '{policy['preset']}' rule is met")
    result.rows = rows
    result.details = {"preset": policy["preset"], "enforcement": policy["enforcement"], "environment": environment,
                      "would_block": len(blocks), "warnings": len(warns)}
    return result


# ---------------------------------------------------------------- Azure facts

def _arm_token() -> str:
    from azure.identity import AzureCliCredential, DefaultAzureCredential
    try:
        return AzureCliCredential(process_timeout=120).get_token(f"{ARM}/.default").token
    except Exception:  # noqa: BLE001 - fall back to any other available login
        return DefaultAzureCredential().get_token(f"{ARM}/.default").token


def _subscription() -> str:
    if os.environ.get("AZURE_SUBSCRIPTION_ID"):
        return os.environ["AZURE_SUBSCRIPTION_ID"]
    out = subprocess.run(["az", "account", "show", "--query", "id", "-o", "tsv"], capture_output=True, text=True,
                         shell=os.name == "nt", check=False)
    if out.returncode or not out.stdout.strip():
        raise PolicyError("Set AZURE_SUBSCRIPTION_ID or log in with az")
    return out.stdout.strip()


def arm_getter(token: str) -> Callable[[str, str], dict[str, Any] | None]:
    def get(path: str, api_version: str) -> dict[str, Any] | None:
        url = f"{ARM}{path}{'&' if '?' in path else '?'}api-version={api_version}"
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            if error.code in (403, 404):
                return None
            raise
    return get


def _prompt_shields(get, account_path: str, rai_policy: str | None) -> bool | None:
    if not rai_policy:
        return False
    if rai_policy in KNOWN_POLICIES:
        return KNOWN_POLICIES[rai_policy]["jailbreak"]
    body = get(f"{account_path}/raiPolicies/{rai_policy}", COGNITIVE_API)
    if body is None:
        return None
    for item in (body.get("properties") or {}).get("contentFilters") or []:
        if str(item.get("name", "")).lower() == "jailbreak" and item.get("source", "Prompt") == "Prompt":
            return bool(item.get("enabled")) and bool(item.get("blocking"))
    return False


def collect_facts(endpoint: str, deployments: list[str], resource_group: str, subscription: str,
                  get: Callable[[str, str], dict[str, Any] | None]) -> dict[str, Any]:
    """Read only what the rules need, with the pipeline's own (read-only) identity."""
    host = urlparse(endpoint).hostname or ""
    account_name = host.split(".")[0]
    project = endpoint.rstrip("/").split("/projects/")[-1]
    account_path = (f"/subscriptions/{subscription}/resourceGroups/{resource_group}"
                    f"/providers/Microsoft.CognitiveServices/accounts/{account_name}")
    account = get(account_path, COGNITIVE_API)
    if account is None:
        raise PolicyError(f"Cannot read Foundry resource {account_name} in {resource_group}: "
                          "grant the pipeline identity Reader (or Foundry User) on it")
    props = account.get("properties") or {}
    facts: dict[str, Any] = {
        "project": project,
        "account": {"name": account_name, "location": account.get("location"),
                    "public_network_access": props.get("publicNetworkAccess"),
                    "disable_local_auth": props.get("disableLocalAuth")},
        "deployments": [], "data_stores": [], "tracing": False,
    }
    for name in dict.fromkeys(deployments):
        body = get(f"{account_path}/deployments/{name}", COGNITIVE_API) or {}
        rai = (body.get("properties") or {}).get("raiPolicyName")
        facts["deployments"].append({"name": name, "sku": (body.get("sku") or {}).get("name"), "rai_policy": rai,
                                     "prompt_shields": _prompt_shields(get, account_path, rai) if body else None})
    connections = get(f"{account_path}/projects/{project}/connections", COGNITIVE_API) or {}
    for connection in connections.get("value") or []:
        cprops = connection.get("properties") or {}
        category = cprops.get("category")
        resource_id = (cprops.get("metadata") or {}).get("ResourceId") or (
            cprops.get("target") if str(cprops.get("target", "")).startswith("/subscriptions/") else "")
        kind = {"CognitiveSearch": "azure_ai_search", "AppInsights": "app_insights",
                "AzureBlob": "storage", "AzureStorageAccount": "storage", "CosmosDb": "cosmos_db"}.get(category)
        if not kind:
            continue
        location = None
        if resource_id:
            api = "2020-02-02" if kind == "app_insights" else "2023-11-01" if kind == "azure_ai_search" else "2023-05-01"
            location = (get(resource_id, api) or {}).get("location")
        facts["data_stores"].append({"name": resource_id.rsplit("/", 1)[-1] if resource_id else connection.get("name"),
                                     "kind": kind, "location": location})
        facts["tracing"] = facts["tracing"] or kind == "app_insights"
    return facts


# ---------------------------------------------------------------- output

def to_markdown(result: GateResult, title: str = "G2: config & residency") -> str:
    icon = {PASS: "✅", REVIEW: "🔍", FAIL: "❌"}.get(result.outcome, "⚠️")
    d = result.details
    lines = [f"### {icon} {title} ({d.get('environment')}): {result.outcome}",
             f"Rules: preset `{d.get('preset')}`, enforcement `{d.get('enforcement')}`", ""]
    lines += [f"- {r}" for r in result.reasons]
    lines += ["", "| Rule | Resource | Status | Finding |", "|---|---|---|---|"]
    mark = {"ok": "✅ ok", BLOCK: "❌ block", WARN: "⚠️ warn", "unknown": "❔ unknown"}
    for row in result.rows:
        lines.append(f"| {row['rule']} | {row['subject']} | {mark.get(row['status'], row['status'])} | {row['finding']} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--agent-dir", type=Path, required=True)
    parser.add_argument("--env", default="dev")
    parser.add_argument("--validate-only", action="store_true", help="Check policy.json and its preset, no Azure calls")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    try:
        policy = load_policy(args.agent_dir)
    except PolicyError as error:
        print(f"::error::{error}")
        return 1
    if args.validate_only:
        print(f"policy.json OK: preset {policy['preset']}, enforcement {policy['enforcement']}, "
              f"{len(policy['rules'])} rules")
        return 0

    rendered = render_agent(args.agent_dir, args.env, require_configured=True)
    raw_config = json.loads((args.agent_dir / "config" / f"{args.env}.json").read_text(encoding="utf-8"))
    resource_group = raw_config.get("azure_resource_group")
    if not resource_group:
        print(f"::error::config/{args.env}.json needs 'azure_resource_group' for the policy gate")
        return 1
    try:
        facts = collect_facts(rendered.project_endpoint,
                              [rendered.definition["model"], rendered.judge_deployment],
                              resource_group, _subscription(), arm_getter(_arm_token()))
        result = evaluate(policy, facts, args.env)
    except PolicyError as error:
        result = GateResult(gate="policy", outcome=FAIL, reasons=[str(error)],
                            details={"environment": args.env, "preset": policy["preset"],
                                     "enforcement": policy["enforcement"]})
        facts = {}
    markdown = to_markdown(result)
    add_summary(markdown)
    write_report(args.report, {"result": result.to_dict(), "facts": facts, "policy": policy})
    set_outputs(policy_outcome=result.outcome)
    return 0 if result.outcome in (PASS, REVIEW) else 1


if __name__ == "__main__":
    sys.exit(main())
