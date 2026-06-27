"""Core loop (Phase 2): a name/subject in, a one-page meeting brief out.

Given only an account name or meeting subject, the model gathers its own context
from the Notion CRM (search -> fetch -> follow relations) and drafts the brief
following the Phase 1 format spec and honesty rule.

Read-only is enforced entirely by the tool lists + permission mode:
- `allowed_tools` approves ONLY notion-search/fetch (plus the ToolSearch built-in used
  to load deferred MCP schemas);
- `permission_mode="dontAsk"` denies anything not pre-approved — without prompting — so
  other connectors (Gmail/Calendar/GitHub/…) and writes cannot run even though
  `setting_sources=["user"]` loads the whole environment;
- `disallowed_tools` additionally hard-denies every Notion write tool plus the
  interactive built-in AskUserQuestion.
This is validated by tests/test_notion_injection_live.py: with the read tools removed
from `allowed_tools`, the CLI refuses every Notion call and the model degrades to an
unresolved brief instead of inventing one. (A `can_use_tool` callback was tried earlier
but is never invoked in the one-shot `query()` flow, so it was removed in favour of this
simpler, proven enforcement.)

After drafting, a length validate-and-retry loop tightens the body to 250-350 words
(up to two retries); the rewrite aims for an INTERNAL target of ~320 words — not the
acceptance window edge — so the model's natural variance band stays under the 350 cap.
Legitimately short "unresolved" briefs (no match / ambiguous) are left alone rather than
padded. The acceptance check is exactly 250-350; the internal target only steers the rewrite.

Every Notion page the agent fetches is also captured (title + real page URL, categorised
by source database) into `BriefResult.sources` — a provenance/audit trail the CLI writes to
a `brief.sources.json` sidecar. The CEO-facing Sources line in the brief itself is untouched.
"""

import json
import os
import re
import sys
from dataclasses import dataclass, field

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)

from .prompt import (
    NOTION_MEETING_TASK_TEMPLATE,
    NOTION_TASK_TEMPLATE,
    OUTPUT_CONTRACT,
    SYSTEM_PROMPT_NOTION,
    SYSTEM_PROMPT_NOTION_MEETING,
)

DEFAULT_MODEL = "opus"

# Body word budget (excludes title + metadata line). The acceptance window is exactly
# [MIN, MAX]; the retry rewrite aims for _RETRY_TARGET (a midpoint with headroom under the
# cap) so post-rewrite variance doesn't drift back over 350.
MIN_BODY_WORDS = 250
MAX_BODY_WORDS = 350
_RETRY_TARGET = 320  # internal rewrite target only — NOT the acceptance window
_MAX_RETRIES = 2     # at most this many tightening passes

# Source databases in the CRM, used to categorise fetched pages for the provenance sidecar.
ACCOUNTS_COLLECTION = "9eef9efb-0005-4b25-a907-42cfd913b668"
MEETINGS_COLLECTION = "d0cc704c-3689-4964-bbb0-1635ec233ebf"
CONTACTS_COLLECTION = "63ce8309-6e71-4dd7-9b4a-2c26efb4865c"
_COLLECTION_CATEGORY = {
    ACCOUNTS_COLLECTION: "account",
    MEETINGS_COLLECTION: "meetings",
    CONTACTS_COLLECTION: "contacts",
}

# The Notion MCP connector ("claude.ai Notion") and its tools.
_NOTION = "mcp__claude_ai_Notion__"

# Read-only allow-list — the only Notion tools the agent may call.
NOTION_READ_TOOLS = [
    f"{_NOTION}notion-search",
    f"{_NOTION}notion-fetch",
]

# Write tools, explicitly denied (defense-in-depth alongside the allow-list).
NOTION_WRITE_TOOLS = [
    f"{_NOTION}notion-create-pages",
    f"{_NOTION}notion-update-page",
    f"{_NOTION}notion-move-pages",
    f"{_NOTION}notion-duplicate-page",
    f"{_NOTION}notion-create-database",
    f"{_NOTION}notion-update-data-source",
    f"{_NOTION}notion-create-view",
    f"{_NOTION}notion-update-view",
    f"{_NOTION}notion-create-comment",
]
_WRITE_TOOL_SET = set(NOTION_WRITE_TOOLS)

