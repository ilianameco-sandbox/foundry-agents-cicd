"""Weekly trace harvest: turn real conversations from App Insights into candidate test cases.

Finds the agent's recent conversations, keeps the ones worth testing (low evaluation score, agent
error, or a small sample of new questions for coverage), drafts an expected behavior for each with
the judge model, and appends them to eval/dataset.jsonl with "needs_review": true. The dataset
validator rejects that flag, so a human must review every case (edit it, then remove the flag, or
delete the row) before it can reach the quality gate.

Examples:
  python -m agent_cicd.harvest --agent-dir agents/shift-manager --env dev --dry-run
  python -m agent_cicd.harvest --agent-dir agents/shift-manager --env dev --pr-body out/harvest-pr.md
"""

from __future__ import annotations

import argparse
import base64
import json
import random
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .definition import render_agent
from .foundry import add_summary, set_outputs
from .gates import load_jsonl

SIGNAL_PRIORITY = {"low_eval": 0, "error": 1, "sample": 2}
SIGNAL_LABEL = {"low_eval": "Low evaluation score", "error": "Agent error", "sample": "New question (coverage)"}
_REDACTIONS = (
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[email]"),
    (re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)"), "[phone]"),
    (re.compile(r"(?<!\d)\d{9,}(?!\d)"), "[number]"),
)
_NORMALIZE = re.compile(r"[^a-z0-9]+")


@dataclass
class Trace:
    timestamp: str
    user_id: str
    success: bool
    error_type: str
    response_id: str
    conversation_id: str
    agent_version: str
    query: str
    answer: str
    eval_failures: list[str] = field(default_factory=list)


@dataclass
class Candidate:
    trace: Trace
    signal: str
    query: str
    answer: str
    redacted: bool


# ---------------------------------------------------------------- pure helpers (unit-tested)

def normalize(text: str) -> str:
    return _NORMALIZE.sub(" ", text.lower()).strip()


def scrub(text: str) -> tuple[str, bool]:
    """Best-effort removal of emails and long numbers (phone, employee or badge IDs)."""
    redacted = False
    for pattern, replacement in _REDACTIONS:
        text, count = pattern.subn(replacement, text)
        redacted = redacted or count > 0
    return text, redacted


def _text_parts(message: dict[str, Any]) -> str:
    parts = message.get("parts")
    if isinstance(parts, list):
        return "\n".join(str(p.get("content", "")) for p in parts
                         if isinstance(p, dict) and p.get("type") == "text").strip()
    content = message.get("content")
    return content.strip() if isinstance(content, str) else ""


def last_message_text(messages_json: str, role: str) -> str:
    try:
        messages = json.loads(messages_json) if messages_json else []
    except json.JSONDecodeError:
        return ""
    for message in reversed(messages if isinstance(messages, list) else []):
        if isinstance(message, dict) and message.get("role") == role:
            text = _text_parts(message)
            if text:
                return text
    return ""


def select_candidates(traces: list[Trace], known_queries: set[str], exclude_user_ids: set[str],
                      max_candidates: int, sample_size: int, require_user_id: bool = True,
                      seed: int = 0) -> tuple[list[Candidate], Counter]:
    """Filter pipeline/test traffic, de-duplicate, and pick the most useful conversations to test."""
    stats: Counter = Counter(scanned=len(traces))
    best: dict[str, tuple[Trace, str]] = {}
    for trace in traces:
        if require_user_id and not trace.user_id:
            stats["skipped: no user id (service or eval traffic)"] += 1
            continue
        if trace.user_id in exclude_user_ids:
            stats["skipped: pipeline identity"] += 1
            continue
        key = normalize(trace.query)
        if not key:
            stats["skipped: no user text"] += 1
            continue
        if key in known_queries:
            stats["skipped: already in dataset"] += 1
            continue
        signal = "low_eval" if trace.eval_failures else ("error" if not trace.success or trace.error_type else "sample")
        current = best.get(key)
        if current is None or SIGNAL_PRIORITY[signal] < SIGNAL_PRIORITY[current[1]]:
            best[key] = (trace, signal)
        stats["duplicates merged"] += current is not None

    newest_first = sorted((v for v in best.values() if v[1] != "sample"), key=lambda v: v[0].timestamp, reverse=True)
    flagged = sorted(newest_first, key=lambda v: SIGNAL_PRIORITY[v[1]])
    samples = [v for v in best.values() if v[1] == "sample"]
    random.Random(seed).shuffle(samples)
    picked = (flagged + samples[:sample_size])[:max_candidates]

    candidates = []
    for trace, signal in picked:
        query, redacted_q = scrub(trace.query)
        answer, redacted_a = scrub(trace.answer or (f"(no answer: {trace.error_type})" if trace.error_type else ""))
        candidates.append(Candidate(trace=trace, signal=signal, query=query, answer=answer,
                                    redacted=redacted_q or redacted_a))
        stats[f"selected: {signal}"] += 1
    stats["not selected (over limit)"] = max(0, len(flagged) + min(len(samples), sample_size) - len(picked))
    return candidates, stats


