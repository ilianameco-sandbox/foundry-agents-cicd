import json
from pathlib import Path

from agent_cicd.gates import FAIL, PASS, REVIEW, decide_quality, decide_redteam, load_jsonl
from agent_cicd.release_report import NOT_RUN, build_packet, render_html, render_markdown, teams_payload

EVAL_DIR = Path(__file__).resolve().parents[1] / "agents" / "shift-manager" / "eval"
THRESHOLDS = json.loads((EVAL_DIR / "thresholds.json").read_text(encoding="utf-8"))
POLICY = json.loads((EVAL_DIR / "redteam.json").read_text(encoding="utf-8"))
DATASET = load_jsonl(EVAL_DIR / "dataset.jsonl")
DOC_JUDGES = ("answer_correctness", "citation_accuracy", "operational_safety")
CONTEXT = {"agent_name": "shiftmanageragent-promptv3", "version": "6", "fingerprint": "ab" * 32,
           "model_version": "gpt-5.4@2026-03-05", "sha": "c0ffee" * 7, "tag": "shift-manager-v1.0.0",
           "run_url": "https://github.com/o/r/actions/runs/1"}


def quality_report(fail=None):
    fail = fail or {}
    items = []
    for row in DATASET:
        judges = THRESHOLDS["suites"][row.get("suite") or "behavior"]["criteria"]
        results = [{"name": j, "passed": j not in fail.get(row["id"], ()), "reason": f"{j} reason <b>"}
                   for j in judges]
        items.append({"datasource_item": {**row, "sample.output_text": f"answer to {row['id']}"}, "results": results})
    result = decide_quality("completed", DATASET, items, THRESHOLDS)
    result.details["suites"] = {"documents": {"report_url": "https://ai.azure.com/doc"},
                                "behavior": {"report_url": "https://ai.azure.com/beh"}}
    return {"result": result.to_dict(), "output_items": items}


def redteam_report(successes=0, attacks=100):
    items = []
    for category in POLICY["categories"]:
        for i in range(attacks):
            items.append({"datasource_item": {"risk_sub_type": "Actions with Potential Physical Harm"},
                          "results": [{"name": category, "score": 5 if i < successes else 0, "threshold": 3,
                                       "properties": {"attack_success": i < successes}}]})
    result = decide_redteam("completed", items, POLICY)
    result.details.update({"enabled_subcategories": ["A", "B"], "report_url": "https://ai.azure.com/rt"})
    return {"result": result.to_dict(), "output_items": items}


def docs(n):
    return [r["id"] for r in DATASET if r.get("suite") == "documents"][:n]


def test_missing_evidence_blocks_the_release():
    packet = build_packet(None, None, THRESHOLDS, POLICY, CONTEXT)
    assert not packet["ready"]
    assert packet["quality"]["outcome"] == NOT_RUN and packet["redteam"]["outcome"] == NOT_RUN
    assert "BLOCKED" in render_markdown(packet)


def test_ready_only_when_both_gates_allow_promotion():
    assert build_packet(quality_report(), redteam_report(), THRESHOLDS, POLICY, CONTEXT)["ready"]
    unsafe = quality_report({docs(1)[0]: {"operational_safety"}})
    assert unsafe["result"]["outcome"] == FAIL
    assert not build_packet(unsafe, redteam_report(), THRESHOLDS, POLICY, CONTEXT)["ready"]
    assert not build_packet(quality_report(), redteam_report(successes=1), THRESHOLDS, POLICY, CONTEXT)["ready"]
    assert not build_packet(quality_report(), None, THRESHOLDS, POLICY, CONTEXT)["ready"]


def test_failures_list_must_pass_and_safety_first_with_answers_and_reasons():
    report = quality_report({docs(1)[0]: {"answer_correctness"}, docs(2)[1]: {"operational_safety"},
                             "AUTH-01": {"expected_behavior"}})
    q = build_packet(report, None, THRESHOLDS, POLICY, CONTEXT)["quality"]
    assert [f["id"] for f in q["failures"]] == ["AUTH-01", docs(2)[1], docs(1)[0]]
    assert q["failures"][0]["answer"] == "answer to AUTH-01"
    assert "expected_behavior reason" in q["failures"][0]["reason"]


def test_document_results_are_sliced_by_modality_and_trap():
    report = quality_report({i: {"citation_accuracy"} for i in docs(5)})
    slices = build_packet(report, None, THRESHOLDS, POLICY, CONTEXT)["quality"]["slices"]
    assert set(slices) == {"modality", "trap"}
    total = sum(b["citation_accuracy"][1] for b in slices["trap"].values())
    failed = total - sum(b["citation_accuracy"][0] for b in slices["trap"].values())
    assert (total, failed) == (94, 5)


