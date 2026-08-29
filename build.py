"""Step 2 of 2: Read announcements.json + deadlines.json (LLM-produced) and
build the .ics file + HTML/plaintext digest. No LLM calls here.

Inputs (in <out-dir>):
  - announcements.json   (from fetch.py)
  - deadlines.json       (from the LLM step — Claude in chat, or routine)
  - summaries.json       (from the LLM step — one per announcement)

Outputs (in <out-dir>):
  - subject.txt
  - digest.html
  - digest.txt
  - deadlines.ics  (only if deadlines were extracted)

deadlines.json schema:
  [
    {
      "title": str,
      "datetime_local": "ISO 8601 local, e.g. 2026-05-15T17:00:00",
      "timezone": "Asia/Shanghai",
      "all_day": bool,
      "confidence": "high"|"medium"|"low",
      "evidence_quote": str,
      "source_announcement_id": int,
      "source_url": str,
      "source_course": str
    },
    ...
  ]

summaries.json schema:
  [
    {"announcement_id": int, "summary": "one-line summary"},
    ...
  ]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from canvas_client import Announcement
from emailer import compose, write_artifacts
from ics_builder import write_calendar
from models import AnnouncementSummary, ExtractedDeadline

log = logging.getLogger("ddl-build")
STATE_FILENAME = "state.json"


def _load_state(path: Path) -> dict:
    if not path.exists():
        return {"last_seen_announcement_ids": [], "last_run_at": None}
    return json.loads(path.read_text())


def _save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2, default=str))


def _announcement_from_dict(d: dict) -> Announcement:
    return Announcement(
        id=d["id"],
        course_id=d["course_id"],
        course_name=d["course_name"],
        title=d["title"],
        message_html="",
        posted_at=datetime.fromisoformat(d["posted_at"]),
        html_url=d["html_url"],
    )


def _records_from(raw: object) -> list[dict]:
    """Accept any of the three shapes a deadline file comes in."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for key in ("all_upcoming", "deadlines"):
            value = raw.get(key)
            if value is not None:
                return value
    return []


def read_deadline_records(out_dir: Path) -> list[dict]:
    """The deadlines this digest describes, newest source of truth first.

    ledger_view.json is DURABLE and complete; deadlines.json holds only what
    this one run extracted and is empty on most days. The first file that
    exists wins outright — an empty ledger view means "nothing is upcoming",
    NOT "fall through to whatever the last announcement-bearing day left
    behind", which is how a daily notifier ends up re-sending stale deadlines.
    """
    for name in ("ledger_view.json", "deadlines.json"):
        path = out_dir / name
        if path.exists():
            return _records_from(json.loads(path.read_text()))
    return []


def _due_at(d: ExtractedDeadline, fallback_tz: ZoneInfo) -> datetime:
    try:
        rec_tz = ZoneInfo(d.timezone) if d.timezone else fallback_tz
    except Exception:
        rec_tz = fallback_tz
    return datetime.fromisoformat(d.datetime_local).replace(tzinfo=rec_tz)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out-dir", type=Path, default=Path("out"))
    p.add_argument("--state", type=Path, default=Path(STATE_FILENAME))
    p.add_argument("--no-state-update", action="store_true",
                   help="Don't mark announcements as seen (useful for re-runs while iterating).")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    announcements_path = args.out_dir / "announcements.json"
    summaries_path = args.out_dir / "summaries.json"

    if not announcements_path.exists():
        log.error("Missing %s — run fetch.py first.", announcements_path)
        return 2

    payload = json.loads(announcements_path.read_text())
    tz_name = payload.get("timezone", "Asia/Shanghai")
    tz = ZoneInfo(tz_name)
    announcements = [_announcement_from_dict(a) for a in payload["announcements"]]
    announcements_by_id = {a.id: a for a in announcements}
    log.info("Loaded %d announcements", len(announcements))

    now_local = datetime.now(tz=tz)
    all_deadlines = [ExtractedDeadline.from_dict(d) for d in read_deadline_records(args.out_dir)]
    # Drop deadlines more than 24h in the past (don't remind about ancient ones).
    # Each record is compared in ITS OWN timezone: a wall time of 23:30 is a
    # different instant in Shanghai and New York, and getting that wrong
    # silently moves a deadline in or out of the digest by up to a day.
    cutoff = now_local - timedelta(hours=24)
    deadlines = [d for d in all_deadlines if _due_at(d, tz) > cutoff]
    dropped = len(all_deadlines) - len(deadlines)
    if dropped:
        log.info("Dropped %d past deadline(s) from output", dropped)
    log.info("Loaded %d active deadlines", len(deadlines))

    if summaries_path.exists():
        summaries = [AnnouncementSummary.from_dict(s) for s in json.loads(summaries_path.read_text())]
    else:
        log.info("No summaries.json — falling back to announcement titles")
        summaries = [
            AnnouncementSummary(announcement_id=a.id, summary=a.title) for a in announcements
        ]

    ics_path: Path | None = None
    if deadlines:
        ics_path = args.out_dir / "deadlines.ics"
        write_calendar(deadlines, ics_path)
        log.info("Wrote .ics with %d event(s) to %s", len(deadlines), ics_path)

    run_date_label = datetime.now(tz=tz).strftime("%b %-d")
    artifacts = compose(
        deadlines=deadlines,
        summaries=summaries,
        announcements_by_id=announcements_by_id,
        run_date_label=run_date_label,
        ics_path=ics_path,
    )
    written = write_artifacts(artifacts, args.out_dir)
    log.info("Wrote artifacts: %s", written)

    if not args.no_state_update and announcements:
        state = _load_state(args.state)
        seen_ids = set(state.get("last_seen_announcement_ids", []))
        new_state = {
            "last_seen_announcement_ids": sorted(seen_ids | {a.id for a in announcements}),
            "last_run_at": datetime.now(tz=timezone.utc).isoformat(),
        }
        _save_state(args.state, new_state)
        log.info("State committed (%d IDs tracked)", len(new_state["last_seen_announcement_ids"]))

    print()
    print("=" * 60)
    print(f"  {artifacts.subject}")
    print("=" * 60)
    print(f"  Deadlines: {artifacts.deadline_count}")
    print(f"  Announcements: {artifacts.announcement_count}")
    print(f"  Output dir: {args.out_dir.resolve()}")
    if ics_path:
        print(f"  ICS file:   {ics_path.resolve()}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