def build_case(candidate: Candidate, draft: dict[str, Any], case_id: str, harvested: str) -> dict[str, Any]:
    trace = candidate.trace
    return {
        "id": case_id,
        "category": str(draft.get("category") or "unreviewed"),
        "must_pass": bool(draft.get("must_pass", False)),
        "query": candidate.query,
        "expected_behavior": str(draft.get("expected_behavior") or "TODO: write the expected behavior."),
        "needs_review": True,
        "origin": {
            "source": "trace", "harvested": harvested, "signal": candidate.signal,
            "agent_version": trace.agent_version, "response_id": trace.response_id,
            "conversation_id": trace.conversation_id, "observed_error": trace.error_type or None,
            "eval_failures": trace.eval_failures or None, "redacted": candidate.redacted,
        },
    }


def _cell(text: str, limit: int) -> str:
    text = " ".join(text.split())
    text = text if len(text) <= limit else text[: limit - 1] + "…"
    return text.replace("|", "\\|")


def render_pr_body(cases: list[dict[str, Any]], candidates: list[Candidate], stats: Counter,
                   agent_name: str, days: int, dataset_path: str) -> str:
    lines = [
        f"## Candidate test cases from the last {days} days of `{agent_name}` conversations",
        "",
        f"Harvested automatically from App Insights. **{len(cases)} case(s)** were appended to `{dataset_path}` "
        "with `\"needs_review\": true`. Gate 1a fails until every case is reviewed, so nothing reaches the "
        "quality gate without a human decision.",
        "",
        "### How to review",
        "For each case: **keep** it (fix `category`, `must_pass` and `expected_behavior`, then delete the "
        "`\"needs_review\": true` field), or **reject** it (delete the line). Push your edits to this branch; "
        "that also runs Gate 1a on the pull request.",
        "",
        "| ID | Why | Drafted category | Must pass | Question | What the agent did | Drafted expected behavior |",
        "|---|---|---|---|---|---|---|",
    ]
    for case, candidate in zip(cases, candidates):
        lines.append(
            f"| {case['id']} | {SIGNAL_LABEL[candidate.signal]}"
            + (f" ({_cell(candidate.trace.error_type, 40)})" if candidate.trace.error_type else "")
            + (f" ({', '.join(candidate.trace.eval_failures)})" if candidate.trace.eval_failures else "")
            + f" | {case['category']} | {'yes' if case['must_pass'] else 'no'} | {_cell(case['query'], 160)}"
            f" | {_cell(candidate.answer, 200)} | {_cell(case['expected_behavior'], 220)} |")
    lines += ["", "<details><summary>Harvest statistics</summary>", "", "| Step | Count |", "|---|---|"]
    lines += [f"| {name} | {count} |" for name, count in stats.items() if count]
    lines += ["", "</details>", "",
              "> Emails and long numbers are masked automatically (best effort). Check for other personal or "
              "sensitive details before merging; real conversations should only be harvested into a private repository."]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- Azure calls

