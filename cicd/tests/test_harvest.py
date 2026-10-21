import json

from agent_cicd.gates import validate_dataset
from agent_cicd.harvest import (Trace, build_case, last_message_text, normalize, render_pr_body, scrub,
                                select_candidates)


def trace(query, user="user-1", success=True, error="", evals=None, ts="2026-10-20T10:00:00Z", answer="ok"):
    return Trace(timestamp=ts, user_id=user, success=success, error_type=error, response_id=f"resp_{abs(hash(query))}",
                 conversation_id="conv_1", agent_version="6", query=query, answer=answer, eval_failures=evals or [])


def test_last_message_text_reads_the_last_user_turn_and_skips_system_prompt():
    messages = json.dumps([
        {"role": "developer", "parts": [{"type": "text", "content": "You are the Shift Manager agent."}]},
        {"role": "user", "parts": [{"type": "text", "content": "first"}]},
        {"role": "assistant", "parts": [{"type": "text", "content": "reply"}]},
        {"role": "user", "parts": [{"type": "text", "content": "What changed overnight?"}]},
    ])
    assert last_message_text(messages, "user") == "What changed overnight?"
    assert last_message_text(messages, "assistant") == "reply"
    assert last_message_text("not json", "user") == ""


def test_scrub_masks_emails_and_long_numbers():
    text, redacted = scrub("Call 416-555-0199 or mail jane.doe@opg.com about badge 123456789, unit 2")
    assert redacted
    assert "416" not in text and "jane.doe" not in text and "123456789" not in text
    assert "unit 2" in text
    assert scrub("What restrictions are on unit 2?") == ("What restrictions are on unit 2?", False)
    assert scrub("Logs from 2026-10-21 for WO 4471") == ("Logs from 2026-10-21 for WO 4471", False)


def test_pipeline_and_known_traffic_is_excluded():
    traces = [
        trace("Write me a short poem about the ocean."),           # already in dataset
        trace("What changed on unit 3?", user=""),                  # service / eval traffic
        trace("Any open restrictions on unit 4?", user="ci-bot"),  # pipeline identity
        trace("Which pumps had alarms last night?"),
    ]
    known = {normalize("Write me a short poem about the ocean")}
    candidates, stats = select_candidates(traces, known, {"ci-bot"}, max_candidates=10, sample_size=5)
    assert [c.query for c in candidates] == ["Which pumps had alarms last night?"]
    assert stats["skipped: already in dataset"] == 1
    assert stats["skipped: no user id (service or eval traffic)"] == 1
    assert stats["skipped: pipeline identity"] == 1


def test_problems_rank_before_coverage_samples_and_limits_apply():
    traces = [trace(f"new question {i}") for i in range(10)]
    traces += [trace("why did it fail", success=False, error="tool_user_error"),
               trace("bad answer", evals=["task_adherence"]),
               trace("bad answer!", ts="2026-10-19T10:00:00Z")]  # duplicate after normalization
    candidates, stats = select_candidates(traces, set(), set(), max_candidates=4, sample_size=2)
    assert [c.signal for c in candidates] == ["low_eval", "error", "sample", "sample"]
    assert stats["duplicates merged"] == 1


def test_harvested_cases_block_the_dataset_until_reviewed():
    candidates, _ = select_candidates([trace("Which pumps had alarms last night?")], set(), set(), 5, 5)
    case = build_case(candidates[0], {"category": "handover", "must_pass": False,
                                      "expected_behavior": "Lists alarms with citations."}, "TR-20261021-01", "20261021")
    base = [{"id": f"C{i}", "query": "q", "category": "scope", "must_pass": True, "expected_behavior": "e"}
            for i in range(25)]
    assert validate_dataset(base, 25) == []
    problems = validate_dataset(base + [case], 25)
    assert any("TR-20261021-01" in p and "needs review" in p for p in problems)

    reviewed = {k: v for k, v in case.items() if k != "needs_review"}
    assert validate_dataset(base + [reviewed], 25) == []
    todo = dict(reviewed, expected_behavior="TODO: write the expected behavior.")
    assert any("TODO" in p for p in validate_dataset(base + [todo], 25))


def test_failed_draft_still_produces_a_reviewable_case():
    candidates, stats = select_candidates([trace("Status of WO 4471?", success=False, error="timeout")], set(), set(), 5, 5)
    case = build_case(candidates[0], {}, "TR-20261021-01", "20261021")
    assert case["expected_behavior"].startswith("TODO")
    assert case["origin"]["signal"] == "error" and case["origin"]["observed_error"] == "timeout"
    body = render_pr_body([case], candidates, stats, "shiftmanageragent-prompt", 7, "cicd/agents/x/eval/dataset.jsonl")
    assert "TR-20261021-01" in body and "Agent error (timeout)" in body and "needs_review" in body
