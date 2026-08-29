"""Verify digest composition produces the expected structure for various inputs."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import emailer
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


# --- email fallback delivery ----------------------------------------------

class FakeSMTP:
    """Stands in for smtplib.SMTP. Records what a real server would receive."""
    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, fail_on_send=False):
        self.host, self.port = host, port
        self.started_tls = False
        self.login_args = None
        self.sent = []
        self.fail_on_send = fail_on_send
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.started_tls = True

    def login(self, user, password):
        self.login_args = (user, password)

    def send_message(self, msg):
        if self.fail_on_send:
            raise OSError("server said no")
        self.sent.append(msg)


@pytest.fixture
def outbox(tmp_path):
    (tmp_path / "subject.txt").write_text("[Canvas] 2 deadlines (May 15)")
    (tmp_path / "digest.txt").write_text("Group Report — due Fri 15 May 17:00")
    (tmp_path / "digest.html").write_text("<p>Group Report</p>")
    (tmp_path / "deadlines.ics").write_text("BEGIN:VCALENDAR\nEND:VCALENDAR\n")
    return tmp_path


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS",
              "EMAIL_FROM", "EMAIL_TO", "SMTP_STARTTLS"):
        monkeypatch.delenv(k, raising=False)
    FakeSMTP.instances = []


def _configure(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USER", "me@example.com")
    monkeypatch.setenv("SMTP_PASS", "hunter2")
    monkeypatch.setenv("EMAIL_TO", "me@example.com")


def test_send_refuses_when_no_smtp_host_is_configured(outbox, monkeypatch):
    monkeypatch.setenv("EMAIL_TO", "me@example.com")
    assert emailer.send(outbox, smtp_factory=FakeSMTP) is False
    assert FakeSMTP.instances == []  # never dialled out


def test_send_refuses_when_no_recipient_is_configured(outbox, monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    assert emailer.send(outbox, smtp_factory=FakeSMTP) is False
    assert FakeSMTP.instances == []


def test_send_delivers_the_subject_and_both_bodies(outbox, monkeypatch):
    _configure(monkeypatch)
    assert emailer.send(outbox, smtp_factory=FakeSMTP) is True
    smtp = FakeSMTP.instances[0]
    assert smtp.started_tls is True
    assert smtp.login_args == ("me@example.com", "hunter2")
    msg = smtp.sent[0]
    assert msg["Subject"] == "[Canvas] 2 deadlines (May 15)"
    assert msg["To"] == "me@example.com"
    bodies = {p.get_content_type() for p in msg.walk()}
    assert "text/plain" in bodies and "text/html" in bodies


def test_send_attaches_the_ics_when_one_exists(outbox, monkeypatch):
    _configure(monkeypatch)
    emailer.send(outbox, smtp_factory=FakeSMTP)
    names = [p.get_filename() for p in FakeSMTP.instances[0].sent[0].walk()]
    assert "deadlines.ics" in names


def test_send_works_when_there_is_no_ics_to_attach(outbox, monkeypatch):
    _configure(monkeypatch)
    (outbox / "deadlines.ics").unlink()
    assert emailer.send(outbox, smtp_factory=FakeSMTP) is True


def test_send_reports_failure_instead_of_raising(outbox, monkeypatch):
    _configure(monkeypatch)
    factory = lambda h, p: FakeSMTP(h, p, fail_on_send=True)
    assert emailer.send(outbox, smtp_factory=factory) is False


def test_multiple_recipients_are_split_on_commas(outbox, monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("EMAIL_TO", "a@example.com, b@example.com")
    emailer.send(outbox, smtp_factory=FakeSMTP)
    assert FakeSMTP.instances[0].sent[0]["To"] == "a@example.com, b@example.com"
