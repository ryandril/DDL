"""Thin Canvas LMS API client. Read-only: courses + announcements."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator

import httpx


@dataclass
class Course:
    id: int
    name: str
    course_code: str


@dataclass
class CanvasFile:
    id: int
    course_id: int
    course_name: str
    name: str
    size: int  # bytes
    content_type: str
    url: str
    updated_at: datetime


@dataclass
class Announcement:
    id: int
    course_id: int
    course_name: str
    title: str
    message_html: str
    posted_at: datetime
    html_url: str


class CanvasClient:
    def __init__(self, base_url: str, token: str, timeout: float = 30.0, trust_env: bool = False):
        # trust_env=False by default: ignore HTTPS_PROXY/HTTP_PROXY from the shell.
        # Without this, sandbox/system proxies can mangle the TLS handshake to Canvas.
        # Set CANVAS_USE_SYSTEM_PROXY=1 in .env to opt back in if your network requires it.
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=f"{self.base_url}/api/v1",
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            trust_env=trust_env,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _paginate(self, path: str, params: dict | None = None) -> Iterator[dict]:
        url: str | None = path
        first = True
        while url:
            resp = self._client.get(url, params=params if first else None)
            resp.raise_for_status()
            for item in resp.json():
                yield item
            url = _next_link(resp.headers.get("link", ""))
            if url and url.startswith(self.base_url):
                # httpx wants the path relative to base_url; strip the prefix.
                url = url[len(self.base_url) + len("/api/v1"):]
            first = False

    def list_active_courses(self) -> list[Course]:
        params = {
            "enrollment_state": "active",
            "per_page": 100,
            "include[]": "term",
        }
        out: list[Course] = []
        for c in self._paginate("/courses", params=params):
            if c.get("access_restricted_by_date"):
                continue
            out.append(
                Course(
                    id=c["id"],
                    name=c.get("name") or c.get("course_code") or f"course_{c['id']}",
                    course_code=c.get("course_code") or "",
                )
            )
        return out

    def list_files(self, course_id: int, course_name: str | None = None) -> list[CanvasFile]:
        """List all files in a course."""
        params = {"per_page": 100, "include[]": ["user"]}
        name = course_name or f"course_{course_id}"
        files = []
        for f in self._paginate(f"/courses/{course_id}/files", params=params):
            files.append(
                CanvasFile(
                    id=f["id"],
                    course_id=course_id,
                    course_name=course_name,
                    name=f.get("display_name", "") or f.get("filename", "unknown"),
                    size=f.get("size", 0) or 0,
                    content_type=f.get("content-type", "application/octet-stream"),
                    url=f.get("url", ""),
                    updated_at=_parse_iso(f.get("updated_at", "")),
                )
            )
        return files

    def search_files(self, query: str, courses: list[Course] | None = None) -> list[CanvasFile]:
        """Search for files by name across all courses or a specific course list."""
        if courses:
            target_courses = courses
        else:
            target_courses = self.list_active_courses()
        self._course_name_by_id = {c.id: c.name for c in target_courses}
        results = []
        for course in target_courses:
            for f in self.list_files(course.id, course_name=course.name):
                if query.lower() in f.name.lower():
                    results.append(f)
        return results

    def download_file(self, file_id: int) -> tuple[bytes, str]:
        """Download a file by ID. Returns (bytes, filename)."""
        # Get file info first to get the actual download URL
        resp = self._client.get(f"/files/{file_id}")
        resp.raise_for_status()
        info = resp.json()
        filename = info.get("display_name", "") or info.get("filename", f"file_{file_id}")
        # The download URL
        download_url = info.get("url", "")
        if not download_url:
            # Try the direct download endpoint
            download_url = f"{self.base_url}/api/v1/files/{file_id}/download"
        download_resp = self._client.get(download_url, follow_redirects=True)
        download_resp.raise_for_status()
        return download_resp.content, filename

    def list_announcements(
        self,
        courses: list[Course],
        since: datetime,
    ) -> list[Announcement]:
        if not courses:
            return []
        context_codes = [f"course_{c.id}" for c in courses]
        course_name_by_id = {c.id: c.name for c in courses}
        params = [
            ("start_date", since.isoformat()),
            ("per_page", 50),
            ("active_only", "true"),
        ]
        for code in context_codes:
            params.append(("context_codes[]", code))
        out: list[Announcement] = []
        for a in self._paginate("/announcements", params=params):
            ctx = a.get("context_code", "")
            course_id = int(ctx.split("_", 1)[1]) if ctx.startswith("course_") else 0
            posted_raw = a.get("posted_at") or a.get("created_at")
            if not posted_raw:
                continue
            out.append(
                Announcement(
                    id=a["id"],
                    course_id=course_id,
                    course_name=course_name_by_id.get(course_id, ctx),
                    title=a.get("title", "(no title)"),
                    message_html=a.get("message", "") or "",
                    posted_at=_parse_iso(posted_raw),
                    html_url=a.get("html_url", ""),
                )
            )
        return out


_LINK_RE = re.compile(r'<([^>]+)>;\s*rel="next"')


def _next_link(link_header: str) -> str | None:
    m = _LINK_RE.search(link_header)
    return m.group(1) if m else None


def _parse_iso(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)
