"""G5 UAT evidence, G6 release decision, and the prod drift check (all offline)."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_cicd.definition import fingerprint
from agent_cicd.drift import find_drift
from agent_cicd.gates import FAIL, INCONCLUSIVE, PASS
from agent_cicd.release_decision import decide_release, load_governance, to_markdown as decision_md
from agent_cicd.uat_evidence import Rating, decide_uat, load_rules, to_markdown as uat_md

AGENT_DIR = Path(__file__).resolve().parents[1] / "agents" / "shift-manager"
CREATED = datetime(2026, 11, 1, tzinfo=timezone.utc)
RULES = {"min_rated_answers": 10, "min_thumbs_up_rate": 0.8, "evaluation_names": ["task_completion", "overall_quality"],
         "sources": ["builder", "end_user"], "lookback_days": 30}


def rating(i, up=True, version="3", source="builder", name="agent", when=None, response=None,
           eval_name="task_completion"):
    when = when or CREATED + timedelta(hours=i + 1)
    return Rating(timestamp=when.isoformat().replace("+00:00", "Z"), source=source, eval_name=eval_name,
                  score=1.0 if up else 0.0, label="pass" if up else "fail", explanation="" if up else f"wrong {i}",
                  agent_name=name, agent_version=version, response_id=response or f"resp-{i}", trace_id=f"t{i}")


# ---------------------------------------------------------------- UAT evidence

def test_shipped_uat_rules():
    rules = load_rules(AGENT_DIR)
    assert rules["min_rated_answers"] == 10 and rules["min_thumbs_up_rate"] == 0.8
    assert {"task_completion", "overall_quality"} <= set(rules["evaluation_names"])


def test_portal_trace_thumbs_count():
    # Foundry portal annotations arrive as gen_ai.evaluation.result with gen_ai.evaluation.name "overall_quality".
    result = decide_uat([rating(i, eval_name="overall_quality") for i in range(10)], RULES, "agent", "3", CREATED)
    assert result.outcome == PASS and result.details["rated"] == 10


def test_automated_evaluator_results_are_not_ratings():
    automated = [rating(i, eval_name="prohibited_actions") for i in range(20)]
    assert decide_uat(automated, RULES, "agent", "3", CREATED).details["rated"] == 0


def test_enough_positive_ratings_pass():
    result = decide_uat([rating(i, up=i < 9) for i in range(10)], RULES, "agent", "3", CREATED)
    assert result.outcome == PASS and result.details["rate"] == 0.9


def test_too_few_ratings_is_inconclusive_not_a_failure():
    result = decide_uat([rating(i) for i in range(4)], RULES, "agent", "3", CREATED)
    assert result.outcome == INCONCLUSIVE and "at least 10" in result.reasons[0]


def test_low_thumbs_up_rate_fails_and_lists_thumbs_down():
    result = decide_uat([rating(i, up=i < 7) for i in range(10)], RULES, "agent", "3", CREATED)
    assert result.outcome == FAIL
    md = uat_md(result, "agent")
    assert "👎 answers" in md and "wrong 9" in md


def test_other_versions_and_agents_are_ignored():
    ratings = [rating(i) for i in range(10)] + [rating(20 + i, up=False, version="2") for i in range(5)]
    ratings += [rating(40, up=False, name="other-agent")]
    result = decide_uat(ratings, RULES, "agent", "3", CREATED)
    assert result.outcome == PASS and result.details["rated"] == 10
    assert any("5 rating(s) for other versions" in r for r in result.reasons)


def test_unversioned_ratings_count_only_after_the_version_was_created():
    before = [rating(i, version="", when=CREATED - timedelta(hours=1), response=f"old-{i}") for i in range(5)]
    after = [rating(i, version="") for i in range(10)]
    result = decide_uat(before + after, RULES, "agent", "3", CREATED)
    assert result.details["rated"] == 10 and result.details["matched_by_time"] == 10


def test_latest_rating_per_answer_wins():
    ratings = [rating(i) for i in range(10)]
    ratings.append(rating(0, up=False, when=CREATED + timedelta(days=2), response="resp-0"))
    result = decide_uat(ratings, RULES, "agent", "3", CREATED)
    assert result.details["rated"] == 10 and result.details["thumbs_down"] == 1


# ---------------------------------------------------------------- release decision

NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)
GOV = {"signoffs": {"qa": {"title": "QA", "environment": "signoff-qa"},
                    "business": {"title": "Business", "environment": "signoff-business"},
                    "rai": {"title": "RAI", "environment": "signoff-rai"}},
       "require_distinct_approvers": True, "max_signoff_age_days": 14}


def approval(env, user, state="approved"):
    return {"environments": [{"name": env}], "state": state, "user": {"login": user}, "comment": f"ok {env}"}


def job(env, days_ago=1, conclusion="success"):
    return {"name": f"G5 sign-off ({env})", "conclusion": conclusion,
            "completed_at": (NOW - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")}


def all_signed(users=("qa", "biz", "rai"), ages=(1, 1, 1)):
    envs = ["signoff-qa", "signoff-business", "signoff-rai"]
    return [approval(e, u) for e, u in zip(envs, users)], [job(e, a) for e, a in zip(envs, ages)]


def test_shipped_governance_has_three_roles():
    gov = load_governance(AGENT_DIR)
    assert [v["environment"] for v in gov["signoffs"].values()] == ["signoff-qa", "signoff-business", "signoff-rai"]
    assert gov["max_signoff_age_days"] == 14


def test_three_distinct_recent_signoffs_pass():
    approvals, jobs = all_signed()
    result = decide_release(GOV, approvals, jobs, author="dev", now=NOW)
    assert result.outcome == PASS
    assert "| QA | `signoff-qa` | qa |" in decision_md(result)


def test_missing_or_rejected_signoff_fails():
    approvals, jobs = all_signed()
    result = decide_release(GOV, approvals[:2], jobs, author="dev", now=NOW)
    assert result.outcome == FAIL and any("RAI: missing" in r for r in result.reasons)
    approvals[2] = approval("signoff-rai", "rai", state="rejected")
    assert any("rejected by rai" in r for r in decide_release(GOV, approvals, jobs, "dev", NOW).reasons)


def test_old_signoff_expires():
    approvals, jobs = all_signed(ages=(1, 20, 1))
    result = decide_release(GOV, approvals, jobs, author="dev", now=NOW)
    assert result.outcome == FAIL and any("expired" in r for r in result.reasons)


def test_self_approval_and_shared_roles_fail_when_distinct_is_required():
    approvals, jobs = all_signed(users=("dev", "biz", "rai"))
    assert any("no self-approval" in r for r in decide_release(GOV, approvals, jobs, "dev", NOW).reasons)
    approvals, jobs = all_signed(users=("same", "same", "rai"))
    assert decide_release(GOV, approvals, jobs, "dev", NOW).outcome == FAIL


def test_sandbox_allows_one_person_in_every_role():
    approvals, jobs = all_signed(users=("me", "me", "me"))
    result = decide_release({**GOV, "require_distinct_approvers": False}, approvals, jobs, author="me", now=NOW)
    assert result.outcome == PASS and any("several roles" in r for r in result.reasons)


@pytest.mark.parametrize("bad, message", [({"signoffs": {}}, "at least one"),
                                          ({"signoffs": {"a": {"environment": "x"}, "b": {"environment": "x"}}},
                                           "its own environment")])
def test_invalid_governance_is_rejected(tmp_path, bad, message):
    import json
    (tmp_path / "governance.json").write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_governance(tmp_path)


# ---------------------------------------------------------------- drift

DEFINITION = {"kind": "prompt", "model": "gpt", "instructions": "be helpful", "tools": []}


def live(definition=None, deployed_by="agent-cicd", model_version="gpt@1"):
    definition = definition or DEFINITION
    return {"version": "4", "definition": definition,
            "metadata": {"deployed_by": deployed_by, "model_version": model_version,
                         "fingerprint": fingerprint(definition, model_version)[:32]}}


def test_no_drift_when_prod_matches_the_release():
    assert find_drift(DEFINITION, "gpt@1", live()) == []


def test_portal_edit_is_drift():
    edited = {**DEFINITION, "instructions": "be very helpful"}
    findings = find_drift(DEFINITION, "gpt@1", live(edited, deployed_by=None))
    assert any("not created by the pipeline" in f for f in findings)
    assert any("differs from the released files" in f for f in findings)


def test_model_upgrade_is_drift():
    findings = find_drift(DEFINITION, "gpt@2", live())
    assert findings == ["Model deployment changed since release: released on gpt@1, now gpt@2"]
