#!/usr/bin/env python3
"""
DDL -> WeChat alert sender.

NOT REQUIRED TO RUN DDL. This is the author's own notifier, kept in the repo as
a worked example of a richer $NOTIFY_CMD. It needs OpenClaw (for the WeChat
channel) and the `gog` Google CLI (for the email fallback), so it will not work
out of the box. Point NOTIFY_CMD at your own sender instead; it needs only a
the notifier NOTIFY_CMD points at — see the Notifications section of the README.

Renders the morning Canvas digest and sends it directly via `openclaw message
send`, so delivery does not depend on an agent being available.

Every run shows the full list of UPCOMING deadlines, split into:
  - New since last check   (first seen this run)
  - Already tracked        (carried over, still upcoming)
The list comes from out/ledger_view.json (produced by deadline_ledger.py), which
only ever contains deadlines that have NOT passed -- so a past deadline can never
leak into the message (this was the old "Product Red (A)" bug).

Usage:
    python3 send_wechat_alert.py [--trigger PATH] [--dry-run]

Exit codes:
    0 -- message sent (or no-op because no trigger file)
    1 -- trigger present but send failed
    2 -- usage/config error
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = pathlib.Path(os.environ.get("DDL_DIR") or pathlib.Path(__file__).resolve().parent)

TRIGGER_DEFAULT = str(BASE_DIR / ".cron_trigger.json")
LEDGER_VIEW_PATH = str(BASE_DIR / "out" / "ledger_view.json")

# Email fallback — used when the WeChat send fails (the recurring ret=-2 = push
# window closed at cron time). Gmail has no push-window limit and Ryan reads it
# anyway, so a closed WeChat window never silently eats the alert. gog is the
# box's durably-authed Google CLI; resolve by absolute path because cron's PATH
# (/usr/bin:/bin) excludes /usr/local/bin. Override with $GOG_BIN.
GOG_BIN = os.environ.get("GOG_BIN") or shutil.which("gog") or "/usr/local/bin/gog"
EMAIL_ACCOUNT = os.environ.get("GOG_ACCOUNT", "")   # Gmail account gog sends as
EMAIL_TO = os.environ.get("ALERT_EMAIL_TO", "")     # where failure alerts land

# WeChat delivery target (single-user setup; move to env if multi-tenant)
WX_CHANNEL = "openclaw-weixin"
WX_ACCOUNT = os.environ.get("WECHAT_ACCOUNT", "")  # fallback; the live account is auto-discovered (it changes on each re-link)
WX_TARGET = os.environ.get("WECHAT_TARGET", "")
WX_ACCOUNTS_DIR = os.environ.get(
    "OPENCLAW_ACCOUNTS_DIR", "/root/.openclaw/openclaw-weixin/accounts"
)

# WhatsApp fallback — used ONLY when the WeChat send fails (e.g. the recurring
# ret=-2 stale-session). Requires the operator device to have WhatsApp send scope
# (one-time: `openclaw devices approve <request>`). Harmless if not yet approved:
# the fallback attempt just fails and we leave the trigger for retry.
ENABLE_WHATSAPP_FALLBACK = False  # WhatsApp send needs operator.write scope (approve in OpenClaw app); off until then
WA_CHANNEL = "whatsapp"
WA_ACCOUNT = "default"
WA_TARGET = os.environ.get("WHATSAPP_TARGET", "")

LABEL = os.environ.get("DIGEST_LABEL", "Canvas")

LOG = str(BASE_DIR / "cron.log")


def log(msg: str) -> None:
    """Append to cron.log. (Only writes to the file -- the caller redirects
    stdout/stderr to the same log, so we deliberately do NOT also print, to
    avoid every line showing up twice.)"""
    try:
        with open(LOG, "a") as f:
            f.write(f"[DDL Alert] {msg}\n")
    except Exception:
        print(f"[DDL Alert] {msg}", file=sys.stderr)


def _fmt_dt(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%a %d %b, %H:%M")
    except Exception:
        return (iso or "").replace("T", " ")[:16] or "(no date)"


def _conf_tag(conf: str) -> str:
    if conf == "medium":
        return "  [medium — verify]"
    if conf == "low":
        return "  [low — verify]"
    return ""


def _fmt_deadline(d: dict) -> list[str]:
    title = d.get("title") or "(untitled)"
    out = [f"• {_fmt_dt(d.get('datetime_local',''))} — {title}{_conf_tag(d.get('confidence',''))}"]
    course = d.get("source_course")
    if course:
        out.append(f"   {course}")
    url = d.get("source_url")
    if url:
        out.append(f"   → {url}")
    others = d.get("_possible_reschedule_of") or []
    if others:
        when = ", ".join(_fmt_dt(t) for t in others[:3])
        out.append(f"   ⚠️ also listed for {when} — possible reschedule, check which is current")
    return out


def render_deadlines(view_path: str | None = None) -> list[str]:
    """Build the upcoming-deadlines block (New + Existing) from the ledger view."""
    view_path = view_path or LEDGER_VIEW_PATH
    try:
        view = json.loads(Path(view_path).read_text())
    except Exception as e:
        log(f"warning: could not read ledger view {view_path}: {e}")
        return ["", "(deadline list unavailable this run)"]

    new = view.get("new", []) or []
    existing = view.get("existing", []) or []
    total = len(new) + len(existing)

    lines = [""]
    if total == 0:
        lines.append("✅ No upcoming deadlines.")
        return lines

    lines.append(f"📅 Upcoming deadlines ({total}):")
    if new:
        lines.append("")
        lines.append(f"🆕 New since last check ({len(new)}):")
        for d in new:
            lines.extend(_fmt_deadline(d))
    if existing:
        lines.append("")
        lines.append(f"📌 Already tracked ({len(existing)}):")
        for d in existing:
            lines.extend(_fmt_deadline(d))
    return lines


def build_message(trigger: dict) -> str:
    action = trigger.get("action", "")
    date = trigger.get("date", "today")

    lines = [f"📚 {LABEL} — {date}", ""]

    if action == "failure":
        lines.append(f"⚠️ Routine had a problem fetching Canvas:")
        lines.append(f"   {trigger.get('error', '(unspecified)')}")
        lines.append("   (Showing last-known deadlines below.)")
        lines.append("")

    try:
        count = int(trigger.get("count", 0) or 0)
    except (TypeError, ValueError):
        count = 0
    titles = trigger.get("announcement_titles") or []

    if action == "failure":
        pass  # status already shown
    elif count <= 0:
        lines.append("No new announcements since last check.")
    else:
        lines.append(f"🆕 {count} new announcement" + ("s" if count != 1 else "") + ":")
        for t in titles[:6]:
            lines.append(f"• {t}")

    lines.extend(render_deadlines())
    return "\n".join(lines).rstrip() + "\n"


def _weixin_account() -> str:
    """The weixin account/session id changes on every re-link, so discover the
    currently-bound account from its credentials file rather than trusting a
    hardcoded id (keeps delivery working across forced re-logins). Falls back to
    WX_ACCOUNT if discovery is ambiguous."""
    try:
        bound = sorted(Path(WX_ACCOUNTS_DIR).glob("*-im-bot.json"))  # binding files only
        if len(bound) == 1:
            return bound[0].stem
    except Exception:
        pass
    return WX_ACCOUNT


def _send(channel: str, account: str, target: str, message: str) -> bool:
    """Send one message via the openclaw CLI. Returns True only on a clean send."""
    cmd = [
        "openclaw", "message", "send",
        "--channel", channel,
        "--account", account,
        "--target", target,
        "--message", message,
        "--json",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    except subprocess.TimeoutExpired:
        log(f"[{channel}] send timed out after 30s")
        return False
    except FileNotFoundError:
        log("openclaw CLI not found in PATH")
        return False
    if result.returncode != 0:
        log(f"[{channel}] send failed exit={result.returncode} stderr={result.stderr.strip()[:200]}")
        return False
    try:
        j = json.loads(result.stdout)
        mid = j.get("payload", {}).get("result", {}).get("messageId") or j.get("messageId")
        log(f"[{channel}] send OK messageId={mid}")
    except Exception:
        log(f"[{channel}] send returned exit=0 (no messageId parsed)")
    return True


def _email_subject(message: str) -> str:
    """Use the message's first non-empty line (e.g. '📚 Canvas — Jul 8')."""
    for ln in message.splitlines():
        ln = ln.strip()
        if ln:
            return ln[:120]
    return f"{LABEL} — deadlines"


