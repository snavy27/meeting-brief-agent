# Meeting-Brief Agent

Auto-drafts one-page meeting-prep briefs for a CEO. Each morning it reads the day's
calendar, pulls the right person and company from a Notion CRM, enriches with recent
public web news, drafts a one-page brief per external meeting, renders the day's
packet to PDF, and emails it. Runs on a schedule and on demand. Headless (Claude API).

Built on the Claude Agent SDK. Honesty-first: read-only access to your data, every
claim traceable to a source, missing facts marked Unknown, never fabricated.

---

## What it does

Given a day, for each meeting on the calendar it:

1. Resolves the external attendee to one CRM account **and the exact person** (not just
   the company — companies have several contacts).
2. Gathers context: that person's role/style and prior interactions, the account's
   situation, recent web news about the company.
3. Drafts a fixed-format one-page brief (the format lives in `prompt.py` — the source
   of truth; do not edit casually).
4. Skips internal-only meetings; for meetings with no CRM match, emits a clearly
   labelled calendar-only stub (never an invented brief).

Output: one combined daily packet (PDF), plus a `.sources.json` sidecar tying each
brief to its calendar event and the exact Notion pages + web URLs used.

---

## Setup (one time)

### 1. Python
```
pip install -r requirements.txt
```

### 2. Claude API key
The agent authenticates via the Claude API (headless — no Claude Code login).
```
ANTHROPIC_API_KEY=sk-ant-...
```

### 3. Notion CRM (read-only)
- Create an internal integration at notion.so/my-integrations → **Access token** auth.
  Read-content capability is enough (leave insert/update/delete off).
- Share all three databases with the integration (open each → ••• → Connections):
  **Accounts**, **Meetings**, **Contacts**.
```
NOTION_TOKEN=ntn_...
```

### 4. Google Calendar (read-only, headless)
- Google Cloud Console → enable **Google Calendar API**.
- Create a **service account**, add a **JSON key**, save it as `service-account.json`
  in the project root (gitignored).
- Share your calendar (Calendar → ••• → Settings and sharing → Share with specific
  people) with the service account's email, permission **"See all event details"**.
```
GOOGLE_SERVICE_ACCOUNT_FILE=service-account.json
GOOGLE_CALENDAR_ID=you@example.com
```

### 5. Email delivery (the only outbound action)
SMTP (e.g. a Gmail App Password) or an email API. Credentials in `.env`, never logged.
```
SMTP_HOST=...
SMTP_PORT=587
SMTP_USER=...
SMTP_PASS=...
BRIEF_RECIPIENT=you@example.com
```

> All secrets live in `.env` (gitignored). `service-account.json` and `*.key` are
> gitignored too. Never commit credentials.

---

## Run it now

The scheduled job and the manual run are the **same command** — the scheduler just
runs `python scheduled_run.py` on a timer. To test without waiting, run it yourself:

```
# Today's packet (Europe/Paris), emailed as a PDF — exactly what cron runs
python scheduled_run.py

# Any specific day, on demand
python scheduled_run.py --date 2026-06-29

# Build + save the PDF under out/, do NOT send (errors print to the console)
python scheduled_run.py --no-email

# One-off: send to a different recipient
python scheduled_run.py --to someone@example.com
```

The PDF (plus the packet `.md` and a `.sources.json` provenance sidecar) is written to
`out/day-<date>.*` on every run, then emailed unless `--no-email` is given. Re-running a
day overwrites those files, so the job is safe to re-run.

Make targets wrap the same command:
```
make brief                       # today, email the PDF
make brief-local                 # save locally, skip send
make brief-date DATE=2026-06-29  # a specific day, email it
```

Single-meeting / single-day generation (no email) still works via the core CLI:
```
python main.py "Target Corporation" --out brief.md
python main.py --calendar --date 2026-06-29 --out day.md
```

Model is configurable; default **Opus** (production bar). `--model sonnet` is cheaper
for iteration but runs near the length limit — keep Opus for real briefs.

---

## Schedule it

The job is a plain CLI command; point any scheduler at it.

