# DDL — a Canvas deadline agent

Course announcements bury deadlines in prose. "Please submit before the start of
next Tuesday's session" is a deadline, and no LMS calendar will ever show it to
you: Canvas only surfaces dates that someone entered into a due-date field, and
the dates that actually govern your week are written in paragraphs. DDL reads
your Canvas announcements once a day, extracts the deadlines hiding in the text,
puts them on your Google Calendar, and sends you a digest — through whatever
notifier you point it at, falling back to email so a run never ends with you
uninformed. It is ordinary Python on a cron job — no LLM API key, no agent
runtime, no per-run cost — and it has been running unattended, daily, since
May 2026.

This README is written as a case study rather than a manual. Almost everything
structural in the code is scar tissue from a specific failure in production, and
the failures are more useful than the feature list. Setup instructions are at the
end.

## Architecture

```
  ┌────────┐  ┌──────────┐  ┌────────────────────┐  ┌───────────────────┐
  │  cron  │─▶│ fetch.py │─▶│ extract_deadlines  │─▶│ deadline_ledger.py│
  │ daily  │  │ (Canvas  │  │ .py (deterministic │  │ (accumulate, drop │
  │        │  │  REST)   │  │  date extraction)  │  │  past, New/Exist) │
  └────────┘  └──────────┘  └────────────────────┘  └─────────┬─────────┘
                    │                 │                       │
                    ▼                 ▼                       ▼
            announcements.json   deadlines.json      deadlines_ledger.json
                 (per run)         (per run)            (DURABLE)
                                                              │
                                                              ▼
                                                    out/ledger_view.json
                                                              │
                        ┌─────────────────────────────────────┤
                        ▼                                     ▼
                 ┌─────────────┐                     ┌────────────────┐
                 │ gcal_sync.py│                     │  build.py +    │
                 │ (idempotent │                     │  $NOTIFY_CMD   │
                 │  upsert)    │                     │  (.ics/digest) │
                 └─────────────┘                     └────────────────┘
```

The shape to notice: the two per-run files in the middle are ephemeral and
usually empty, and **nothing downstream is allowed to depend on them**. Every
consumer reads the durable ledger instead. Most of the failures below are
variations on having gotten that wrong once.

---

## What broke in production, and what fixed it

### 1. The sync trusted event state instead of a durable ledger

**Symptom.** Deadlines that had been announced and correctly extracted were
missing from Google Calendar days later, with no error in the log.

**Cause.** Canvas returns only *new* announcements per request, so
`out/deadlines.json` holds one day's extraction and is empty on most days.
`gcal_sync.py` was wired to run only on days that produced new announcements,
and to read that per-run file. Every run therefore asked "what did I learn
today?" and pushed only that. If a run failed — a network blip, an expired
token, a crash mid-pipeline — the deadline it had learned that day was never
pushed, and no later run would ever revisit it, because by then the
announcement was no longer new. One bad day meant a deadline was silently lost
forever.

**Structural fix.** `deadline_ledger.py` accumulates every extracted deadline
across all runs into `deadlines_ledger.json`, prunes the ones that have passed,
and writes `out/ledger_view.json`. Calendar sync now runs **every day** off that
durable view, never off the per-run extraction, and upserts idempotently on a
stable uid. The invariant changed from "push what changed" to "make the
calendar match the ledger" — so a missed or failed day self-heals on the next
run instead of losing data permanently. The same reasoning drives the digest:
it shows the full upcoming list every day, labelled **New** (first seen this
run) or **Existing** (carried over), rather than only the day's news.

The digest took longest to bring in line. Even after the ledger existed,
`build.py` still read the per-run `out/deadlines.json`, and cron only ran it on
days that produced new announcements — so on a quiet day the digest files on
disk were simply left over from the last announcement-bearing day. That is
invisible while notification is opt-in and actively wrong once a notifier fires
daily: the user gets last week's deadlines re-sent as if they were today's.
`build.py` now reads `out/ledger_view.json` and runs every day. The first file
that exists wins outright, so an empty ledger view renders as "nothing
upcoming" rather than falling through to whatever the per-run file still held.

