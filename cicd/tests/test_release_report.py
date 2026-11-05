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
        judges = ["expected_behavior"] if row.get("suite") == "behavior" else list(DOC_JUDGES)
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
    assert "READY FOR HUMAN APPROVAL" in md
    assert "[documents](https://ai.azure.com/doc)" in md and "[Foundry](https://ai.azure.com/rt)" in md
    assert "Review deployments" in md and "shift-manager-v1.0.0" in md
    assert "| prohibited_actions | 100 | 0 |" in md and "task_adherence (advisory)" in md


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
