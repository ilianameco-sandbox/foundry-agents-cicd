"""Release approval packet: one place where the prod approver sees the evidence from every gate.

Reads the Gate 1b (quality) and Gate 2 (red-team) reports, then writes:
  * a Markdown summary (GitHub job summary and the release-approval issue),
  * a self-contained HTML report (uploaded as a run artifact),
  * an optional Microsoft Teams message when TEAMS_WEBHOOK_URL is set.

A missing or unreadable report is shown as NOT RUN and blocks the release, so the packet never looks
greener than the evidence.

Example:
  python -m agent_cicd.release_report --agent-dir agents/shift-manager \
      --quality out/quality.json --redteam out/redteam.json --version 6 --fingerprint <sha256> \
      --markdown out/release-packet.md --html out/release-packet.html
"""

from __future__ import annotations

import argparse
import html
import json
import os
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any

from .foundry import add_summary, set_outputs
from .gates import FAIL, INCONCLUSIVE, PASS, REVIEW, suite_of

NOT_RUN = "NOT RUN"
ICON = {PASS: "✅", REVIEW: "🔍", INCONCLUSIVE: "⚠️", FAIL: "❌", NOT_RUN: "⛔"}
COLOR = {PASS: "#2e7d32", REVIEW: "#b26a00", INCONCLUSIVE: "#8d6e00", FAIL: "#c62828", NOT_RUN: "#616161"}
MAX_FAILURES = 15


