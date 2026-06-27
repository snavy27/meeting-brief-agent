# Meeting Brief Agent

A Python agent built on the [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/python)
that drafts one-page meeting briefs.

**Phase 2 (current):** *agentic over the Notion CRM.* Give it just an account name or
meeting subject and it searches Notion itself, follows the account's Contact and Meeting
relations, and drafts the brief — read-only, never writing to Notion.

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

```bash
python main.py "Meridian" --out brief.md
```

- `target` (positional) — account name or meeting subject to brief on.
- `--out`, `-o` — where to write the brief. Defaults to `brief.md`.
- `--model`, `-m` — model alias (`opus`, `sonnet`, `haiku`) or full ID.
  Defaults to `opus`; can also be set with the `BRIEF_AGENT_MODEL` env var.

Examples:

```bash
python main.py "Orbit Telecom"                 # default Opus -> brief.md
python main.py "Meridian" --model sonnet -o m.md
BRIEF_AGENT_MODEL=sonnet python main.py "Meridian"
```

The brief is written to the `--out` file. An audit trail prints to stderr: body word
count, the Notion tools called, whether a length retry ran, and a confirmation that
**zero** write tools were used.

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
                                      └─ length validate-and-retry (≤1 rewrite to 250–350 words)
```

### Read-only safety

- `allowed_tools` is whitelisted to `notion-search` and `notion-fetch` only.
- Every Notion write tool (`create_pages`, `update_page`, `move_pages`, `duplicate_page`,
  `create_database`, `update_data_source`, `create_view`, `update_view`, `create_comment`)
  is in `disallowed_tools` as well — defense in depth. The agent can never modify Notion.
- `BriefResult.wrote_to_notion` is asserted `False`; the CLI errors out if a write is ever seen.

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
