"""Notion failure-injection tests — the agent must degrade safely, never hallucinate.

These mock the SDK boundary (`brief_agent.agent.query`) with deterministic message
streams that simulate Notion being unavailable / erroring / returning empty. No
network and no real model call, so the suite runs every time.

Covered injected failures:
  1. notion-search returns a hard error / times out (turn fails).
  2. notion-search errors but the model still produces an unresolved brief.
  3. notion-search ok, notion-fetch errors on the account page.
  4. account resolves but linked Contacts/Meetings come back empty/error.
  5. the model returns no brief at all.
  6. (invariant guard) a stray write tool-use is detected.
  7. (regression) the normal length-retry path still fires exactly once.

For each failure the agent must: not emit a normal brief of invented facts (it is
flagged `unresolved`), surface a clear could-not-retrieve state, NOT pad via the
length-retry loop, make zero write calls, and exit cleanly (a hard failure raises a
RuntimeError that the CLI turns into a non-zero exit, never an unhandled crash).

Run:  python tests/test_resilience.py        (self-contained)
  or: pytest tests/test_resilience.py
"""

import asyncio
import io
import sys
import tempfile
from contextlib import contextmanager, redirect_stderr
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

import brief_agent.agent as agent
import brief_agent.cli as cli

NOTION = "mcp__claude_ai_Notion__"
SEARCH = f"{NOTION}notion-search"
FETCH = f"{NOTION}notion-fetch"
UPDATE = f"{NOTION}notion-update-page"


# --------------------------------------------------------------------------- #
# message-stream builders (real SDK dataclasses, so agent.py isinstance passes)
# --------------------------------------------------------------------------- #
def asst(*blocks):
    return AssistantMessage(content=list(blocks), model="test")


def text(t):
    return TextBlock(text=t)


def tool(name, **inp):
    return ToolUseBlock(id="tu", name=name, input=inp)


def result(is_error=False, subtype="success"):
    return ResultMessage(
        subtype=subtype,
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=1,
        session_id="s",
    )


class FakeQuery:
    """Stand-in for claude_agent_sdk.query: yields a preset stream per call."""

    def __init__(self, *streams):
        self.streams = list(streams)
        self.calls = []

    def __call__(self, *, prompt=None, options=None, **kw):
        idx = len(self.calls)
        self.calls.append(options)
        msgs = self.streams[idx] if idx < len(self.streams) else self.streams[-1]

        async def gen():
            for m in msgs:
                yield m

        return gen()


@contextmanager
def fake_query(*streams):
    fq = FakeQuery(*streams)
    with patch.object(agent, "query", fq):
        yield fq


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def unresolved_brief(reason: str) -> str:
    """A correctly-degraded brief: all-Unknown, says it couldn't reach Notion."""
    return (
        f"# Meeting Brief — Unknown (could not retrieve from Notion)\n\n"
        f"**When:** Unknown · **Who:** Unknown · **Purpose:** Unknown · "
        f"**Sources:** could not retrieve from Notion — {reason}\n\n"
        "---\n\n"
        "## Bottom line\n"
        "Notion could not be reached to gather this meeting's context, so no brief can be "
        "built from source. Confirm the account and retry; treat every detail as unverified "
        "until the CRM responds.\n\n"
        "## Who you're meeting\n- Unknown — Notion lookup failed.\n\n"
        "## What's changed since you last spoke\n- Unknown — could not retrieve history.\n\n"
        "## Likely to come up\n- Unknown — no source to anticipate from.\n\n"
        "## Your goals & talking points\n- **Retry when Notion is reachable** and rebuild from source.\n\n"
        "## Watch-outs\n- Do not invent context — nothing was retrieved.\n\n"
        "## Desired outcome\nNotion becomes reachable and a fact-based brief is produced."
    )