def _load(path: Path | None) -> dict[str, Any] | None:
    if not path or not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _clip(text: Any, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _rate(passed: int, total: int) -> str:
    return f"{passed / total:.0%}" if total else "–"


# ---------------------------------------------------------------- model

def quality_section(report: dict[str, Any] | None, thresholds: dict[str, Any]) -> dict[str, Any]:
    if report is None:
        return {"outcome": NOT_RUN, "reasons": ["Quality gate report is missing: the gate did not run or crashed"]}
    result = report.get("result") or {}
    details = result.get("details") or {}
    criteria = thresholds.get("criteria", {})
    criteria_rows = []
    for row in result.get("rows") or []:
        applies = criteria.get(row["criterion"], {}).get("applies_to") or ["all"]
        criteria_rows.append({**row, "suite": ", ".join(applies)})

    slices: dict[str, dict[str, dict[str, list[int]]]] = {}
    failures = []
    doc_judges = (thresholds.get("suites", {}).get("documents") or {}).get("criteria", [])
    slice_fields = (thresholds.get("suites", {}).get("documents") or {}).get("slices", [])
    # Cases the gate already treated as safe blocks or as "could not run" are reported separately, not as failures.
    excluded = set(details.get("content_filtered") or []) | set(details.get("could_not_run") or {})
    for item in report.get("output_items") or []:
        ds = item.get("datasource_item") or {}
        results = {r.get("name"): r for r in item.get("results") or [] if isinstance(r, dict)}
        suite = suite_of(ds)
        judges = doc_judges if suite == "documents" else ["expected_behavior"]
        if ds.get("id") in excluded:
            continue
        if suite == "documents":
            for field in slice_fields:
                value = str(ds.get(field) or "none")
                for judge in judges:
                    verdict = (results.get(judge) or {}).get("passed")
                    if isinstance(verdict, bool):
                        bucket = slices.setdefault(field, {}).setdefault(value, {}).setdefault(judge, [0, 0])
                        bucket[0] += verdict
                        bucket[1] += 1
        failed = [j for j in judges if (results.get(j) or {}).get("passed") is False]
        if failed:
            failures.append({
                "id": ds.get("id"), "suite": suite, "must_pass": bool(ds.get("must_pass")),
                "judges": failed, "query": ds.get("query"),
                "answer": ds.get("sample.output_text") or "",
                "reason": " | ".join(f"{j}: {(results.get(j) or {}).get('reason') or 'no reason given'}" for j in failed),
                "trap": ds.get("trap"), "modality": ds.get("modality"),
            })
    # Must-pass and safety failures first: they are what blocks a release.
    failures.sort(key=lambda f: (not f["must_pass"], "operational_safety" not in f["judges"], f["id"] or ""))
    return {
        "outcome": result.get("outcome", NOT_RUN), "reasons": result.get("reasons") or [],
        "criteria": criteria_rows, "slices": slices, "failures": failures,
        "failure_count": len(failures), "suites": details.get("suites") or {},
        "could_not_run": details.get("could_not_run") or {}, "content_filtered": details.get("content_filtered") or [],
        "report_url": details.get("report_url"),
    }


def redteam_section(report: dict[str, Any] | None, policy: dict[str, Any]) -> dict[str, Any]:
    if report is None:
        return {"outcome": NOT_RUN, "reasons": ["Red-team gate did not run (it only runs when the quality gate passes)"]}
    result = report.get("result") or {}
    details = result.get("details") or {}
    by_subcategory: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for item in report.get("output_items") or []:
        ds = item.get("datasource_item") or {}
        sub = ds.get("risk_sub_type") or ds.get("risk_subcategory") or ds.get("subcategory")
        if not sub:
            continue
        success = any((r.get("properties") or {}).get("attack_success") is True
                      for r in item.get("results") or [] if isinstance(r, dict))
        by_subcategory[str(sub)][0] += success
        by_subcategory[str(sub)][1] += 1
    return {
        "outcome": result.get("outcome", NOT_RUN), "reasons": result.get("reasons") or [],
        "categories": result.get("rows") or [], "reused": bool(details.get("reused")),
        "report_url": details.get("report_url"), "attacks": details.get("attacks"),
        "enabled_subcategories": details.get("enabled_subcategories") or [],
        "strategies": policy.get("attack_strategies", []), "turns": policy.get("num_turns"),
        "by_subcategory": sorted(([k, v[0], v[1]] for k, v in by_subcategory.items() if v[0]),
                                 key=lambda r: -r[1]),
    }


def build_packet(quality: dict[str, Any] | None, redteam: dict[str, Any] | None, thresholds: dict[str, Any],
                 policy: dict[str, Any], context: dict[str, str]) -> dict[str, Any]:
    q = quality_section(quality, thresholds)
    r = redteam_section(redteam, policy)
    ready = q["outcome"] in (PASS, REVIEW) and r["outcome"] in (PASS, REVIEW)
    flagged = REVIEW in (q["outcome"], r["outcome"])
    return {"ready": ready, "flagged": flagged, "quality": q, "redteam": r, "context": context}


# ---------------------------------------------------------------- markdown

def _flag_note(row: dict[str, Any]) -> str:
    n = row.get("contradictions") or 0
    return f" ({n} service flags disagree with the evaluator score)" if n else ""


def render_markdown(packet: dict[str, Any]) -> str:
    c, q, r = packet["context"], packet["quality"], packet["redteam"]
    if packet["ready"]:
        verdict = "🟢 **READY FOR HUMAN APPROVAL**" + (" (some results are flagged for review)" if packet["flagged"] else "")
    else:
        verdict = "🔴 **BLOCKED: do not approve.** Prod deployment will not be offered for this run."
    lines = [
        f"# Release approval packet: {c.get('agent_name')} {c.get('tag') or ''}".rstrip(),
        "",
        verdict,
        "",
        "| | |", "|---|---|",
        f"| Agent | `{c.get('agent_name')}` version **{c.get('version')}** in dev |",
        f"| Fingerprint | `{(c.get('fingerprint') or '')[:16]}` (definition + model `{c.get('model_version')}`) |",
        f"| Commit | `{(c.get('sha') or '')[:12]}` |",
        f"| Pipeline run | {c.get('run_url') or 'local'} |",
        "",
        "## Gates",
        "| Gate | Outcome | Summary | Evidence |", "|---|---|---|---|",
        f"| 1b · Evaluation | {ICON[q['outcome']]} {q['outcome']} | {_clip('; '.join(q['reasons']), 220)} | "
        + (" · ".join(f"[{n}]({s['report_url']})" for n, s in q.get("suites", {}).items() if s.get("report_url")) or "–") + " |",
        f"| 2 · AI red teaming | {ICON[r['outcome']]} {r['outcome']}{' (reused scan)' if r.get('reused') else ''} | "
        f"{_clip('; '.join(r['reasons']), 220)} | " + (f"[Foundry]({r['report_url']})" if r.get("report_url") else "–") + " |",
        "",
    ]
    if q.get("criteria"):
        lines += ["## 1b · Evaluation results", "| Criterion | Suite | Passed | Pass rate | Bar | Outcome |",
                  "|---|---|---|---|---|---|"]
        lines += [f"| {row['criterion']} | {row['suite']} | {row['passed']}/{row['scored']} | {row['pass_rate']:.1%} | "
                  f"{row['floor']:.0%} | {ICON.get(row['outcome'], '')} {row['outcome']} |" for row in q["criteria"]]
        for field, values in q.get("slices", {}).items():
            judges = sorted({j for v in values.values() for j in v})
            lines += ["", f"**Document questions by {field}**", "", f"| {field} | n | " + " | ".join(judges) + " |",
                      "|---|---|" + "---|" * len(judges)]
            for value, buckets in sorted(values.items(), key=lambda kv: -max(b[1] for b in kv[1].values())):
                n = max(b[1] for b in buckets.values())
                lines.append(f"| {value} | {n} | " + " | ".join(_rate(*buckets.get(j, [0, 0])) for j in judges) + " |")
        if q.get("content_filtered"):
            lines += ["", "Blocked by Azure safety filters (counted as safe): " + ", ".join(q["content_filtered"])]
        if q.get("could_not_run"):
            lines += ["", "Could not run (blocks the gate): " + ", ".join(sorted(q["could_not_run"]))]
        if q["failures"]:
            lines += ["", f"**Failing cases** ({q['failure_count']}; must-pass and safety first, top {MAX_FAILURES})", "",
                      "| Case | Failed judge(s) | Question | Agent answer | Judge reason |", "|---|---|---|---|---|"]
            for f in q["failures"][:MAX_FAILURES]:
                tag = " 🔒" if f["must_pass"] else ""
                lines.append(f"| {f['id']}{tag} | {', '.join(f['judges'])} | {_clip(f['query'], 110)} | "
                             f"{_clip(f['answer'], 140)} | {_clip(f['reason'], 220)} |".replace("\n", " "))
        lines.append("")
    if r.get("categories"):
        lines += ["## 2 · AI red teaming results",
                  f"Attack strategies: {', '.join(r['strategies'])} · {r['turns']} turns · "
                  f"{len(r['enabled_subcategories'])} prohibited-action sub-categories · {r.get('attacks')} scored attacks",
                  "", "| Risk category | Attacks | Successful | ASR | 95% upper bound | Limit | Outcome |",
                  "|---|---|---|---|---|---|---|"]
        lines += [f"| {row['category']}{' (advisory)' if row.get('advisory') else ''} | {row['attacks']} | {row['successes']} | "
                  f"{row['asr']:.1%} | {row['upper_bound']:.1%} | {row['limit']:.0%} | {ICON.get(row['outcome'], '')} {row['outcome']}"
                  f"{_flag_note(row)} |" for row in r["categories"]]
        if r["by_subcategory"]:
            lines += ["", "Successful attacks by sub-category: "
                      + ", ".join(f"{name} ({k}/{n})" for name, k, n in r["by_subcategory"][:10])]
        lines.append("")
    lines += [
        "## For the approver",
        "Approving deploys **the exact definition above** (same fingerprint) to the prod Foundry project, then smoke-tests it "
        "and rolls back automatically if the smoke test fails.",
        "",
        "- [ ] Every gate is green, or each 🔍 flagged result is understood and acceptable",
        "- [ ] The failing cases above are acceptable for this release (none are must-pass 🔒 or safety)",
        "- [ ] Red-team limits are met for every risk category",
        "- [ ] Known limits: content harms (hate, sexual, violence, self-harm) are covered by Azure content filters, not this scan; "
        "IndirectJailbreak is not supported for this agent",
        "",
        f"**To decide:** open the [pipeline run]({c.get('run_url')}) → *Review deployments* → `prod` → Approve or Reject with a comment."
        if c.get("run_url") else "",
    ]
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------- HTML

def _badge(outcome: str) -> str:
    return (f'<span class="badge" style="background:{COLOR.get(outcome, "#616161")}">'
            f'{html.escape(outcome)}</span>')


def _table(headers: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def render_html(packet: dict[str, Any]) -> str:
    c, q, r = packet["context"], packet["quality"], packet["redteam"]
    e = html.escape
    banner = ("ready", "Ready for human approval" + (" — some results flagged for review" if packet["flagged"] else "")) \
        if packet["ready"] else ("blocked", "Blocked — do not approve")
    parts = [f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Release approval packet · {e(str(c.get('agent_name')))} {e(str(c.get('tag') or ''))}</title>
<style>
body{{font-family:Segoe UI,Arial,sans-serif;margin:32px;color:#1f1f1f;max-width:1200px}}
h1{{font-size:24px;margin-bottom:4px}} h2{{font-size:18px;margin-top:32px;border-bottom:1px solid #ddd;padding-bottom:4px}}
.banner{{padding:14px 18px;border-radius:8px;font-size:18px;font-weight:600;margin:16px 0}}
.ready{{background:#e8f5e9;color:#1b5e20;border:1px solid #a5d6a7}} .blocked{{background:#ffebee;color:#b71c1c;border:1px solid #ef9a9a}}
table{{border-collapse:collapse;width:100%;margin:8px 0 16px;font-size:13px}}
th,td{{border:1px solid #e0e0e0;padding:6px 8px;text-align:left;vertical-align:top}} th{{background:#f5f5f5}}
.badge{{color:#fff;border-radius:10px;padding:2px 8px;font-size:12px;font-weight:600;white-space:nowrap}}
.muted{{color:#666;font-size:12px}} code{{background:#f5f5f5;padding:1px 4px;border-radius:3px}}
details{{margin:8px 0}} li{{margin:4px 0}}
</style></head><body>
<h1>Release approval packet</h1>
<div class="muted">{e(str(c.get('agent_name')))} {e(str(c.get('tag') or ''))} · generated by the agent CI/CD pipeline</div>
<div class="banner {banner[0]}">{e(banner[1])}</div>""",
             _table(["", ""], [
                 ["Agent", f"<code>{e(str(c.get('agent_name')))}</code> version <b>{e(str(c.get('version')))}</b> in dev"],
                 ["Fingerprint", f"<code>{e((c.get('fingerprint') or '')[:16])}</code> (definition + model "
                                 f"<code>{e(str(c.get('model_version')))}</code>)"],
                 ["Commit", f"<code>{e((c.get('sha') or '')[:12])}</code>"],
                 ["Pipeline run", f'<a href="{e(c["run_url"])}">{e(c["run_url"])}</a>' if c.get("run_url") else "local"],
             ]),
             "<h2>Gates</h2>",
             _table(["Gate", "Outcome", "Summary", "Evidence"], [
                 ["1b · Evaluation", _badge(q["outcome"]), "<br>".join(e(x) for x in q["reasons"]),
                  " · ".join(f'<a href="{e(s["report_url"])}">{e(n)}</a>' for n, s in q.get("suites", {}).items()
                             if s.get("report_url")) or "–"],
                 ["2 · AI red teaming", _badge(r["outcome"]) + (" <span class='muted'>reused scan</span>" if r.get("reused") else ""),
                  "<br>".join(e(x) for x in r["reasons"]),
                  f'<a href="{e(r["report_url"])}">Foundry</a>' if r.get("report_url") else "–"],
             ])]
    if q.get("criteria"):
        parts += ["<h2>1b · Evaluation results</h2>",
                  _table(["Criterion", "Suite", "Passed", "Pass rate", "Bar", "Outcome"],
                         [[e(row["criterion"]), e(row["suite"]), f"{row['passed']}/{row['scored']}", f"{row['pass_rate']:.1%}",
                           f"{row['floor']:.0%}", _badge(row["outcome"])] for row in q["criteria"]])]
        for field, values in q.get("slices", {}).items():
            judges = sorted({j for v in values.values() for j in v})
            rows = []
            for value, buckets in sorted(values.items(), key=lambda kv: -max(b[1] for b in kv[1].values())):
                rows.append([e(value), str(max(b[1] for b in buckets.values()))]
                            + [_rate(*buckets.get(j, [0, 0])) for j in judges])
            parts += [f"<h3>Document questions by {e(field)}</h3>", _table([field, "n"] + judges, rows)]
        if q["failures"]:
            parts += [f"<h3>Failing cases ({q['failure_count']}; must-pass and safety first)</h3>",
                      _table(["Case", "Failed judge(s)", "Question", "Agent answer", "Judge reason"],
                             [[e(str(f["id"])) + (" 🔒" if f["must_pass"] else ""), e(", ".join(f["judges"])),
                               e(_clip(f["query"], 300)), e(_clip(f["answer"], 500)), e(_clip(f["reason"], 600))]
                              for f in q["failures"]])]
    if r.get("categories"):
        parts += ["<h2>2 · AI red teaming results</h2>",
                  f"<p class='muted'>Attack strategies: {e(', '.join(r['strategies']))} · {e(str(r['turns']))} turns · "
                  f"{len(r['enabled_subcategories'])} prohibited-action sub-categories · {e(str(r.get('attacks')))} scored attacks</p>",
                  _table(["Risk category", "Attacks", "Successful", "ASR", "95% upper bound", "Limit", "Outcome"],
                         [[e(row["category"]) + (" <span class='muted'>(advisory)</span>" if row.get("advisory") else ""),
                           str(row["attacks"]), str(row["successes"]), f"{row['asr']:.1%}", f"{row['upper_bound']:.1%}",
                           f"{row['limit']:.0%}", _badge(row["outcome"]) + e(_flag_note(row))] for row in r["categories"]])]
        if r["by_subcategory"]:
            parts.append(_table(["Sub-category with successful attacks", "Successful / attempted"],
                                [[e(name), f"{k}/{n}"] for name, k, n in r["by_subcategory"]]))
        if r["enabled_subcategories"]:
            parts.append("<details><summary>Sub-categories tested</summary><ul>"
                         + "".join(f"<li>{e(s)}</li>" for s in r["enabled_subcategories"]) + "</ul></details>")
    parts += ["<h2>For the approver</h2>",
              "<p>Approving deploys <b>the exact definition above</b> (same fingerprint) to the prod Foundry project, then "
              "smoke-tests it and rolls back automatically if the smoke test fails.</p><ul>"
              "<li>Every gate is green, or each flagged result is understood and acceptable</li>"
              "<li>The failing cases are acceptable for this release (none are must-pass 🔒 or safety)</li>"
              "<li>Red-team limits are met for every risk category</li>"
              "<li>Known limits: content harms (hate, sexual, violence, self-harm) are covered by Azure content filters, not "
              "this scan; IndirectJailbreak is not supported for this agent</li></ul>",
              (f'<p><b>To decide:</b> open the <a href="{e(c["run_url"])}">pipeline run</a> → <i>Review deployments</i> → '
               f'<code>prod</code> → Approve or Reject with a comment.</p>') if c.get("run_url") else "",
              "</body></html>"]
    return "\n".join(parts)


# ---------------------------------------------------------------- Teams

def teams_payload(packet: dict[str, Any]) -> dict[str, Any]:
    c, q, r = packet["context"], packet["quality"], packet["redteam"]
    title = (f"{'🟢 Ready for approval' if packet['ready'] else '🔴 Release blocked'}: "
             f"{c.get('agent_name')} {c.get('tag') or ''}").strip()
    facts = [{"title": "Evaluation", "value": f"{q['outcome']} ({q.get('failure_count', 0)} failing cases)"},
             {"title": "Red teaming", "value": r["outcome"] + (" (reused scan)" if r.get("reused") else "")},
             {"title": "Agent version", "value": f"{c.get('version')} · fingerprint {(c.get('fingerprint') or '')[:12]}"}]
    card = {"type": "AdaptiveCard", "$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "version": "1.4",
            "body": [{"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "wrap": True},
                     {"type": "FactSet", "facts": facts}],
            "actions": [{"type": "Action.OpenUrl", "title": "Review and approve" if packet["ready"] else "Open run",
                         "url": c.get("run_url") or "https://github.com"}]}
    return {"type": "message",
            "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}]}


def notify_teams(packet: dict[str, Any], url: str) -> None:
    request = urllib.request.Request(url, data=json.dumps(teams_payload(packet)).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - URL comes from a repository secret
        print(f"Teams notification sent ({response.status})")


# ---------------------------------------------------------------- CLI

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--agent-dir", type=Path, required=True)
    parser.add_argument("--quality", type=Path)
    parser.add_argument("--redteam", type=Path)
    parser.add_argument("--version", default="")
    parser.add_argument("--fingerprint", default="")
    parser.add_argument("--model-version", default="")
    parser.add_argument("--agent-name", default="")
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--html", type=Path)
    args = parser.parse_args()

    eval_dir = args.agent_dir / "eval"
    thresholds = json.loads((eval_dir / "thresholds.json").read_text(encoding="utf-8"))
    policy = json.loads((eval_dir / "redteam.json").read_text(encoding="utf-8"))
    server, repo, run_id = (os.environ.get(k, "") for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
    context = {"agent_name": args.agent_name, "version": args.version, "fingerprint": args.fingerprint,
               "model_version": args.model_version, "sha": os.environ.get("GITHUB_SHA", "local"),
               "tag": os.environ.get("GITHUB_REF_NAME", "") if os.environ.get("GITHUB_REF_TYPE") == "tag" else "",
               "run_url": f"{server}/{repo}/actions/runs/{run_id}" if run_id else ""}
    packet = build_packet(_load(args.quality), _load(args.redteam), thresholds, policy, context)

    markdown = render_markdown(packet)
    for path, text in ((args.markdown, markdown), (args.html, render_html(packet) if args.html else None)):
        if path and text is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    add_summary(markdown)
    webhook = os.environ.get("TEAMS_WEBHOOK_URL")
    if webhook:
        try:
            notify_teams(packet, webhook)
        except Exception as error:  # noqa: BLE001 - a notification failure must not hide the packet
            print(f"WARNING: Teams notification failed: {type(error).__name__}: {error}")
    set_outputs(ready=str(packet["ready"]).lower())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
