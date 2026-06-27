"""Phase 4 calendar input adapter.

Turns a day's raw Google Calendar events into normalised `CalEvent`s the daily-briefing
orchestrator can batch over. The normaliser (`parse_events`) is a PURE function of the raw
event JSON — no clock, no network — so the eval suite can feed it mocked payloads and stay
deterministic. The only networked piece is `fetch_day_events`, used by the live CLI; the
evals never call it.

Read-only by construction: `fetch_day_events` runs an agent allowed ONLY the calendar-read
tools (list/get), with every calendar AND Notion write tool hard-denied and non-interactive
auto-deny on — and it records every tool call so the caller can assert zero writes.
"""

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

from .agent import (
    CALENDAR_READ_TOOLS,
    CALENDAR_WRITE_TOOLS,
    NOTION_WRITE_TOOLS,
    _result_error,
)

# Domain suffixes that mark a TEST attendee address (name@company.example.com) rather than a
# real one. Stripped when deriving the company root from an email domain.
_TEST_SUFFIXES = (".example.com", ".example.org", ".example")

# Separators a "<purpose> — <Company>" event title may use (em dash, en dash, spaced hyphen).
_TITLE_SEPARATORS = ("—", "–", " - ")


@dataclass
class CalEvent:
    """One normalised calendar event."""

    id: str
    title: str
    start: datetime
    end: datetime | None
    description: str
    tz_name: str
    # Every attendee, normalised to {"name", "email", "self"}.
    attendees: list[dict] = field(default_factory=list)
    self_domain: str = ""

    # --- derived helpers -------------------------------------------------- #
    @property
    def external_attendees(self) -> list[dict]:
        """Attendees who are not us — not flagged `self` and not on our domain."""
        out = []
        for a in self.attendees:
            if a.get("self"):
                continue
            dom = _email_domain(a.get("email", ""))
            if dom and self.self_domain and dom == self.self_domain:
                continue
            out.append(a)
        return out

    @property
    def is_internal(self) -> bool:
        """True if there is no external attendee (internal-only / no attendees)."""
        return not self.external_attendees

    @property
    def attendee(self) -> dict | None:
        """The single external person being met (prefer one with a real display name)."""
        ext = self.external_attendees
        if not ext:
            return None
        named = [a for a in ext if a.get("name")]
        chosen = named[0] if named else ext[0]
        return {
            "name": chosen.get("name") or _name_from_email(chosen.get("email", "")),
            "email": chosen.get("email", ""),
        }

    @property
    def company_token(self) -> str:
        """Best human-readable company guess: from the title, else the email domain root."""
        from_title = _company_from_title(self.title)
        if from_title:
            return from_title
        a = self.attendee
        return _company_from_email(a["email"]) if a else ""

    def when_str(self) -> str:
        """Human meeting time for the brief metadata, e.g. 'Mon 29 Jun 2026, 10:00–10:30 (Europe/Paris)'."""
        day = self.start.strftime("%a %d %b %Y")
        start_t = self.start.strftime("%H:%M")
        end_t = self.end.strftime("%H:%M") if self.end else ""
        span = f"{start_t}–{end_t}" if end_t else start_t
        tz = f" ({self.tz_name})" if self.tz_name else ""
        return f"{day}, {span}{tz}"

    def sort_key(self) -> float:
        """UTC timestamp for stable start-time ordering across mixed/naive offsets."""
        dt = self.start
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def _email_domain(email: str) -> str:
    return email.split("@")[-1].lower() if "@" in email else ""


def _name_from_email(email: str) -> str:
    """'greg.sullivan@…' -> 'Greg Sullivan' (a readable fallback when no displayName)."""
    local = email.split("@")[0] if "@" in email else email
    parts = [p for p in local.replace(".", " ").replace("_", " ").split() if p]
    return " ".join(p.capitalize() for p in parts) if parts else email


def _company_from_title(title: str) -> str:
    for sep in _TITLE_SEPARATORS:
        if sep in title:
            tail = title.split(sep)[-1].strip()
            if tail:
                return tail
    return ""


def _company_from_email(email: str) -> str:
    dom = _email_domain(email)
    for suf in _TEST_SUFFIXES:
        if dom.endswith(suf):
            dom = dom[: -len(suf)]
            break
    root = dom.split(".")[0] if dom else ""
    return root.capitalize()


def _parse_dt(obj: dict | None) -> datetime | None:
    """Parse a Google Calendar start/end object ({dateTime} timed, or {date} all-day)."""
    if not obj:
        return None
    if obj.get("dateTime"):
        return datetime.fromisoformat(obj["dateTime"])
    if obj.get("date"):  # all-day → midnight
        return datetime.fromisoformat(obj["date"] + "T00:00:00")
    return None