def brief_with_body(words: int) -> str:
    """A normal-looking (resolved) brief whose body is `words` words long."""
    head = (
        "# Meeting Brief — Acme (Jane Doe, CEO)\n\n"
        "**When:** Tue 30 Jun 2026 · **Who:** Jane Doe · **Purpose:** Renewal · "
        "**Sources:** Acme (account)\n\n---\n\n## Bottom line\n"
    )
    return head + " ".join(["alpha"] * words)


def assert_(cond, msg):
    if not cond:
        raise AssertionError(msg)


# --------------------------------------------------------------------------- #
# scenario 1 — search hard error / timeout: turn fails -> clean RuntimeError
# --------------------------------------------------------------------------- #
async def _scenario_search_hard_error():
    stream = [asst(tool(SEARCH, query="Meridian")), result(is_error=True, subtype="error_during_execution")]
    with fake_query(stream):
        raised = False
        try:
            await agent.draft_brief("Meridian", model="test")
        except RuntimeError as e:
            raised = True
            assert_("error" in str(e).lower(), f"unexpected message: {e}")
        assert_(raised, "hard search error must raise RuntimeError, not return a brief")


def test_search_hard_error_raises_cleanly():
    asyncio.run(_scenario_search_hard_error())


def test_search_hard_error_cli_exits_nonzero_without_crash():
    """The CLI must turn the failure into exit code 1, not an unhandled traceback."""
    stream = [asst(tool(SEARCH, query="x")), result(is_error=True, subtype="error_during_execution")]
    with tempfile.TemporaryDirectory() as d:
        out = str(Path(d) / "brief.md")
        with fake_query(stream):
            err = io.StringIO()
            with redirect_stderr(err):
                rc = cli.main(["Meridian", "--out", out])
        assert_(rc == 1, f"expected exit 1, got {rc}")
        assert_("error:" in err.getvalue().lower(), "CLI should print a clear error line")
        assert_(not Path(out).exists(), "no brief file should be written on hard failure")


# --------------------------------------------------------------------------- #
# scenario 2 — search errors but model degrades to an unresolved brief
# --------------------------------------------------------------------------- #
async def _scenario_search_error_degrades():
    brief = unresolved_brief("account search failed (Notion unavailable)")
    stream = [asst(tool(SEARCH, query="Meridian")), asst(text(brief)), result()]
    with fake_query(stream) as fq:
        res = await agent.draft_brief("Meridian", model="test")
        assert_(res.text.lstrip().startswith("# Meeting Brief"), "brief must start clean (no preamble)")
        assert_(res.unresolved is True, "must be flagged unresolved, not a normal brief")
        assert_(res.retried is False, "must NOT pad an unresolved brief via length-retry")
        assert_(res.wrote_to_notion is False, "must make zero write calls")
        assert_(res.body_words < agent.MIN_BODY_WORDS, "fixture is intentionally short")
        assert_(len(fq.calls) == 1, "no retry => query called exactly once")


def test_search_error_degrades_unresolved():
    asyncio.run(_scenario_search_error_degrades())


# --------------------------------------------------------------------------- #
# scenario 3 — search ok, fetch errors on the account page
# --------------------------------------------------------------------------- #
async def _scenario_account_fetch_error():
    brief = unresolved_brief("the account page could not be retrieved from Notion")
    stream = [
        asst(tool(SEARCH, query="Meridian")),
        asst(tool(FETCH, id="acct-url")),
        asst(text(brief)),
        result(),
    ]
    with fake_query(stream) as fq:
        res = await agent.draft_brief("Meridian", model="test")
        assert_(res.unresolved is True, "account-fetch failure must be flagged unresolved")
        assert_(res.retried is False, "must not pad")
        assert_(res.wrote_to_notion is False, "zero writes")
        assert_("could not retrieve" in res.text.lower(), "must surface a could-not-retrieve note")
        assert_(len(fq.calls) == 1, "no retry")


def test_account_fetch_error_degrades_unresolved():
    asyncio.run(_scenario_account_fetch_error())