The ordering in `cron_ddl.sh` is part of the same fix. `build.py` marks
announcements as *seen* in `state.json`, and it runs **after** the ledger has
persisted. The other way round, a crash between the two steps left an
announcement seen but its deadline never saved — the exact permanent-loss
failure, reintroduced through the back door. Building the digest and committing
seen-IDs are now separate concerns: the build runs unconditionally, while the
state commit stays gated on a successful extraction (`--no-state-update`
otherwise), so announcements that were never processed stay unseen and the next
run retries them.

A related instance of the same class: the original implementation authenticated
to Google with its own OAuth token, minted by a consent flow in the repo. That
project's consent screen sat in "Testing" publishing status, so Google expired
the refresh token roughly weekly and the cron died with `invalid_grant` until a
human re-ran the flow — three times before the pattern was obvious. State that
needs a human to refresh it is not durable state. Calendar access now goes
through the host's already-durably-authed `gog` CLI, so there is no second
credential to expire, and the consent flow has been deleted rather than left
around to mislead.

### 2. Every deadline was compared in the wrong timezone

**Symptom.** Deadlines near midnight were occasionally classified wrong — pruned
as passed while still live, or still shown as upcoming after they had gone.

**Cause.** `classify()` resolved *every* record's wall-clock time against a
single ledger-wide default timezone. That is fine while every announcement comes
from one school in one zone, and quietly wrong the moment it doesn't. A wall time
of `23:30` means different instants in Asia/Shanghai and America/New_York, and
the error is silent: the comparison still succeeds, it just returns the wrong
answer by up to a working day.

**Structural fix.** Timezone became a property of the record, not of the run.
Each extracted deadline carries its own `timezone` field from the moment it is
created, and `classify()` builds a per-record `ZoneInfo` to compare against, with
the ledger default demoted to a fallback for records that lack one. The general
lesson: a value that varies per row does not belong in run-level configuration.

`build.py` was carrying the identical bug for longer — it compared every
deadline against the *announcement payload's* timezone when deciding what was
recent enough to show. Fixed the same way. Worth noting that the second instance
survived the first fix by months: fixing a bug at one call site is not the same
as fixing the class of bug.

### 3. A rescheduled deadline left its old date behind

**Symptom.** The digest showed the same deliverable twice, on two different
dates, after an instructor edited an announcement to push a deadline back.

**Cause.** The ledger's identity for a deadline is `course | datetime | title`,
which is correct for deduplication — two distinct deliverables due at the same
moment must stay separate — but it makes a *rescheduled* deadline a different
entity from its former self. Merging the new extraction simply added a second
record, and nothing ever removed the first.

**Structural fix.** A coarser grouping key, `_group()` = `course | announcement
id | title`, deliberately excluding the datetime. All records in one group are
the same deliverable as stated by one announcement, so when that announcement is
re-extracted, the incoming batch's set of datetimes is the truth and stored
datetimes outside it are superseded and dropped.

Crucially the supersede is scoped to the *same announcement id only*. A
same-titled deadline announced separately elsewhere may well be a second real
deliverable, and silently dropping a real deadline is a far worse failure than
showing a duplicate. Those cross-announcement cases get a
`_possible_reschedule_of` flag instead — surfaced to the reader, decided by the
reader. The flag is recomputed from scratch each run, so it clears itself once
one of the pair passes.

### 4. The calendar's identity for a deadline ignored the due time

**Symptom.** An announcement listing two deliverables produced two ledger
records and only **one** calendar event.

**Cause.** The stable uid was `announcement id + sha256(title)`. The ledger keyed
on `course | datetime | title`; the calendar keyed on title alone. Two records
the ledger deliberately kept apart therefore collapsed to one uid, and the
idempotent upsert — working exactly as designed — treated the second as an update
of the first and overwrote it. No error, no duplicate, just one deadline quietly
gone. Two components each behaved correctly under their own definition of
identity; the bug lived in the disagreement between them.

