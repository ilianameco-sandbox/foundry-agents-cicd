"""Reuse rules that keep the pipeline from spending tokens or creating clutter when nothing changed."""
import contextlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_cicd import deploy as deploy_module
from agent_cicd import redteam_gate
from agent_cicd.gates import load_jsonl
from agent_cicd.quality_gate import previous_runs, quality_key, suites_for

AGENT_DIR = Path(__file__).resolve().parents[1] / "agents" / "shift-manager"
NOW = 1_800_000_000


# ------------------------------------------------------------------ fakes

class _Page(list):
    pass


class _FakeRuns:
    def __init__(self, runs, items):
        self.runs, self.items_by_run = runs, items
        self.output_items = SimpleNamespace(list=lambda run_id, eval_id, **_: list(self.items_by_run.get(run_id, [])))

    def list(self, eval_id, **_):
        return _Page(sorted((r for r in self.runs if r.eval_id == eval_id), key=lambda r: r.created_at, reverse=True))

    def retrieve(self, run_id, eval_id):
        return next(r for r in self.runs if r.id == run_id and r.eval_id == eval_id)


class _FakeEvals:
    def __init__(self, evals, runs, items):
        self.store = {e.id: e for e in evals}
        self.runs = _FakeRuns(runs, items)
        self.created = []

    def retrieve(self, eval_id):
        if eval_id not in self.store:
            raise KeyError(eval_id)
        return self.store[eval_id]

    def list(self, **_):
        return [e for e in self.store.values() if getattr(e, "listed", True)]

    def create(self, name, metadata=None, **_):
        evaluation = SimpleNamespace(id=f"eval_new{len(self.created)}", name=name, metadata=metadata or {})
        self.created.append(evaluation)
        self.store[evaluation.id] = evaluation
        return evaluation


def _run(run_id, eval_id, created, metadata, status="completed"):
    return SimpleNamespace(id=run_id, eval_id=eval_id, created_at=created, metadata=metadata, status=status,
                           report_url=None)


# ------------------------------------------------------------------ Gate 1b reuse

def _quality_inputs():
    thresholds = json.loads((AGENT_DIR / "eval" / "thresholds.json").read_text(encoding="utf-8"))
    dataset = load_jsonl(AGENT_DIR / "eval" / "dataset.jsonl")
    return thresholds, dataset, suites_for(thresholds, dataset)


def test_quality_key_changes_with_agent_cases_bars_or_judges():
    thresholds, dataset, _ = _quality_inputs()
    criteria = {"behavior": [{"name": "expected_behavior"}]}
    base = quality_key("fp", dataset, thresholds, criteria)
    assert base == quality_key("fp", [dict(r) for r in dataset], json.loads(json.dumps(thresholds)), criteria)
    assert base != quality_key("fp2", dataset, thresholds, criteria)
    assert base != quality_key("fp", dataset[:-1], thresholds, criteria)
    assert base != quality_key("fp", dataset, {**thresholds, "min_cases": 1}, criteria)
    assert base != quality_key("fp", dataset, thresholds, {"behavior": [{"name": "expected_behavior", "v": 2}]})


def _suite_items(rows):
    return [{"datasource_item": {"id": r["id"]}, "results": []} for r in rows]


def test_previous_runs_finds_matching_full_runs_and_applies_rescores(monkeypatch):
    monkeypatch.setattr("agent_cicd.quality_gate.time.time", lambda: NOW)
    _, _, suites = _quality_inputs()
    evals = {name: SimpleNamespace(id=f"eval_{name}") for name in suites}
    runs, items = [], {}
    for name, spec in suites.items():
        runs.append(_run(f"{name}-old", f"eval_{name}", NOW - 100, {"quality_key": "other"}))
        runs.append(_run(f"{name}-full", f"eval_{name}", NOW - 50, {"quality_key": "k"}))
        runs.append(_run(f"{name}-rescore", f"eval_{name}", NOW - 40, {"quality_key": "k", "rescore_of": f"{name}-full"}))
        items[f"{name}-full"] = _suite_items(spec["rows"])
        items[f"{name}-rescore"] = [{"datasource_item": {"id": spec["rows"][0]["id"]}, "results": ["fresh"]}]
    client = SimpleNamespace(evals=_FakeEvals([], runs, items))
    found = previous_runs(client, evals, suites, "k", reuse_days=30)
    assert set(found) == set(suites)
    for name, (run, suite_items) in found.items():
        assert run.id == f"{name}-full"
        assert suite_items[0]["results"] == ["fresh"] and len(suite_items) == len(suites[name]["rows"])


def test_previous_runs_needs_every_suite_complete_and_recent(monkeypatch):
    monkeypatch.setattr("agent_cicd.quality_gate.time.time", lambda: NOW)
    _, _, suites = _quality_inputs()
    evals = {name: SimpleNamespace(id=f"eval_{name}") for name in suites}
    names = list(suites)
    runs = [_run("a", f"eval_{names[0]}", NOW - 10, {"quality_key": "k"}),
            _run("b", f"eval_{names[1]}", NOW - 31 * 86400, {"quality_key": "k"})]  # too old
    items = {"a": _suite_items(suites[names[0]]["rows"]), "b": _suite_items(suites[names[1]]["rows"])}
    client = SimpleNamespace(evals=_FakeEvals([], runs, items))
    assert previous_runs(client, evals, suites, "k", reuse_days=30) is None
    runs[1].created_at = NOW - 10
    items["b"] = items["b"][:-1]  # partial run
    assert previous_runs(client, evals, suites, "k", reuse_days=30) is None


