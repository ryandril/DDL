#!/usr/bin/env python3
"""Persistent deadline ledger: the source of truth for "all known upcoming deadlines".

The bash cron fetches only *new* announcements each run, so deadlines extracted
today would vanish tomorrow. This ledger fixes that: it accumulates every
extracted deadline across runs, drops the ones that have passed, and labels the
rest NEW (first seen this run) vs EXISTING (carried over from a previous run).

Flow each run:
    deadlines.json (this run's extraction)  --merge-->  deadlines_ledger.json
    deadlines_ledger.json  --prune past + classify-->  out/ledger_view.json

ledger_view.json is what send_wechat_alert.py renders, so the WeChat message
shows the full upcoming list (New + Existing) every run, never a passed item.

Usage:
    python3 deadline_ledger.py --out-dir out                # normal run
    python3 deadline_ledger.py --out-dir out --run-id seed  # seed/backfill
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = pathlib.Path(os.environ.get("DDL_DIR") or pathlib.Path(__file__).resolve().parent)

LEDGER_DEFAULT = BASE_DIR / "deadlines_ledger.json"
TZ_DEFAULT = "Asia/Shanghai"


def _key(d: dict) -> str:
    """Identity of a deadline = course + due datetime + title. Reminders of the
    same deadline (posted in different announcements) collapse to one entry, but
    two distinct deliverables due at the same moment (e.g. a case analysis AND a
    peer evaluation) stay separate so the count matches the calendar."""
    course = re.sub(r"\s+", " ", (d.get("source_course") or "").strip().lower())
    title = re.sub(r"\s+", " ", (d.get("title") or "").strip().lower())
    return f"{course}|{d.get('datetime_local', '')}|{title}"


def _conf_rank(c: str) -> int:
    return {"high": 3, "medium": 2, "low": 1}.get(c, 0)


def load_ledger(path: Path) -> dict:
    if not path.exists():
        return {"deadlines": [], "updated_at": None}
    try:
        data = json.loads(path.read_text())
        if isinstance(data, list):  # tolerate a bare-list legacy file
            data = {"deadlines": data, "updated_at": None}
        data.setdefault("deadlines", [])
        return data
    except Exception as e:
        print(f"[ledger] WARNING: ledger unreadable ({e}); starting fresh", file=sys.stderr)
        return {"deadlines": [], "updated_at": None}


def _group(d: dict) -> str:
    """Coarser identity than _key: course + announcement + title, WITHOUT the
    datetime. All records in one group describe the same deliverable as stated
    by one announcement — so when that announcement is re-extracted, its current
    set of datetimes is the truth and stale ones must be superseded."""
    course = re.sub(r"\s+", " ", (d.get("source_course") or "").strip().lower())
    title = re.sub(r"\s+", " ", (d.get("title") or "").strip().lower())
    return f"{course}|{d.get('source_announcement_id', '')}|{title}"


def merge(ledger: dict, new_deadlines: list[dict], run_id: str) -> dict:
    by_key = {_key(d): d for d in ledger["deadlines"]}

    # Supersede rescheduled deadlines: if this batch re-extracts an announcement,
    # the batch's datetimes for a (course, announcement, title) group replace the
    # stored ones — an announcement now reading "due July 12" must not leave a
    # stale "due July 10" entry behind. Scoped to the SAME announcement id only:
    # a same-titled deadline from a *different* announcement is kept (it may be a
    # second real deliverable, and silently dropping a deadline is worse than
    # showing a duplicate — classify() flags those as possible reschedules).
    incoming_groups: dict[str, set[str]] = {}
    for nd in new_deadlines:
        if nd.get("datetime_local"):
            incoming_groups.setdefault(_group(nd), set()).add(nd["datetime_local"])
    superseded = [
        d for d in ledger["deadlines"]
        if _group(d) in incoming_groups
        and d.get("datetime_local") not in incoming_groups[_group(d)]
    ]
    for d in superseded:
        print(f"[ledger] superseding rescheduled entry: {d.get('title')!r} "
              f"was {d.get('datetime_local')}", file=sys.stderr)
        by_key.pop(_key(d), None)

    for nd in new_deadlines:
        k = _key(nd)
        if not nd.get("datetime_local"):
            continue
        if k in by_key:
            cur = by_key[k]
            cur["_last_seen"] = run_id
            # keep the richest info: prefer higher confidence + longer evidence
            if _conf_rank(nd.get("confidence", "")) > _conf_rank(cur.get("confidence", "")):
                cur["confidence"] = nd["confidence"]
            if len(nd.get("evidence_quote", "")) > len(cur.get("evidence_quote", "")):
                cur["evidence_quote"] = nd["evidence_quote"]
            if nd.get("source_url") and not cur.get("source_url"):
                cur["source_url"] = nd["source_url"]
        else:
            rec = dict(nd)
            rec["_first_seen"] = run_id
            rec["_last_seen"] = run_id
            by_key[k] = rec
    ledger["deadlines"] = list(by_key.values())
    return ledger


def classify(ledger: dict, run_id: str, tz_name: str) -> dict:
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz=tz)
    upcoming_new, upcoming_existing, passed = [], [], []
    for d in ledger["deadlines"]:
        try:
            # Each record carries its own timezone; the ledger default is only a
            # fallback. Comparing a record's wall time in the wrong zone can
            # misclassify near-boundary deadlines by hours.
            try:
                rec_tz = ZoneInfo(d.get("timezone") or tz_name)
            except Exception:
                rec_tz = tz
            dt = datetime.fromisoformat(d["datetime_local"]).replace(tzinfo=rec_tz)
        except Exception:
            continue
        if dt < now:
            passed.append(d)
            continue
        (upcoming_new if d.get("_first_seen") == run_id else upcoming_existing).append(d)

    # Flag possible reschedules: the same course+title upcoming on two different
    # dates (necessarily from different announcements — same-announcement dupes
    # were superseded in merge()). Recomputed fresh each run so the flag clears
    # itself once one of the pair passes or is removed.
    by_ct: dict[tuple[str, str], list[dict]] = {}
    for d in upcoming_new + upcoming_existing:
        d.pop("_possible_reschedule_of", None)
        course = re.sub(r"\s+", " ", (d.get("source_course") or "").strip().lower())
        title = re.sub(r"\s+", " ", (d.get("title") or "").strip().lower())
        by_ct.setdefault((course, title), []).append(d)
    for group in by_ct.values():
        dts = sorted({g["datetime_local"] for g in group})
        if len(dts) > 1:
            for g in group:
                g["_possible_reschedule_of"] = [t for t in dts if t != g["datetime_local"]]

    # prune passed deadlines out of the stored ledger so it can't grow forever
    keep = set(id(x) for x in upcoming_new) | set(id(x) for x in upcoming_existing)
    ledger["deadlines"] = [d for d in ledger["deadlines"] if id(d) in keep]
    ledger["updated_at"] = now.isoformat()

    skey = lambda x: x["datetime_local"]
    return {
        "generated_at": now.isoformat(),
        "now_local": now.strftime("%Y-%m-%dT%H:%M:%S"),
        "timezone": tz_name,
        "run_id": run_id,
        "new": sorted(upcoming_new, key=skey),
        "existing": sorted(upcoming_existing, key=skey),
        "all_upcoming": sorted(upcoming_new + upcoming_existing, key=skey),
        "pruned_count": len(passed),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out-dir", type=Path, default=Path("out"))
    p.add_argument("--ledger", type=Path, default=LEDGER_DEFAULT)
    p.add_argument("--deadlines", type=Path, default=None,
                   help="this run's extraction (default: <out-dir>/deadlines.json)")
    p.add_argument("--run-id", default=None,
                   help="identifier for this run (default: now ISO). Use 'seed' for backfill.")
    p.add_argument("--tz", default=TZ_DEFAULT)
    args = p.parse_args(argv)

    run_id = args.run_id or datetime.now(tz=ZoneInfo(args.tz)).isoformat()
    deadlines_path = args.deadlines or (args.out_dir / "deadlines.json")
    new_deadlines = []
    if deadlines_path.exists():
        try:
            new_deadlines = json.loads(deadlines_path.read_text())
        except Exception as e:
            print(f"[ledger] WARNING: {deadlines_path} unreadable ({e})", file=sys.stderr)

    ledger = load_ledger(args.ledger)
    ledger = merge(ledger, new_deadlines, run_id)
    view = classify(ledger, run_id, args.tz)

    args.ledger.write_text(json.dumps(ledger, indent=2, ensure_ascii=False))
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "ledger_view.json").write_text(json.dumps(view, indent=2, ensure_ascii=False))

    print(f"[ledger] upcoming: {len(view['new'])} new + {len(view['existing'])} existing "
          f"| pruned {view['pruned_count']} passed | run_id={run_id}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
