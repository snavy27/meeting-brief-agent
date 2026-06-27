"""Phase 4 calendar eval runner — deterministic (calendar payloads are MOCKED).

Two kinds of case:
  * Quality (real Notion): each external event is briefed by the real meeting-aware engine,
    then graded for format/length/person-precision + an LLM judge. The calendar is NOT hit —
    we feed mocked events through `parse_events` and call the per-meeting engine directly.
  * Structural (no model): `run_daily_briefing` is exercised with a FAKE per-meeting engine to
    assert ordering, counts, and internal-skip on a full day and a 3-event day.

Writes a scorecard to eval/results/, asserts ZERO writes (Calendar + Notion) across the real
runs, and exits nonzero if any case fails — so it can act as a CI gate.

Usage:  python eval/run_eval_calendar.py --model sonnet [--judge-model opus]
"""

import argparse
import asyncio
import contextvars
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import claude_agent_sdk
from claude_agent_sdk import AssistantMessage, ToolUseBlock, UserMessage

import brief_agent.agent as agent
import brief_agent.daily as daily
from brief_agent.agent import BriefResult
from brief_agent.calendar import parse_events
from brief_agent.daily import _hint, _prepend_meeting_time, _stub_brief, run_daily_briefing
from eval.cases_calendar import (
    ACCOUNT_CASES,
    DAY_EVENTS,
    DAY_EXPECTED,
    STUB_CASE,
    THREE_EVENT_DAY,
    THREE_EVENT_EXPECTED,
)
from eval.graders import (
    judge_case,
    programmatic_grade_calendar,
    programmatic_grade_calendar_stub,
)

RESULTS_DIR = Path(__file__).resolve().parent / "results"

# Per-task capture buffer (same pattern as run_eval.py): concurrent cases each capture into
# their own buffer despite the one global query() patch.
_CAP: contextvars.ContextVar[dict | None] = contextvars.ContextVar("cal_cap", default=None)


def _result_text(m: UserMessage) -> str:
    out: list[str] = []
    c = m.content
    if isinstance(c, str):
        out.append(c)
    elif isinstance(c, list):
        for b in c:
            inner = getattr(b, "content", None)
            if isinstance(inner, str):
                out.append(inner)
            elif isinstance(inner, list):
                for x in inner:
                    if isinstance(x, dict):
                        out.append(x.get("text") or json.dumps(x))
            elif getattr(b, "text", None):
                out.append(b.text)
    return "\n".join(o for o in out if o)


def _tee(*args, **kwargs):
    agen = claude_agent_sdk.query(*args, **kwargs)
    cap = _CAP.get()

    async def wrapper():
        async for m in agen:
            if cap is not None:
                if isinstance(m, AssistantMessage):
                    for b in m.content:
                        if isinstance(b, ToolUseBlock):
                            cap["tool_calls"].append(b.name)
                elif isinstance(m, UserMessage):
                    txt = _result_text(m)
                    if txt:
                        cap["context"].append(txt)
            yield m

    return wrapper()


# --------------------------------------------------------------------------- #
# Quality cases (real Notion via the meeting-aware engine)
# --------------------------------------------------------------------------- #
async def run_account_case(case, events_map, model, judge_model, sem) -> dict:
    row = {"id": case["id"], "type": "account", "attendee": case["attendee"]}
    ev = events_map[case["event_id"]]
    cap = {"tool_calls": [], "context": []}
    _CAP.set(cap)
    async with sem:
        print(f"  briefing {case['id']} ({case['attendee']}) ...", file=sys.stderr)
        try:
            result = await agent.draft_brief(ev.company_token, model=model, meeting=_hint(ev))
        except Exception as e:  # noqa: BLE001
            row.update({"error": f"{type(e).__name__}: {e}", "overall_pass": False})
            return row

        if result.unresolved:  # account case must resolve — surface, don't mask
            brief = _stub_brief(ev)
            status = "stub"
        else:
            brief = _prepend_meeting_time(result.text, ev.when_str())
            status = "briefed"

        prog = programmatic_grade_calendar(
            case, brief, result.tool_calls, result.body_words, status
        )
        context = "\n\n".join(cap["context"])
        judge_input = {
            "kind": "account", "input": case["company"],
            "note": f"calendar meeting with {case['attendee']}",
            "attendee": case["attendee"], "wrong_person": case.get("wrong_person"),
        }
        judge = await judge_case(judge_input, brief, context, judge_model)

        row.update({
            "brief": brief, "body_words": result.body_words, "retried": result.retried,
            "status": status, "tool_calls": sorted(set(result.tool_calls)),
            "wrote": result.made_any_write, "sources": result.sources,
            "programmatic": prog, "judge": judge,
            "overall_pass": prog["passed"] and judge["passed"],
        })
        return row


