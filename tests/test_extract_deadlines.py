"""Behaviour of the deterministic extractor.

These pin down the precision-first contract: a date only becomes a deadline when
a cue word sits near it, and the time conventions in the README are what the
code actually does.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from extract_deadlines import extract_from_announcement

TZ = "Asia/Shanghai"
POSTED = "2026-05-08T03:21:00+00:00"


def ann(body: str, *, title: str = "Announcement", posted: str = POSTED, id_: int = 1) -> dict:
    return {
        "id": id_,
        "title": title,
        "body_text": body,
        "posted_at": posted,
        "html_url": "https://canvas.example.edu/courses/1/discussion_topics/1",
        "course_name": "Strategy II",
    }


def only(body: str, **kw) -> dict:
    found = extract_from_announcement(ann(body, **kw), TZ)
    assert len(found) == 1, f"expected exactly 1 deadline, got {len(found)}: {found}"
    return found[0]


# --- what counts as a deadline -------------------------------------------

def test_explicit_date_and_time_is_high_confidence():
    d = only("The midterm is due Friday, May 15 at 5:00pm. Submit on Canvas.")
    assert d["datetime_local"] == "2026-05-15T17:00:00"
    assert d["confidence"] == "high"
    assert d["timezone"] == TZ


def test_date_without_a_time_defaults_to_end_of_day_and_medium():
    d = only("The case write-up is due May 20.")
    assert d["datetime_local"] == "2026-05-20T23:59:00"
    assert d["confidence"] == "medium"


def test_a_date_with_no_cue_word_is_not_a_deadline():
    assert extract_from_announcement(ann("We will cover chapter 4 on May 15."), TZ) == []


def test_office_hours_are_not_deadlines():
    assert extract_from_announcement(
        ann("Office hours move to May 15 this week."), TZ
    ) == []


def test_cue_still_wins_when_an_anti_cue_is_merely_nearby():
    # "grade" is an anti-cue, but an explicit "due" must not be suppressed by it.
    d = only("Your graded feedback is out; the revision is due May 22.")
    assert d["datetime_local"] == "2026-05-22T23:59:00"


# --- time conventions ------------------------------------------------------

@pytest.mark.parametrize(
    "phrase,expected",
    [
        ("due May 15 at noon", "2026-05-15T12:00:00"),
        ("due May 15 by EOD", "2026-05-15T23:59:00"),
        ("due May 15 at midnight", "2026-05-15T23:59:00"),
        ("due May 15 at 9am", "2026-05-15T09:00:00"),
        ("due May 15 at 11:59 pm", "2026-05-15T23:59:00"),
        ("due May 15 at 12:30", "2026-05-15T12:30:00"),
    ],
)
def test_time_conventions(phrase, expected):
    assert only(f"The assignment is {phrase}.")["datetime_local"] == expected


# --- date shapes -----------------------------------------------------------

@pytest.mark.parametrize(
    "phrase",
    ["due May 15", "due 15 May", "due 2026-05-15", "due May 15th"],
)
def test_recognised_date_shapes(phrase):
    assert only(f"Report {phrase}.")["datetime_local"].startswith("2026-05-15")


def test_year_rolls_forward_when_the_date_would_be_far_in_the_past():
    # Posted in December, "due January 10" means next January.
    d = only("Final paper due January 10.", posted="2026-12-01T00:00:00+00:00")
    assert d["datetime_local"].startswith("2027-01-10")


# --- known limitation (documented in the README) ---------------------------

@pytest.mark.parametrize(
    "phrase",
    ["due this Monday by EOD", "due next Wednesday", "due tomorrow"],
)
def test_relative_dates_are_not_yet_supported(phrase):
    """Weekday and relative phrasing is a known gap — see README.

    This test documents current behaviour rather than endorsing it. When
    relative-date resolution lands, invert it.
    """
    assert extract_from_announcement(ann(f"The reflection is {phrase}."), TZ) == []


# --- precision guards ------------------------------------------------------

def test_a_time_does_not_bleed_across_a_neighbouring_date():
    found = extract_from_announcement(
        ann("Case A is due 5pm July 3. Case B is due July 10."), TZ
    )
    by_date = {d["datetime_local"][:10]: d for d in found}
    assert by_date["2026-07-03"]["datetime_local"].endswith("17:00:00")
    # July 10 has no time of its own, so it must fall back to end of day.
    assert by_date["2026-07-10"]["datetime_local"].endswith("23:59:00")


def test_the_same_deadline_restated_collapses_to_one_record():
    found = extract_from_announcement(
        ann("Essay due May 15 at 5:00pm. Reminder: the essay is due May 15 at 5:00pm."),
        TZ,
    )
    assert len(found) == 1


def test_provenance_is_carried_through():
    d = only("Quiz due May 15.", id_=5001)
    assert d["source_announcement_id"] == 5001
    assert d["source_course"] == "Strategy II"
    assert d["source_url"].startswith("https://canvas.example.edu/")
    assert d["evidence_quote"]  # non-empty, so a human can check the claim
