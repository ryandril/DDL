"""Round-trip a calendar through icalendar to verify the .ics is parseable."""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from icalendar import Calendar

sys.path.insert(0, str(Path(__file__).parent.parent))

from ics_builder import build_calendar
from models import ExtractedDeadline


def _make_deadline(**overrides) -> ExtractedDeadline:
    defaults = dict(
        title="Submit midterm",
        datetime_local="2026-05-15T17:00:00",
        timezone="Asia/Shanghai",
        all_day=False,
        confidence="high",
        evidence_quote="due Friday, May 15 at 5:00pm",
        source_announcement_id=5001,
        source_url="https://canvas.example.edu/courses/101/discussion_topics/5001",
        source_course="Strategy II",
    )
    defaults.update(overrides)
    return ExtractedDeadline(**defaults)


def test_calendar_has_one_event_per_deadline():
    cal = build_calendar([_make_deadline(), _make_deadline(source_announcement_id=5002, title="Case write-up")])
    events = [c for c in cal.walk("VEVENT")]
    assert len(events) == 2


def test_event_has_correct_local_time_and_30min_duration():
    cal = build_calendar([_make_deadline()])
    [event] = cal.walk("VEVENT")
    dtstart = event["DTSTART"].dt
    dtend = event["DTEND"].dt
    assert dtstart.year == 2026 and dtstart.month == 5 and dtstart.day == 15
    assert dtstart.hour == 17 and dtstart.minute == 0
    assert dtstart.tzinfo is not None  # timezone-aware
    assert (dtend - dtstart).total_seconds() == 30 * 60


def test_event_includes_source_url_and_quote_in_description():
    cal = build_calendar([_make_deadline()])
    [event] = cal.walk("VEVENT")
    desc = str(event["DESCRIPTION"])
    assert "due Friday" in desc
    assert "Strategy II" in desc
    assert "https://canvas.example.edu" in desc
    assert str(event["URL"]) == "https://canvas.example.edu/courses/101/discussion_topics/5001"


def test_event_has_two_alarms_for_timed_deadline():
    cal = build_calendar([_make_deadline()])
    [event] = cal.walk("VEVENT")
    alarms = [c for c in event.walk("VALARM")]
    assert len(alarms) == 2


def test_all_day_deadline_uses_date_and_no_alarms():
    cal = build_calendar([_make_deadline(all_day=True, datetime_local="2026-05-20T00:00:00")])
    [event] = cal.walk("VEVENT")
    dtstart = event["DTSTART"].dt
    # all-day should be date, not datetime
    from datetime import date
    assert isinstance(dtstart, date) and not isinstance(dtstart, datetime)
    alarms = [c for c in event.walk("VALARM")]
    assert len(alarms) == 0


def test_calendar_round_trips_through_ical_parser():
    cal = build_calendar([_make_deadline()])
    raw = cal.to_ical()
    parsed = Calendar.from_ical(raw)
    events = [c for c in parsed.walk("VEVENT")]
    assert len(events) == 1
    assert "Submit midterm" in str(events[0]["SUMMARY"])


def test_summary_includes_confidence_flag_for_non_high():
    cal = build_calendar([_make_deadline(confidence="medium")])
    [event] = cal.walk("VEVENT")
    assert "[MEDIUM]" in str(event["SUMMARY"])

    cal = build_calendar([_make_deadline()])  # high
    [event] = cal.walk("VEVENT")
    assert "[HIGH]" not in str(event["SUMMARY"])
