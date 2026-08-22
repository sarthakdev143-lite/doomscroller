"""One-time OAuth setup for YouTube publishing.

This script:
1. Reads client_id/client_secret from ./secrets/youtube_client_secrets.json
2. Opens a browser for Google OAuth consent
3. Exchanges the code for tokens
4. Writes ./secrets/youtube_token.json

After running this once, doomscroller will use the stored token to upload Shorts
without further interaction.

Usage:
    python scripts/youtube_oauth.py

If you already have a token and want to refresh it, pass --force.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import settings

CLIENT_SECRETS_PATH = "secrets/youtube_client_secrets.json"
TOKEN_PATH = "secrets/youtube_token.json"


def main():
    if not os.path.exists(CLIENT_SECRETS_PATH):
        print(f"ERROR: missing {CLIENT_SECRETS_PATH}")
        print("")
        print("To fix:")
        print("1. Go to https://console.cloud.google.com/")
        print("2. Create a project → enable YouTube Data API v3")
        print("3. OAuth consent screen → External → fill in app name + email")
        print("4. Credentials → Create Credentials → OAuth 2.0 Client ID")
        print("   - Application type: Desktop App")
        print("   - Name: Doomscroller")
        print("5. Download the JSON and save as:", CLIENT_SECRETS_PATH)
        sys.exit(1)

    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="Re-authorize even if a token exists")
    args = ap.parse_args()

    if os.path.exists(TOKEN_PATH) and not args.force:
        print(f"Token already exists at {TOKEN_PATH}. Use --force to re-authorize.")
        sys.exit(0)

    from google_auth_oauthlib.flow import InstalledAppFlow

    scopes = ["https://www.googleapis.com/auth/youtube.upload"]
    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS_PATH, scopes)
    print("Opening browser for Google OAuth…")
    creds = flow.run_local_server(port=0)

    os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
    with open(TOKEN_PATH, "w") as fh:
        fh.write(creds.to_json())
    print(f"Token saved to {TOKEN_PATH}")
    print("YouTube publishing is ready. The scheduler will upload automatically.")


if __name__ == "__main__":
    main()