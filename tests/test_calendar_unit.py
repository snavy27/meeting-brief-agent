"""Pure unit tests for the Phase 4 calendar adapter + orchestration logic.

No network, no model: these test `parse_events` (internal detection, company token,
external-attendee pick, start-time ordering), `resolve_date`, the deterministic stub /
metadata-time prepend, and `run_daily_briefing`'s order/counts/skip with a fake engine.

Run:  python tests/test_calendar_unit.py     (self-contained)
  or: pytest tests/test_calendar_unit.py
"""

import asyncio
import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import brief_agent.daily as daily
from brief_agent.agent import BriefResult
from brief_agent.calendar import parse_events, resolve_date
from brief_agent.daily import _prepend_meeting_time, _stub_brief, run_daily_briefing

SELF = {"email": "shardanavalika@gmail.com", "self": True, "organizer": True}


def _ev(eid, summary, start_h, attendees, end_h=None):
    end_h = end_h or f"{int(start_h)+1:02d}"
    return {
        "id": eid, "summary": summary,
        "start": {"dateTime": f"2026-06-29T{start_h}:00:00+02:00", "timeZone": "Europe/Paris"},
        "end": {"dateTime": f"2026-06-29T{end_h}:00:00+02:00", "timeZone": "Europe/Paris"},
        "organizer": {"email": "shardanavalika@gmail.com", "self": True},
        "creator": {"email": "shardanavalika@gmail.com", "self": True},
        "attendees": attendees,
    }


def _ext(name, email):
    return {"displayName": name, "email": email}


# --------------------------------------------------------------------------- #
def test_orders_by_start_time():
    raw = [
        _ev("c", "C", "14", [SELF, _ext("Priya Nair", "priya.nair@cobaltsoftware.example.com")]),
        _ev("a", "A", "09", [SELF, _ext("Sarah Chen", "sarah.chen@meridianretail.example.com")]),
        _ev("b", "B", "10", [SELF, _ext("Greg Sullivan", "greg.sullivan@orbittelecom.example.com")]),
    ]
    assert [e.id for e in parse_events(raw)] == ["a", "b", "c"]


def test_internal_when_no_external_attendee():
    raw = [_ev("s", "Internal standup", "11", [SELF])]
    ev = parse_events(raw)[0]
    assert ev.is_internal is True
    assert ev.attendee is None


def test_internal_when_all_share_our_domain():
    raw = [_ev("s", "Team sync", "11", [SELF, _ext("Colleague", "colleague@gmail.com")])]
    assert parse_events(raw)[0].is_internal is True


def test_external_attendee_picked():
    raw = [_ev("o", "Reliability review — Orbit Telecom", "10",
               [SELF, _ext("Greg Sullivan", "greg.sullivan@orbittelecom.example.com")])]
    ev = parse_events(raw)[0]
    assert ev.is_internal is False
    assert ev.attendee == {
        "name": "Greg Sullivan", "email": "greg.sullivan@orbittelecom.example.com"
    }


def test_company_token_from_title():
    raw = [_ev("o", "Reliability review — Orbit Telecom", "10",
               [SELF, _ext("Greg Sullivan", "greg.sullivan@orbittelecom.example.com")])]
    assert parse_events(raw)[0].company_token == "Orbit Telecom"


def test_company_token_from_email_when_no_title_sep():
    raw = [_ev("o", "Catch up", "10",
               [SELF, _ext("Greg Sullivan", "greg.sullivan@orbittelecom.example.com")])]
    # falls back to the email domain root, with the test suffix stripped
    assert parse_events(raw)[0].company_token == "Orbittelecom"


def test_attendee_name_falls_back_to_email_localpart():
    raw = [_ev("x", "Call — Foo", "10", [SELF, {"email": "jane.doe@foo.example.com"}])]
    assert parse_events(raw)[0].attendee["name"] == "Jane Doe"


def test_resolve_date():
    today = date(2026, 6, 27)
    assert resolve_date(None, today=today) == date(2026, 6, 28)
    assert resolve_date("tomorrow", today=today) == date(2026, 6, 28)
    assert resolve_date("today", today=today) == today
    assert resolve_date("2026-06-29", today=today) == date(2026, 6, 29)


def test_stub_marks_no_crm_match_and_names_attendee():
    raw = [_ev("q", "Intro call — Quantum Robotics", "13",
               [SELF, _ext("Jane Doe", "jane.doe@quantumrobotics.example.com")])]
    ev = parse_events(raw)[0]
    stub = _stub_brief(ev)
    assert stub.startswith("# Meeting Brief —")
    assert "No CRM match — calendar details only" in stub
    assert "Jane Doe" in stub
    assert "29 Jun 2026, 13:00" in stub


def test_prepend_meeting_time_leads_metadata_line():
    brief = "# Meeting Brief — X\n\n**When:** TBD · **Who:** Y\n\n---\n\n## Bottom line\nb"
    out = _prepend_meeting_time(brief, "Mon 29 Jun 2026, 10:00–10:30 (Europe/Paris)")
    meta = out.split("\n")[2]
    assert meta.startswith("**Meeting:** Mon 29 Jun 2026, 10:00–10:30")
    # idempotent: a second pass doesn't double-prepend
    assert _prepend_meeting_time(out, "x") == out


# --- orchestration with a fake engine (no network) ------------------------- #
async def _fake_draft(target, model="opus", *, meeting=None):
    company = (meeting.company if meeting else target).lower()
    return BriefResult(
        text="# Meeting Brief — fake\n\n**When:** x\n\n---\n\n## Bottom line\nb",
        unresolved=("quantum" in company),
        event_id=meeting.event_id if meeting else None,
    )


def test_run_daily_briefing_counts_and_order():
    raw = [
        _ev("brightline", "Compliance — Brightline Health", "15",
            [SELF, _ext("Marcus Reed", "marcus.reed@brightlinehealth.example.com")]),
        _ev("standup", "Internal standup", "11", [SELF]),
        _ev("orbit", "Reliability — Orbit Telecom", "10",
            [SELF, _ext("Greg Sullivan", "greg.sullivan@orbittelecom.example.com")]),
        _ev("quantum", "Intro — Quantum Robotics", "13",
            [SELF, _ext("Jane Doe", "jane.doe@quantumrobotics.example.com")]),
    ]
    events = parse_events(raw)
    with patch.object(daily, "draft_brief", _fake_draft):
        packet = asyncio.run(run_daily_briefing(events))
    assert packet.total == 4
    assert packet.briefed == 2          # orbit + brightline
    assert packet.stubs == 1            # quantum
    assert len(packet.skipped) == 1     # standup
    assert [i.event_id for i in packet.items] == ["orbit", "quantum", "brightline"]
    assert packet.made_any_write is False


# --------------------------------------------------------------------------- #
def _all_tests():
    return [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]


def main() -> int:
    failures = 0
    for name, fn in _all_tests():
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL  {name}\n        {type(e).__name__}: {e}")
    total = len(_all_tests())
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
