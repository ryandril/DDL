"""Build a .ics file from extracted deadlines."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from icalendar import Alarm, Calendar, Event


def build_calendar(deadlines: list, prodid: str = "-//Canvas Deadline Agent//EN") -> Calendar:
    cal = Calendar()
    cal.add("prodid", prodid)
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")

    for d in deadlines:
        ev = Event()
        tz = ZoneInfo(d.timezone)
        local_dt = datetime.fromisoformat(d.datetime_local).replace(tzinfo=tz)

        ev.add("summary", _summary(d))
        ev.add("uid", f"canvas-deadline-{d.source_announcement_id}-{abs(hash(d.title)) % 10**8}@canvas-agent")
        ev.add("dtstamp", datetime.now(tz=ZoneInfo("UTC")))

        if d.all_day:
            day = local_dt.date()
            ev.add("dtstart", day)
            ev.add("dtend", day + timedelta(days=1))
        else:
            ev.add("dtstart", local_dt)
            ev.add("dtend", local_dt + timedelta(minutes=30))

        description_lines = [
            f"Course: {d.source_course}",
            f"Confidence: {d.confidence}",
            f"Quote: \"{d.evidence_quote}\"",
            f"Source: {d.source_url}",
        ]
        ev.add("description", "\n".join(description_lines))
        if d.source_url:
            ev.add("url", d.source_url)

        if not d.all_day:
            ev.add_component(_alarm(timedelta(hours=-24), "24 hours until deadline"))
            ev.add_component(_alarm(timedelta(hours=-1), "1 hour until deadline"))

        cal.add_component(ev)
    return cal


def _summary(d) -> str:
    flag = "" if d.confidence == "high" else f" [{d.confidence.upper()}]"
    return f"{d.title} — {d.source_course}{flag}"


def _alarm(trigger: timedelta, description: str) -> Alarm:
    a = Alarm()
    a.add("action", "DISPLAY")
    a.add("description", description)
    a.add("trigger", trigger)
    return a


def write_calendar(deadlines: list, path: Path) -> Path:
    cal = build_calendar(deadlines)
    path.write_bytes(cal.to_ical())
    return path
