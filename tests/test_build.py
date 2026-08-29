"""Which deadline list the digest is built from.

The digest must reflect the DURABLE ledger, not the per-run extraction: the
per-run file is empty on most days and stale on the rest, and a notifier that
fires daily would otherwise re-send yesterday's deadlines.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from build import read_deadline_records

LEDGER_ITEM = {"title": "Group Report", "datetime_local": "2026-11-03T17:00:00"}
PER_RUN_ITEM = {"title": "Stale Essay", "datetime_local": "2026-05-15T17:00:00"}


def test_prefers_the_durable_ledger_view_over_the_per_run_extraction(tmp_path):
    (tmp_path / "ledger_view.json").write_text(json.dumps({"all_upcoming": [LEDGER_ITEM]}))
    (tmp_path / "deadlines.json").write_text(json.dumps([PER_RUN_ITEM]))
    assert read_deadline_records(tmp_path) == [LEDGER_ITEM]


def test_an_empty_ledger_view_yields_nothing_rather_than_stale_deadlines(tmp_path):
    """The whole point: no upcoming deadlines must render as no deadlines, not
    as whatever the last announcement-bearing day happened to extract."""
    (tmp_path / "ledger_view.json").write_text(json.dumps({"all_upcoming": []}))
    (tmp_path / "deadlines.json").write_text(json.dumps([PER_RUN_ITEM]))
    assert read_deadline_records(tmp_path) == []


def test_falls_back_to_the_per_run_extraction_when_no_ledger_view_exists(tmp_path):
    (tmp_path / "deadlines.json").write_text(json.dumps([PER_RUN_ITEM]))
    assert read_deadline_records(tmp_path) == [PER_RUN_ITEM]


def test_accepts_the_raw_ledger_shape(tmp_path):
    (tmp_path / "ledger_view.json").write_text(json.dumps({"deadlines": [LEDGER_ITEM]}))
    assert read_deadline_records(tmp_path) == [LEDGER_ITEM]


def test_returns_empty_when_there_is_nothing_to_read(tmp_path):
    assert read_deadline_records(tmp_path) == []
