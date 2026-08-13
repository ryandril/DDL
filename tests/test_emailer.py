"""Verify digest composition produces the expected structure for various inputs."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from canvas_client import Announcement
from datetime import datetime, timezone
from emailer import compose
from models import AnnouncementSummary, ExtractedDeadline


def _ann(id_=5001, course="Strategy II", title="Test") -> Announcement:
    return Announcement(
        id=id_,
        course_id=101,
        course_name=course,
        title=title,
        message_html="",
        posted_at=datetime(2026, 5, 8, tzinfo=timezone.utc),
        html_url=f"https://canvas.example.edu/courses/101/discussion_topics/{id_}",
    )


def test_subject_when_no_new_content():
    artifacts = compose(deadlines=[], summaries=[], announcements_by_id={}, run_date_label="May 8", ics_path=None)
    assert artifacts.deadline_count == 0
    assert artifacts.announcement_count == 0
    assert "No new" in artifacts.subject


def test_subject_lists_both_counts_when_present():
    a = _ann()
    s = AnnouncementSummary(announcement_id=a.id, summary="Summary line")
    d = ExtractedDeadline(
        title="Submit midterm", datetime_local="2026-05-15T17:00:00", timezone="Asia/Shanghai",
        all_day=False, confidence="high", evidence_quote="due Friday",
        source_announcement_id=a.id, source_url=a.html_url, source_course=a.course_name,
    )
    artifacts = compose(deadlines=[d], summaries=[s], announcements_by_id={a.id: a}, run_date_label="May 8", ics_path=Path("deadlines.ics"))
    assert "1 deadline" in artifacts.subject
    assert "1 announcement" in artifacts.subject


def test_html_includes_evidence_quote_and_url():
    a = _ann()
    d = ExtractedDeadline(
        title="Submit midterm", datetime_local="2026-05-15T17:00:00", timezone="Asia/Shanghai",
        all_day=False, confidence="medium", evidence_quote="due Friday May 15",
        source_announcement_id=a.id, source_url=a.html_url, source_course=a.course_name,
    )
    artifacts = compose(deadlines=[d], summaries=[], announcements_by_id={a.id: a}, run_date_label="May 8", ics_path=Path("deadlines.ics"))
    assert "due Friday May 15" in artifacts.html_body
    assert a.html_url in artifacts.html_body
    assert "[MEDIUM]" in artifacts.html_body
