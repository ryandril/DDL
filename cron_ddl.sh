#!/bin/bash
# DDL cron runner — the whole daily pipeline, no agent runtime required.
#
# Pipeline:  fetch -> extract deadlines -> ledger (prune past + classify
#            New/Existing) -> build .ics/digest + commit state -> Google
#            Calendar -> notify
#
# NOTE: intentionally NOT `set -e`. Every step is best-effort; we ALWAYS reach
# the notify step, so you get a message even on failure (it doubles as a
# run-status report).
#
# Credentials come from .env, loaded by the Python scripts via python-dotenv.
# Do NOT hardcode CANVAS_TOKEN here — a truncated copy pasted into this file
# once broke every run with a UnicodeEncodeError.
set -o pipefail

# Run from the script's own directory unless DDL_DIR overrides it.
DDL_DIR="${DDL_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
cd "$DDL_DIR" || exit 1

# Use the project's virtualenv if the README's setup was followed; fall back to
# system python3 otherwise (e.g. a server with the deps installed globally).
# Without this, cron runs the system interpreter and every run dies with
# ModuleNotFoundError while manual `.venv/bin/python` runs look fine.
PYBIN="$DDL_DIR/.venv/bin/python"
[ -x "$PYBIN" ] || PYBIN="$(command -v python3)"

LOG="$DDL_DIR/cron.log"
TRIGGER="$DDL_DIR/.cron_trigger.json"
OUT="$DDL_DIR/out"
DATE_LABEL=$(date '+%b %-d')

# How to deliver the digest. NOTIFY_CMD is your primary channel — any command
# that delivers out/subject.txt and out/digest.txt. Email is the FALLBACK and
# needs no command: if NOTIFY_CMD is unset, or it exits non-zero, the digest is
# emailed via emailer.py. A run must never end with the user uninformed.
NOTIFY_CMD="${NOTIFY_CMD:-}"

notify() {
    if [ -n "$NOTIFY_CMD" ]; then
        if $NOTIFY_CMD >> "$LOG" 2>&1; then
            return 0
        fi
        echo "[DDL Cron] NOTIFY_CMD failed — falling back to email" >> "$LOG"
    fi
    if "$PYBIN" emailer.py --send --out-dir "$OUT" >> "$LOG" 2>&1; then
        return 0
    fi
    echo "[DDL Cron] email fallback did not deliver — digest is in $OUT/digest.txt" >> "$LOG"
}

echo "[DDL Cron] $(date '+%Y-%m-%d %H:%M:%S') — Starting" >> "$LOG"

# --- Step 1: fetch new announcements (capture real rc despite the pipe) ---
"$PYBIN" fetch.py --out-dir "$OUT" 2>&1 | tail -5 >> "$LOG"
FETCH_RC=${PIPESTATUS[0]}

if [ "$FETCH_RC" -ne 0 ]; then
    echo "[DDL Cron] fetch.py FAILED rc=$FETCH_RC — sending status with last-known deadlines" >> "$LOG"
    # Refresh/prune the ledger AND rebuild the digest from it, so the message
    # carries today's standing deadlines rather than the last successful run's.
    # Read-only: nothing was fetched, so nothing may be marked seen.
    "$PYBIN" deadline_ledger.py --out-dir "$OUT" 2>&1 | tail -2 >> "$LOG" || true
    "$PYBIN" build.py --out-dir "$OUT" --no-state-update 2>&1 | tail -2 >> "$LOG" || true
    "$PYBIN" - "$TRIGGER" "$DATE_LABEL" "$FETCH_RC" <<'PY' >> "$LOG" 2>&1
import json, sys
trigger, date, rc = sys.argv[1], sys.argv[2], sys.argv[3]
json.dump({"action": "failure", "date": date,
           "error": f"fetch.py exited {rc} — check the Canvas token in .env or network."},
          open(trigger, "w"), ensure_ascii=False)
PY
    notify
    echo "[DDL Cron] Done (fetch-failure path) at $(date)" >> "$LOG"
    exit 0
fi

# --- announcement count ---
COUNT=$("$PYBIN" -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["announcements"]))' "$OUT/announcements.json" 2>/dev/null || echo 0)
echo "[DDL Cron] Found $COUNT new announcement(s)" >> "$LOG"

# --- Step 2: extract deadlines (only if new announcements) ---
EXTRACT_RC=0
if [ "$COUNT" -gt 0 ]; then
    "$PYBIN" extract_deadlines.py --out-dir "$OUT" 2>&1 | tail -2 >> "$LOG"
    EXTRACT_RC=${PIPESTATUS[0]}
    [ "$EXTRACT_RC" -ne 0 ] && echo "[DDL Cron] extract_deadlines FAILED rc=$EXTRACT_RC — announcements stay unseen for retry" >> "$LOG"
fi

# --- Step 3: ledger merge + prune past + classify New/Existing (ALWAYS) ---
# Writes out/ledger_view.json = every standing upcoming deadline (durable truth).
"$PYBIN" deadline_ledger.py --out-dir "$OUT" 2>&1 | tail -2 >> "$LOG" || true

# --- Step 3b: reconcile Google Calendar from the DURABLE ledger (ALWAYS) ---
# Runs every day off ledger_view.json, not just on new-announcement days off the
# ephemeral (usually-empty) out/deadlines.json. Idempotent upsert dedupes on
# ddlUid, so re-running is safe and any missed/failed day self-heals next run.
"$PYBIN" gcal_sync.py --deadlines "$OUT/ledger_view.json" --create-calendar 2>&1 | tail -3 >> "$LOG" || true

# --- Step 3c: build .ics/digest EVERY day, from the DURABLE ledger ----------
# Not gated on new announcements: build.py reads out/ledger_view.json, and on a
# quiet day the answer is "here is what is still upcoming", not last week's
# digest left on disk. Skipping the build is how a daily notifier ends up
# re-sending the same stale message.
#
# Committing seen-IDs is a SEPARATE concern that stays gated. build.py writes
# them to state.json, which must happen only AFTER the ledger persisted (a
# crash between the two left an announcement seen but its deadline never
# saved), and only when this run actually extracted successfully. Otherwise the
# build runs read-only so those announcements stay unseen and the next run
# retries them.
BUILD_ARGS=(--out-dir "$OUT")
if [ "$COUNT" -eq 0 ] || [ "$EXTRACT_RC" -ne 0 ]; then
    BUILD_ARGS+=(--no-state-update)
fi
"$PYBIN" build.py "${BUILD_ARGS[@]}" 2>&1 | tail -3 >> "$LOG" || true

# --- Step 4: write trigger (announcement summary; deadlines come from the ledger view) ---
"$PYBIN" - "$TRIGGER" "$DATE_LABEL" "$COUNT" "$OUT/announcements.json" <<'PY' >> "$LOG" 2>&1
import json, sys
trigger, date, count, ann = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
titles = []
try:
    titles = [a.get("title", "").strip() for a in json.load(open(ann)).get("announcements", [])][:6]
except Exception:
    pass
json.dump({"action": "new_deadlines" if count > 0 else "no_announcements",
           "date": date, "count": count, "announcement_titles": titles},
          open(trigger, "w"), ensure_ascii=False)
PY

# --- Step 5: notify (ALWAYS) ---
notify
echo "[DDL Cron] Done at $(date)" >> "$LOG"