async def run_stub_case(case, events_map, model, sem) -> dict:
    row = {"id": case["id"], "type": "stub", "attendee": case["attendee"]}
    ev = events_map[case["event_id"]]
    cap = {"tool_calls": [], "context": []}
    _CAP.set(cap)
    async with sem:
        print(f"  briefing {case['id']} ({case['attendee']}, expect stub) ...", file=sys.stderr)
        try:
            result = await agent.draft_brief(ev.company_token, model=model, meeting=_hint(ev))
        except Exception as e:  # noqa: BLE001
            row.update({"error": f"{type(e).__name__}: {e}", "overall_pass": False})
            return row

        # No CRM match → the orchestrator renders a deterministic calendar-only stub.
        brief = _stub_brief(ev) if result.unresolved else _prepend_meeting_time(result.text, ev.when_str())
        prog = programmatic_grade_calendar_stub(case, brief, result.tool_calls)
        # The whole point: the engine must NOT have resolved a real account here.
        resolved_ok = result.unresolved
        passed = prog["passed"] and resolved_ok

        row.update({
            "brief": brief, "agent_unresolved": result.unresolved,
            "tool_calls": sorted(set(result.tool_calls)), "wrote": result.made_any_write,
            "sources": result.sources, "programmatic": prog,
            "checks_extra": [("agent_declined_no_match", resolved_ok, f"unresolved={result.unresolved}")],
            "overall_pass": passed,
        })
        return row


# --------------------------------------------------------------------------- #
# Structural cases (no model): exercise run_daily_briefing's order/counts/skip
# --------------------------------------------------------------------------- #
async def _fake_draft(target, model=agent.DEFAULT_MODEL, *, meeting=None):
    """Stand-in engine: 'resolves' everything except a Quantum company (→ unresolved)."""
    company = (meeting.company if meeting else target).lower()
    unresolved = "quantum" in company
    text = (
        "# Meeting Brief — fake\n\n**When:** x · **Who:** y · **Purpose:** z · **Sources:** s\n\n"
        "---\n\n## Bottom line\nfake body"
    )
    return BriefResult(
        text=text, unresolved=unresolved,
        event_id=meeting.event_id if meeting else None,
    )


async def run_structural_case(label, events, expected) -> dict:
    checks: list[tuple[str, bool, str]] = []
    with patch.object(daily, "draft_brief", _fake_draft):
        packet = await run_daily_briefing(parse_events(events))

    checks.append(("total", packet.total == expected["total"], f"{packet.total} vs {expected['total']}"))
    checks.append(("briefed", packet.briefed == expected["briefed"], f"{packet.briefed} vs {expected['briefed']}"))
    checks.append(("stub", packet.stubs == expected["stub"], f"{packet.stubs} vs {expected['stub']}"))
    checks.append(("skipped", len(packet.skipped) == expected["skipped"], f"{len(packet.skipped)} vs {expected['skipped']}"))
    order = [i.event_id for i in packet.items]
    checks.append(("start_time_order", order == expected["item_order"], f"{order}"))

    passed = all(ok for _, ok, _ in checks)
    return {"id": label, "type": "structural", "checks": checks, "overall_pass": passed}