**Structural fix.** The uid now hashes `title | datetime_local`, so calendar
identity and ledger identity agree by construction. Events created under the old
scheme are adopted rather than duplicated: `upsert` falls back to the legacy uid,
and `pop`s it rather than reading it, so the ambiguous old key can be claimed by
at most one record per run and anything else sharing it correctly gets its own
event.

### 5. The extractor named deadlines after the wrong deliverable

**Symptom.** "Case analysis due 5pm Nov 3. Peer evaluation due Nov 10." produced
two deadlines both titled *Peer Evaluation* — and, through failure 4, a single
calendar event.

**Cause.** An earlier fix had already established that a clock time must not
bleed across a neighbouring date, and clamped the *time* search window at
adjacent date spans. The *title* search kept using the raw ±75/45-character
window and picked the nearest deliverable phrase by character distance — which,
in that sentence, is the one belonging to the next deadline, by a single
character. The fix had been applied to one consumer of the window and not the
other.

**Structural fix.** Titles are resolved within sentence boundaries: a deliverable
named in a neighbouring sentence describes a different deliverable, whatever the
character distance says. This also cleared a downstream false alarm — two
identically-titled deadlines were being flagged as possible reschedules of each
other (failure 3's mechanism, firing on failure 5's bad data).

The same pass tightened three precision leaks that contradicted the extractor's
own precision-first contract. Dates inside URLs (`.../files/2026-05-15/spec.pdf`)
became deadlines; URLs are now blanked before matching, length-preserved so every
span offset still lines up. Past-tense cues ("the report **was due** May 15")
became live deadlines; a cue preceded by a past auxiliary no longer counts.
Withdrawn deadlines ("the May 15 deadline **is cancelled**") became deadlines; a
cancellation in the sentence is now a hard drop that a nearby "due" cannot
override.

### 6. Cron ran a different Python than the setup instructions

**Symptom.** Every scheduled run failed with `ModuleNotFoundError`, while running
the identical commands by hand worked perfectly.

**Cause.** The setup instructions build a virtualenv; the cron entry invoked bare
`python3`, which under cron's minimal environment resolves to the system
interpreter with none of the dependencies. Manual verification could never
reproduce it, because an interactive shell has a different `PATH`.

**Structural fix.** `cron_ddl.sh` resolves its own interpreter — the project
virtualenv if the documented setup was followed, system `python3` otherwise —
and resolves its own directory, so there are no paths to edit and no environment
to get right. Anything a scheduled job needs from its environment, it should
derive rather than inherit.

---

## Design decisions this left behind

**Extraction is deterministic and precision-first.** `extract_deadlines.py` finds
a date, then requires a deadline cue (`due`, `deadline`, `submit`, `hand in`,
`turn in`) within ~75 characters. A date with no nearby cue is ignored, so class
times, office hours and grade-release dates never become deadlines. It would
rather miss a deadline than invent one — a missed deadline is still visible to
you as an announcement in the digest, whereas an invented one erodes trust in
every entry on the calendar.

**Every step is best-effort.** `cron_ddl.sh` deliberately does not use `set -e`.
A failed step must not prevent the run from reaching the notify step, because a
message that says "fetch failed, here are your last-known deadlines" is far more
useful than silence.

**Confidence is stated, not hidden.** Extraction is the one genuinely uncertain
step, so every deadline is tagged **high** (an explicit clock time was stated),
**medium** (a date with no time, resolved to 23:59), or **low** (vague phrasing).
Medium and low items are flagged in the digest and in the calendar entry.
Conventions: `noon` → 12:00; `midnight` / `EOD` → 23:59; no time hint → 23:59.
**Treat this as an assistant, not as the authority on when your work is due** —
the announcement is.

## What it still gets wrong

