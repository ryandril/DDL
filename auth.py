"""Google Calendar OAuth — headless / VPS-friendly flow.

Run on the VPS:

    python3 auth.py

The script prints an authorization URL. Open it in any browser (your Mac
is fine — it doesn't need to be a browser on the VPS). After authorizing
the Google account this OAuth client is configured for:

  • Google redirects you to a `http://127.0.0.1:8765/oauth2/callback?...`
    URL. Your browser will show "site can't be reached" — that's fine,
    we're not actually running a server here.
  • Copy the FULL redirect URL from your browser address bar.
  • Paste it back into this script.

The script extracts the `code` query param, exchanges it for tokens, and
writes `token.json` (chmod 600) next to this file. Subsequent runs of
`gcal_sync.py` will refresh that token automatically.

If you ever get 'Token has been expired or revoked' from gcal_sync, run
this script again to mint a new token.
"""

from __future__ import annotations

# Google may return a broader scope set than we requested (e.g. user previously
# consented to Gmail+Calendar for this client). Relax oauthlib's strict scope
# check so fetch_token() accepts whatever Google returns.
# PATCH:relax-scope@1
import os as _os
_os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

import sys
import urllib.parse
from pathlib import Path

from google_auth_oauthlib.flow import Flow

SCOPES = ["https://www.googleapis.com/auth/calendar"]
ROOT = Path(__file__).parent
CREDENTIALS_PATH = ROOT / "credentials.json"
TOKEN_PATH = ROOT / "token.json"

# Loopback IP redirect — Google's "Desktop application" OAuth clients
# accept any http://127.0.0.1:PORT and http://localhost:PORT URI without
# pre-registering it. We're not actually running a server; we just need
# the URI to match what the OAuth library sends to Google.
REDIRECT_URI = "http://127.0.0.1:8765/oauth2/callback"

RULE = "=" * 72


def main() -> int:
    if not CREDENTIALS_PATH.exists():
        print(
            f"ERROR: {CREDENTIALS_PATH} not found.\n"
            "Download your OAuth client credentials from Google Cloud Console "
            "(APIs & Services → Credentials → OAuth 2.0 Client IDs → Download JSON), "
            "save as credentials.json next to this script.",
            file=sys.stderr,
        )
        return 2

    flow = Flow.from_client_secrets_file(str(CREDENTIALS_PATH), scopes=SCOPES)
    flow.redirect_uri = REDIRECT_URI
    auth_url, _state = flow.authorization_url(
        prompt="consent",
        access_type="offline",
        include_granted_scopes="true",
    )

    print(RULE)
    print("Open this URL in any browser (your Mac is fine — does NOT need to be")
    print("a browser on the VPS):")
    print()
    print(auth_url)
    print()
    print("After you click 'Allow':")
    print(f"  • Google redirects to {REDIRECT_URI}?...")
    print('  • Your browser shows "site can\'t be reached" — that is EXPECTED')
    print("  • Copy the FULL URL from the browser address bar")
    print("  • Paste it below and press Enter")
    print(RULE)
    print()

    try:
        pasted = input("Paste the redirect URL here: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        return 130

    parsed = urllib.parse.urlparse(pasted)
    qs = urllib.parse.parse_qs(parsed.query)
    code = (qs.get("code") or [None])[0]
    if not code:
        print(
            "ERROR: no `code=` parameter found in the pasted URL.\n"
            "Make sure you pasted the FULL redirect URL — it should look like:\n"
            f"  {REDIRECT_URI}?state=...&code=4/0A...&scope=https://www.googleapis.com/auth/calendar",
            file=sys.stderr,
        )
        return 1

    try:
        flow.fetch_token(code=code)
    except Exception as e:
        print(f"ERROR: token exchange failed: {e}", file=sys.stderr)
        return 1

    creds = flow.credentials
    TOKEN_PATH.write_text(creds.to_json())
    try:
        TOKEN_PATH.chmod(0o600)
    except Exception:
        pass

    has_refresh = bool(creds.refresh_token)
    print()
    print(f"✓ Saved token to {TOKEN_PATH}")
    print(f"  refresh_token: {'present' if has_refresh else 'MISSING (auto-refresh will not work)'}")
    if not has_refresh:
        print()
        print("WARNING: no refresh_token returned. This usually means Google has already")
        print("granted offline access to this client and is skipping the consent dialog.")
        print("To force a fresh refresh_token: revoke this app at")
        print("https://myaccount.google.com/permissions and re-run this script.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