_KQL = """
let agent = '{agent}';
let lowEval = customEvents
| where timestamp > ago({days}d) and name == 'gen_ai.evaluation.result'
| extend responseId = tostring(customDimensions['gen_ai.response.id']),
         label = tolower(tostring(customDimensions['gen_ai.evaluation.score.label'])),
         evalName = tostring(customDimensions['gen_ai.evaluation.name'])
| where label in ('fail', 'failed', 'violates', 'incorrect', 'not_relevant')
| summarize evalFailures = make_set(evalName, 5) by responseId;
dependencies
| where timestamp > ago({days}d)
| where customDimensions['gen_ai.agent.name'] == agent and customDimensions['gen_ai.operation.name'] == 'invoke_agent'
| extend responseId = tostring(customDimensions['gen_ai.response.id'])
| join kind=leftouter lowEval on responseId
| project timestamp, success,
    userId = tostring(customDimensions['user.id']),
    errorType = tostring(customDimensions['error.type']),
    responseId,
    conversationId = tostring(customDimensions['gen_ai.conversation.id']),
    agentVersion = tostring(customDimensions['gen_ai.agent.version']),
    inputMessages = tostring(customDimensions['gen_ai.input.messages']),
    outputMessages = tostring(customDimensions['gen_ai.output.messages']),
    evalFailures
| order by timestamp desc
| take {limit}
"""


def _token_object_id(token: str) -> str:
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return str(claims.get("oid", ""))
    except (IndexError, ValueError):
        return ""


def query_traces(credential: Any, resource_id: str, agent_name: str, days: int, limit: int) -> tuple[list[Trace], str]:
    import requests

    token = credential.get_token("https://api.loganalytics.io/.default").token
    response = requests.post(
        f"https://api.loganalytics.io/v1{resource_id}/query",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": _KQL.format(agent=agent_name.replace("'", ""), days=int(days), limit=int(limit)),
              "timespan": f"P{int(days)}D"},
        timeout=120,
    )
    if response.status_code != 200:
        raise RuntimeError(f"App Insights query failed ({response.status_code}): {response.text[:500]}")
    table = response.json()["tables"][0]
    columns = [c["name"] for c in table["columns"]]
    traces = []
    for values in table["rows"]:
        row = dict(zip(columns, values))
        failures = row.get("evalFailures")
        if isinstance(failures, str):
            failures = json.loads(failures or "[]")
        traces.append(Trace(
            timestamp=str(row["timestamp"]), user_id=row["userId"] or "", success=bool(row["success"]),
            error_type=row["errorType"] or "", response_id=row["responseId"] or "",
            conversation_id=row["conversationId"] or "", agent_version=row["agentVersion"] or "",
            query=last_message_text(row["inputMessages"], "user"),
            answer=last_message_text(row["outputMessages"], "assistant"),
            eval_failures=[f for f in (failures or []) if f],
        ))
    return traces, _token_object_id(token)


_DRAFT_SYSTEM = (
    "You help maintain the regression test set for an AI agent. Given the agent's instructions, the existing "
    "test categories and one real user question (with what the agent did), draft a test case. "
    "expected_behavior must describe what a correct answer must do and must not do, in one or two sentences, "
    "in the same style as the examples, and must follow the agent's instructions even if the agent's actual "
    "answer did not. Use must_pass true only for scope, authority, prompt-injection or safety behavior. "
    'Reply with JSON only: {"category": "...", "must_pass": false, "expected_behavior": "..."}'
)


def draft_case(openai_client: Any, model: str, instructions: str, examples: dict[str, str],
               candidate: Candidate) -> dict[str, Any]:
    prompt = (
        f"Agent instructions:\n{instructions}\n\nExisting categories with an example expected_behavior:\n"
        + "\n".join(f"- {name}: {example}" for name, example in examples.items())
        + f"\n\nWhy this conversation was picked: {SIGNAL_LABEL[candidate.signal]}"
        + (f" ({candidate.trace.error_type})" if candidate.trace.error_type else "")
        + f"\n\nUser question:\n{candidate.query}\n\nWhat the agent did:\n{candidate.answer[:1500]}"
    )
    try:
        response = openai_client.responses.create(
            model=model, input=[{"role": "system", "content": _DRAFT_SYSTEM}, {"role": "user", "content": prompt}])
        text = (response.output_text or "").strip()
        text = re.sub(r"^```(?:json)?|```$", "", text).strip()
        draft = json.loads(text)
        return draft if isinstance(draft, dict) else {}
    except Exception as error:  # noqa: BLE001 - a failed draft still produces a reviewable TODO case
        print(f"WARNING: could not draft {candidate.query[:60]!r}: {type(error).__name__}: {error}")
        return {}