def _email_fallback(message: str) -> bool:
    """Last-resort delivery: email the alert to Ryan via gog (no push-window
    limit). Body is piped on stdin to avoid arg-length/escaping issues."""
    cmd = [GOG_BIN, "-a", EMAIL_ACCOUNT, "gmail", "send",
           "--to", EMAIL_TO, "--subject", _email_subject(message),
           "--body-file", "-", "--no-input"]
    try:
        result = subprocess.run(cmd, input=message, capture_output=True,
                                text=True, timeout=60, check=False)
    except subprocess.TimeoutExpired:
        log("[email] fallback timed out after 60s")
        return False
    except FileNotFoundError:
        log(f"[email] gog not found at {GOG_BIN}")
        return False
    if result.returncode != 0:
        log(f"[email] fallback failed exit={result.returncode} "
            f"stderr={(result.stderr or result.stdout).strip()[:200]}")
        return False
    log(f"[email] fallback delivered to {EMAIL_TO}")
    return True


def send_via_cli(message: str, dry_run: bool = False) -> bool:
    """Deliver to WeChat; on failure fall back to WhatsApp (if enabled), then
    to email (always) so a closed WeChat push-window never eats the alert."""
    if dry_run:
        log("DRY-RUN — message follows:")
        log("\n" + message)
        return True

    if _send(WX_CHANNEL, _weixin_account(), WX_TARGET, message):
        return True

    if ENABLE_WHATSAPP_FALLBACK:
        log("WeChat send failed — trying WhatsApp fallback")
        wa_msg = ("⚠️ Delivered via WhatsApp — WeChat is down. Re-login with:\n"
                  "   openclaw channels login --channel openclaw-weixin\n\n" + message)
        if _send(WA_CHANNEL, WA_ACCOUNT, WA_TARGET, wa_msg):
            log("WhatsApp fallback delivered")
            return True
        log("WhatsApp fallback also failed (needs device send scope? `openclaw devices approve`)")

    log("WeChat send failed — trying email fallback")
    email_msg = ("(Delivered by email — WeChat push window was closed. Message the "
                 "bot anything to reopen it.)\n\n" + message)
    if _email_fallback(email_msg):
        return True

    return False


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--trigger", default=TRIGGER_DEFAULT)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--keep-trigger", action="store_true", help="don't delete trigger after sending")
    args = p.parse_args()

    trigger_path = Path(args.trigger)
    if not trigger_path.exists():
        log(f"no trigger at {trigger_path} — nothing to do")
        return 0

    try:
        trigger = json.loads(trigger_path.read_text())
    except Exception as e:
        log(f"trigger {trigger_path} is malformed: {e}")
        return 2

    log(f"trigger action={trigger.get('action')} date={trigger.get('date')}")

    message = build_message(trigger)
    if not message.strip():
        log("empty message — aborting")
        return 2

    sent = send_via_cli(message, dry_run=args.dry_run)
    if not sent:
        log("send failed; leaving trigger in place so a follow-up can retry")
        return 1

    if not args.dry_run and not args.keep_trigger:
        try:
            trigger_path.unlink()
            log("trigger deleted after successful send")
        except Exception as e:
            log(f"trigger send OK but delete failed: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