# --------------------------------------------------------------------------- #
# scenario 4 — account resolves but Contacts/Meetings come back empty/error
# --------------------------------------------------------------------------- #
async def _scenario_empty_relations():
    brief = unresolved_brief("the account resolved but its Contacts and Meetings could not be retrieved")
    stream = [
        asst(tool(SEARCH, query="Meridian")),
        asst(tool(FETCH, id="acct-url")),
        asst(tool(FETCH, id="contact-1")),  # relation fetch that yields nothing
        asst(text(brief)),
        result(),
    ]
    with fake_query(stream) as fq:
        res = await agent.draft_brief("Meridian", model="test")
        assert_(res.unresolved is True, "empty-relations failure must be flagged unresolved")
        assert_(res.retried is False, "must not pad")
        assert_(res.wrote_to_notion is False, "zero writes")
        # did not fabricate contacts: the Who section is Unknown
        who = res.text.split("## Who you're meeting", 1)[1].split("##", 1)[0].lower()
        assert_("unknown" in who, "Who section must be Unknown, not invented contacts")
        assert_(len(fq.calls) == 1, "no retry")


def test_empty_relations_no_fabrication():
    asyncio.run(_scenario_empty_relations())


# --------------------------------------------------------------------------- #
# scenario 5 — model returns no brief at all -> clean RuntimeError
# --------------------------------------------------------------------------- #
async def _scenario_no_brief():
    stream = [asst(tool(SEARCH, query="x")), asst(text("Notion is down; I cannot produce anything.")), result()]
    with fake_query(stream):
        raised = False
        try:
            await agent.draft_brief("Meridian", model="test")
        except RuntimeError as e:
            raised = True
            assert_("no brief" in str(e).lower(), f"unexpected: {e}")
        assert_(raised, "no-brief output must raise RuntimeError, not return garbage")


def test_no_brief_raises_cleanly():
    asyncio.run(_scenario_no_brief())


# --------------------------------------------------------------------------- #
# scenario 6 — invariant guard: a stray write tool-use is detected
# --------------------------------------------------------------------------- #
async def _scenario_write_detected():
    stream = [
        asst(tool(UPDATE, id="acct-url", note="locked")),
        asst(text(unresolved_brief("write was attempted"))),
        result(),
    ]
    with fake_query(stream):
        res = await agent.draft_brief("Meridian", model="test")
        assert_(res.wrote_to_notion is True, "a write tool-use MUST be detected by the audit")


def test_write_attempt_is_detected():
    asyncio.run(_scenario_write_detected())


# --------------------------------------------------------------------------- #
# scenario 7 — regression: normal length-retry still fires exactly once
# --------------------------------------------------------------------------- #
async def _scenario_retry_once():
    long_stream = [asst(text(brief_with_body(400))), result()]
    fixed_stream = [asst(text(brief_with_body(300))), result()]
    with fake_query(long_stream, fixed_stream) as fq:
        res = await agent.draft_brief("Acme", model="test")
        assert_(res.retried is True, "an over-long resolved brief must trigger one retry")
        assert_(len(fq.calls) == 2, "retry must happen exactly once (draft + one tighten)")
        assert_(agent.MIN_BODY_WORDS <= res.body_words <= agent.MAX_BODY_WORDS, "retry must land in range")
        assert_(res.unresolved is False, "a normal brief is not unresolved")


def test_length_retry_fires_exactly_once():
    asyncio.run(_scenario_retry_once())


# --------------------------------------------------------------------------- #
# self-contained runner (works without pytest)
# --------------------------------------------------------------------------- #
def _all_tests():
    return [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]


def main() -> int:
    failures = 0
    for name, fn in _all_tests():
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as e:  # noqa: BLE001 - test runner surfaces all failures
            failures += 1
            print(f"FAIL  {name}\n        {type(e).__name__}: {e}")
    total = len(_all_tests())
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