# Tools the agent may call: the read tools plus ToolSearch (the built-in that loads the
# deferred Notion MCP tool schemas). With permission_mode="dontAsk", everything else is
# denied without prompting.
ALLOWED_TOOLS = NOTION_READ_TOOLS + ["ToolSearch"]

# Tools hard-denied at the option level. Notion writes (above) plus AskUserQuestion:
# this is a non-interactive batch agent, so it must resolve ambiguity by emitting an
# `Unknown` brief, never by trying to prompt the user.
DISALLOWED_TOOLS = NOTION_WRITE_TOOLS + ["AskUserQuestion"]

# The Google Calendar MCP connector ("claude.ai Google Calendar") and its tools. The
# per-meeting brief engine itself never touches the calendar (only the Phase 4 calendar
# adapter reads events), but these are defined here so every run can ASSERT zero calendar
# writes alongside the zero Notion writes — the safety gate extends, it never weakens.
_CALENDAR = "mcp__claude_ai_Google_Calendar__"

# Read-only calendar tools — all the adapter ever needs.
CALENDAR_READ_TOOLS = [
    f"{_CALENDAR}list_events",
    f"{_CALENDAR}get_event",
    f"{_CALENDAR}list_calendars",
]

# Every calendar tool that mutates state — hard-denied everywhere, asserted never called.
CALENDAR_WRITE_TOOLS = [
    f"{_CALENDAR}create_event",
    f"{_CALENDAR}update_event",
    f"{_CALENDAR}delete_event",
    f"{_CALENDAR}respond_to_event",
]
_CAL_WRITE_SET = set(CALENDAR_WRITE_TOOLS)

# Every write tool, across both connectors — the full set the zero-writes assertion guards.
ALL_WRITE_TOOLS = NOTION_WRITE_TOOLS + CALENDAR_WRITE_TOOLS
_ALL_WRITE_SET = _WRITE_TOOL_SET | _CAL_WRITE_SET


@dataclass
class MeetingHint:
    """A calendar meeting to brief on: who/when/what, resolved by the CRM at draft time.

    Carries everything the meeting-aware gather needs. `event_id` ties the resulting brief
    back to its calendar event in the provenance sidecar.
    """

    person: str          # attendee display name (the person being met)
    company: str         # company token (from the event title / email domain)
    when: str            # human-readable meeting time from the calendar
    title: str           # calendar event title
    email: str = ""      # attendee calendar email (domain differs from the CRM's)
    description: str = ""
    event_id: str | None = None


@dataclass
class BriefResult:
    """The finished brief plus an audit trail of how it was produced."""

    text: str
    tool_calls: list[str] = field(default_factory=list)
    retried: bool = False
    unresolved: bool = False
    body_words: int = 0
    # Provenance: the actual Notion pages fetched, categorised by source database.
    # {"account": {title,url}|None, "meetings": [{title,url}], "contacts": [{title,url}]}
    sources: dict = field(default_factory=dict)
    # Phase 4: which calendar event this brief came from, and how it was rendered.
    event_id: str | None = None
    status: str = "briefed"  # "briefed" | "stub" (no CRM match) | "skipped" (internal)

    @property
    def wrote_to_notion(self) -> bool:
        """True if the agent ever called a Notion write tool (must be False)."""
        return any(name in _WRITE_TOOL_SET for name in self.tool_calls)

    @property
    def wrote_to_calendar(self) -> bool:
        """True if a Calendar write tool was ever called (must be False)."""
        return any(name in _CAL_WRITE_SET for name in self.tool_calls)

    @property
    def made_any_write(self) -> bool:
        """True if ANY write tool (Notion or Calendar) was called (must be False)."""
        return any(name in _ALL_WRITE_SET for name in self.tool_calls)


def count_body_words(brief: str) -> int:
    """Count words in the body — everything after the first `---` separator."""
    body = brief.split("---", 1)[1] if "---" in brief else brief
    return len(body.split())


