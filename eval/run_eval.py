"""Evaluation runner — execute the brief agent on each case, grade, write a scorecard.

Runs the REAL `brief_agent.agent.draft_brief` (so the production path incl. the length-retry
is exercised) while tee-capturing the SDK message stream to record tool calls and the fetched
Notion page text (the judge's grounding context). Grades each case with a deterministic grader
+ an LLM judge, prints a scorecard, saves results to eval/results/, and exits nonzero if any
case fails — so it can act as a CI gate.

Usage:
    python eval/run_eval.py --model opus   [--judge-model opus]
    python eval/run_eval.py --model sonnet
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
from eval.cases import CASES
from eval.graders import judge_case, programmatic_grade

RESULTS_DIR = Path(__file__).resolve().parent / "results"


def _result_text(m: UserMessage) -> str:
    """Extract tool-result text from a UserMessage (handles ToolResultBlock / TextBlock)."""
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


# Per-task capture buffer. The patched query() reads this contextvar, so concurrent
# cases (each its own asyncio task) capture into their own buffer despite the global patch.
_CAP: contextvars.ContextVar[dict | None] = contextvars.ContextVar("eval_cap", default=None)


def _tee(*args, **kwargs):
    """Patched claude_agent_sdk.query: pass-through, recording into the task's _CAP."""
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


async def run_case(case: dict, model: str, judge_model: str, sem: asyncio.Semaphore) -> dict:
    row = {
        "id": case["id"], "input": case["input"], "kind": case["kind"], "note": case["note"],
    }
    cap = {"tool_calls": [], "context": []}
    _CAP.set(cap)  # task-local; the patched query() captures here
    async with sem:
        print(f"  running {case['id']} ...", file=sys.stderr)
        try:
            result = await agent.draft_brief(case["input"], model=model)
        except Exception as e:  # noqa: BLE001 - a crashed run is a failed case, not a dead suite
            row.update({"error": f"{type(e).__name__}: {e}", "overall_pass": False})
            return row

        brief = result.text
        prog = programmatic_grade(case, brief, result.tool_calls, result.unresolved, result.body_words)
        context = "\n\n".join(cap["context"])
        judge = await judge_case(case, brief, context, judge_model)

        row.update({
            "brief": brief,
            "body_words": result.body_words,
            "retried": result.retried,
            "unresolved": result.unresolved,
            "tool_calls": sorted(set(result.tool_calls)),
            "wrote_to_notion": result.wrote_to_notion,
            "sources": result.sources,
            "programmatic": prog,
            "judge": judge,
            "overall_pass": prog["passed"] and judge["passed"],
        })
        return row


async def run_suite(model: str, judge_model: str) -> dict:
    # One global patch for the whole suite; per-task _CAP keeps concurrent cases isolated.
    sem = asyncio.Semaphore(3)
    with patch.object(agent, "query", _tee):
        rows = await asyncio.gather(
            *(run_case(case, model, judge_model, sem) for case in CASES)
        )
    rows = list(rows)
    passed = sum(1 for r in rows if r.get("overall_pass"))
    return {
        "model": model,
        "judge_model": judge_model,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "summary": {"total": len(rows), "passed": passed, "failed": len(rows) - passed},
        "cases": rows,
    }


def _failed_checks(row: dict) -> str:
    if "error" in row:
        return f"ERROR {row['error']}"
    bad = [name for name, ok, _ in row["programmatic"]["checks"] if not ok]
    return ",".join(bad) if bad else "ok"


def scorecard_md(report: dict) -> str:
    s = report["summary"]
    lines = [
        f"# Eval scorecard — model `{report['model']}` (judge `{report['judge_model']}`)",
        f"_{report['timestamp']}_  ·  **{s['passed']}/{s['total']} passed**",
        "",
        "| case | kind | prog | ground | correct | tone | retry | safety | overall |",
        "|------|------|------|--------|---------|------|-------|--------|---------|",
    ]
    for r in report["cases"]:
        if "error" in r:
            lines.append(f"| {r['id']} | {r['kind']} | ERROR | – | – | – | – | – | ❌ |")
            continue
        j = r["judge"]
        safety = "ok" if r["programmatic"]["safety_ok"] else "WRITE!"
        prog = "ok" if r["programmatic"]["passed"] else _failed_checks(r)
        lines.append(
            f"| {r['id']} | {r['kind']} | {prog} | {j['grounding']} | {j['correctness']} | "
            f"{j['tone']} | {'yes' if r['retried'] else 'no'} | {safety} | "
            f"{'✅' if r['overall_pass'] else '❌'} |"
        )
    # failure detail
    fails = [r for r in report["cases"] if not r.get("overall_pass")]
    if fails:
        lines += ["", "## Failures"]
        for r in fails:
            if "error" in r:
                lines.append(f"- **{r['id']}**: {r['error']}")
                continue
            bits = []
            if not r["programmatic"]["passed"]:
                bits.append("programmatic: " + _failed_checks(r))
            j = r["judge"]
            for dim in ("grounding", "correctness", "tone"):
                if j[dim] < 4:
                    bits.append(f"{dim}={j[dim]} ({j[f'{dim}_reason'][:120]})")
            if j.get("fabrications"):
                bits.append(f"fabrications={j['fabrications']}")
            lines.append(f"- **{r['id']}**: " + "; ".join(bits))
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="run_eval")
    ap.add_argument("--model", default="opus", help="agent model under test (opus/sonnet/...)")
    ap.add_argument("--judge-model", default="opus", help="fixed model used to grade (default opus)")
    args = ap.parse_args(argv)

    # The scorecard contains ✅/❌; make stdout UTF-8 so printing it can't crash on Windows.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    print(f"Running eval suite on model '{args.model}' (judge '{args.judge_model}')…", file=sys.stderr)
    report = asyncio.run(run_suite(args.model, args.judge_model))

    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = report["timestamp"].replace(":", "").replace("-", "")
    base = RESULTS_DIR / f"{args.model}-{stamp}"
    base.with_suffix(".json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    md = scorecard_md(report)
    base.with_suffix(".md").write_text(md, encoding="utf-8")

    print("\n" + md)
    s = report["summary"]
    print(f"\nSaved: {base.with_suffix('.json').name}, {base.with_suffix('.md').name}", file=sys.stderr)
    return 0 if s["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