# --------------------------------------------------------------------------- #
# Suite
# --------------------------------------------------------------------------- #
async def run_suite(model: str, judge_model: str) -> dict:
    events = parse_events(DAY_EVENTS)
    events_map = {e.id: e for e in events}
    sem = asyncio.Semaphore(3)

    with patch.object(agent, "query", _tee):
        quality = await asyncio.gather(
            *[run_account_case(c, events_map, model, judge_model, sem) for c in ACCOUNT_CASES],
            run_stub_case(STUB_CASE, events_map, model, sem),
        )

    structural = [
        await run_structural_case("day_order_counts", DAY_EVENTS, DAY_EXPECTED),
        await run_structural_case("three_event_day", THREE_EVENT_DAY, THREE_EVENT_EXPECTED),
    ]

    rows = list(quality) + structural
    passed = sum(1 for r in rows if r.get("overall_pass"))
    writes = sorted({t for r in rows for t in r.get("tool_calls", []) if "create" in t or "update" in t or "delete" in t or "respond" in t})
    any_wrote = any(r.get("wrote") for r in rows)
    return {
        "model": model, "judge_model": judge_model,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "summary": {"total": len(rows), "passed": passed, "failed": len(rows) - passed},
        "zero_writes": not any_wrote and not writes,
        "write_tool_calls": writes,
        "cases": rows,
    }


def _failed_checks(row: dict) -> str:
    bad = [n for n, ok, _ in row.get("programmatic", {}).get("checks", []) if not ok]
    bad += [n for n, ok, _ in row.get("checks_extra", []) if not ok]
    bad += [n for n, ok, _ in row.get("checks", []) if not ok]  # structural
    return ",".join(bad) if bad else "ok"


def scorecard_md(report: dict) -> str:
    s = report["summary"]
    lines = [
        f"# Calendar eval scorecard — model `{report['model']}` (judge `{report['judge_model']}`)",
        f"_{report['timestamp']}_  ·  **{s['passed']}/{s['total']} passed**  ·  "
        f"writes: {'0 ✅' if report['zero_writes'] else '⚠ ' + str(report['write_tool_calls'])}",
        "",
        "| case | type | checks | ground | correct | tone | overall |",
        "|------|------|--------|--------|---------|------|---------|",
    ]
    for r in report["cases"]:
        if "error" in r:
            lines.append(f"| {r['id']} | {r.get('type','')} | ERROR | – | – | – | ❌ |")
            continue
        j = r.get("judge")
        g = j["grounding"] if j else "–"
        c = j["correctness"] if j else "–"
        t = j["tone"] if j else "–"
        prog = "ok" if r.get("overall_pass") else _failed_checks(r)
        lines.append(
            f"| {r['id']} | {r.get('type','')} | {prog} | {g} | {c} | {t} | "
            f"{'✅' if r.get('overall_pass') else '❌'} |"
        )
    fails = [r for r in report["cases"] if not r.get("overall_pass")]
    if fails:
        lines += ["", "## Failures"]
        for r in fails:
            if "error" in r:
                lines.append(f"- **{r['id']}**: {r['error']}")
                continue
            bits = [f"checks: {_failed_checks(r)}"]
            j = r.get("judge")
            if j:
                for dim in ("grounding", "correctness", "tone"):
                    if j[dim] < 4:
                        bits.append(f"{dim}={j[dim]} ({j[f'{dim}_reason'][:120]})")
            lines.append(f"- **{r['id']}**: " + "; ".join(bits))
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="run_eval_calendar")
    ap.add_argument("--model", default="opus")
    ap.add_argument("--judge-model", default="opus")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    print(f"Running CALENDAR eval on model '{args.model}' (judge '{args.judge_model}')…", file=sys.stderr)
    report = asyncio.run(run_suite(args.model, args.judge_model))

    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = report["timestamp"].replace(":", "").replace("-", "")
    base = RESULTS_DIR / f"calendar-{args.model}-{stamp}"
    base.with_suffix(".json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    md = scorecard_md(report)
    base.with_suffix(".md").write_text(md, encoding="utf-8")

    print("\n" + md)
    print(f"\nSaved: {base.with_suffix('.json').name}, {base.with_suffix('.md').name}", file=sys.stderr)
    # Fail the suite if any case failed OR any write slipped through.
    return 0 if report["summary"]["failed"] == 0 and report["zero_writes"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