# ------------------------------------------------------------------ Gate 2 reuse

def _policy():
    return redteam_gate.load_policy(AGENT_DIR)


def test_known_scans_reads_pointers_newest_first(tmp_path):
    for folder, eval_id, run_id in [("02-9", "eval_b", "run_b"), ("01-7", "eval_a", "run_a"), ("03-1", "eval_a", "run_a")]:
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "redteam.json").write_text(
            json.dumps({"result": {"details": {"eval_id": eval_id, "run_id": run_id}}}), encoding="utf-8")
    (tmp_path / "04-x").mkdir()
    (tmp_path / "04-x" / "redteam.json").write_text("not json", encoding="utf-8")
    assert redteam_gate.known_scans(tmp_path) == [("eval_a", "run_a"), ("eval_b", "run_b")]
    assert redteam_gate.known_scans(tmp_path / "missing") == []


def test_redteam_reuses_a_hidden_scan_found_through_a_previous_report(monkeypatch):
    monkeypatch.setattr("agent_cicd.redteam_gate.time.time", lambda: NOW)
    policy = _policy()
    fp = "f" * 64
    wanted = {"agent_name": "agent", "fingerprint": fp[:32], "policy": redteam_gate.policy_hash(policy)}
    hidden = SimpleNamespace(id="eval_hidden", name=policy["eval_name"], metadata={}, listed=False)
    runs = [_run("scan", "eval_hidden", NOW - 3600, {**wanted, "agent_version": "9"})]
    client = SimpleNamespace(evals=_FakeEvals([hidden], runs, {"scan": []}))
    monkeypatch.setattr(redteam_gate, "decide_redteam",
                        lambda status, items, pol: SimpleNamespace(allows_promotion=True, reasons=[], details={}))
    evaluation = redteam_gate._find_or_create_eval(client, policy, "gpt-5.4", ["eval_hidden"])
    assert evaluation.id.startswith("eval_new")  # old eval has no criteria hash, so new scans get a tagged eval
    found = redteam_gate._reusable_run(client, evaluation.id, [("eval_hidden", "scan")], "agent", fp, policy)
    assert found and found[0] == "eval_hidden" and found[1].id == "scan"
    # A different fingerprint or policy never reuses.
    assert redteam_gate._reusable_run(client, evaluation.id, [("eval_hidden", "scan")], "agent", "0" * 64, policy) is None
    # The tagged eval is found again next time instead of creating another one.
    again = redteam_gate._find_or_create_eval(client, policy, "gpt-5.4", [evaluation.id])
    assert again.id == evaluation.id and len(client.evals.created) == 1


# ------------------------------------------------------------------ deploy skips unchanged versions

class _FakeAgents:
    def __init__(self, latest):
        self.latest, self.created = latest, []

    def get(self, agent_name):
        return SimpleNamespace(as_dict=lambda: {"versions": {"latest": self.latest}})

    def create_version(self, agent_name, definition, metadata):
        self.created.append(metadata)
        return SimpleNamespace(version="11")


def _deploy_with(monkeypatch, latest, force=False):
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://example.services.ai.azure.com/api/projects/p")
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    agents = _FakeAgents(latest)
    client = SimpleNamespace(
        agents=agents,
        deployments=SimpleNamespace(get=lambda name: SimpleNamespace(
            as_dict=lambda: {"modelName": "gpt-5.4", "modelVersion": "2026-03-05"})))
    monkeypatch.setattr(deploy_module, "project_client", lambda endpoint: contextlib.nullcontext(client))
    assert deploy_module.deploy(AGENT_DIR, "dev", dry_run=False, force_new=force) == 0
    return agents


@pytest.fixture
def current_fingerprint(monkeypatch):
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://example.services.ai.azure.com/api/projects/p")
    rendered = deploy_module.render_agent(AGENT_DIR, "dev")
    return deploy_module.fingerprint(rendered.definition, "gpt-5.4@2026-03-05")


def test_deploy_keeps_the_latest_version_when_nothing_changed(monkeypatch, capsys, current_fingerprint):
    latest = {"version": "10", "metadata": {"deployed_by": "agent-cicd", "fingerprint": current_fingerprint[:32]}}
    agents = _deploy_with(monkeypatch, latest)
    out = capsys.readouterr().out
    assert agents.created == [] and "agent_version=10" in out and "previous_version=\n" in out


def test_deploy_creates_a_version_when_changed_edited_in_portal_or_forced(monkeypatch, capsys, current_fingerprint):
    assert _deploy_with(monkeypatch, {"version": "10", "metadata": {"deployed_by": "agent-cicd",
                                                                    "fingerprint": "different"}}).created
    assert _deploy_with(monkeypatch, {"version": "10", "metadata": {"fingerprint": current_fingerprint[:32]}}).created
    same = {"version": "10", "metadata": {"deployed_by": "agent-cicd", "fingerprint": current_fingerprint[:32]}}
    assert _deploy_with(monkeypatch, same, force=True).created
    assert "agent_version=11" in capsys.readouterr().out
