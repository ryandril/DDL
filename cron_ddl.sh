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

LOG="$DDL_DIR/cron.log"
TRIGGER="$DDL_DIR/.cron_trigger.json"
OUT="$DDL_DIR/out"
DATE_LABEL=$(date '+%b %-d')

# How to deliver the digest. Default is the dependency-free Server酱 push;
# set NOTIFY_CMD in .env to use your own sender (see README).
NOTIFY_CMD="${NOTIFY_CMD:-python3 $DDL_DIR/serverchan_push.py --title-file $OUT/subject.txt --body-file $OUT/digest.txt}"

echo "[DDL Cron] $(date '+%Y-%m-%d %H:%M:%S') — Starting" >> "$LOG"

# --- Step 1: fetch new announcements (capture real rc despite the pipe) ---
python3 fetch.py --out-dir "$OUT" 2>&1 | tail -5 >> "$LOG"
FETCH_RC=${PIPESTATUS[0]}

if [ "$FETCH_RC" -ne 0 ]; then
    echo "[DDL Cron] fetch.py FAILED rc=$FETCH_RC — sending status with last-known deadlines" >> "$LOG"
    # Refresh/prune the ledger so the message still shows standing upcoming deadlines.
    python3 deadline_ledger.py --out-dir "$OUT" 2>&1 | tail -2 >> "$LOG" || true
    python3 - "$TRIGGER" "$DATE_LABEL" "$FETCH_RC" <<'PY' >> "$LOG" 2>&1
import json, sys
trigger, date, rc = sys.argv[1], sys.argv[2], sys.argv[3]
json.dump({"action": "failure", "date": date,
           "error": f"fetch.py exited {rc} — check the Canvas token in .env or network."},
          open(trigger, "w"), ensure_ascii=False)
PY
    $NOTIFY_CMD >> "$LOG" 2>&1 || echo "[DDL Cron] send exit=$?" >> "$LOG"
    echo "[DDL Cron] Done (fetch-failure path) at $(date)" >> "$LOG"
    exit 0
fi

# --- announcement count ---
COUNT=$(python3 -c "import json; print(len(json.load(open('$OUT/announcements.json'))['announcements']))" 2>/dev/null || echo 0)
echo "[DDL Cron] Found $COUNT new announcement(s)" >> "$LOG"

# --- Step 2: extract deadlines (only if new announcements) ---
EXTRACT_RC=0
if [ "$COUNT" -gt 0 ]; then
    python3 extract_deadlines.py --out-dir "$OUT" 2>&1 | tail -2 >> "$LOG"
    EXTRACT_RC=${PIPESTATUS[0]}
    [ "$EXTRACT_RC" -ne 0 ] && echo "[DDL Cron] extract_deadlines FAILED rc=$EXTRACT_RC — announcements stay unseen for retry" >> "$LOG"
fi

# --- Step 3: ledger merge + prune past + classify New/Existing (ALWAYS) ---
# Writes out/ledger_view.json = every standing upcoming deadline (durable truth).
python3 deadline_ledger.py --out-dir "$OUT" 2>&1 | tail -2 >> "$LOG" || true

# --- Step 3b: reconcile Google Calendar from the DURABLE ledger (ALWAYS) ---
# Runs every day off ledger_view.json, not just on new-announcement days off the
# ephemeral (usually-empty) out/deadlines.json. Idempotent upsert dedupes on
# ddlUid, so re-running is safe and any missed/failed day self-heals next run.
python3 gcal_sync.py --deadlines "$OUT/ledger_view.json" --create-calendar 2>&1 | tail -3 >> "$LOG" || true

# --- Step 3c: build .ics/digest + COMMIT seen-IDs — AFTER the ledger persisted.
# Ordering matters: build.py marks announcements as seen in state.json; doing
# that before the ledger merge meant a crash between the two steps lost the
# deadline forever (seen but never durably saved). If extraction failed, skip
# so the announcements stay unseen and next run retries them.
if [ "$COUNT" -gt 0 ] && [ "$EXTRACT_RC" -eq 0 ]; then
    python3 build.py --out-dir "$OUT" 2>&1 | tail -3 >> "$LOG" || true          # commits seen-IDs to state.json
fi

# --- Step 4: write trigger (announcement summary; deadlines come from the ledger view) ---
python3 - "$TRIGGER" "$DATE_LABEL" "$COUNT" "$OUT/announcements.json" <<'PY' >> "$LOG" 2>&1
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

# --- Step 5: send WeChat (ALWAYS) ---
$NOTIFY_CMD >> "$LOG" 2>&1 || echo "[DDL Cron] send_wechat_alert exit=$?" >> "$LOG"
echo "[DDL Cron] Done at $(date)" >> "$LOG"
