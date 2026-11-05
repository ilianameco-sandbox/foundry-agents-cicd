import json
from pathlib import Path

from agent_cicd.gates import load_jsonl
from agent_cicd.smoke import PROMPT, check_answer, load_smoke, smoke_prompts

AGENT_DIR = Path(__file__).resolve().parents[1] / "agents" / "shift-manager"


def _check(check_id):
    return next(c for c in load_smoke(AGENT_DIR)["checks"] if c["id"] == check_id)


def test_shift_manager_smoke_checks_load_and_keep_the_capability_prompt():
    smoke = load_smoke(AGENT_DIR)
    assert smoke["attempts"] == 2
    assert PROMPT in smoke_prompts(AGENT_DIR)


def test_document_search_check_accepts_a_cited_answer():
    answer = ("If flow is above 565 L/s in the Unit 1 Main Steam severe accident guide, go to **SAG-7** and notify the "
              "Technical Support Centre. [NK38-SAM-1-64302, Rev R003, page 1]【5:0†source】")
    assert check_answer(_check("document-search"), answer) == []


def test_document_search_check_rejects_search_failures_and_wrong_answers():
    check = _check("document-search")
    assert check_answer(check, "No source found in the indexed documents.")
    assert check_answer(check, "Go to SAG-7.")  # no citation
    assert check_answer(check, "Go to SAG-70. [NK38-SAM-1-64302, Rev R003, page 1]")  # wrong guide
    assert check_answer(check, "")


def test_document_search_check_matches_the_golden_dataset():
    row = next(r for r in load_jsonl(AGENT_DIR / "eval" / "dataset.jsonl") if r["id"] == "GQ-067")
    check = _check("document-search")
    assert check["prompt"] == row["query"]
    assert row["ground_truth"] in check["must_match"][0]
    assert row["expected_doc_number"] in check["must_match"][1]


def test_missing_smoke_file_falls_back_to_the_capability_prompt(tmp_path):
    (tmp_path / "eval").mkdir()
    smoke = load_smoke(tmp_path)
    assert smoke == {"attempts": 1, "checks": [{"id": "capability", "prompt": PROMPT}]}
    assert check_answer(smoke["checks"][0], "I help with shift handovers.") == []
    (tmp_path / "eval" / "smoke.json").write_text(json.dumps({"checks": [{"id": "x", "prompt": " "}]}), encoding="utf-8")
    try:
        load_smoke(tmp_path)
    except ValueError as error:
        assert "no prompt" in str(error)
    else:
        raise AssertionError("an empty prompt must be rejected")
