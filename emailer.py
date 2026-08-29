"""Compose the digest email (HTML + plaintext), write it to disk, and send it.

Sending is the email FALLBACK: cron runs $NOTIFY_CMD first, and drops to
`emailer.py --send` when that command is unset or fails, so a run never ends
with the user uninformed. Delivery is stdlib smtplib — no extra dependency and
no CLI to install — configured entirely through SMTP_HOST / SMTP_PORT /
SMTP_USER / SMTP_PASS / EMAIL_FROM / EMAIL_TO in .env.
"""
from __future__ import annotations

import argparse
import html
import logging
import os
import smtplib
import sys
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from dotenv import load_dotenv

log = logging.getLogger("ddl-emailer")


@dataclass
class DigestArtifacts:
    subject: str
    html_body: str
    text_body: str
    ics_path: Path | None
    deadline_count: int
    announcement_count: int


def compose(
    *,
    deadlines: list,
    summaries: list,
    announcements_by_id: dict,
    run_date_label: str,
    ics_path: Path | None,
) -> DigestArtifacts:
    deadlines_sorted = sorted(deadlines, key=lambda d: d.datetime_local)
    subject = _subject(len(deadlines_sorted), len(summaries), run_date_label)
    html_body = _html(deadlines_sorted, summaries, announcements_by_id, ics_path)
    text_body = _text(deadlines_sorted, summaries, announcements_by_id, ics_path)
    return DigestArtifacts(
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        ics_path=ics_path,
        deadline_count=len(deadlines_sorted),
        announcement_count=len(summaries),
    )


# Prefix on the digest subject line. Set DIGEST_LABEL in .env to your school.
LABEL = os.environ.get("DIGEST_LABEL", "Canvas")


def _subject(n_deadlines: int, n_announcements: int, date_label: str) -> str:
    if n_deadlines == 0 and n_announcements == 0:
        return f"[{LABEL}] No new announcements ({date_label})"
    parts = []
    if n_deadlines:
        parts.append(f"{n_deadlines} deadline{'s' if n_deadlines != 1 else ''}")
    if n_announcements:
        parts.append(f"{n_announcements} announcement{'s' if n_announcements != 1 else ''}")
    return f"[{LABEL}] {' + '.join(parts)} ({date_label})"


def _confidence_badge(c: str) -> str:
    color = {"high": "#137333", "medium": "#b06000", "low": "#a50e0e"}.get(c, "#666")
    return f'<span style="color:{color};font-weight:600;font-size:11px">[{c.upper()}]</span>'


def _fmt_dt(iso_local: str, all_day: bool) -> str:
    dt = datetime.fromisoformat(iso_local)
    if all_day:
        return dt.strftime("%a %b %-d (all day)")
    return dt.strftime("%a %b %-d, %H:%M")


def _html(deadlines, summaries, announcements_by_id, ics_path) -> str:
    parts = [
        '<div style="font-family:-apple-system,BlinkMacSystemFont,sans-serif;max-width:680px;line-height:1.5">',
    ]

    parts.append(f'<h2 style="margin:0 0 4px 0">📅 New deadlines ({len(deadlines)})</h2>')
    if deadlines:
        parts.append('<ul style="padding-left:20px;margin-top:8px">')
        for d in deadlines:
            parts.append(
                f'<li style="margin-bottom:10px">'
                f'<strong>{html.escape(_fmt_dt(d.datetime_local, d.all_day))}</strong> — '
                f'{html.escape(d.title)} {_confidence_badge(d.confidence)}<br>'
                f'<span style="color:#555;font-size:13px">{html.escape(d.source_course)} · '
                f'"{html.escape(d.evidence_quote)}"</span><br>'
                f'<a href="{html.escape(d.source_url)}" style="font-size:12px">View announcement</a>'
                f'</li>'
            )
        parts.append("</ul>")
        if ics_path is not None:
            parts.append(
                f'<p style="background:#f0f4ff;padding:8px 12px;border-radius:4px;font-size:13px">'
                f"📎 <strong>{html.escape(ics_path.name)}</strong> attached — open it to add all "
                f"{len(deadlines)} deadline{'s' if len(deadlines) != 1 else ''} to your calendar."
                f"</p>"
            )
    else:
        parts.append('<p style="color:#666;font-style:italic">No deadlines extracted from this batch.</p>')

    parts.append(f'<h2 style="margin:24px 0 4px 0">📣 New announcements ({len(summaries)})</h2>')
    if summaries:
        parts.append('<ul style="padding-left:20px;margin-top:8px">')
        for s in summaries:
            a = announcements_by_id.get(s.announcement_id)
            if not a:
                continue
            parts.append(
                f'<li style="margin-bottom:8px">'
                f'<strong>{html.escape(a.course_name)}</strong> — '
                f'<a href="{html.escape(a.html_url)}">{html.escape(a.title)}</a><br>'
                f'<span style="color:#555;font-size:13px">{html.escape(s.summary)}</span>'
                f'</li>'
            )
        parts.append("</ul>")
    else:
        parts.append('<p style="color:#666;font-style:italic">No new announcements since last run.</p>')

    parts.append(
        '<hr style="margin-top:24px;border:none;border-top:1px solid #eee">'
        '<p style="font-size:11px;color:#999">Generated by canvas-deadline-agent. '
        'High-confidence deadlines are extracted from explicit dates+times. '
        'Medium/low items resolved relative phrasing or vague timing — please verify.</p>'
    )
    parts.append("</div>")
    return "".join(parts)


