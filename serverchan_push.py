"""Push the Canvas deadline digest to Server酱 Turbo (sctapi.ftqq.com).

Best-effort: failures are logged and signaled via non-zero exit; the routine
should treat this as advisory and never abort because of a push error.

Reads SERVERCHAN_SENDKEY from .env or environment.

Usage:
    .venv/bin/python serverchan_push.py --title-file out/subject.txt --body-file out/digest.txt
    .venv/bin/python serverchan_push.py --title "..." --body "..."
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

log = logging.getLogger("ddl-serverchan")

API_URL = "https://sctapi.ftqq.com/{key}.send"
TITLE_MAX_CHARS = 32


def push(send_key: str, title: str, body: str, *, timeout: float = 10.0) -> dict:
    if len(title) > TITLE_MAX_CHARS:
        title = title[: TITLE_MAX_CHARS - 1] + "…"
    resp = httpx.post(
        API_URL.format(key=send_key),
        data={"title": title, "desp": body},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def _read_text(path: Path | None) -> str | None:
    if path is None or not path.exists():
        return None
    return path.read_text().strip()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--title")
    p.add_argument("--body")
    p.add_argument("--title-file", type=Path)
    p.add_argument("--body-file", type=Path)
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    load_dotenv()
    send_key = os.environ.get("SERVERCHAN_SENDKEY")
    if not send_key:
        log.error("SERVERCHAN_SENDKEY missing from environment / .env")
        return 1

    title = args.title or _read_text(args.title_file)
    body = args.body or _read_text(args.body_file) or ""
    if not title:
        log.error("No title provided (use --title or --title-file).")
        return 1

    try:
        result = push(send_key, title, body)
    except Exception as exc:
        log.error("ServerChan push failed: %s", exc)
        return 2

    if result.get("code") != 0:
        log.error("ServerChan returned non-zero code: %s", result)
        return 3
    log.info("ServerChan push OK (pushid=%s)", result.get("data", {}).get("pushid"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