def _strip_fences(text: str) -> str:
    """Remove a stray ``` code fence wrapper if the model added one."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


# Internal identifiers that must never surface in the CEO-facing brief — provenance lives
# in the sidecar, not the reader's Sources line. The gather contract tells the model this,
# but sonnet occasionally leaks the Accounts collection id into a no-match Sources line
# anyway, so we also strip them deterministically. This enforces the "Sources line = readable
# names" contract; it never adds or changes a fact (cf. _strip_fences).
_COLLECTION_REF_RE = re.compile(r"collection://[0-9a-fA-F-]+")
_NOTION_URL_RE = re.compile(r"https?://[^\s)]*notion[^\s)]*")
_BARE_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
# A parenthetical whose only payload is such an id/url, e.g. "(collection://…)" — drop whole.
_ID_PAREN_RE = re.compile(
    r"\s*\([^()]*(?:collection://|https?://[^\s)]*notion)[^()]*\)"
)


def _sanitize_metadata(brief: str) -> str:
    """Strip leaked internal identifiers from the brief's metadata header.

    Only the header (everything before the first `---`) is touched, so the body is never
    altered. Removes `collection://…` ids, Notion page URLs, and bare data-source/page
    UUIDs — first any parenthetical that exists solely to carry one, then stragglers — and
    tidies the punctuation/spacing left behind.
    """
    if "---" in brief:
        header, rest, sep = (*brief.split("---", 1), "---")
    else:
        header, rest, sep = brief, "", ""
    if not (_COLLECTION_REF_RE.search(header) or _NOTION_URL_RE.search(header)
            or _BARE_UUID_RE.search(header)):
        return brief  # nothing leaked — leave the (usually clean) header exactly as-is
    cleaned = _ID_PAREN_RE.sub("", header)
    cleaned = _COLLECTION_REF_RE.sub("", cleaned)
    cleaned = _NOTION_URL_RE.sub("", cleaned)
    cleaned = _BARE_UUID_RE.sub("", cleaned)
    # Tidy artifacts left behind: empty parens, doubled/pre-punctuation spaces.
    cleaned = re.sub(r"\(\s*\)", "", cleaned)
    cleaned = re.sub(r"\(\s+", "(", cleaned)
    cleaned = re.sub(r"\s+\)", ")", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r" +([,.;:])", r"\1", cleaned)
    return cleaned + sep + rest


def _extract_brief(text: str) -> str | None:
    """Return the brief starting at the title, dropping any preamble the model added.

    Robust to commentary before the title (e.g. a refusal or ambiguity note in the
    same message) — we slice from the first `# Meeting Brief`, not require the text
    to start with it.
    """
    cleaned = _strip_fences(text)
    idx = cleaned.find("# Meeting Brief")
    if idx == -1:
        return None
    return cleaned[idx:].strip()


def _is_unresolved(brief: str) -> bool:
    """True if the brief is a no-match / ambiguous / retrieval-failure brief.

    These are legitimately thin (mostly `Unknown`) and must not be padded by the
    length-retry loop.
    """
    head = brief[:800].lower()
    if any(
        s in head
        for s in (
            "no matching account",
            "no account named",
            "could not be resolved",
            "is ambiguous",
            # retrieval failures (Notion unavailable / errored / empty)
            "could not retrieve",
            "could not be retrieved",
            "could not be reached",
            "notion is unavailable",
            "notion error",
            "notion lookup failed",
        )
    ):
        return True
    return (
        "when:** unknown" in head
        and "who:** unknown" in head
        and "purpose:** unknown" in head
    )


def _result_block_text(block: ToolResultBlock) -> str:
    """Flatten a ToolResultBlock's content (str or list-of-dicts) to text."""
    inner = block.content
    if isinstance(inner, str):
        return inner
    if isinstance(inner, list):
        parts = []
        for x in inner:
            if isinstance(x, dict) and x.get("type") == "text":
                parts.append(x.get("text", ""))
            elif isinstance(x, str):
                parts.append(x)
        return "\n".join(parts)
    return ""


_PARENT_COLLECTION_RE = re.compile(r'parent-data-source url="collection://([0-9a-f-]+)"')


def _parse_fetch_result(text: str) -> dict | None:
    """Parse a notion-fetch result. Returns {title, url, category} or None.

    The result is a JSON object with top-level `title`/`url`; the page's own database is
    named in its `text` via a <parent-data-source url="collection://…"> tag.
    """
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    url = obj.get("url")
    title = obj.get("title")
    if not url or not title:
        return None
    m = _PARENT_COLLECTION_RE.search(obj.get("text", "") or "")
    category = _COLLECTION_CATEGORY.get(m.group(1)) if m else None
    return {"title": title, "url": url, "category": category}


