"""Command-line entry point: a name/subject in, a brief file out.

    python main.py "Meridian" --out brief.md
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from .agent import DEFAULT_MODEL, draft_brief


def _sidecar_path(out_path: Path) -> Path:
    """`brief.md` -> `brief.sources.json` (provenance sidecar next to the brief)."""
    return out_path.with_suffix(".sources.json")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="brief-agent",
        description="Draft a one-page meeting brief by pulling context from the Notion CRM.",
    )
    parser.add_argument(
        "target",
        help='Account name or meeting subject to brief on, e.g. "Meridian".',
    )
    parser.add_argument(
        "--out",
        "-o",
        default="brief.md",
        help="Path to write the brief (default: brief.md).",
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


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    print(
        f"Gathering context from Notion for '{args.target}' using model '{args.model}'…",
        file=sys.stderr,
    )
    try:
        result = asyncio.run(draft_brief(args.target, model=args.model))
    except Exception as exc:  # surface SDK / model / MCP errors cleanly
        print(f"error: {exc}", file=sys.stderr)
        return 1

    out_path = Path(args.out)
    out_path.write_text(result.text + "\n", encoding="utf-8")

    # Provenance sidecar: the real Notion page URLs the agent fetched, categorised by
    # source database. The brief's CEO-facing Sources line stays readable page names;
    # this JSON is the machine-checkable audit trail.
    sidecar = _sidecar_path(out_path)
    provenance = {"target": args.target, "model": args.model, **result.sources}
    sidecar.write_text(json.dumps(provenance, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # Audit trail to stderr (stdout stays clean for the brief, if piped).
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
    if result.wrote_to_notion:
        print("ERROR: a Notion WRITE tool was called — this should never happen.", file=sys.stderr)
        return 1
    print("Notion writes: 0 (read-only).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
