"""Verification harness — runs the Phase 2 agent with FULL tool-call capture.

Mirrors brief_agent.agent._agentic_draft's ClaudeAgentOptions exactly (imports the
same constants) but records every ToolUseBlock (name + input) so we can prove the
read-only property and inspect grounding. Emits JSON.

Usage: python verify/capture.py "<target>" [model] [out.json]
"""

import asyncio
import json
import sys
from pathlib import Path

# Make the project root importable when run as `python verify/capture.py`.
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
    ALLOWED_TOOLS,
    DISALLOWED_TOOLS,
    _WRITE_TOOL_SET,
    _extract_brief,
    count_body_words,
)
from brief_agent.prompt import NOTION_TASK_TEMPLATE, SYSTEM_PROMPT_NOTION


async def capture(target: str, model: str) -> dict:
    options = ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT_NOTION,
        model=model,
        allowed_tools=ALLOWED_TOOLS,
        disallowed_tools=DISALLOWED_TOOLS,
        permission_mode="dontAsk",
        setting_sources=["user"],
        max_turns=40,
    )
    transcript: list[dict] = []
    assistant_texts: list[str] = []
    result_meta: dict = {}
    async for m in query(prompt=NOTION_TASK_TEMPLATE.format(target=target), options=options):
        if isinstance(m, AssistantMessage):
            parts = [b.text for b in m.content if isinstance(b, TextBlock)]
            for b in m.content:
                if isinstance(b, ToolUseBlock):
                    transcript.append({"tool": b.name, "input": b.input})
            if parts:
                assistant_texts.append("".join(parts))
        elif isinstance(m, ResultMessage):
            result_meta = {
                "is_error": m.is_error,
                "subtype": m.subtype,
                "num_turns": m.num_turns,
            }

    brief = ""
    for t in reversed(assistant_texts):
        c = _extract_brief(t)
        if c:
            brief = c
            break

    tool_names = [e["tool"] for e in transcript]
    return {
        "target": target,
        "model": model,
        "tool_calls": tool_names,
        "transcript": transcript,
        "brief": brief,
        "starts_clean": brief.lstrip().startswith("# Meeting Brief") if brief else False,
        "body_words": count_body_words(brief) if brief else 0,
        "wrote_to_notion": any(n in _WRITE_TOOL_SET for n in tool_names),
        "result": result_meta,
    }


async def main() -> None:
    target = sys.argv[1]
    model = sys.argv[2] if len(sys.argv) > 2 else "opus"
    out = sys.argv[3] if len(sys.argv) > 3 else None
    data = await capture(target, model)
    js = json.dumps(data, indent=2, ensure_ascii=False)
    if out:
        Path(out).write_text(js, encoding="utf-8")
    print(js)


if __name__ == "__main__":
    asyncio.run(main())