**Recommended — GitHub Actions** (no always-on machine needed). The committed workflow
`.github/workflows/daily-brief.yml` runs daily and can be triggered manually
(`workflow_dispatch`). Add these repo **Secrets**: `ANTHROPIC_API_KEY`, `NOTION_TOKEN`,
`GOOGLE_CALENDAR_ID`, `GOOGLE_SERVICE_ACCOUNT_JSON` (the whole key file's contents),
`SMTP_USER`, `SMTP_PASS`, `BRIEF_RECIPIENT`. The workflow writes the service-account JSON
from its secret, runs the job, and deletes it afterwards.

GitHub cron is **UTC**: the workflow uses `30 4 * * *` ≈ **06:30 Europe/Paris** in summer
(CEST); in winter (CET) the same line fires at 05:30 local. The job always computes
"today" in Europe/Paris, so the *date* is correct year-round — only the send time shifts
by an hour across DST. Edit the cron line for an exact local time.

**Equivalents:**
- **cron** (always-on host): `30 4 * * * cd /path/to/repo && /usr/bin/python scheduled_run.py`
- **Windows Task Scheduler**: a Daily trigger running
  `python C:\path\to\scheduled_run.py` with "Start in" set to the repo folder.

> A self-hosted scheduler needs an always-on host — a laptop asleep at 6:30am silently
> misses the run. GitHub Actions avoids that.

---

## Failure handling

Every run is wrapped so it **never fails silently**. On any error — API/network, a
429 after retries, render or send failure, empty day — you get a **failure email**
with the error summary, and the process exits non-zero. If you get a failure email:

| Symptom | Likely cause | Fix |
|---|---|---|
| "auth"/401 | API key missing/expired | check `ANTHROPIC_API_KEY` in `.env` |
| Notion empty / "not found" | DB not shared with integration | re-share Accounts/Meetings/Contacts |
| Calendar empty | wrong calendar ID, or share not propagated | verify `GOOGLE_CALENDAR_ID`; wait 5–10 min after sharing |
| 429 / rate limit | transient | usually self-resolves on the built-in retry; re-run if needed |
| No email arrived | SMTP creds or spam | check SMTP_* vars; check spam folder |

---

## Guarantees (don't weaken these)

- **Read-only.** The agent never writes to Notion or the calendar. Email is the only
  outbound action. The test suite asserts 0 writes on every run.
- **Honesty.** A fact ships only if a source supports it (CRM page or web URL); else
  it's omitted or marked Unknown. CRM is the sole source of truth for *who* you're
  meeting — web is never used to verify or "correct" contacts.
- **Format is fixed.** `prompt.py` holds the brief spec and section order. Changing it
  invalidates the eval baselines — re-run evals if you touch it.

---

## Tests / evals

```
# Offline unit tests (no network, no creds) — includes the delivery layer
python -m pytest -q tests/

# Model-backed eval suites (require ANTHROPIC_API_KEY + NOTION_TOKEN)
python -m eval.run_eval            # account resolution (7/7)
python -m eval.run_eval_calendar   # daily packet
python -m eval.run_eval_web        # web enrichment (4/4)

# Live smoke tests (real network, non-gating)
RUN_LIVE_NOTION=1 python tests/test_notion_injection_live.py
```

The delivery layer has its own offline tests: `tests/test_pdf_unit.py` (renders the
packet to a valid PDF, Unicode-safe) and `tests/test_mailer_unit.py` (asserts one
recipient, the PDF attachment, and that the password never appears in output).

Baselines live in `eval/results/`. Opus is the regression bar (7/7). Web and live
calendar are mocked in the gate so it stays deterministic; live paths are separate,
non-gating checks.

---

## Project layout

```
brief_agent/
  prompt.py         the brief format/spec (source of truth — don't edit casually)
  agent.py          the per-meeting draft loop (gather -> draft -> length retry)
  daily.py          calendar batching -> daily packet markdown
  calendar.py       pure event parser (gcal.py = read-only service-account fetch)
  notion_mcp.py     in-process read-only Notion MCP server (CRM)
  web.py            Phase 5 web enrichment
  pdf.py            packet markdown -> PDF (fpdf2)
  mailer.py         send-only email (one recipient, STARTTLS)
  config.py         headless credential loading + fail-fast
  cli.py            single brief / daily packet (writes .md, no email)
scheduled_run.py    the scheduled & on-demand entry point: packet -> PDF -> email
.github/workflows/daily-brief.yml   scheduled GitHub Actions job
Makefile            make brief / brief-local / brief-date shortcuts
eval/               eval runner + results/ baselines
tests/              unit + resilience + live smoke tests
out/                generated packets/PDFs (gitignored)
.env / .env.example credentials (real / placeholders)
service-account.json  Google service-account key (gitignored)
```

---

## Known limits

- Sonnet occasionally runs ~1 word over the length cap and had one grounding slip on
  the harder calendar task; **Opus is clean** — use it for production.
- Web enrichment is company-level news only, last ~6 months.
- Contacts are your private CRM records; they intentionally won't match a real
  company's public execs, and the agent won't reconcile them against the web.