def test_markdown_has_gates_links_and_approver_steps():
    md = render_markdown(build_packet(quality_report(), redteam_report(), THRESHOLDS, POLICY, CONTEXT))
    assert "READY FOR SIGN-OFF" in md
    assert "[documents](https://ai.azure.com/doc)" in md and "[Foundry](https://ai.azure.com/rt)" in md
    assert "Review deployments" in md and "shift-manager-v1.0.0" in md
    assert "signoff-qa" in md and "signoff-business" in md and "signoff-rai" in md
    assert "| prohibited_actions | 100 | 0 |" in md and "task_adherence (advisory)" in md
    assert "## Responsible AI scorecard" in md


def test_close_to_limit_red_team_is_ready_but_flagged():
    report = redteam_report(successes=2, attacks=200)  # task_adherence 1% ASR, upper bound near 5% limit
    for row in report["result"]["rows"]:
        row["outcome"] = REVIEW if row["category"] == "task_adherence" else row["outcome"]
    report["result"]["outcome"] = REVIEW
    packet = build_packet(quality_report(), report, THRESHOLDS, POLICY, CONTEXT)
    assert packet["ready"] and packet["flagged"]


def test_html_escapes_agent_and_judge_text():
    report = quality_report({docs(1)[0]: {"citation_accuracy"}})
    report["output_items"][-94]["datasource_item"]["sample.output_text"] = "<script>alert(1)</script>"
    page = render_html(build_packet(report, redteam_report(), THRESHOLDS, POLICY, CONTEXT))
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page
    assert "reason &lt;b&gt;" in page


def test_teams_card_points_to_the_run():
    payload = teams_payload(build_packet(quality_report(), redteam_report(), THRESHOLDS, POLICY, CONTEXT))
    card = payload["attachments"][0]["content"]
    assert card["actions"][0]["url"] == CONTEXT["run_url"]
    assert "Ready for approval" in card["body"][0]["text"]


def test_content_filtered_cases_are_not_listed_as_failures():
    report = quality_report({"INJ-01": {"expected_behavior"}})
    for item in report["output_items"]:
        if item["datasource_item"]["id"] == "INJ-01":
            item["sample"] = {"error": {"code": "content_filtered", "message": "filtered"}}
    report["result"] = decide_quality("completed", DATASET, report["output_items"], THRESHOLDS).to_dict()
    q = build_packet(report, None, THRESHOLDS, POLICY, CONTEXT)["quality"]
    assert report["result"]["outcome"] == PASS
    assert q["failures"] == [] and q["content_filtered"] == ["INJ-01"]
    assert "counted as safe): INJ-01" in render_markdown(build_packet(report, None, THRESHOLDS, POLICY, CONTEXT))


def policy_report(outcome, env="test"):
    rows = [{"rule": "model_deployment_type", "subject": "gpt-5.4", "status": "block", "severity": "block",
             "finding": "GlobalStandard: processed in any Azure region worldwide"}]
    return {"result": {"outcome": outcome, "reasons": ["model_deployment_type (gpt-5.4): GlobalStandard"],
                       "rows": rows, "details": {"preset": "canada-strict", "enforcement": "warn",
                                                 "environment": env, "would_block": 1, "warnings": 0}}}


def test_residency_warnings_flag_and_blocks_stop_the_release():
    flagged = build_packet(quality_report(), redteam_report(), THRESHOLDS, POLICY, CONTEXT,
                           {"dev": policy_report(PASS), "test": policy_report(REVIEW)})
    assert flagged["ready"] and flagged["flagged"]
    md = render_markdown(flagged)
    assert "G2 · Config & residency (test)" in md and "Config & residency findings (test)" in md
    blocked = build_packet(quality_report(), redteam_report(), THRESHOLDS, POLICY, CONTEXT,
                           {"test": policy_report(FAIL)})
    assert not blocked["ready"]
    missing = build_packet(quality_report(), redteam_report(), THRESHOLDS, POLICY, CONTEXT, {"test": None})
    assert not missing["ready"]


def test_rai_scorecard_maps_evidence_to_principles():
    packet = build_packet(quality_report(), redteam_report(), THRESHOLDS, POLICY, CONTEXT,
                          {"test": policy_report(REVIEW)})
    card = {row["principle"]: row for row in packet["scorecard"]}
    assert card["Privacy & security"]["status"] == REVIEW
    assert any("pii_leak" in e for e in card["Privacy & security"]["evidence"])
    assert any("red team prohibited_actions" in e for e in card["Reliability & safety"]["evidence"])
    assert card["Transparency"]["status"] == PASS
    assert card["Fairness"]["status"] == "HUMAN" and card["Accountability"]["status"] == "HUMAN"
