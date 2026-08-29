"""Shared data shapes — used by Canvas client, builder, emailer, and tests."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Confidence = Literal["high", "medium", "low"]


@dataclass
class ExtractedDeadline:
    title: str
    datetime_local: str  # ISO 8601 local, no offset, e.g. "2026-11-14T17:00:00"
    timezone: str
    all_day: bool
    confidence: Confidence
    evidence_quote: str
    source_announcement_id: int
    source_url: str
    source_course: str

    @classmethod
    def from_dict(cls, d: dict) -> "ExtractedDeadline":
        return cls(
            title=d["title"],
            datetime_local=d["datetime_local"],
            timezone=d["timezone"],
            all_day=bool(d.get("all_day", False)),
            confidence=d.get("confidence", "low"),
            evidence_quote=d.get("evidence_quote", ""),
            source_announcement_id=int(d["source_announcement_id"]),
            source_url=d.get("source_url", ""),
            source_course=d.get("source_course", ""),
        )


@dataclass
class AnnouncementSummary:
    announcement_id: int
    summary: str  # one-line auto-summary for the digest

    @classmethod
    def from_dict(cls, d: dict) -> "AnnouncementSummary":
        return cls(announcement_id=int(d["announcement_id"]), summary=d["summary"])
