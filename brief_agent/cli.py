"""Command-line entry point.

Two modes:
  Single brief (Phase 2):   python main.py "Meridian" --out brief.md
  Daily packet  (Phase 4):  python main.py --calendar [--date tomorrow|today|YYYY-MM-DD] --out day.md

Both are read-only: the single brief reads the Notion CRM; the daily packet also reads the
calendar. Every run asserts ZERO writes to Notion and Calendar before exiting 0.
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from .agent import DEFAULT_MODEL, draft_brief
from .calendar import fetch_day_events, parse_events, resolve_date
from .daily import render_packet, run_daily_briefing


def _sidecar_path(out_path: Path) -> Path:
    """`brief.md` -> `brief.sources.json` (provenance sidecar next to the output)."""
    return out_path.with_suffix(".sources.json")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="brief-agent",
        description="Draft a one-page meeting brief, or a calendar-driven daily packet, "
        "by pulling context from the Notion CRM.",
    )
    parser.add_argument(
        "target",
        nargs="?",
        help='Account name or meeting subject to brief on, e.g. "Meridian". '
        "Omit when using --calendar.",
    )
    parser.add_argument(
        "--calendar",
        action="store_true",
        help="Daily-packet mode: read the calendar for a day and brief every external meeting.",
    )
    parser.add_argument(
        "--date",
        default="tomorrow",
        help="With --calendar: today | tomorrow (default) | YYYY-MM-DD.",
    )
    parser.add_argument(
        "--out",
        "-o",
        default=None,
        help="Output path (default: brief.md for a single brief, day.md for --calendar).",
    )
    parser.add_argument(
        "--model",
        "-m",
        default=os.environ.get("BRIEF_AGENT_MODEL", DEFAULT_MODEL),
        help=(
            'Model alias ("opus", "sonnet", "haiku") or full ID. '
            "Overrides $BRIEF_AGENT_MODEL (default: opus)."
        ),
    )
    return parser


# --------------------------------------------------------------------------- #
# Single-brief mode (Phase 2) — unchanged behaviour
# --------------------------------------------------------------------------- #
def _run_single(args) -> int:
    print(
        f"Gathering context from Notion for '{args.target}' using model '{args.model}'…",
        file=sys.stderr,
    )
    try:
        result = asyncio.run(draft_brief(args.target, model=args.model))
    except Exception as exc:  # surface SDK / model / MCP errors cleanly
        print(f"error: {exc}", file=sys.stderr)
        return 1

    out_path = Path(args.out or "brief.md")
    out_path.write_text(result.text + "\n", encoding="utf-8")

    # Provenance sidecar: the real Notion page URLs the agent fetched, categorised by
    # source database. The brief's CEO-facing Sources line stays readable page names;
    # this JSON is the machine-checkable audit trail.
    sidecar = _sidecar_path(out_path)
    provenance = {"target": args.target, "model": args.model, **result.sources}
    sidecar.write_text(
        json.dumps(provenance, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    unique_tools = sorted(set(result.tool_calls))
    print(f"Wrote {out_path} ({result.body_words} body words).", file=sys.stderr)
    n_meet = len(result.sources.get("meetings", []))
    n_con = len(result.sources.get("contacts", []))
    acct = (result.sources.get("account") or {}).get("title", "—")
    print(
        f"Wrote {sidecar} (account: {acct}; {n_meet} meetings, {n_con} contacts).",
        file=sys.stderr,
    )
    print(
        f"Tool calls: {len(result.tool_calls)} "
        f"({', '.join(t.split('__')[-1] for t in unique_tools) or 'none'}).",
        file=sys.stderr,
    )
    print(
        f"Length retry: {'yes' if result.retried else 'no'}"
        f"{' (unresolved — not padded)' if result.unresolved else ''}.",
        file=sys.stderr,
    )
    if result.made_any_write:
        print("ERROR: a WRITE tool was called — this should never happen.", file=sys.stderr)
        return 1
    print("Writes: 0 (read-only).", file=sys.stderr)
    return 0


# --------------------------------------------------------------------------- #
# Daily-packet mode (Phase 4)
# --------------------------------------------------------------------------- #
async def _build_packet(args, day):
    raw, fetch_calls = await fetch_day_events(day, args.model)
    events = parse_events(raw)
    packet = await run_daily_briefing(
        events, model=args.model, fetch_tool_calls=fetch_calls
    )
    return packet


def _run_calendar(args) -> int:
    try:
        day = resolve_date(args.date, today=datetime.now().date())
    except ValueError:
        print(f"error: bad --date {args.date!r} (use today | tomorrow | YYYY-MM-DD).", file=sys.stderr)
        return 1

    print(
        f"Reading the calendar for {day.isoformat()} and briefing each external meeting "
        f"using model '{args.model}'…",
        file=sys.stderr,
    )
    try:
        packet = asyncio.run(_build_packet(args, day))
    except Exception as exc:  # surface SDK / model / MCP / parse errors cleanly
        print(f"error: {exc}", file=sys.stderr)
        return 1

    out_path = Path(args.out or "day.md")
    out_path.write_text(render_packet(packet), encoding="utf-8")

    # Combined provenance sidecar: per brief, the calendar event id + the Notion pages used.
    sidecar = _sidecar_path(out_path)
    provenance = {
        "date": day.isoformat(),
        "model": args.model,
        "counts": {
            "total": packet.total,
            "briefed": packet.briefed,
            "unresolved": packet.stubs,
            "skipped": len(packet.skipped),
        },
        "items": [i.sources for i in packet.items],
        "skipped": [
            {"event_id": s.event_id, "status": "skipped", "title": s.title} for s in packet.skipped
        ],
    }
    sidecar.write_text(
        json.dumps(provenance, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    print(
        f"Wrote {out_path}: {packet.total} meetings · {packet.briefed} briefed / "
        f"{packet.stubs} unresolved / {len(packet.skipped)} skipped.",
        file=sys.stderr,
    )
    print(f"Wrote {sidecar} (per-brief provenance).", file=sys.stderr)
    if packet.made_any_write:
        print(
            f"ERROR: WRITE tool(s) called — {sorted(set(packet.write_tool_calls))}.",
            file=sys.stderr,
        )
        return 1
    print("Writes: 0 to Calendar and Notion (read-only).", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.calendar:
        return _run_calendar(args)
    if not args.target:
        print("error: provide an account/subject, or use --calendar.", file=sys.stderr)
        return 2
    return _run_single(args)


if __name__ == "__main__":
    raise SystemExit(main())
