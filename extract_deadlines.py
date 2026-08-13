#!/usr/bin/env python3
"""Deterministic deadline extractor: announcements.json -> deadlines.json.

Replaces the (now-defunct) LLM extraction step in the bash cron path. Parses
each announcement's body_text/title for concrete due dates that sit next to a
deadline cue word ("due", "deadline", "submit", ...). Precision-first: a date
with no nearby cue is ignored, so class times / office hours / grade-release
dates don't get reported as deadlines.

Output schema matches models.ExtractedDeadline (consumed by build.py + the
ledger). Times follow the conventions documented in the README:
  - explicit clock time            -> that time, confidence "high"
  - "noon"                         -> 12:00
  - "midnight" / "EOD" / "23:59"   -> 23:59 (end-of-day convention)
  - no time hint                   -> 23:59, confidence "medium"

Usage:
    python3 extract_deadlines.py --out-dir out
    python3 extract_deadlines.py --announcements out/announcements.json --stdout
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_MON_ALT = "jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"

# Three date shapes. day uses (?!\d) so we never eat into a 4-digit year.
RE_MON_DAY = re.compile(rf"\b(?P<mon>{_MON_ALT})\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?(?!\d)\b", re.I)
RE_DAY_MON = re.compile(rf"\b(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<mon>{_MON_ALT})\b", re.I)
RE_YMD = re.compile(r"\b(?P<y>20\d{2})[/-](?P<m>\d{1,2})[/-](?P<d>\d{1,2})\b")

RE_TIME_HM = re.compile(r"\b(?P<h>\d{1,2})[:.](?P<min>\d{2})\s*(?P<ap>am|pm)?\b", re.I)
RE_TIME_HAP = re.compile(r"\b(?P<h>\d{1,2})\s*(?P<ap>am|pm)\b", re.I)

# Deliverable phrases, longest/most-specific first, used to label the deadline
# with what is actually due (the announcement title is often generic/misleading).
DELIVERABLES = [
    "draft final report", "final report", "interim report", "group report",
    "group case analysis", "case analysis", "case study",
    "learning journal", "case write-up", "case writeup", "pre-assignment",
    "peer-evaluation", "peer evaluation", "course evaluation", "abstract/proposal", "questionnaire",
    "proposal", "abstract", "assignment", "presentation", "report", "journal",
    "evaluation", "registration", "survey", "paper", "essay", "quiz", "exam",
]
RE_DELIVERABLE = re.compile("|".join(re.escape(d) for d in DELIVERABLES), re.I)

CUE = re.compile(r"\b(due|deadline|submit|submission|hand[- ]?in|turn[- ]?in)\b", re.I)
# date sits in a non-deadline context -> drop even if a stray cue is nearby
ANTI_CUE = re.compile(r"\b(office hour|class starts|class begins|seat chart|holiday|grade|presentation #?\d?)\b", re.I)

WIN_BEFORE = 75   # chars of context to scan before a date for cue/time
WIN_AFTER = 45    # chars after


def _norm(text: str) -> str:
    return re.sub(r"[ \t]*\n[ \t]*", " ", text).replace(" ", " ")


def _resolve_year(month: int, day: int, posted: datetime) -> int:
    """Pick the year that puts the deadline closest to (and mostly after) posting."""
    year = posted.year
    try:
        cand = datetime(year, month, day)
    except ValueError:
        return year
    # Deadline far in the past relative to posting -> it's next year's.
    if cand < posted.replace(tzinfo=None) - timedelta(days=120):
        year += 1
    return year


def _derive_title(window: str, date_pos: int, fallback: str) -> str:
    """Name the deadline after what's actually due, picking the deliverable phrase
    closest to the date (windows often list several deadlines). Falls back to the
    announcement title when no known deliverable is present."""
    best, best_dist = None, 1 << 30
    for m in RE_DELIVERABLE.finditer(window):
        if m.end() <= date_pos:
            dist = date_pos - m.end()       # deliverable precedes the date
        elif m.start() >= date_pos:
            dist = m.start() - date_pos     # deliverable follows the date
        else:
            dist = 0
        if dist < best_dist:
            best, best_dist = m.group(0), dist
    if best:
        return best.strip().title()
    return (fallback or "Deadline").strip()[:120]


def _find_time(window: str) -> tuple[int, int] | None:
    if re.search(r"\bnoon\b", window, re.I):
        return (12, 0)
    if re.search(r"\b(midnight|eod|end of day|23[:.]59|11[:.]59\s*pm)\b", window, re.I):
        return (23, 59)
    m = RE_TIME_HM.search(window)
    if m:
        h, mn = int(m.group("h")), int(m.group("min"))
        ap = (m.group("ap") or "").lower()
        if ap == "pm" and h < 12:
            h += 12
        elif ap == "am" and h == 12:
            h = 0
        if 0 <= h <= 23 and 0 <= mn <= 59:
            return (h, mn)
    m = RE_TIME_HAP.search(window)
    if m:
        h = int(m.group("h"))
        ap = m.group("ap").lower()
        if ap == "pm" and h < 12:
            h += 12
        elif ap == "am" and h == 12:
            h = 0
        if 0 <= h <= 23:
            return (h, 0)
    return None


def _iter_dates(text: str, posted: datetime):
    """Yield (start, end, month, day, year) for each date-shaped match."""
    for m in RE_YMD.finditer(text):
        y, mo, d = int(m.group("y")), int(m.group("m")), int(m.group("d"))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            yield m.start(), m.end(), mo, d, y
    for rx in (RE_MON_DAY, RE_DAY_MON):
        for m in rx.finditer(text):
            mon_raw = m.group("mon").lower()
            mo = MONTHS.get(mon_raw) or MONTHS.get(mon_raw[:3])
            day = int(m.group("day"))
            if not mo or not (1 <= day <= 31):
                continue
            yield m.start(), m.end(), mo, day, _resolve_year(mo, day, posted)


def extract_from_announcement(a: dict, tz_name: str) -> list[dict]:
    posted = datetime.fromisoformat(a["posted_at"])
    text = _norm(f"{a.get('title','')}. {a.get('body_text','')}")
    found: dict[str, dict] = {}
    matches = list(_iter_dates(text, posted))
    date_spans = sorted((s, e) for s, e, *_ in matches)
    for start, end, mo, day, year in matches:
        try:
            date_obj = datetime(year, mo, day)
        except ValueError:
            continue
        win_start = max(0, start - WIN_BEFORE)
        window = text[win_start: end + WIN_AFTER]
        date_pos = start - win_start
        if not CUE.search(window):
            continue
        if ANTI_CUE.search(window) and not re.search(r"\b(due|deadline|submit)\b", window, re.I):
            continue
        # Search for a clock time in a window CLAMPED at neighboring dates, so a
        # time belonging to another deadline can't bleed in ("A due 5pm July 3.
        # B due July 10" must not give July 10 a 5pm). The cue window above stays
        # wide on purpose — a shared "due:" prefix may legitimately serve a list.
        t_lo = max((e2 for s2, e2 in date_spans if e2 <= start), default=win_start)
        t_hi = min((s2 for s2, e2 in date_spans if s2 >= end), default=end + WIN_AFTER)
        tm = _find_time(text[max(win_start, t_lo): min(end + WIN_AFTER, t_hi)])
        if tm is not None:
            hh, mm = tm
            confidence = "high"
        else:
            hh, mm = 23, 59
            confidence = "medium"
        dt_local = date_obj.replace(hour=hh, minute=mm).strftime("%Y-%m-%dT%H:%M:%S")
        quote = re.sub(r"\s+", " ", window).strip()
        rec = {
            "title": _derive_title(window, date_pos, a.get("title", "")),
            "datetime_local": dt_local,
            "timezone": tz_name,
            "all_day": False,
            "confidence": confidence,
            "evidence_quote": quote[:160],
            "source_announcement_id": int(a["id"]),
            "source_url": a.get("html_url", ""),
            "source_course": a.get("course_name", ""),
        }
        # de-dupe within an announcement by (datetime, title): a single deadline
        # restated collapses, but two distinct deliverables due at the same moment
        # (e.g. a case analysis AND a peer evaluation) each survive. Keep the
        # higher-confidence record when the same key recurs.
        key = (dt_local, rec["title"])
        if key not in found or (confidence == "high" and found[key]["confidence"] != "high"):
            found[key] = rec
    return list(found.values())


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out-dir", type=Path, default=Path("out"))
    p.add_argument("--announcements", type=Path, default=None)
    p.add_argument("--stdout", action="store_true", help="print to stdout instead of writing deadlines.json")
    args = p.parse_args(argv)

    ann_path = args.announcements or (args.out_dir / "announcements.json")
    if not ann_path.exists():
        print(f"[extract] no announcements at {ann_path}", file=sys.stderr)
        return 2
    payload = json.loads(ann_path.read_text())
    tz_name = payload.get("timezone", "Asia/Shanghai")

    deadlines: list[dict] = []
    for a in payload.get("announcements", []):
        deadlines.extend(extract_from_announcement(a, tz_name))
    deadlines.sort(key=lambda d: d["datetime_local"])

    if args.stdout:
        print(json.dumps(deadlines, indent=2, ensure_ascii=False))
    else:
        out = args.out_dir / "deadlines.json"
        out.write_text(json.dumps(deadlines, indent=2, ensure_ascii=False))
        print(f"[extract] wrote {len(deadlines)} deadline(s) -> {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
