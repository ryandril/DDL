"""Calendar identity and upsert behaviour.

The stable uid is the contract between the ledger and Google Calendar: two
records the ledger keeps apart must not collapse into one calendar event.
No network and no `gog` binary — these exercise pure identity/dedupe logic.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from gcal_sync import deadline_uid, legacy_deadline_uid, upsert
from models import ExtractedDeadline


def dl(title: str, dt: str, *, ann_id: int = 77) -> ExtractedDeadline:
    return ExtractedDeadline(
        title=title,
        datetime_local=dt,
        timezone="Asia/Shanghai",
        all_day=False,
        confidence="high",
        evidence_quote="",
        source_announcement_id=ann_id,
        source_url="",
        source_course="Strategy II",
    )


# --- uid identity ----------------------------------------------------------

def test_uid_is_stable_for_the_same_deadline():
    a = dl("Group Report", "2026-11-03T17:00:00")
    b = dl("Group Report", "2026-11-03T17:00:00")
    assert deadline_uid(a) == deadline_uid(b)


def test_uid_differs_when_only_the_due_time_differs():
    """The ledger keys on course|datetime|title. The calendar must agree, or a
    second deadline silently overwrites the first."""
    a = dl("Peer Evaluation", "2026-11-03T17:00:00")
    b = dl("Peer Evaluation", "2026-11-10T23:59:00")
    assert deadline_uid(a) != deadline_uid(b)


def test_uid_differs_across_announcements():
    a = dl("Group Report", "2026-11-03T17:00:00", ann_id=1)
    b = dl("Group Report", "2026-11-03T17:00:00", ann_id=2)
    assert deadline_uid(a) != deadline_uid(b)


# --- upsert dedupe ---------------------------------------------------------

def test_same_title_at_two_times_creates_two_events():
    existing: dict[str, dict] = {}
    a = dl("Peer Evaluation", "2026-11-03T17:00:00")
    b = dl("Peer Evaluation", "2026-11-10T23:59:00")
    assert upsert("cal", a, existing, dry_run=True) == "inserted"
    assert upsert("cal", b, existing, dry_run=True) == "inserted"
    assert len(existing) == 2


def test_re_running_the_same_deadline_is_unchanged():
    existing: dict[str, dict] = {}
    a = dl("Group Report", "2026-11-03T17:00:00")
    assert upsert("cal", a, existing, dry_run=True) == "inserted"
    # A real second run sees the event Google actually stored.
    from gcal_sync import _summary
    existing[deadline_uid(a)] = {"id": "e1", "summary": _summary(a),
                                 "start": {"dateTime": "2026-11-03T17:00:00+08:00"}}
    assert upsert("cal", a, existing, dry_run=True) == "unchanged"


# --- migration off the old title-only uid ----------------------------------

def test_an_event_created_under_the_legacy_uid_is_updated_not_duplicated():
    a = dl("Group Report", "2026-11-03T17:00:00")
    existing = {legacy_deadline_uid(a): {"id": "old-1", "summary": "stale", "start": {}}}
    assert upsert("cal", a, existing, dry_run=True) == "updated"


def test_a_legacy_event_is_claimed_only_once():
    """Two deadlines share a legacy uid (that was the bug). Only the first may
    adopt the old event; the second must get its own."""
    a = dl("Peer Evaluation", "2026-11-03T17:00:00")
    b = dl("Peer Evaluation", "2026-11-10T23:59:00")
    assert legacy_deadline_uid(a) == legacy_deadline_uid(b)
    existing = {legacy_deadline_uid(a): {"id": "old-1", "summary": "stale", "start": {}}}
    assert upsert("cal", a, existing, dry_run=True) == "updated"
    assert upsert("cal", b, existing, dry_run=True) == "inserted"
