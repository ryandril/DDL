"""Push extracted deadlines to Google Calendar — via the `gog` CLI.

Rewritten 2026-07-03: the previous implementation used a private OAuth token
(token.json, minted by auth.py) against a Desktop OAuth client in the
`ddl-agent` project. That project's consent screen is in "Testing" publishing
status, so Google expires its refresh token every ~7 days -> the cron died
with `invalid_grant: Token has been expired or revoked` roughly weekly and
needed a manual re-auth each time (3x). See cron.log around 2026-06-12.

This version drives the calendar through `gog` (v0.19+), the box's already
durably-authed Google CLI (calendar scope). gog is
now the single Google auth authority on this host, so there is no second token
to expire. token.json / credentials.json / auth.py are no longer used here.

Dedupe: every managed event carries private extended properties
`managedBy=ddl` and `ddlUid=<stable-uid>`. Re-running never duplicates. Events
created by the OLD implementation instead carried a custom iCalUID equal to the
same stable uid; we also match on that as a fallback so the transition is
seamless.

Same CLI as before (cron_ddl.sh is unchanged):
    python3 gcal_sync.py --deadlines out/deadlines.json --create-calendar
    python3 gcal_sync.py --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from models import ExtractedDeadline

log = logging.getLogger("ddl-gcal")

DEFAULT_CALENDAR_NAME = os.environ.get("CALENDAR_NAME", "Canvas Deadlines")
# Resolve gog by absolute path — cron's PATH (/usr/bin:/bin) usually excludes
# /usr/local/bin, so bare "gog" would fail under cron even though it works in
# an interactive shell. Override with $GOG_BIN if it ever moves.
GOG = os.environ.get("GOG_BIN") or shutil.which("gog") or "/usr/local/bin/gog"
MANAGED_MARK = ("managedBy", "ddl")
# Wide window so the "list existing managed events" call catches every managed
# event regardless of date (school year spans ~a year; be generous).
LIST_FROM = "2024-01-01T00:00:00+08:00"
LIST_TO = "2028-01-01T00:00:00+08:00"


# --------------------------------------------------------------------------- #
# gog subprocess helpers
# --------------------------------------------------------------------------- #
def _run(args: list[str], *, json_out: bool) -> tuple[int, str, str]:
    """Run a gog command. Returns (returncode, stdout, stderr)."""
    cmd = [GOG]
    if json_out:
        cmd.append("-j")
    cmd += ["--no-input", *args]
    log.debug("exec: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def _gog_json(args: list[str]) -> object:
    rc, out, err = _run(args, json_out=True)
    if rc != 0:
        raise RuntimeError(f"gog {' '.join(args)} failed (rc={rc}): {err.strip() or out.strip()}")
    out = out.strip()
    if not out:
        return {}
    return json.loads(out)


# --------------------------------------------------------------------------- #
# calendar + event helpers
# --------------------------------------------------------------------------- #
def _calendars() -> list[dict]:
    data = _gog_json(["calendar", "calendars"])
    if isinstance(data, dict):
        return data.get("calendars", data.get("items", []))
    return data if isinstance(data, list) else []


def find_or_create_calendar(name: str, create_if_missing: bool) -> str:
    for cal in _calendars():
        if cal.get("summary") == name:
            return cal["id"]
    if not create_if_missing:
        raise RuntimeError(
            f'Calendar "{name}" not found. Re-run with --create-calendar to make it.'
        )
    log.info("Creating calendar %r…", name)
    rc, out, err = _run(
        ["calendar", "create-calendar", name, "--timezone", "Asia/Shanghai"],
        json_out=True,
    )
    if rc != 0:
        raise RuntimeError(f"create-calendar failed (rc={rc}): {err.strip() or out.strip()}")
    # Re-list to resolve the id robustly.
    for cal in _calendars():
        if cal.get("summary") == name:
            log.info("Created calendar id=%s", cal["id"])
            return cal["id"]
    raise RuntimeError(f'Created calendar "{name}" but could not resolve its id.')


def deadline_uid(d: ExtractedDeadline) -> str:
    """Stable calendar identity for a deadline.

    MUST agree with the ledger's own identity (course|datetime|title), or two
    records the ledger keeps apart collapse into one calendar event and the
    second silently overwrites the first. The due datetime is therefore part of
    the hash: one announcement can legitimately state the same deliverable at
    two different times (a draft and a final), and both must land.
    """
    h = hashlib.sha256(f"{d.title}|{d.datetime_local}".encode()).hexdigest()[:10]
    return f"ddl-canvas-{d.source_announcement_id}-{h}@ddl"


def legacy_deadline_uid(d: ExtractedDeadline) -> str:
    """The pre-2026-08 uid, hashed on the title alone.

    Kept only so events already on the calendar are adopted and updated in
    place instead of being duplicated under the new scheme. Because the old
    scheme could not tell two same-titled deadlines apart, a legacy event is
    claimed by at most one record per run (see upsert).
    """
    h = hashlib.sha256(d.title.encode()).hexdigest()[:10]
    return f"ddl-canvas-{d.source_announcement_id}-{h}@ddl"


def _summary(d: ExtractedDeadline) -> str:
    flag = "" if d.confidence == "high" else f" [{d.confidence.upper()}]"
    return f"{d.title} — {d.source_course}{flag}"


def _description(d: ExtractedDeadline) -> str:
    return (
        f"Course: {d.source_course}\n"
        f"Confidence: {d.confidence}\n"
        f'Quote: "{d.evidence_quote}"\n'
        f"Source: {d.source_url}"
    )


def _times(d: ExtractedDeadline) -> tuple[str, str, bool]:
    """Return (from, to, all_day) strings ready for gog flags."""
    if d.all_day:
        day = datetime.fromisoformat(d.datetime_local).date()
        return day.isoformat(), (day + timedelta(days=1)).isoformat(), True
    local = datetime.fromisoformat(d.datetime_local).replace(tzinfo=ZoneInfo(d.timezone))
    end = local + timedelta(minutes=30)
    return local.isoformat(), end.isoformat(), False


def list_existing_events(calendar_id: str) -> dict[str, dict]:
    """Map stable-uid -> event, for events managed by this tool.

    Keys on the ddlUid private prop (new scheme) AND on iCalUID (old scheme,
    where iCalUID == the stable uid). Either resolves the same uid value.
    """
    key, val = MANAGED_MARK
    data = _gog_json(
        [
            "calendar", "events", calendar_id,
            "--private-prop-filter", f"{key}={val}",
            "--from", LIST_FROM, "--to", LIST_TO,
            "--max", "2500",
        ]
    )
    events = data.get("events", []) if isinstance(data, dict) else (data or [])
    out: dict[str, dict] = {}
    for ev in events:
        priv = (ev.get("extendedProperties") or {}).get("private") or {}
        ddluid = priv.get("ddlUid")
        if ddluid:
            out[ddluid] = ev
        ical = ev.get("iCalUID")
        if ical and ical not in out:  # old-scheme fallback
            out.setdefault(ical, ev)
    return out


def _start_key(start: dict | None) -> str:
    """Normalize an event start to a comparable string (instant or date)."""
    if not start:
        return ""
    if "date" in start:
        return f"date:{start['date']}"
    dt = start.get("dateTime")
    if not dt:
        return ""
    try:
        return "inst:" + datetime.fromisoformat(dt).isoformat()
    except ValueError:
        return "inst:" + dt


def _new_start_key(d: ExtractedDeadline) -> str:
    frm, _to, all_day = _times(d)
    if all_day:
        return f"date:{frm}"
    return "inst:" + datetime.fromisoformat(frm).isoformat()


def _create_args(calendar_id: str, d: ExtractedDeadline) -> list[str]:
    frm, to, all_day = _times(d)
    uid = deadline_uid(d)
    args = [
        "calendar", "create", calendar_id,
        "--summary", _summary(d),
        "--from", frm, "--to", to,
        "--description", _description(d),
        "--private-prop", f"{MANAGED_MARK[0]}={MANAGED_MARK[1]}",
        "--private-prop", f"ddlUid={uid}",
        "--source-url", d.source_url,
        "--source-title", "Canvas announcement",
    ]
    if all_day:
        args.append("--all-day")
    else:
        args += ["--start-timezone", d.timezone, "--end-timezone", d.timezone]
        args += ["--reminder", "popup:1d,popup:1h"]
    return args


def _update_args(calendar_id: str, event_id: str, d: ExtractedDeadline) -> list[str]:
    frm, to, all_day = _times(d)
    args = [
        "calendar", "update", calendar_id, event_id,
        "--summary", _summary(d),
        "--from", frm, "--to", to,
    ]
    if all_day:
        args.append("--all-day")
    else:
        args += ["--start-timezone", d.timezone, "--end-timezone", d.timezone]
    return args


def upsert(calendar_id: str, d: ExtractedDeadline, existing: dict[str, dict], dry_run: bool) -> str:
    uid = deadline_uid(d)
    ev = existing.get(uid)
    if ev is None:
        # Adopt an event created under the old title-only uid so the scheme
        # change doesn't duplicate the calendar. pop() not get(): the old uid is
        # ambiguous by construction, so only the first record may claim it and
        # any other record sharing it gets its own new event.
        legacy = legacy_deadline_uid(d)
        if legacy != uid:
            ev = existing.pop(legacy, None)
            if ev is not None:
                log.info("adopting legacy-uid event for %s", _summary(d))
    if ev is not None:
        same = (
            ev.get("summary") == _summary(d)
            and _start_key(ev.get("start")) == _new_start_key(d)
        )
        if same:
            return "unchanged"
        if dry_run:
            log.info("[dry-run] WOULD UPDATE: %s", _summary(d))
            return "updated"
        rc, out, err = _run(_update_args(calendar_id, ev["id"], d), json_out=True)
        if rc != 0:
            raise RuntimeError(f"update failed: {err.strip() or out.strip()}")
        return "updated"
    if dry_run:
        log.info("[dry-run] WOULD INSERT: %s", _summary(d))
        existing[uid] = {"summary": _summary(d), "start": {}}  # mirror real-run dedupe
        return "inserted"
    rc, out, err = _run(_create_args(calendar_id, d), json_out=True)
    if rc != 0:
        raise RuntimeError(f"create failed: {err.strip() or out.strip()}")
    # Register the new event under its uid so a genuine restatement of the same
    # deadline later in this run takes the update path instead of inserting a
    # duplicate. If gog's output isn't parseable, store a stub — that record
    # then fails loudly in update (no id) rather than silently duplicating.
    try:
        created = json.loads(out)
        if isinstance(created, dict):
            created = created.get("event", created)
    except Exception:
        created = None
    if not isinstance(created, dict) or not created.get("id"):
        created = {"summary": _summary(d), "start": {}}
    existing[uid] = created
    return "inserted"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--deadlines", type=Path, default=Path("out/deadlines.json"))
    p.add_argument("--calendar-name", default=DEFAULT_CALENDAR_NAME)
    p.add_argument("--create-calendar", action="store_true",
                   help="Create the named calendar if it doesn't exist.")
    p.add_argument("--dry-run", action="store_true",
                   help="Don't actually write to Google Calendar.")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not args.deadlines.exists():
        log.error("%s not found. Run fetch.py and the build step first.", args.deadlines)
        return 2

    # Accept three input shapes so the sync can be driven off the DURABLE ledger,
    # not just the ephemeral per-day extraction:
    #   * a bare list                 -> out/deadlines.json (that day's extraction)
    #   * {"all_upcoming": [...]}      -> out/ledger_view.json   (PREFERRED)
    #   * {"deadlines": [...]}         -> deadlines_ledger.json
    # Ledger-driven sync is idempotent (upsert dedupes on ddlUid) and self-heals:
    # every standing deadline is re-pushed each day, so one missed/failed run no
    # longer means the deadline never lands in Calendar (the old bug — gcal_sync
    # only ran on new-announcement days off an often-empty deadlines.json).
    raw = json.loads(args.deadlines.read_text())
    if isinstance(raw, dict):
        items = raw.get("all_upcoming")
        if items is None:
            items = raw.get("deadlines", [])
    else:
        items = raw
    deadlines = [ExtractedDeadline.from_dict(x) for x in items]
    log.info("Loaded %d deadline(s) from %s", len(deadlines), args.deadlines)
    if not deadlines:
        log.info("Nothing to sync.")
        return 0

    cal_id = find_or_create_calendar(args.calendar_name, args.create_calendar)
    log.info("Target calendar: %r (%s)", args.calendar_name, cal_id)

    existing = list_existing_events(cal_id)
    log.info("Existing managed events: %d", len(existing))

    counts = {"inserted": 0, "updated": 0, "unchanged": 0, "failed": 0}
    for d in deadlines:
        try:
            outcome = upsert(cal_id, d, existing, args.dry_run)
            counts[outcome] += 1
            log.info("  [%s] %s — %s", outcome, _summary(d), d.datetime_local)
        except Exception as e:  # noqa: BLE001 — one bad deadline shouldn't sink the batch
            counts["failed"] += 1
            log.error("  [failed] %s — %s", _summary(d), e)

    print()
    print("=" * 60)
    print(f"  Calendar: {args.calendar_name}")
    print(f"  Inserted: {counts['inserted']}")
    print(f"  Updated:  {counts['updated']}")
    print(f"  Unchanged: {counts['unchanged']}")
    if counts["failed"]:
        print(f"  Failed:   {counts['failed']}")
    if args.dry_run:
        print("  (dry-run — no actual changes written)")
    print("=" * 60)
    return 0 if counts["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
