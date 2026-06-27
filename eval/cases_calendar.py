"""Phase 4 calendar eval cases — MOCKED calendar payloads (no live calendar).

`DAY_EVENTS` mirrors the shape `list_events` returns (and the real seeded 2026-06-29 day),
but is hand-built so the suite is deterministic. Attendee emails use the `@company.example.com`
test form on purpose — the engine must match the CRM contact by name + company root, not by the
literal address (the CRM stores `@company.com`).

Events are listed OUT OF start-time order so ordering is actually exercised.
"""

_SELF = {"email": "shardanavalika@gmail.com", "self": True, "organizer": True}


def _ev(eid, summary, start_h, end_h, attendees, description=""):
    return {
        "id": eid,
        "summary": summary,
        "description": description,
        "start": {"dateTime": f"2026-06-29T{start_h}:00+02:00", "timeZone": "Europe/Paris"},
        "end": {"dateTime": f"2026-06-29T{end_h}:00+02:00", "timeZone": "Europe/Paris"},
        "organizer": {"email": "shardanavalika@gmail.com", "self": True},
        "creator": {"email": "shardanavalika@gmail.com", "self": True},
        "attendees": attendees,
    }


def _ext(name, email):
    return {"displayName": name, "email": email}


# --- the mocked day (unsorted on purpose) ---------------------------------- #
EV_BRIGHTLINE = _ev(
    "ev-brightline", "Compliance deep-dive — Brightline Health", "15", "15:30",
    [_SELF, _ext("Marcus Reed", "marcus.reed@brightlinehealth.example.com")],
    "Security and HIPAA review.",
)
EV_MERIDIAN = _ev(
    "ev-meridian", "Renewal sync — Meridian Retail", "09", "09:30",
    [_SELF, _ext("Sarah Chen", "sarah.chen@meridianretail.example.com")],
    "Quarterly renewal check-in ahead of the September renewal.",
)
EV_STANDUP = _ev(
    "ev-standup", "Internal: Sales pipeline standup", "11", "11:30",
    [_SELF],
    "Weekly internal pipeline review. No external attendees.",
)
EV_ORBIT = _ev(
    "ev-orbit", "Reliability review — Orbit Telecom", "10", "10:30",
    [_SELF, _ext("Greg Sullivan", "greg.sullivan@orbittelecom.example.com")],
    "Review reliability improvement plan after Q2 outages.",
)
EV_QUANTUM = _ev(
    "ev-quantum", "Intro call — Quantum Robotics", "13", "13:30",
    [_SELF, _ext("Jane Doe", "jane.doe@quantumrobotics.example.com")],
    "First exploratory call with a new inbound prospect (not in CRM).",
)
EV_COBALT = _ev(
    "ev-cobalt", "Expansion chat — Cobalt Software", "14", "14:30",
    [_SELF, _ext("Priya Nair", "priya.nair@cobaltsoftware.example.com")],
    "Discuss expansion to two sister teams (~40 seats).",
)

DAY_EVENTS = [EV_BRIGHTLINE, EV_MERIDIAN, EV_STANDUP, EV_ORBIT, EV_QUANTUM, EV_COBALT]

# Expected packet shape for the full day.
DAY_EXPECTED = {
    "total": 6,
    "briefed": 4,   # Meridian, Orbit, Cobalt, Brightline
    "stub": 1,      # Quantum Robotics (no CRM match)
    "skipped": 1,   # internal standup
    # briefed+stub items, in start order:
    "item_order": ["ev-meridian", "ev-orbit", "ev-quantum", "ev-cobalt", "ev-brightline"],
}

# A deliberately-small 3-event day for the structural order+counts case (d).
THREE_EVENT_DAY = [EV_QUANTUM, EV_STANDUP, EV_ORBIT]  # unsorted
THREE_EVENT_EXPECTED = {
    "total": 3, "briefed": 1, "stub": 1, "skipped": 1,
    "item_order": ["ev-orbit", "ev-quantum"],
}


# --- per-brief grading specs ----------------------------------------------- #
# Account cases: full person-centered briefs. `wrong_person` is the other CRM contact at the
# same account who must NOT become the primary subject (may appear only as background).
ACCOUNT_CASES = [
    {
        "id": "meridian", "event_id": "ev-meridian", "company": "Meridian Retail",
        "attendee": "Sarah Chen", "wrong_person": None,
        "when_token": "29 Jun 2026, 09:00",
        "must_appear": ["Sarah Chen", "renewal"],
        "must_not_appear": ["Greg Sullivan", "Orbit Telecom"],
    },
    {
        "id": "orbit", "event_id": "ev-orbit", "company": "Orbit Telecom",
        "attendee": "Greg Sullivan", "wrong_person": "Dana Cole",
        "when_token": "29 Jun 2026, 10:00",
        "must_appear": ["Greg Sullivan"],
        "must_not_appear": ["Meridian", "Sarah Chen"],
    },
    {
        "id": "cobalt", "event_id": "ev-cobalt", "company": "Cobalt Software",
        "attendee": "Priya Nair", "wrong_person": "Mark Lin",
        "when_token": "29 Jun 2026, 14:00",
        "must_appear": ["Priya Nair"],
        "must_not_appear": ["Meridian", "Greg Sullivan"],
    },
    {
        "id": "brightline", "event_id": "ev-brightline", "company": "Brightline Health",
        "attendee": "Marcus Reed", "wrong_person": "Aisha Bello",
        "when_token": "29 Jun 2026, 15:00",
        "must_appear": ["Marcus Reed"],
        "must_not_appear": ["Orbit Telecom", "Greg Sullivan"],
    },
]

# Stub case: external attendee with no CRM record → calendar-only stub, invents nothing.
STUB_CASE = {
    "id": "quantum_stub", "event_id": "ev-quantum", "company": "Quantum Robotics",
    "attendee": "Jane Doe", "when_token": "29 Jun 2026, 13:00",
    "must_not_appear": ["Sarah Chen", "Greg Sullivan", "Priya Nair", "Marcus Reed", "ARR"],
}