def _empty_sources() -> dict:
    return {"account": None, "meetings": [], "contacts": []}


def _result_error(message: ResultMessage) -> str:
    """Human-readable error for a failed ResultMessage.

    The CLI reports HTTP-level failures (rate limit 429, overload 529, 5xx) as
    `is_error=True` with `subtype="success"` and the code in `api_error_status` — so
    surface that code, otherwise a transient rate limit looks like a baffling
    'error: success'.
    """
    status = getattr(message, "api_error_status", None)
    if status is not None:
        return f"HTTP {status} (transient API error — e.g. rate limit/overload)"
    return message.subtype


async def _agentic_draft(
    model: str, *, system_prompt: str, task_prompt: str
) -> tuple[str, list[str], dict]:
    """Run the agentic gather + draft pass. Returns (brief, tool_calls, sources).

    `system_prompt` / `task_prompt` select the mode: the Phase 2 single-name pair by default,
    or the Phase 4 meeting-aware pair. The tool/permission gates are identical either way —
    read-only Notion, everything else denied — so the safety posture is mode-independent.
    """
    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=model,
        allowed_tools=ALLOWED_TOOLS,
        disallowed_tools=DISALLOWED_TOOLS,
        permission_mode="dontAsk",  # deny anything not allow-listed, without prompting
        setting_sources=["user"],   # load the claude.ai Notion connector
        max_turns=40,
    )

    tool_calls: list[str] = []
    assistant_texts: list[str] = []
    fetch_ids: set[str] = set()          # tool_use ids of notion-fetch calls
    pages: list[dict] = []               # fetched pages, in order, deduped by url
    seen_urls: set[str] = set()
    async for message in query(
        prompt=task_prompt,
        options=options,
    ):
        if isinstance(message, AssistantMessage):
            parts = [b.text for b in message.content if isinstance(b, TextBlock)]
            for b in message.content:
                if isinstance(b, ToolUseBlock):
                    tool_calls.append(b.name)
                    if b.name.endswith("notion-fetch"):
                        fetch_ids.add(b.id)
            if parts:
                assistant_texts.append("".join(parts))
        elif isinstance(message, UserMessage):
            content = message.content
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, ToolResultBlock) and b.tool_use_id in fetch_ids:
                        page = _parse_fetch_result(_result_block_text(b))
                        if page and page["url"] not in seen_urls:
                            seen_urls.add(page["url"])
                            pages.append(page)
        elif isinstance(message, ResultMessage) and message.is_error:
            raise RuntimeError(f"Agent error: {_result_error(message)}")

    brief = ""
    for text in reversed(assistant_texts):
        candidate = _extract_brief(text)
        if candidate:
            brief = candidate
            break
    if not brief:
        raise RuntimeError("Agent returned no brief text.")
    # Keep internal ids/urls out of the CEO-facing brief; provenance is captured below.
    brief = _sanitize_metadata(brief)

    # Categorise fetched pages into the provenance sidecar shape.
    sources = _empty_sources()
    for p in pages:
        rec = {"title": p["title"], "url": p["url"]}
        cat = p["category"]
        if cat == "account":
            if sources["account"] is None:
                sources["account"] = rec
            else:  # more than one account page fetched (e.g. ambiguous) — keep them all
                sources.setdefault("other_accounts", []).append(rec)
        elif cat in ("meetings", "contacts"):
            sources[cat].append(rec)
        else:
            sources.setdefault("other", []).append(rec)
    return brief, tool_calls, sources


