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


# --- titles name the right deliverable -------------------------------------

def test_title_comes_from_the_deliverable_in_the_same_sentence():
    found = extract_from_announcement(
        ann("Case analysis due 5pm Nov 3. Peer evaluation due Nov 10."), TZ
    )
    by_date = {d["datetime_local"][:10]: d["title"] for d in found}
    assert by_date["2026-11-03"] == "Case Analysis"
    assert by_date["2026-11-10"] == "Peer Evaluation"


def test_a_deliverable_in_a_neighbouring_sentence_does_not_steal_the_title():
    d = only("The interim report is due Nov 3. Peer evaluation opens afterwards.")
    assert d["title"] == "Interim Report"


def test_problem_sets_and_homework_are_recognised_deliverables():
    assert only("Problem set 4 is due Nov 3.")["title"] == "Problem Set"
    assert only("Homework is due Nov 3.")["title"] == "Homework"


# --- numeric date shapes ---------------------------------------------------

@pytest.mark.parametrize(
    "phrase,expected",
    [
        ("due 5/15/2026 at 5pm", "2026-05-15T17:00:00"),   # month-first, explicit year
        ("due 15/05/2026", "2026-05-15T23:59:00"),          # day-first, unambiguous
        ("due 15.05.2026", "2026-05-15T23:59:00"),          # dotted, day-first
        ("due 5/15/26", "2026-05-15T23:59:00"),             # two-digit year
        ("due 5/15", "2026-05-15T23:59:00"),                # no year, from posted_at
    ],
)
def test_numeric_date_shapes(phrase, expected):
    assert only(f"The report is {phrase}.")["datetime_local"] == expected


def test_ambiguous_numeric_date_follows_the_configured_order():
    from extract_deadlines import extract_from_announcement as ex
    a = ann("The report is due 5/6/2026.")
    assert ex(a, TZ, date_order="MDY")[0]["datetime_local"].startswith("2026-05-06")
    assert ex(a, TZ, date_order="DMY")[0]["datetime_local"].startswith("2026-06-05")


def test_an_impossible_numeric_date_is_ignored():
    assert extract_from_announcement(ann("Submit form 13/32/2026 for review."), TZ) == []


def test_a_decimal_number_is_not_read_as_a_date():
    assert extract_from_announcement(
        ann("Submissions must be under 1.5 MB; the limit is strict."), TZ
    ) == []


def test_an_iso_date_is_not_double_matched_as_a_numeric_date():
    found = extract_from_announcement(ann("Report due 2026/05/15."), TZ)
    assert len(found) == 1
    assert found[0]["datetime_local"].startswith("2026-05-15")


# --- precision: things that look like deadlines but are not -----------------

def test_a_date_inside_a_url_is_not_a_deadline():
    assert extract_from_announcement(
        ann("Submission spec: https://canvas.example.edu/files/2026-05-15/spec.pdf"), TZ
    ) == []


@pytest.mark.parametrize(
    "phrase",
    [
        "The report was due May 15 and has now been graded.",
        "The essay were due May 15, so marks are final.",
        "The paper had been due May 15 before the extension.",
    ],
)
def test_a_past_tense_cue_is_not_a_live_deadline(phrase):
    assert extract_from_announcement(ann(phrase), TZ) == []


@pytest.mark.parametrize(
    "phrase",
    [
        "The May 15 deadline is cancelled; no submission is required.",
        "The May 15 submission has been canceled.",
        "The quiz due May 15 is waived this term.",
        "You no longer need to submit the report on May 15.",
    ],
)
def test_a_cancelled_deadline_is_not_extracted(phrase):
    assert extract_from_announcement(ann(phrase), TZ) == []


def test_a_postponed_deadline_is_still_a_deadline():
    # "postponed to" moves a deadline; it does not remove one.
    d = only("The report deadline is postponed to May 22.")
    assert d["datetime_local"].startswith("2026-05-22")