def _self_domain(raw: dict) -> str:
    """Derive our own domain from the event's self attendee / organizer / creator."""
    for a in raw.get("attendees", []) or []:
        if a.get("self") and a.get("email"):
            return _email_domain(a["email"])
    for key in ("organizer", "creator"):
        node = raw.get(key) or {}
        if node.get("self") and node.get("email"):
            return _email_domain(node["email"])
    for key in ("organizer", "creator"):
        node = raw.get(key) or {}
        if node.get("email"):
            return _email_domain(node["email"])
    return ""


def _norm_attendee(a: dict) -> dict:
    return {
        "name": a.get("displayName", ""),
        "email": a.get("email", ""),
        "self": bool(a.get("self")),
    }


def parse_events(raw_events: list[dict]) -> list[CalEvent]:
    """Normalise raw Google Calendar events into CalEvents, sorted by start time.

    Pure: no clock, no I/O. `raw_events` is the `events` list from a `list_events` response
    (or the equivalent mocked payload).
    """
    events: list[CalEvent] = []
    for raw in raw_events:
        self_domain = _self_domain(raw)
        attendees = [_norm_attendee(a) for a in (raw.get("attendees") or [])]
        start = _parse_dt(raw.get("start"))
        if start is None:
            continue  # an event with no start time is unusable
        events.append(
            CalEvent(
                id=raw.get("id", ""),
                title=raw.get("summary", "(no title)"),
                start=start,
                end=_parse_dt(raw.get("end")),
                description=raw.get("description", "") or "",
                tz_name=(raw.get("start") or {}).get("timeZone", ""),
                attendees=attendees,
                self_domain=self_domain,
            )
        )
    events.sort(key=lambda e: e.sort_key())
    return events


def resolve_date(spec: str | None, *, today: date) -> date:
    """Resolve a --date spec to a concrete date. Default (None/'tomorrow') = tomorrow.

    `today` is injected so callers control the clock (deterministic tests + CLI).
    """
    s = (spec or "tomorrow").strip().lower()
    if s == "today":
        return today
    if s == "tomorrow":
        return today + timedelta(days=1)
    return date.fromisoformat(s)  # YYYY-MM-DD (raises on bad input — surfaced by the CLI)


# --------------------------------------------------------------------------- #
# Production fetch (agentic, read-only) — NOT used by the deterministic evals
# --------------------------------------------------------------------------- #
_FETCH_SYSTEM = """\
You are a read-only calendar export tool. You call ONLY the calendar list/get tools to read
events; you NEVER create, update, delete, or respond to events. Return data, nothing else."""

_FETCH_TASK = """\
List ALL events on the user's primary calendar for {day} (the entire day, local time), using
the list_events tool with startTime "{day}T00:00:00" and endTime "{next_day}T00:00:00".

Then output ONLY a JSON array (no prose, no code fences) where each element is:
{{"id","summary","description","start","end","attendees","organizer","creator"}}
copying these fields VERBATIM from each event (keep start/end as their full objects with
dateTime/date and timeZone, and keep each attendee's displayName, email, self, organizer).
TRUNCATE each "description" to its first ~300 characters (plain text only — drop any HTML,
links, or boilerplate) so the JSON stays small and valid. If there are no events, output []."""


def _extract_json_array(text: str) -> list[dict]:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    start, end = t.find("["), t.rfind("]")
    if start == -1 or end == -1:
        raise RuntimeError("Calendar fetch did not return a JSON array of events.")
    return json.loads(t[start : end + 1])


async def fetch_day_events(day: date, model: str) -> tuple[list[dict], list[str]]:
    """Read a day's raw events via a read-only calendar agent.

    Returns (raw_events, tool_calls). The caller asserts no write tool appears in tool_calls.
    """
    options = ClaudeAgentOptions(
        system_prompt=_FETCH_SYSTEM,
        model=model,
        allowed_tools=CALENDAR_READ_TOOLS + ["ToolSearch"],
        disallowed_tools=CALENDAR_WRITE_TOOLS + NOTION_WRITE_TOOLS + ["AskUserQuestion"],
        permission_mode="dontAsk",
        setting_sources=["user"],
        max_turns=20,
    )
    task = _FETCH_TASK.format(day=day.isoformat(), next_day=(day + timedelta(days=1)).isoformat())
    tool_calls: list[str] = []
    texts: list[str] = []
    async for message in query(prompt=task, options=options):
        if isinstance(message, AssistantMessage):
            for b in message.content:
                if isinstance(b, ToolUseBlock):
                    tool_calls.append(b.name)
                elif isinstance(b, TextBlock):
                    texts.append(b.text)
        elif isinstance(message, ResultMessage) and message.is_error:
            raise RuntimeError(f"Calendar fetch error: {_result_error(message)}")
    raw = _extract_json_array("".join(texts))
    return raw, tool_calls