async def _tighten(brief: str, words: int, model: str, target: int = _RETRY_TARGET) -> str:
    """One non-agentic rewrite pass to bring the body toward `target` words.

    The rewrite aims for `target` (default ~_RETRY_TARGET, well inside the window) rather than
    the 350 acceptance edge so the model's natural variance keeps the result under the cap.
    The caller may pass a lower/higher target on later passes when the model under-corrects.
    """
    direction = (
        f"It is {words} words — too long. Cut it to about {target} words "
        f"(and never more than {MAX_BODY_WORDS})"
        if words > MAX_BODY_WORDS
        else f"It is only {words} words — too short. Expand it (using ONLY facts already "
        f"present in the brief, never new ones) to about {target} words "
        f"(and never fewer than {MIN_BODY_WORDS})"
    )
    options = ClaudeAgentOptions(
        system_prompt=OUTPUT_CONTRACT,
        model=model,
        allowed_tools=[],
        setting_sources=[],
        max_turns=1,
        permission_mode="default",
    )
    prompt = (
        f"Here is a finished meeting brief. {direction}, preserving every fact, the exact "
        f"section order, and the title/metadata/Sources line. Do not invent anything. "
        f"Output only the rewritten brief.\n\n{brief}"
    )
    chunks: list[str] = []
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for b in message.content:
                if isinstance(b, TextBlock):
                    chunks.append(b.text)
        elif isinstance(message, ResultMessage) and message.is_error:
            raise RuntimeError(f"Tighten error: {_result_error(message)}")
    result = "".join(chunks)
    return _extract_brief(result) or _strip_fences(result)


async def draft_brief(
    target: str, model: str = DEFAULT_MODEL, *, meeting: MeetingHint | None = None
) -> BriefResult:
    """Gather context from Notion for `target` and draft a one-page brief.

    Args:
        target: An account name or meeting subject (e.g. "Meridian"). Ignored when `meeting`
            is given (the meeting fields drive the gather instead).
        model: Model alias ("opus", "sonnet", "haiku") or a full model ID.
        meeting: When provided, draft a Phase 4 calendar brief centered on the specific
            attendee (meeting-aware system prompt + task template). When None, behaviour is
            exactly the Phase 2 single-name brief.

    Returns:
        A BriefResult with the brief text and an audit trail (tool calls, retry,
        unresolved flag, body word count, and — for meeting briefs — the calendar event id).
    """
    if meeting is not None:
        system_prompt = SYSTEM_PROMPT_NOTION_MEETING
        task_prompt = NOTION_MEETING_TASK_TEMPLATE.format(
            title=meeting.title,
            when=meeting.when,
            person=meeting.person,
            email=meeting.email or "(not provided)",
            company=meeting.company,
            description=meeting.description or "(none)",
        )
    else:
        system_prompt = SYSTEM_PROMPT_NOTION
        task_prompt = NOTION_TASK_TEMPLATE.format(target=target)

    brief, tool_calls, sources = await _agentic_draft(
        model, system_prompt=system_prompt, task_prompt=task_prompt
    )
    words = count_body_words(brief)
    unresolved = _is_unresolved(brief)

    # Validate-and-retry on length, up to _MAX_RETRIES passes. The rewrite aims for an
    # internal target (~320), not the 350 acceptance edge, so variance stays under the cap.
    # The model tends to under-correct (asked for 320 it may land 356), so when a pass is
    # still out of range we adapt the next target by the miss it just made. Acceptance stays
    # exactly 250-350. Never pad a legitimately short unresolved (no-match / ambiguous) brief.
    retried = False
    target = _RETRY_TARGET
    for _ in range(_MAX_RETRIES):
        too_long = words > MAX_BODY_WORDS
        too_short = words < MIN_BODY_WORDS
        if not (too_long or (too_short and not unresolved)):
            break  # inside the acceptance window — done
        tightened = await _tighten(brief, words, model, target)
        if not tightened.lstrip().startswith("# Meeting Brief"):
            break  # rewrite didn't return a valid brief — keep the last good one
        new_words = count_body_words(tightened)
        brief = tightened
        retried = True
        # Adapt: aim for ~15 words inside the breached edge, offset by however far the model
        # just missed its target — so a model that under-cuts gets a correspondingly lower aim.
        miss = new_words - target
        if new_words > MAX_BODY_WORDS:
            target = max(MIN_BODY_WORDS + 5, (MAX_BODY_WORDS - 15) - miss)
        elif new_words < MIN_BODY_WORDS:
            target = min(MAX_BODY_WORDS - 5, (MIN_BODY_WORDS + 15) - miss)
        words = new_words
        if os.environ.get("BRIEF_DEBUG"):
            print(f"  [length-retry] -> {words} words; next target {target}", file=sys.stderr)

    return BriefResult(
        text=brief,
        tool_calls=tool_calls,
        retried=retried,
        unresolved=unresolved,
        body_words=words,
        sources=sources,
        event_id=meeting.event_id if meeting else None,
        status="briefed",
    )
