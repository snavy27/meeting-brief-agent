"""Evaluation cases for the brief agent, keyed to real Notion CRM accounts.

Each case is a dict:
  id                 short slug
  input              the target string passed to the agent
  kind               "account" (a real account) or "negative" (no-match / ambiguous)
  note               human label of the account state
  expect_unresolved  True if the brief MUST be an unresolved/Unknown brief
  must_appear        substrings that MUST be in the brief (case-insensitive)
  must_not_appear    substrings that MUST NOT be in the brief (fabrication / wrong-account traps)

`must_appear` holds real, distinctive facts for that account; `must_not_appear` holds
strong markers of OTHER accounts (cross-contamination) or, for negatives, real figures the
agent must never invent. Deeper grounding/correctness is left to the LLM judge.
"""

CASES = [
    {
        "id": "meridian",
        "input": "Meridian",
        "kind": "account",
        "note": "healthy / renewal",
        "expect_unresolved": False,
        "must_appear": ["Sarah Chen", "renewal"],
        "must_not_appear": ["Greg Sullivan", "Orbit Telecom", "Priya Nair"],
    },
    {
        "id": "orbit",
        "input": "Orbit Telecom",
        "kind": "account",
        "note": "at-risk / outages",
        "expect_unresolved": False,
        "must_appear": ["Greg Sullivan", "outage"],
        "must_not_appear": ["Sarah Chen", "Meridian", "David Klein"],
    },
    {
        "id": "cobalt",
        "input": "Cobalt Software",
        "kind": "account",
        "note": "expansion",
        "expect_unresolved": False,
        "must_appear": ["Priya Nair", "expansion"],
        "must_not_appear": ["Greg Sullivan", "Meridian", "outage"],
    },
    {
        "id": "brightline",
        "input": "Brightline Health",
        "kind": "account",
        "note": "prospect / compliance",
        "expect_unresolved": False,
        "must_appear": ["Marcus Reed", "HIPAA"],
        "must_not_appear": ["Sarah Chen", "Orbit Telecom", "renewal date 2026"],
    },
    {
        "id": "pinnacle",
        "input": "Pinnacle Bank",
        "kind": "account",
        "note": "new / regulated",
        "expect_unresolved": False,
        "must_appear": ["David Klein", "SOC 2"],
        "must_not_appear": ["Sarah Chen", "Greg Sullivan", "Meridian"],
    },
    {
        "id": "zephyr_nonexistent",
        "input": "Zephyr Corp",
        "kind": "negative",
        "note": "nonexistent account",
        "expect_unresolved": True,
        "must_appear": ["Unknown"],
        # must not fabricate a real account's data
        "must_not_appear": ["$3.2M", "Sarah Chen", "Greg Sullivan", "Priya Nair"],
    },
    {
        "id": "ambiguous_health",
        "input": "Health",
        "kind": "negative",
        "note": "ambiguous (Brightline Health vs Cedar Health Systems)",
        "expect_unresolved": True,
        "must_appear": ["Unknown"],
        # must not silently commit to one account as if resolved
        "must_not_appear": ["$3.2M", "Greg Sullivan"],
    },
]
