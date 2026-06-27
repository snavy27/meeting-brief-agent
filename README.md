# Meeting Brief Agent

A Python agent built on the [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/python)
that drafts one-page meeting briefs.

**Phase 2:** *agentic over the Notion CRM.* Give it just an account name or meeting subject and
it searches Notion itself, follows the account's Contact and Meeting relations, and drafts the
brief — read-only, never writing to Notion.

**Phase 4 (current):** *calendar-driven daily packet.* Point it at a day and it reads your
calendar (read-only), skips internal-only meetings, and drafts one brief per external meeting —
**centered on the specific person you're meeting**, not just their company. The one-page format is
unchanged; the calendar context only sharpens the existing sections.

## Requirements

- **Python 3.10+**
- **Node.js** (the SDK runs the Claude Code CLI under the hood)
- **Claude Code CLI** installed and logged in (`claude /login`). The agent uses your
  Claude Code session for auth, so no `ANTHROPIC_API_KEY` is required.
- **Notion connected in Claude Code.** This agent reuses your existing `claude.ai Notion`
  connector (check with `claude mcp list`). No Notion token or extra setup is needed.

## Setup

```bash
pip install -r requirements.txt
```

## Usage

### Single brief (Phase 2)

```bash
python main.py "Meridian" --out brief.md
```

- `target` (positional) — account name or meeting subject to brief on.
- `--out`, `-o` — where to write the brief. Defaults to `brief.md`.
- `--model`, `-m` — model alias (`opus`, `sonnet`, `haiku`) or full ID.
  Defaults to `opus`; can also be set with the `BRIEF_AGENT_MODEL` env var.

### Daily packet (Phase 4)

```bash
python main.py --calendar --date 2026-06-29 --out day.md
```

- `--calendar` — read the calendar for a day and brief every external meeting.
- `--date` — `today` | `tomorrow` (default) | `YYYY-MM-DD`.
- `--out` — output path (defaults to `day.md` in calendar mode).

The packet leads with a header (`N meetings · X briefed / Y unresolved / Z skipped`), lists the
skipped internal meetings, then the briefs in start-time order. Internal-only meetings (no
external attendee) are skipped; external meetings with no CRM match get a minimal calendar-only
**stub** that invents nothing. Each brief is centered on the actual attendee — when a company has
several CRM contacts, the brief leads with the one in the meeting and treats the others as
background. Attendees are matched to CRM contacts by **name + company**, not by exact email.

Examples:

```bash
python main.py "Orbit Telecom"                 # single brief, default Opus -> brief.md
python main.py "Meridian" --model sonnet -o m.md
python main.py --calendar                      # tomorrow's packet -> day.md
python main.py --calendar --date today -m sonnet
```

The output is written to `--out` (plus a `*.sources.json` provenance sidecar). An audit trail
prints to stderr, ending in a confirmation that **zero** write tools were used (Notion in single
mode; Notion **and** Calendar in calendar mode).

## How it works

```
main.py → brief_agent/cli.py → brief_agent/agent.py (draft_brief)
                                      │
                                      ├─ claude_agent_sdk.query() — AGENTIC loop
                                      │     system prompt: gather-from-Notion contract
                                      │                    + format spec  (brief_agent/prompt.py)
                                      │     tools (read-only): notion-search, notion-fetch
                                      │       1. search Accounts DS → resolve to one account
                                      │       2. fetch the account page (properties + body)
                                      │       3. follow Contacts + Meetings relations
                                      │       4. draft the brief from only those pages
                                      │
                                      └─ length validate-and-retry (≤2 rewrites to 250–350 words)
```

In calendar mode, `brief_agent/calendar.py` reads the day's events (read-only) and
`brief_agent/daily.py` batches the engine above over each external meeting, ordering the results
into one packet. A meeting-aware gather (`SYSTEM_PROMPT_NOTION_MEETING` in `prompt.py`) centers
each brief on the specific attendee — the output format is identical to Phase 2.

### Read-only safety

- `allowed_tools` is whitelisted to `notion-search`/`notion-fetch` (and, for the calendar
  adapter, the calendar list/get tools) only.
- Every Notion **and** Calendar write tool is in `disallowed_tools` as well — defense in depth.
  The agent can never modify Notion or the calendar.
- `permission_mode="dontAsk"` denies anything not allow-listed without prompting (non-interactive).
- `BriefResult.made_any_write` / `DayPacket.made_any_write` are asserted `False`; the CLI and both
  eval suites error out if any write to either connector is ever seen.

### Notes on the CRM

The CRM is three linked Notion databases — Accounts, Meetings, Contacts (data source IDs
live in `brief_agent/prompt.py`). The agent gathers context via **search → fetch →
follow relations**; it does **not** use `query_data_sources`/SQL (that requires a Notion
Business plan + AI, which isn't assumed here).

### Honesty rule

If a fact isn't in Notion, the brief marks it `Unknown` rather than inventing it. If the
input doesn't resolve to an account, the brief says so in the metadata line instead of
fabricating one — a CEO acting on a made-up detail is the worst failure mode.

## The brief format

Title → metadata line → Bottom line → Who you're meeting → What's changed since you last
spoke → Likely to come up → Your goals & talking points → Watch-outs → Desired outcome.
~250–350 words, one page. Full spec in `brief_agent/prompt.py`.
