"""One-time OAuth setup: lets this app upload to, and read the analytics of, your channel.

Needs secrets/client_secret.json from Google Cloud. README.md has the steps.

    venv/bin/python scripts/youtube_oauth.py               # opens a browser
    venv/bin/python scripts/youtube_oauth.py --no-browser  # prints a link, for a machine with no screen
    venv/bin/python scripts/youtube_oauth.py --force       # replace an existing token
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.publishers.youtube_publisher import (ANALYTICS_SCOPE, READ_SCOPE, SCOPES, UPLOAD_SCOPE, explain_api_error,
                                              write_token_file)

CLIENT_SECRETS_PATH = "secrets/client_secret.json"
TOKEN_PATH = "secrets/youtube_token.json"


def _show_warnings(token: dict) -> None:
    granted = set(token.get("scopes") or [])
    if not token.get("refresh_token"):
        print("WARNING: no refresh token came back, so uploads will stop within the hour. Re-run with --force.")
    if UPLOAD_SCOPE not in granted:
        print("WARNING: upload permission was not ticked. Re-run with --force and tick every box.")
    if READ_SCOPE not in granted:
        print("WARNING: read permission was not ticked, so the loop can post but never measure. "
              "Re-run with --force and tick every box.")
    if ANALYTICS_SCOPE not in granted:
        print("WARNING: analytics permission was not ticked, so the loop will have no retention data.")


def _show_existing(path: str) -> None:
    try:
        with open(path, encoding="utf-8") as fh:
            _show_warnings(json.load(fh))
    except (OSError, ValueError) as exc:
        print(f"WARNING: the token cannot be read ({exc}). Re-run with --force.")


def _show_env(secrets: str, token: str) -> None:
    print(f"\nAdd to .env:\n  USE_MOCK=false\n  YOUTUBE_CLIENT_SECRETS={secrets}\n  YOUTUBE_TOKEN_PATH={token}\n")


def _channel_paths(slug: str) -> tuple[str, str]:
    """A managed channel's client_secret.json and token paths, from channels.json."""
    from app.db import init_db, session_scope
    from app.growth import channels

    init_db()
    with session_scope() as db:
        channels.sync_channels(db)
        channel = channels.channel_by_slug(db, slug)
        if channel is None:
            known = ", ".join(c.slug for c in channels.all_channels(db)) or "none"
            raise SystemExit(f"ERROR: no channel {slug!r} in channels.json. Known channels: {known}.")
        if not channel.client_secrets or not channel.token_path:
            raise SystemExit(f"ERROR: channel {slug!r} needs client_secrets and token_path set in channels.json.")
        return channel.client_secrets, channel.token_path


def _show_channel(creds, slug: str | None = None) -> int:
    from googleapiclient.discovery import build

    try:
        channels = build("youtube", "v3", credentials=creds, cache_discovery=False).channels().list(
            part="snippet", mine=True).execute().get("items", [])
    except Exception as exc:
        print(f"Could not read the channel: {explain_api_error(exc)}")
        return 1
    if channels:
        print(f"Connected channel: {channels[0]['snippet']['title']}")
        print("If that is the wrong channel, re-run with --force and pick the right one on Google's screen.")
        if slug:
            _store_channel_id(slug, channels[0]["id"])
    else:
        print("WARNING: this Google account has no YouTube channel. Create one, then re-run with --force.")
    return 0


def _store_channel_id(slug: str, youtube_id: str) -> None:
    """Remember which YouTube channel this token is for, so doctor can catch a later wrong-channel swap."""
    from app.db import init_db, session_scope
    from app.growth import channels

    init_db()
    with session_scope() as db:
        channels.sync_channels(db)
        channel = channels.channel_by_slug(db, slug)
        if channel is not None:
            channel.youtube_channel_id = youtube_id


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true", help="Re-authorize even if a token exists")
    ap.add_argument("--channel", help="A managed channel's slug (its secrets/token come from channels.json)")
    ap.add_argument("--secrets", default=CLIENT_SECRETS_PATH, help="Path to client_secret.json")
    ap.add_argument("--token", default=TOKEN_PATH, help="Path to save the token")
    ap.add_argument("--no-browser", action="store_true", help="Print the link instead of opening a browser")
    ap.add_argument("--port", type=int, default=8765, help="Local port Google redirects back to")
    args = ap.parse_args()

    # A named channel in channels.json fills its own secrets and token paths (one Cloud project each).
    if args.channel:
        args.secrets, args.token = _channel_paths(args.channel)
        print(f"Authorizing channel {args.channel!r}: secrets {args.secrets}, token {args.token}")

    if not os.path.exists(args.secrets):
        print(f"ERROR: missing {args.secrets}. See 'YouTube token' in README.md.")
        return 1
    if os.path.exists(args.token) and not args.force:
        print(f"Token already exists at {args.token}. Use --force to replace it.")
        _show_existing(args.token)
        _show_env(args.secrets, args.token)
        return 0

    from google_auth_oauthlib.flow import InstalledAppFlow

    # Google lets people untick permissions. Without this, oauthlib treats that as an error
    # rather than letting the warnings below explain it.
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    flow = InstalledAppFlow.from_client_secrets_file(args.secrets, SCOPES)
    if args.no_browser:
        print(f"On another machine, first run: ssh -L {args.port}:localhost:{args.port} <this-machine>")
    # prompt=consent is what makes Google issue a refresh token again on a re-run.
    creds = flow.run_local_server(port=args.port, open_browser=not args.no_browser, prompt="consent",
                                  access_type="offline")

    # The token file records what was asked for, which is not always what was granted.
    token = json.loads(creds.to_json())
    token["scopes"] = sorted(set(getattr(creds, "granted_scopes", None) or creds.scopes or []))
    os.makedirs(os.path.dirname(args.token) or ".", exist_ok=True)
    write_token_file(args.token, json.dumps(token))
    print(f"Token saved to {args.token}")

    _show_warnings(token)
    # Before the API call, since a refused call must not cost the owner these lines.
    _show_env(args.secrets, args.token)
    return _show_channel(creds, args.channel)


if __name__ == "__main__":
    sys.exit(main())
