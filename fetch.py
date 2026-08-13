"""Step 1 of 2: Fetch new Canvas announcements and write JSON for the LLM step.

Output: <out-dir>/announcements.json
        {
          "run_at_utc": "...",
          "since_utc": "...",
          "timezone": "Asia/Shanghai",
          "courses": [{"id": ..., "name": ..., "course_code": ...}, ...],
          "announcements": [
            {
              "id": ...,
              "course_id": ...,
              "course_name": "...",
              "title": "...",
              "posted_at": "...",
              "html_url": "...",
              "body_text": "...plain text, html stripped..."
            },
            ...
          ]
        }

State (state.json) is updated with seen IDs only after a successful build step,
NOT here — this step is read-only. Pass --commit-state to update state from this
step (used in routine mode where build is fire-and-forget).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from dotenv import load_dotenv

from canvas_client import Announcement, CanvasClient, Course

log = logging.getLogger("ddl-fetch")
STATE_FILENAME = "state.json"
MAX_BODY_CHARS = 8000


def _load_state(path: Path) -> dict:
    if not path.exists():
        return {"last_seen_announcement_ids": [], "last_run_at": None}
    return json.loads(path.read_text())


def _save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2, default=str))


def _resolve_since(state: dict, lookback_days: int | None, default_lookback: int = 1) -> datetime:
    if lookback_days is not None:
        return datetime.now(tz=timezone.utc) - timedelta(days=lookback_days)
    if state.get("last_run_at"):
        return datetime.fromisoformat(state["last_run_at"])
    return datetime.now(tz=timezone.utc) - timedelta(days=default_lookback)


def _filter_unseen(announcements: list[Announcement], seen_ids: set[int]) -> list[Announcement]:
    return [a for a in announcements if a.id not in seen_ids]


def _html_to_text(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator="\n", strip=True)
    if len(text) > MAX_BODY_CHARS:
        text = text[:MAX_BODY_CHARS] + "\n[truncated]"
    return text


def _load_fixture(path: Path) -> tuple[list[Course], list[Announcement]]:
    data = json.loads(path.read_text())
    courses = [Course(**c) for c in data.get("courses", [])]
    announcements: list[Announcement] = []
    for a in data.get("announcements", []):
        a = dict(a)
        a["posted_at"] = datetime.fromisoformat(a["posted_at"])
        announcements.append(Announcement(**a))
    return courses, announcements


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--lookback-days", type=int, default=None)
    p.add_argument("--out-dir", type=Path, default=Path("out"))
    p.add_argument("--state", type=Path, default=Path(STATE_FILENAME))
    p.add_argument("--dry-run-fixtures", type=Path, default=None)
    p.add_argument("--commit-state", action="store_true",
                   help="Mark fetched announcements as seen even if build hasn't run yet.")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    load_dotenv()
    tz_name = os.environ.get("TIMEZONE", "Asia/Shanghai")
    state = _load_state(args.state)
    seen_ids = set(state.get("last_seen_announcement_ids", []))
    since = _resolve_since(state, args.lookback_days)
    log.info("Lookback since: %s (timezone: %s)", since.isoformat(), tz_name)

    if args.dry_run_fixtures:
        log.info("Loading fixtures from %s", args.dry_run_fixtures)
        courses, announcements = _load_fixture(args.dry_run_fixtures)
    else:
        base_url = os.environ.get("CANVAS_BASE_URL")
        token = os.environ.get("CANVAS_TOKEN")
        if not base_url or not token:
            log.error("CANVAS_BASE_URL and CANVAS_TOKEN must be set in .env.")
            return 2
        use_proxy = os.environ.get("CANVAS_USE_SYSTEM_PROXY", "").lower() in {"1", "true", "yes"}
        with CanvasClient(base_url, token, trust_env=use_proxy) as canvas:
            courses = canvas.list_active_courses()
            log.info("Active courses: %d", len(courses))
            for c in courses:
                log.debug("  - %s (%s)", c.name, c.course_code)
            announcements = canvas.list_announcements(courses, since=since)
            log.info("Announcements fetched: %d", len(announcements))

    new_announcements = _filter_unseen(announcements, seen_ids)
    log.info("New (unseen) announcements: %d", len(new_announcements))

    args.out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_at_utc": datetime.now(tz=timezone.utc).isoformat(),
        "since_utc": since.isoformat(),
        "timezone": tz_name,
        "courses": [
            {"id": c.id, "name": c.name, "course_code": c.course_code} for c in courses
        ],
        "announcements": [
            {
                "id": a.id,
                "course_id": a.course_id,
                "course_name": a.course_name,
                "title": a.title,
                "posted_at": a.posted_at.isoformat(),
                "html_url": a.html_url,
                "body_text": _html_to_text(a.message_html),
            }
            for a in new_announcements
        ],
    }
    out_path = args.out_dir / "announcements.json"
    out_path.write_text(json.dumps(payload, indent=2, default=str))
    log.info("Wrote %s (%d new announcements)", out_path, len(new_announcements))

    if args.commit_state:
        new_state = {
            "last_seen_announcement_ids": sorted(seen_ids | {a.id for a in new_announcements}),
            "last_run_at": datetime.now(tz=timezone.utc).isoformat(),
        }
        _save_state(args.state, new_state)
        log.info("State committed (%d IDs tracked)", len(new_state["last_seen_announcement_ids"]))

    print()
    print("=" * 60)
    print(f"  Fetched {len(new_announcements)} new announcement(s) from {len(courses)} course(s)")
    print(f"  Wrote: {out_path.resolve()}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