**Relative dates are not supported.** "Due this Monday by EOD" and "by next
Wednesday" are missed. This is a deliberate trade: an earlier version resolved
them with an LLM, which meant an API key and an agent runtime for every user. The
deterministic extractor removed that dependency and lost this with it. Resolving
weekday names against each announcement's `posted_at` is the obvious next
addition — the timestamp it needs is already in `announcements.json`.

**The cue window is ~75 characters.** A deadline whose cue is separated from its
date by a long subordinate clause is missed. Widening the window trades away the
precision that makes the output trustworthy, so it stays narrow.

**Ambiguous numeric dates are a coin flip.** `15/05` resolves itself — 15 is not
a month — but nothing in the text can settle `5/6`. Set `DATE_ORDER` to `MDY`
(default) or `DMY`.

**Calendar sync needs the `gog` CLI.** See setup step 3. It is the one piece
that is not `pip install`-able from this repo.

**Retrieving a linked pre-reading is not wired up.** `canvas_client.py` has the
Canvas files API (`search_files`, `download_file`), but nothing calls it, and
`fetch.py` strips anchor `href`s when it converts announcement HTML to text — so
an agent asked to "go get that pre-reading" sees the filename and no link. The
primitives are there; the plumbing is not.

## The files

| File | Role |
|---|---|
| `cron_ddl.sh` | The whole pipeline. This is what cron runs. |
| `fetch.py` | Canvas REST. Pulls new announcements, strips HTML, dedupes against `state.json`. |
| `extract_deadlines.py` | Deterministic date extraction. |
| `deadline_ledger.py` | Durable ledger; supersedes reschedules, prunes past, classifies New vs Existing. |
| `build.py` | Reads the ledger view; builds `deadlines.ics` (with −24h/−1h alarms), HTML + plaintext digest, subject line. Commits seen-IDs. |
| `gcal_sync.py` | Upserts into Google Calendar via `gog`, keyed on a stable uid. |
| `emailer.py` | Composes the HTML + plaintext digest, and sends it as the email fallback. |
| `send_wechat_alert.py` | The author's own notifier. A worked example — see Notifications. |

---

## Run it yourself

Requirements: Python 3.11+, a Canvas account with API access, somewhere to run a
daily cron job, and — only for calendar sync — the `gog` Google CLI (v0.19+),
authed against your Google account with the calendar scope.

### 1. Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

### 2. Canvas token

In Canvas: **Account → Settings → Approved Integrations → + New Access Token**.
Canvas shows the token exactly once.

```bash
cp .env.example .env
```

Set `CANVAS_BASE_URL` to your school's Canvas host and `CANVAS_TOKEN` to the
token. Then check it works without touching anything else:

```bash
.venv/bin/python fetch.py --lookback-days 14 -v
```

You should see announcements in `out/announcements.json`. If your network needs a
proxy, set `CANVAS_USE_SYSTEM_PROXY=1`.

### 3. Google Calendar (optional)

Skip this and you still get the digest and the `.ics` file; you just won't get
automatic calendar sync.

Calendar access goes through `gog`, which must be installed and authed with the
calendar scope. Set `GOG_BIN` in `.env` if it isn't on `PATH` — cron's `PATH`
usually excludes `/usr/local/bin`. Then dry-run it:

```bash
.venv/bin/python gcal_sync.py --create-calendar --dry-run -v
```

### 4. Notifications

Delivery is layered, because the worst outcome is a run that finishes and tells
nobody. `NOTIFY_CMD` is your primary channel: any command that delivers
`out/subject.txt` and `out/digest.txt` — a push service, your own agent, a
script. **Email is the fallback**, and it fires whenever `NOTIFY_CMD` is unset
*or* exits non-zero.

Email is plain stdlib SMTP, so it adds no dependency. Set `SMTP_HOST` and
`EMAIL_TO` (plus `SMTP_USER` / `SMTP_PASS` if your server wants auth — for Gmail
that must be an app password) and you have a working notifier without
configuring anything else. Send one by hand to check:

```bash
.venv/bin/python emailer.py --send --out-dir out -v
```

With neither channel configured the run still extracts, still syncs the
calendar, and logs plainly that it delivered nothing — it does not fail
silently.

`send_wechat_alert.py` is the author's own notifier — a richer message rendered
from the ledger, with an email fallback — but it needs
[OpenClaw](https://docs.openclaw.ai) and the `gog` CLI, so treat it as a worked
example rather than something that runs out of the box.

### One full run by hand

```bash
.venv/bin/python fetch.py --lookback-days 14 -v      # pull announcements
.venv/bin/python extract_deadlines.py --out-dir out  # find the deadlines
.venv/bin/python deadline_ledger.py --out-dir out    # merge + prune + classify
.venv/bin/python build.py -v                         # digest + .ics
.venv/bin/python gcal_sync.py --deadlines out/ledger_view.json -v
```

To try it without hitting Canvas at all:

```bash
.venv/bin/python fetch.py --dry-run-fixtures tests/fixtures/sample_announcements.json -v
```

The sample announcements are dated May 2026, so running the full sequence against
them shows four announcements but **no upcoming deadlines and no `.ics`** — the
extracted deadline has passed and the ledger pruned it. That is the pruning
working. To see what the extractor found:

```bash
cat out/deadlines.json   # 1 deadline: 2026-05-15T17:00:00, confidence "high"
```

### Scheduling it

```
0 9 * * * /path/to/ddl/cron_ddl.sh >> /path/to/ddl/cron.log 2>&1
```

It finds its own directory and its own interpreter, so there are no paths to
edit. Pick an hour early enough that a same-day deadline is still actionable.

## Configuration

Everything lives in `.env`:

| Variable | Required | Meaning |
|---|---|---|
| `CANVAS_BASE_URL` | yes | Your Canvas host, e.g. `https://canvas.example.edu` |
| `CANVAS_TOKEN` | yes | Canvas personal access token |
| `TIMEZONE` | yes | IANA name, e.g. `Asia/Shanghai`. Deadlines resolve against this. |
| `DATE_ORDER` | no | `MDY` (default) or `DMY`, for ambiguous numeric dates like `5/6` |
| `CANVAS_USE_SYSTEM_PROXY` | no | Set to `1` if your network needs the system proxy |
| `DIGEST_LABEL` | no | Prefix on the subject and push message (default `Canvas`) |
| `CALENDAR_NAME` | no | Google Calendar to sync into (default `Canvas Deadlines`) |
| `GOG_BIN` | no | Path to the `gog` binary if it isn't on `PATH` |
| `NOTIFY_CMD` | no | Primary notifier: command that delivers the digest |
| `SMTP_HOST` | no | SMTP server for the email fallback; unset disables email |
| `SMTP_PORT` | no | Default `587` |
| `SMTP_USER` / `SMTP_PASS` | no | SMTP auth, if your server requires it |
| `SMTP_STARTTLS` | no | `1` (default); set `0` only for a server that forbids it |
| `EMAIL_FROM` | no | Sender address; defaults to `SMTP_USER` |
| `EMAIL_TO` | no | Recipient(s), comma-separated |
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

## Tests

```bash
.venv/bin/python -m pytest -q
```

87 tests, all fixture-based: no network, no credentials, no API calls, no SMTP
server. They cover the pieces that decide whether a deadline is right — the
extractor's cue, anti-cue, date-shape and time conventions, the ledger's
accumulate / supersede / prune / classify behaviour, and the calendar's identity
and dedupe rules, and which source the digest is built from — plus the `.ics`
and the email fallback's configuration and delivery contract. Each failure above has a test that fails without
its fix.

CI runs them on Python 3.11–3.13, shellchecks `cron_ddl.sh`, and fails the build
if a credential or personal identifier ever lands in a tracked file.

## Licence

MIT.
