"""Phase 4 orchestration: a calendar day in, one daily briefing packet out.

Batches the existing per-meeting engine (`brief_agent.agent.draft_brief`) over a day's events:
- internal-only events are skipped (listed, not briefed);
- external events get a person-centered brief via the meeting-aware engine;
- external events with no CRM match get a deterministic calendar-only STUB (nothing invented).

Everything is ordered by start time and rendered into one packet, with a provenance sidecar
(calendar event id + the Notion pages used per brief). The packet records every tool call across
the run so the caller can ASSERT zero writes to both Calendar and Notion.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import date

from .agent import (
    _ALL_WRITE_SET,
    DEFAULT_MODEL,
    MeetingHint,
    draft_brief,
)
from .calendar import CalEvent

_MAX_CONCURRENCY = 3  # cap concurrent per-meeting drafts (matches the eval suite)


@dataclass
class PacketItem:
    """One line of the day: a full brief, a calendar-only stub, or a skipped internal event."""

    event_id: str
    title: str
    when: str
    status: str            # "briefed" | "stub" | "skipped"
    sort_key: float
    person: str | None = None
    text: str = ""         # rendered brief/stub markdown ("" for skipped)
    sources: dict = field(default_factory=dict)
    body_words: int = 0
    retried: bool = False
    tool_calls: list[str] = field(default_factory=list)


@dataclass
class DayPacket:
    day: date
    items: list[PacketItem]    # briefed + stub, in start order
    skipped: list[PacketItem]  # internal, in start order
    tool_calls: list[str] = field(default_factory=list)  # every call across the whole run

    @property
    def briefed(self) -> int:
        return sum(1 for i in self.items if i.status == "briefed")

    @property
    def stubs(self) -> int:
        return sum(1 for i in self.items if i.status == "stub")

    @property
    def total(self) -> int:
        return len(self.items) + len(self.skipped)

    @property
    def write_tool_calls(self) -> list[str]:
        """Any write tool (Calendar or Notion) seen anywhere in the run — must be empty."""
        return [t for t in self.tool_calls if t in _ALL_WRITE_SET]

    @property
    def made_any_write(self) -> bool:
        return bool(self.write_tool_calls)


# --------------------------------------------------------------------------- #
# Deterministic stub + metadata-time prepend (no model, nothing invented)
# --------------------------------------------------------------------------- #
def _stub_brief(event: CalEvent) -> str:
    """A minimal calendar-only brief for an event with no CRM match. Invents nothing."""
    a = event.attendee or {}
    person = a.get("name") or "(unknown attendee)"
    email = f" ({a['email']})" if a.get("email") else ""
    company = event.company_token or "the company"
    return (
        f"# Meeting Brief — {event.title}\n\n"
        f"**When:** {event.when_str()} · **Who:** {person}{email} · "
        f"**Purpose:** {event.title}\n"
        f"**Sources:** No CRM match — calendar details only.\n\n"
        f"_No CRM match — calendar details only. {person} / {company} was not found in the "
        f"Notion CRM, so there is no account context to brief. Confirm the record in Notion "
        f"before the meeting._"
    )


def _prepend_meeting_time(brief: str, when: str) -> str:
    """Prepend the real calendar meeting time to the brief's metadata line.

    Guarantees the metadata line leads with the actual meeting time regardless of what the
    model wrote into `When:`. Idempotent-ish: only the first `**When:**` line is touched.
    """
    lines = brief.split("\n")
    for i, ln in enumerate(lines):
        if "**when:**" in ln.lower():
            if ln.lstrip().startswith("**Meeting:**"):
                return brief  # already prepended
            lines[i] = f"**Meeting:** {when} · " + ln
            return "\n".join(lines)
    return brief  # no metadata line found — leave untouched


def _item_sources(result_sources: dict, event_id: str, status: str) -> dict:
    """Provenance record for the sidecar: event id + the Notion pages used."""
    src = result_sources or {}
    return {
        "event_id": event_id,
        "status": status,
        "account": src.get("account"),
        "contacts": src.get("contacts", []),
        "meetings": src.get("meetings", []),
    }


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def _hint(event: CalEvent) -> MeetingHint:
    a = event.attendee or {}
    return MeetingHint(
        person=a.get("name", ""),
        email=a.get("email", ""),
        company=event.company_token,
        when=event.when_str(),
        title=event.title,
        description=event.description,
        event_id=event.id,
    )


async def _brief_one(event: CalEvent, model: str, sem: asyncio.Semaphore) -> PacketItem:
    """Draft one external event into a PacketItem (full brief, or stub on no CRM match)."""
    a = event.attendee or {}
    async with sem:
        result = await draft_brief(event.company_token, model=model, meeting=_hint(event))

    if result.unresolved:
        # Agent could not resolve the account/person → deterministic calendar-only stub.
        return PacketItem(
            event_id=event.id,
            title=event.title,
            when=event.when_str(),
            status="stub",
            sort_key=event.sort_key(),
            person=a.get("name"),
            text=_stub_brief(event),
            sources=_item_sources(result.sources, event.id, "stub"),
            tool_calls=result.tool_calls,
        )

    return PacketItem(
        event_id=event.id,
        title=event.title,
        when=event.when_str(),
        status="briefed",
        sort_key=event.sort_key(),
        person=a.get("name"),
        text=_prepend_meeting_time(result.text, event.when_str()),
        sources=_item_sources(result.sources, event.id, "briefed"),
        body_words=result.body_words,
        retried=result.retried,
        tool_calls=result.tool_calls,
    )


async def run_daily_briefing(
    events: list[CalEvent],
    model: str = DEFAULT_MODEL,
    *,
    fetch_tool_calls: list[str] | None = None,
) -> DayPacket:
    """Batch the per-meeting engine over a day's (already-parsed) events.

    `fetch_tool_calls` lets the caller fold the calendar-read agent's tool calls into the
    packet's zero-writes accounting. `events` must be the output of `calendar.parse_events`
    (sorted by start time).
    """
    day = events[0].start.date() if events else date.today()

    skipped: list[PacketItem] = [
        PacketItem(
            event_id=e.id,
            title=e.title,
            when=e.when_str(),
            status="skipped",
            sort_key=e.sort_key(),
        )
        for e in events
        if e.is_internal
    ]

    external = [e for e in events if not e.is_internal]
    sem = asyncio.Semaphore(_MAX_CONCURRENCY)
    items = await asyncio.gather(*(_brief_one(e, model, sem) for e in external))
    items = sorted(items, key=lambda i: i.sort_key)

    tool_calls = list(fetch_tool_calls or [])
    for i in items:
        tool_calls.extend(i.tool_calls)

    return DayPacket(day=day, items=items, skipped=skipped, tool_calls=tool_calls)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def render_packet(packet: DayPacket) -> str:
    """Render the combined daily packet: header + counts + skipped summary + briefs in order."""
    day_str = packet.day.strftime("%A %d %B %Y")
    out: list[str] = [
        f"# Daily briefing — {day_str}",
        "",
        f"{packet.total} meetings · {packet.briefed} briefed / {packet.stubs} unresolved / "
        f"{len(packet.skipped)} skipped (internal)",
        "",
        "## Skipped (internal)",
    ]
    if packet.skipped:
        out += [f"- {s.when} — {s.title}" for s in packet.skipped]
    else:
        out.append("_None._")

    for item in packet.items:
        out += ["", "---", "", item.text]

    return "\n".join(out) + "\n"