def _text(deadlines, summaries, announcements_by_id, ics_path) -> str:
    lines: list[str] = []
    lines.append(f"NEW DEADLINES ({len(deadlines)})")
    lines.append("-" * 50)
    if deadlines:
        for d in deadlines:
            lines.append(
                f"- {_fmt_dt(d.datetime_local, d.all_day)} — {d.title} [{d.confidence.upper()}]"
            )
            lines.append(f'    {d.source_course} · "{d.evidence_quote}"')
            lines.append(f"    {d.source_url}")
        if ics_path is not None:
            lines.append(f"\nAttached: {ics_path.name}")
    else:
        lines.append("(none)")

    lines.append("")
    lines.append(f"NEW ANNOUNCEMENTS ({len(summaries)})")
    lines.append("-" * 50)
    if summaries:
        for s in summaries:
            a = announcements_by_id.get(s.announcement_id)
            if not a:
                continue
            lines.append(f"- {a.course_name} — {a.title}")
            lines.append(f"    {s.summary}")
            lines.append(f"    {a.html_url}")
    else:
        lines.append("(none)")
    return "\n".join(lines)


def write_artifacts(artifacts: DigestArtifacts, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "subject": out_dir / "subject.txt",
        "html": out_dir / "digest.html",
        "text": out_dir / "digest.txt",
    }
    paths["subject"].write_text(artifacts.subject)
    paths["html"].write_text(artifacts.html_body)
    paths["text"].write_text(artifacts.text_body)
    if artifacts.ics_path is not None:
        paths["ics"] = artifacts.ics_path
    return {k: str(v) for k, v in paths.items()}


# --------------------------------------------------------------------------- #
# delivery (the email fallback)
# --------------------------------------------------------------------------- #
def build_message(subject: str, text_body: str, html_body: str | None, *,
                  sender: str, to: list[str], ics_path: Path | None = None) -> EmailMessage:
    """Assemble the digest as a real multipart message.

    Plaintext is the body every client can render; the HTML alternative is a
    nicety. The .ics rides along so a reader with no calendar sync can still
    import the deadlines by hand.
    """
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    msg.set_content(text_body or "(no digest body was generated for this run)")
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    if ics_path is not None and ics_path.exists():
        msg.add_attachment(ics_path.read_bytes(), maintype="text",
                           subtype="calendar", filename=ics_path.name)
    return msg


def send(out_dir: Path, *, smtp_factory=None) -> bool:
    """Email the artifacts in out_dir. Returns True only on a delivered message.

    Never raises: this is the last step of a best-effort cron pipeline, and a
    failure here must be logged rather than allowed to kill the run.
    """
    out_dir = Path(out_dir)
    host = (os.environ.get("SMTP_HOST") or "").strip()
    to = [a.strip() for a in (os.environ.get("EMAIL_TO") or "").split(",") if a.strip()]
    if not host or not to:
        log.error("email fallback not configured — set SMTP_HOST and EMAIL_TO in .env")
        return False

    def _read(name: str, default: str = "") -> str:
        f = out_dir / name
        return f.read_text() if f.exists() else default

    subject = _read("subject.txt").strip() or "Canvas deadlines"
    ics = out_dir / "deadlines.ics"
    sender = (os.environ.get("EMAIL_FROM") or os.environ.get("SMTP_USER") or to[0]).strip()
    msg = build_message(subject, _read("digest.txt"), _read("digest.html") or None,
                        sender=sender, to=to, ics_path=ics if ics.exists() else None)

    factory = smtp_factory or smtplib.SMTP
    user, password = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASS")
    try:
        with factory(host, int(os.environ.get("SMTP_PORT") or 587)) as smtp:
            if (os.environ.get("SMTP_STARTTLS") or "1") != "0":
                smtp.starttls()
            if user and password:
                smtp.login(user, password)
            smtp.send_message(msg)
    except Exception as e:  # noqa: BLE001 — any failure is just "not delivered"
        log.error("email fallback failed: %s", e)
        return False
    log.info("digest emailed to %s", ", ".join(to))
    return True


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Send the composed digest (email fallback).")
    p.add_argument("--send", action="store_true", help="deliver out-dir's digest by SMTP")
    p.add_argument("--out-dir", type=Path, default=Path("out"))
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not args.send:
        p.error("nothing to do — pass --send")
    load_dotenv()
    return 0 if send(args.out_dir) else 1


if __name__ == "__main__":
    sys.exit(main())