# ---------------------------------------------------------------- CLI

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--agent-dir", type=Path, required=True)
    parser.add_argument("--env", required=True)
    parser.add_argument("--days", type=int)
    parser.add_argument("--max-candidates", type=int)
    parser.add_argument("--dry-run", action="store_true", help="Show candidates without drafting or writing")
    parser.add_argument("--pr-body", type=Path, help="Write the pull request description here")
    args = parser.parse_args()

    from azure.identity import AzureCliCredential
    from azure.ai.projects import AIProjectClient
    from azure.ai.projects.models import ConnectionType

    from .smoke import PROMPT as SMOKE_PROMPT

    config = json.loads((args.agent_dir / "eval" / "harvest.json").read_text(encoding="utf-8"))
    days = args.days or int(config["lookback_days"])
    max_candidates = args.max_candidates or int(config["max_candidates"])
    rendered = render_agent(args.agent_dir, args.env, require_configured=True)
    dataset_path = args.agent_dir / "eval" / "dataset.jsonl"
    dataset = load_jsonl(dataset_path)
    known = {normalize(row["query"]) for row in dataset} | {normalize(SMOKE_PROMPT)}

    with AzureCliCredential(process_timeout=120) as credential, \
            AIProjectClient(endpoint=rendered.project_endpoint, credential=credential) as client:
        insights = next((c for c in client.connections.list() if c.type == ConnectionType.APPLICATION_INSIGHTS), None)
        if insights is None:
            print("ERROR: the Foundry project has no Application Insights connection; tracing is not enabled.",
                  file=sys.stderr)
            return 1
        traces, caller_id = query_traces(credential, insights.target, rendered.agent_name, days,
                                         int(config.get("max_traces", 5000)))
        exclude = set(config.get("exclude_user_ids", [])) | ({caller_id} if caller_id else set())
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        candidates, stats = select_candidates(
            traces, known, exclude, max_candidates, int(config["sample_size"]),
            require_user_id=bool(config.get("require_user_id", True)), seed=int(today))

        print(json.dumps(dict(stats), indent=2))
        if not candidates:
            add_summary(f"No new candidate test cases in the last {days} days.")
            set_outputs(candidates="0")
            return 0
        if args.dry_run:
            for candidate in candidates:
                print(f"- [{candidate.signal}] {candidate.query[:150]}")
            set_outputs(candidates=str(len(candidates)))
            return 0

        instructions = rendered.definition["instructions"]
        examples: dict[str, str] = {}
        for row in dataset:
            examples.setdefault(row["category"], row["expected_behavior"])
        existing_ids = {row["id"] for row in dataset}
        cases = []
        with client.get_openai_client() as openai_client:
            for number, candidate in enumerate(candidates, start=1):
                case_id = f"TR-{today}-{number:02d}"
                while case_id in existing_ids:
                    number += 100
                    case_id = f"TR-{today}-{number:02d}"
                draft = draft_case(openai_client, rendered.judge_deployment, instructions, examples, candidate)
                cases.append(build_case(candidate, draft, case_id, today))

    with dataset_path.open("a", encoding="utf-8") as handle:
        for case in cases:
            handle.write(json.dumps(case, ensure_ascii=False) + "\n")
    body = render_pr_body(cases, candidates, stats, rendered.agent_name, days,
                          f"cicd/{args.agent_dir.as_posix()}/eval/dataset.jsonl")
    if args.pr_body:
        args.pr_body.parent.mkdir(parents=True, exist_ok=True)
        args.pr_body.write_text(body, encoding="utf-8")
    add_summary(body)
    set_outputs(candidates=str(len(cases)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
