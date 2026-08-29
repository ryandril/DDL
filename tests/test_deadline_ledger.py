"""Behaviour of the durable deadline ledger.

The ledger is what makes the daily message show every upcoming deadline rather
than only the ones extracted today, so these cover accumulation, pruning, the
New/Existing split, and the reschedule cases that motivated it.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent))

from deadline_ledger import classify, load_ledger, merge

TZ = "Asia/Shanghai"


def dl(when: str, *, title="Case write-up", course="Strategy II", ann_id=1,
       confidence="high", quote="due ...", tz=TZ) -> dict:
    return {
        "title": title,
        "datetime_local": when,
        "timezone": tz,
        "all_day": False,
        "confidence": confidence,
        "evidence_quote": quote,
        "source_announcement_id": ann_id,
        "source_url": "https://canvas.example.edu/courses/1/discussion_topics/1",
        "source_course": course,
    }


def local(days: int) -> str:
    """A local wall time `days` from now, in the ledger's timezone."""
    from zoneinfo import ZoneInfo
    return (datetime.now(tz=ZoneInfo(TZ)) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")


def empty() -> dict:
    return {"deadlines": [], "updated_at": None}


# --- accumulation ----------------------------------------------------------

def test_merge_records_when_a_deadline_was_first_seen():
    led = merge(empty(), [dl(local(3))], run_id="run-1")
    assert len(led["deadlines"]) == 1
    assert led["deadlines"][0]["_first_seen"] == "run-1"


def test_re_extracting_the_same_deadline_does_not_duplicate_it():
    led = merge(empty(), [dl(local(3))], run_id="run-1")
    led = merge(led, [dl(local(3))], run_id="run-2")
    assert len(led["deadlines"]) == 1
    # still attributed to the first run, but touched by the second
    assert led["deadlines"][0]["_first_seen"] == "run-1"
    assert led["deadlines"][0]["_last_seen"] == "run-2"


def test_a_deadline_survives_a_run_that_extracts_nothing():
    """The whole reason the ledger exists: Canvas only returns *new* posts."""
    led = merge(empty(), [dl(local(5))], run_id="run-1")
    led = merge(led, [], run_id="run-2")
    view = classify(led, run_id="run-2", tz_name=TZ)
    assert len(view["all_upcoming"]) == 1
    assert len(view["existing"]) == 1


def test_merge_keeps_the_higher_confidence_of_two_sightings():
    led = merge(empty(), [dl(local(3), confidence="medium")], run_id="run-1")
    led = merge(led, [dl(local(3), confidence="high")], run_id="run-2")
    assert led["deadlines"][0]["confidence"] == "high"


def test_two_deliverables_due_at_the_same_moment_stay_separate():
    when = local(4)
    led = merge(empty(), [dl(when, title="Case analysis"), dl(when, title="Peer evaluation")],
                run_id="run-1")
    assert len(led["deadlines"]) == 2


# --- New vs Existing -------------------------------------------------------

def test_first_sighting_is_new_and_later_runs_are_existing():
    led = merge(empty(), [dl(local(3))], run_id="run-1")
    first = classify(led, run_id="run-1", tz_name=TZ)
    assert len(first["new"]) == 1 and first["existing"] == []

    led = merge(led, [], run_id="run-2")
    second = classify(led, run_id="run-2", tz_name=TZ)
    assert second["new"] == [] and len(second["existing"]) == 1


# --- pruning ---------------------------------------------------------------

def test_passed_deadlines_are_pruned_from_the_view_and_the_stored_ledger():
    led = merge(empty(), [dl(local(-1), title="Old"), dl(local(2), title="Upcoming")],
                run_id="run-1")
    view = classify(led, run_id="run-1", tz_name=TZ)

    assert view["pruned_count"] == 1
    assert [d["title"] for d in view["all_upcoming"]] == ["Upcoming"]
    # and it is gone from the ledger, so the file cannot grow forever
    assert [d["title"] for d in led["deadlines"]] == ["Upcoming"]


def test_each_record_is_judged_in_its_own_timezone():
    """A record carries its own tz; the ledger default is only a fallback."""
    led = merge(empty(), [dl(local(1), tz="America/New_York")], run_id="run-1")
    view = classify(led, run_id="run-1", tz_name=TZ)
    assert len(view["all_upcoming"]) == 1  # not misclassified as passed


def test_upcoming_is_sorted_by_due_time():
    led = merge(empty(), [dl(local(9), title="Later"), dl(local(2), title="Sooner")],
                run_id="run-1")
    view = classify(led, run_id="run-1", tz_name=TZ)
    assert [d["title"] for d in view["all_upcoming"]] == ["Sooner", "Later"]


# --- reschedules -----------------------------------------------------------

def test_re_extracting_an_announcement_supersedes_its_stale_datetime():
    led = merge(empty(), [dl(local(3), ann_id=7)], run_id="run-1")
    # the same announcement, re-read, now says a different date
    led = merge(led, [dl(local(6), ann_id=7)], run_id="run-2")
    assert len(led["deadlines"]) == 1
    assert led["deadlines"][0]["datetime_local"] == local(6)


def test_same_title_from_a_different_announcement_is_flagged_not_dropped():
    led = merge(empty(), [dl(local(3), ann_id=1)], run_id="run-1")
    led = merge(led, [dl(local(6), ann_id=2)], run_id="run-2")
    view = classify(led, run_id="run-2", tz_name=TZ)

    # both kept — silently dropping a real deadline is worse than a duplicate
    assert len(view["all_upcoming"]) == 2
    assert all(d.get("_possible_reschedule_of") for d in view["all_upcoming"])


def test_the_reschedule_flag_clears_itself_once_the_conflict_is_gone():
    led = merge(empty(), [dl(local(-1), ann_id=1), dl(local(6), ann_id=2)], run_id="run-1")
    view = classify(led, run_id="run-1", tz_name=TZ)
    assert len(view["all_upcoming"]) == 1
    assert "_possible_reschedule_of" not in view["all_upcoming"][0]


# --- durability ------------------------------------------------------------

def test_a_missing_ledger_file_starts_empty(tmp_path):
    assert load_ledger(tmp_path / "nope.json") == {"deadlines": [], "updated_at": None}


def test_a_corrupt_ledger_file_does_not_crash_the_run(tmp_path):
    p = tmp_path / "ledger.json"
    p.write_text("{not json")
    assert load_ledger(p)["deadlines"] == []


def test_a_legacy_bare_list_ledger_still_loads(tmp_path):
    p = tmp_path / "ledger.json"
    p.write_text('[{"title": "x", "datetime_local": "2026-05-15T09:00:00"}]')
    assert len(load_ledger(p)["deadlines"]) == 1
