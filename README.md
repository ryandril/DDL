# DDL — a Canvas deadline agent

Course announcements bury deadlines in prose. "Please submit before the start of
next Tuesday's session" is a deadline; no LMS calendar will ever show it to you.

This is a small daily agent that reads your Canvas announcements, extracts the
deadlines hiding in the text, puts them on your Google Calendar, and pushes you a
digest. It has been running unattended, once a day, since May 2026.

It works with any Canvas instance, not just one school's. **It is ordinary Python
on a cron job** — no LLM API key, no agent runtime, no per-run cost.

## How it works

```
  ┌────────┐  ┌──────────┐  ┌────────────────────┐  ┌───────────────────┐
  │  cron  │─▶│ fetch.py │─▶│ extract_deadlines  │─▶│ deadline_ledger.py│
  │ daily  │  │ (Canvas  │  │ .py (deterministic │  │ (accumulate, drop │
  │        │  │  REST)   │  │  date extraction)  │  │  past, New/Exist) │
  └────────┘  └──────────┘  └────────────────────┘  └─────────┬─────────┘
                    │                 │                       │
                    ▼                 ▼                       ▼
            announcements.json   deadlines.json      out/ledger_view.json
                                                              │
                        ┌─────────────────────────────────────┤
                        ▼                                     ▼
                 ┌─────────────┐                     ┌────────────────┐
                 │ gcal_sync.py│                     │  build.py +    │
                 │ (idempotent │                     │  notify        │
                 │  upsert)    │                     │  (.ics/digest) │
                 └─────────────┘                     └────────────────┘
```

Three ideas do most of the work:

**Extraction is deterministic, and precision-first.** `extract_deadlines.py`
looks for a date, then checks whether a deadline cue (`due`, `deadline`,
`submit`, `hand in`, `turn in`) sits within ~75 characters of it. A date with no
nearby cue is ignored, so class times, office hours and grade-release dates never
become deadlines. Anti-cues (`office hour`, `class starts`, `holiday`, `grade`)
drop a match even when a stray cue is nearby. It would rather miss a deadline
than invent one.

**The ledger is the source of truth.** Canvas only returns *new* announcements
each run, so a deadline extracted today would vanish tomorrow.
`deadline_ledger.py` accumulates every extracted deadline across runs into
`deadlines_ledger.json`, drops the ones that have passed, and labels the rest
**New** (first seen this run) or **Existing** (carried over). Your daily message
therefore shows the full upcoming list every day, never a stale one.

**Ordering is chosen for crash-safety.** Calendar sync runs off the durable
ledger every day, not off the usually-empty per-run extraction, so a missed or
failed day self-heals on the next run. And `build.py` — which marks
announcements as *seen* — runs only *after* the ledger has persisted. Doing it
the other way round meant a crash between the two steps lost a deadline forever:
seen, but never saved.

