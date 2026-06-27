"""LIVE failure-injection — runs the REAL agent with Notion made unavailable.

Unlike test_resilience.py (deterministic, mocks the SDK boundary, runs every time),
this exercises the real model + real Notion connector but injects failure by removing
the Notion read tools from `allowed_tools`, so the CLI refuses every notion-search /
notion-fetch call (as if Notion were unavailable). It proves the MODEL itself degrades
to an unresolved brief instead of hallucinating — something a mock can't show.

Why this layer (not a can_use_tool gate): a probe established that `can_use_tool` is
never invoked in the one-shot `query()` flow, so the real read-only / availability
control is `allowed_tools` + the CLI's non-interactive auto-deny. This test injects at
exactly that layer.

The three distinct failure *points* (search error, account-fetch error, empty
relations) are covered deterministically in tests/test_resilience.py. This live test
covers the end-to-end "Notion unavailable -> real model degrades safely" case.

It needs a live model call, so it is OPT-IN and excluded from the every-time suite:

    RUN_LIVE_NOTION=1 python tests/test_notion_injection_live.py
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

from brief_agent.agent import (
    DISALLOWED_TOOLS,
    _WRITE_TOOL_SET,
    _extract_brief,
    _is_unresolved,
    count_body_words,
)
from brief_agent.prompt import NOTION_TASK_TEMPLATE, SYSTEM_PROMPT_NOTION


async def _run_notion_unavailable(target: str = "Meridian") -> dict:
    """Run the real agent with NO Notion tools allow-listed -> every call is refused."""
    options = ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT_NOTION,
        model="opus",
        allowed_tools=[],              # <- inject: no Notion tool is approved
        disallowed_tools=DISALLOWED_TOOLS,
        permission_mode="dontAsk",     # deny anything not pre-approved, never hang
        setting_sources=["user"],
        max_turns=40,
    )
    tool_calls: list[str] = []
    assistant_texts: list[str] = []
    async for m in query(prompt=NOTION_TASK_TEMPLATE.format(target=target), options=options):
        if isinstance(m, AssistantMessage):
            for b in m.content:
                if isinstance(b, ToolUseBlock):
                    tool_calls.append(b.name)
            parts = [b.text for b in m.content if isinstance(b, TextBlock)]
            if parts:
                assistant_texts.append("".join(parts))
        elif isinstance(m, ResultMessage) and m.is_error:
            raise RuntimeError(f"Agent error: {m.subtype}")
    brief = ""
    for t in reversed(assistant_texts):
        c = _extract_brief(t)
        if c:
            brief = c
            break
    return {
        "brief": brief,
        "tool_calls": tool_calls,
        "wrote": any(n in _WRITE_TOOL_SET for n in tool_calls),
        "unresolved": _is_unresolved(brief) if brief else True,
        "body_words": count_body_words(brief) if brief else 0,
    }


def test_live_notion_unavailable_degrades_safely():
    r = asyncio.run(_run_notion_unavailable())
    print(f"  wrote={r['wrote']} unresolved={r['unresolved']} words={r['body_words']}")
    print(f"  title: {(r['brief'].splitlines()[0] if r['brief'] else '(no brief)')}")
    assert r["wrote"] is False, "made a write call under Notion failure"
    assert r["brief"].lstrip().startswith("# Meeting Brief"), "brief missing or has preamble"
    assert r["unresolved"] is True, "model did NOT degrade — produced a confident brief from no data"


def main() -> int:
    if os.environ.get("RUN_LIVE_NOTION") != "1":
        print("SKIP live Notion injection (set RUN_LIVE_NOTION=1 to run)")
        return 0
    try:
        test_live_notion_unavailable_degrades_safely()
        print("PASS  test_live_notion_unavailable_degrades_safely")
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"FAIL  test_live_notion_unavailable_degrades_safely\n        {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
