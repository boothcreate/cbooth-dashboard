"""google_calendar_auth.py — one-time Google Calendar OAuth setup.

Run this once, by hand, after creating a Desktop-app OAuth client in Google
Cloud Console (see BUILD.md, "Google Calendar reminders" for the exact
click-by-click steps). It opens a browser for you to sign in and consent,
then prints a refresh token to paste into .env.

    python scripts/google_calendar_auth.py

Needs GOOGLE_CALENDAR_CLIENT_ID and GOOGLE_CALENDAR_CLIENT_SECRET already in
.env — that's the client Cloud Console gave you. This script's only job is
to turn those into GOOGLE_CALENDAR_REFRESH_TOKEN, the third value serve.py
needs. It never touches your calendar data itself.

Nothing here runs from serve.py — this is a standalone, run-once tool.
"""

from __future__ import annotations

import sys
from pathlib import Path

from dotenv import dotenv_values
from google_auth_oauthlib.flow import InstalledAppFlow

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


def main() -> None:
    env = dotenv_values(ENV_FILE) if ENV_FILE.exists() else {}
    client_id = env.get("GOOGLE_CALENDAR_CLIENT_ID")
    client_secret = env.get("GOOGLE_CALENDAR_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("GOOGLE_CALENDAR_CLIENT_ID and GOOGLE_CALENDAR_CLIENT_SECRET must both be set "
              "in .env before running this. See BUILD.md, 'Google Calendar reminders'.")
        sys.exit(1)

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }

    flow = InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)
    print("Opening a browser to sign in to Google and grant calendar access...")
    creds = flow.run_local_server(port=0)

    if not creds.refresh_token:
        print("Google didn't return a refresh token. This usually means you've already "
              "authorized this app before — go to https://myaccount.google.com/permissions, "
              "remove access for this app, and run this script again.")
        sys.exit(1)

    print("\nSuccess. Add this line to .env:\n")
    print(f"GOOGLE_CALENDAR_REFRESH_TOKEN={creds.refresh_token}")
    print("\nThen restart serve.py (or the app running it) for it to take effect.")


if __name__ == "__main__":
    main()