| File | Role |
|---|---|
| `cron_ddl.sh` | The whole pipeline. This is what cron runs. |
| `fetch.py` | Canvas REST. Pulls new announcements, strips HTML, dedupes against `state.json`. |
| `extract_deadlines.py` | Deterministic date extraction (see above). |
| `deadline_ledger.py` | Durable ledger; prunes past deadlines, classifies New vs Existing. |
| `build.py` | Builds `deadlines.ics` (with −24h/−1h alarms), HTML + plaintext digest, subject line. Commits seen-IDs. |
| `gcal_sync.py` | Upserts into Google Calendar with stable iCalUIDs, so re-runs update rather than duplicate. |
| `serverchan_push.py` | Phone push via [Server酱](https://sct.ftqq.com/) (WeChat). The default notifier. |
| `send_wechat_alert.py` | The author's richer notifier (OpenClaw + `gog`). Optional — see Notifications. |
| `auth.py` | One-time Google OAuth consent flow. |

## Requirements

- Python 3.11+
- A Canvas account with API access
- A Google account (only if you want calendar sync)
- Somewhere to run a daily cron job — a cheap VPS, or your own laptop

## Setup

### 1. Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

### 2. Canvas token

In Canvas: **Account → Settings → Approved Integrations → + New Access Token**.
Copy the token — Canvas shows it exactly once.

```bash
cp .env.example .env
```

Then edit `.env` and set `CANVAS_BASE_URL` to your school's Canvas host and
`CANVAS_TOKEN` to the token you just generated.

Check it works without touching anything else:

```bash
.venv/bin/python fetch.py --lookback-days 14 -v
```

You should see announcements written to `out/announcements.json`. If your network
requires a proxy, set `CANVAS_USE_SYSTEM_PROXY=1` in `.env`.

### 3. Google Calendar (optional)

Skip this and you still get the digest and the `.ics` file; you just won't get
automatic calendar sync.

1. In the [Google Cloud Console](https://console.cloud.google.com/), create a
   project and enable the **Google Calendar API**.
2. **APIs & Services → Credentials → Create credentials → OAuth client ID**,
   application type **Desktop app**.
3. Download the JSON and save it in this folder as `credentials.json`.
4. Run the one-time consent flow:

```bash
.venv/bin/python auth.py
```

This opens a browser, asks you to approve calendar access, and writes
`token.json`. Both files are gitignored — never commit them.

Then create the calendar and do a dry run:

```bash
.venv/bin/python gcal_sync.py --create-calendar --dry-run -v
```

### 4. Notifications (optional)

For a phone push, get a SendKey from [Server酱](https://sct.ftqq.com/) and set
`SERVERCHAN_SENDKEY` in `.env`. That's the default and needs nothing else.

To use a different sender, set `NOTIFY_CMD` in `.env` to any command that
delivers `out/subject.txt` and `out/digest.txt`. `send_wechat_alert.py` is the
author's own notifier — a richer message rendered from the ledger, with an email
fallback — but it needs [OpenClaw](https://docs.openclaw.ai) and the `gog` Google
CLI, so treat it as a worked example rather than something that runs out of the
box.

## One full run by hand

```bash
.venv/bin/python fetch.py --lookback-days 14 -v   # pull announcements
.venv/bin/python extract_deadlines.py --out-dir out   # find the deadlines
.venv/bin/python deadline_ledger.py --out-dir out     # merge + prune + classify
.venv/bin/python build.py -v                          # digest + .ics
.venv/bin/python gcal_sync.py --deadlines out/ledger_view.json -v
```

To try it without hitting Canvas at all:

```bash
.venv/bin/python fetch.py --dry-run-fixtures tests/fixtures/sample_announcements.json -v
```

The sample announcements are dated May 2026, so if you run the full sequence
against them the digest will show the four announcements but **no upcoming
deadlines, and no `.ics`** — the extracted deadline has already passed and the
ledger pruned it. That is the pruning working, not a failure. To see what the
extractor actually found, look at `out/deadlines.json`:

```bash
cat out/deadlines.json   # 1 deadline: 2026-05-15T17:00:00, confidence "high"
```

## Scheduling it

`cron_ddl.sh` runs all of the above, in the right order, with the failure
handling described above. Point cron at it:

```
0 9 * * * /path/to/ddl/cron_ddl.sh >> /path/to/ddl/cron.log 2>&1
```

It finds its own directory, so you don't need to edit any paths. Pick whatever
hour you like — early enough that a same-day deadline is still actionable.

Every step is best-effort and the script deliberately does **not** use `set -e`:
it always reaches the notify step, so a failed run still messages you instead of
failing silently. If extraction fails, the announcements stay unseen and the next
run retries them.

## Configuration

Everything lives in `.env`:

| Variable | Required | Meaning |
|---|---|---|
| `CANVAS_BASE_URL` | yes | Your Canvas host, e.g. `https://canvas.example.edu` |
| `CANVAS_TOKEN` | yes | Canvas personal access token |
| `TIMEZONE` | yes | IANA name, e.g. `Asia/Shanghai`. Deadlines resolve against this. |
| `CANVAS_USE_SYSTEM_PROXY` | no | Set to `1` if your network needs the system proxy |
| `DIGEST_LABEL` | no | Prefix on the subject and push message (default `Canvas`) |
| `CALENDAR_NAME` | no | Google Calendar to sync into (default `Canvas Deadlines`) |
| `SERVERCHAN_SENDKEY` | no | Server酱 SendKey for the default push; blank disables it |
| `NOTIFY_CMD` | no | Override the notifier entirely |
| `DDL_DIR` | no | Override the working directory (defaults to the script's own) |

`send_wechat_alert.py` additionally reads `GOG_ACCOUNT`, `ALERT_EMAIL_TO`,
`WECHAT_ACCOUNT`, `WECHAT_TARGET`, `WHATSAPP_TARGET` and
`OPENCLAW_ACCOUNTS_DIR`. All are blank by default.

## Outputs

| File | Contents |
|---|---|
| `deadlines_ledger.json` | **Durable** — every deadline ever extracted, past ones pruned |
| `out/ledger_view.json` | This run's upcoming list, classified New vs Existing |
| `out/announcements.json` | Canvas data for this run |
| `out/deadlines.json` | Just this run's extraction (ephemeral, often empty) |
| `out/digest.html` / `digest.txt` / `subject.txt` | The message |
| `out/deadlines.ics` | Importable calendar file |
| `state.json` | Which announcements have been seen; delete to force a re-pull |

All are gitignored.

## Confidence levels

Extraction is the one genuinely uncertain step, so every deadline is tagged:

- **high** — an explicit clock time was stated (`due Friday, May 15 at 5:00pm`)
- **medium** — a date with no time, resolved to 23:59
- **low** — vague phrasing

Conventions: `noon` → 12:00; `midnight` / `EOD` → 23:59; a date with no time hint
→ 23:59. Medium and low items are flagged in the digest and the calendar entry,
so you know which to verify. **Treat this as an assistant, not as the authority
on when your work is due** — the announcement is.

### Known limitation: relative dates

The extractor recognises calendar dates — `May 15`, `15 May`, `2026-05-15` — but
**not weekday names or relative phrasing**. "Due this Monday by EOD" and "by next
Wednesday" are currently missed, even though they're common in announcements.

This is a deliberate trade, not an oversight: an earlier version resolved
relative dates with an LLM, which meant an API key and an agent runtime for every
user. The deterministic extractor removed that dependency and lost this with it.
Resolving weekday names against each announcement's `posted_at` is the obvious
next addition — the timestamp needed for it is already in `announcements.json`.

Until then, the digest lists every announcement it saw, so a missed relative
deadline is still visible to you as an announcement — just not as a calendar
entry.

## Tests

```bash
.venv/bin/python -m pytest -q
```

47 tests, all fixture-based: no network, no credentials, no API calls. They
cover the two pieces that decide whether a deadline is right — the extractor's
cue/anti-cue rules and time conventions, and the ledger's accumulate / prune /
New-vs-Existing behaviour — plus the digest and `.ics` output.

CI runs them on Python 3.11–3.13, shellchecks `cron_ddl.sh`, and fails the build
if a credential or personal identifier ever lands in a tracked file.

## Licence

MIT.
