"""One-time OAuth setup: lets this app upload to, and read the analytics of, your channel.

Needs secrets/client_secret.json from Google Cloud. README.md has the steps.

    venv/bin/python scripts/youtube_oauth.py               # opens a browser
    venv/bin/python scripts/youtube_oauth.py --no-browser  # prints a link, for a machine with no screen
    venv/bin/python scripts/youtube_oauth.py --force       # replace an existing token
    venv/bin/python scripts/youtube_oauth.py --status      # offline check (--check alias; add --json for JSON)
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.publishers.youtube_publisher import (
    ANALYTICS_SCOPE,
    READ_SCOPE,
    SCOPES,
    UPLOAD_SCOPE,
    explain_api_error,
    write_token_file,
)

CLIENT_SECRETS_PATH = "secrets/client_secret.json"
TOKEN_PATH = "secrets/youtube_token.json"


def _extract_scopes(token: dict) -> list[str]:
    raw = token.get("scopes")
    if raw is None:
        raw = token.get("scope")
    if isinstance(raw, list):
        return sorted({s.strip() for s in raw if isinstance(s, str) and s.strip()})
    if isinstance(raw, str):
        return sorted({part.strip() for part in raw.split() if part.strip()})
    return []


def _show_warnings(token: Any) -> None:
    if not isinstance(token, dict):
        print("WARNING: token JSON is not an object. Re-run with --force.")
        return
    granted = set(_extract_scopes(token))
    if not str(token.get("refresh_token") or "").strip():
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
            data = json.load(fh)
        if not isinstance(data, dict):
            print("WARNING: the token file does not contain a JSON object. Re-run with --force.")
            return
        _show_warnings(data)
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


def _lookup_bound_channel(token_path: str, slug: str | None = None) -> tuple[str, str | None, str | None]:
    """Resolve ``(effective_token_path, bound_youtube_channel_id, matched_slug)`` offline without requiring secrets."""
    from app.config import settings
    from app.db import init_db, session_scope
    from app.growth import channels

    try:
        init_db()
        with session_scope() as db:
            channels.sync_channels(db)
            if slug:
                ch = channels.channel_by_slug(db, slug)
                if ch is None:
                    known = ", ".join(c.slug for c in channels.all_channels(db)) or "none"
                    raise SystemExit(f"ERROR: no channel {slug!r} in channels.json. Known channels: {known}.")
                eff_path = ch.token_path or settings.youtube_token_path or token_path
                return eff_path, ch.youtube_channel_id, ch.slug

            target_abs = os.path.normcase(os.path.abspath(token_path))
            default_abs = os.path.normcase(os.path.abspath(settings.youtube_token_path or TOKEN_PATH))
            for ch in channels.all_channels(db):
                ch_tok = ch.token_path or (settings.youtube_token_path if ch.slug == "default" else "")
                if ch_tok and os.path.normcase(os.path.abspath(ch_tok)) == target_abs:
                    return token_path, ch.youtube_channel_id, ch.slug
            if target_abs == default_abs:
                default_ch = channels.channel_by_slug(db, "default")
                if default_ch is not None:
                    return token_path, default_ch.youtube_channel_id, default_ch.slug
    except SystemExit:
        raise
    except Exception:
        pass
    return token_path, None, slug


def inspect_token(token_path: str, *, slug: str | None = None) -> dict:
    """Inspect a saved OAuth token file completely offline without leaking secret values."""
    resolved_path, bound_channel_id, matched_slug = _lookup_bound_channel(token_path, slug=slug)
    permissions: dict[str, Any] = {
        "exists": False,
        "readable": False,
        "writable": False,
        "mode": None,
        "private": None,
    }
    report: dict[str, Any] = {
        "ok": False,
        "token_path": resolved_path,
        "channel_slug": matched_slug,
        "bound_channel_id": bound_channel_id,
        "valid_json": False,
        "has_refresh_token": False,
        "has_client_id": False,
        "has_client_secret": False,
        "scopes": [],
        "missing_scopes": list(SCOPES),
        "expiry": None,
        "permissions": permissions,
        "warnings": [],
    }

    if not resolved_path or not os.path.isfile(resolved_path):
        report["warnings"].append(f"Token file missing at {resolved_path}")
        return report

    permissions["exists"] = True
    permissions["readable"] = os.access(resolved_path, os.R_OK)
    permissions["writable"] = os.access(resolved_path, os.W_OK)
    try:
        st = os.stat(resolved_path)
        imode = stat.S_IMODE(st.st_mode)
        permissions["mode"] = f"0o{imode:03o}"
        if os.name != "nt":
            is_private = (imode & 0o077) == 0
            permissions["private"] = is_private
            if not is_private:
                report["warnings"].append(
                    f"Token permissions {permissions['mode']} are group/world-accessible (expected 0o600)"
                )
        else:
            permissions["private"] = True
    except OSError as exc:
        report["warnings"].append(f"Could not stat token file: {exc}")

    try:
        with open(resolved_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        report["warnings"].append(f"Token JSON unreadable: {exc}")
        return report

    if not isinstance(data, dict):
        report["warnings"].append("Token file root must be a JSON object")
        return report

    report["valid_json"] = True
    report["has_refresh_token"] = bool(str(data.get("refresh_token") or "").strip())
    report["has_client_id"] = bool(str(data.get("client_id") or "").strip())
    report["has_client_secret"] = bool(str(data.get("client_secret") or "").strip())
    if isinstance(data.get("expiry"), str) and data["expiry"].strip():
        report["expiry"] = data["expiry"].strip()
    if not report["bound_channel_id"]:
        tok_ch = data.get("youtube_channel_id") or data.get("channel_id")
        if isinstance(tok_ch, str) and tok_ch.strip():
            report["bound_channel_id"] = tok_ch.strip()

    scopes = _extract_scopes(data)
    granted = set(scopes)
    missing = [s for s in SCOPES if s not in granted]
    report["scopes"] = scopes
    report["missing_scopes"] = missing

    if not report["has_refresh_token"]:
        report["warnings"].append("Missing refresh_token (uploads will fail once access token expires)")
    if not report["has_client_id"] or not report["has_client_secret"]:
        report["warnings"].append("Missing client_id or client_secret in token file")
    if UPLOAD_SCOPE not in granted:
        report["warnings"].append(f"Missing required upload scope ({UPLOAD_SCOPE})")
    if READ_SCOPE not in granted:
        report["warnings"].append(f"Missing read scope ({READ_SCOPE})")
    if ANALYTICS_SCOPE not in granted:
        report["warnings"].append(f"Missing analytics scope ({ANALYTICS_SCOPE})")

    report["ok"] = bool(
        report["valid_json"]
        and report["has_refresh_token"]
        and report["has_client_id"]
        and report["has_client_secret"]
        and (UPLOAD_SCOPE in granted)
    )
    return report


def _print_status_report(report: dict, *, as_json: bool = False) -> int:
    if as_json:
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1

    status_word = "OK" if report["ok"] else "INVALID"
    perms = report["permissions"]
    perm_str = perms.get("mode") or "unavailable"
    rw_flags = [name for name in ("readable", "writable") if perms.get(name)]
    if rw_flags:
        perm_str = f"{perm_str} ({', '.join(rw_flags)})"

    print(f"Token status    : {status_word}")
    print(f"Token path      : {report['token_path']}")
    if report.get("channel_slug"):
        print(f"Managed channel : {report['channel_slug']}")
    print(f"Bound channel ID: {report['bound_channel_id'] or 'unbound'}")
    print(f"Refresh token   : {'present' if report['has_refresh_token'] else 'MISSING'}")
    print(f"File permissions: {perm_str}")
    if report.get("expiry"):
        print(f"Access expiry   : {report['expiry']}")
    print(f"Granted scopes  : {', '.join(report['scopes']) if report['scopes'] else 'none'}")
    if report["missing_scopes"]:
        print(f"Missing scopes  : {', '.join(report['missing_scopes'])}")
    for warning in report["warnings"]:
        print(f"WARNING: {warning}")
    return 0 if report["ok"] else 1


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
    """Remember which YouTube channel this token is for, so the loop can refuse a later wrong-account token.

    Once the channel has posted, a different account is not accepted quietly: its videos live on the first
    one, so posting or reading through this token would cross-post, and make every earlier video look deleted.
    """
    from sqlalchemy import func, select

    from app.db import init_db, session_scope
    from app.growth import channels
    from app.models import Video, VideoStatus

    init_db()
    with session_scope() as db:
        channels.sync_channels(db)
        channel = channels.channel_by_slug(db, slug)
        if channel is None:
            return
        if channel.youtube_channel_id and channel.youtube_channel_id != youtube_id:
            posted = db.scalar(select(func.count(Video.id)).where(
                Video.channel_id == channel.id, Video.status == VideoStatus.PUBLISHED,
                Video.youtube_video_id.isnot(None), ~Video.youtube_video_id.startswith("MOCK_", autoescape=True))) or 0
            if posted:
                print(f"\nWARNING: channel {slug!r} has already posted {posted} videos to a DIFFERENT YouTube channel.\n"
                      "This token is for another account, so the loop will not post or read with it.\n"
                      "  - Picked the wrong account? Re-run with --force and pick the original one.\n"
                      "  - Moving to a new YouTube channel? Add it as a new channel instead:\n"
                      "      venv/bin/python scripts/channels.py add <new-slug>")
                return
        channel.youtube_channel_id = youtube_id


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true", help="Re-authorize even if a token exists")
    ap.add_argument(
        "--status",
        "--check",
        dest="status",
        action="store_true",
        help="Offline token inspection (scopes, bound channel ID, refresh token, file permissions)",
    )
    ap.add_argument("--json", dest="as_json", action="store_true", help="Emit --status/--check output as JSON")
    ap.add_argument("--channel", help="A managed channel's slug (its secrets/token come from channels.json)")
    ap.add_argument("--secrets", default=CLIENT_SECRETS_PATH, help="Path to client_secret.json")
    ap.add_argument("--token", default=None, help="Path to save or inspect the token")
    ap.add_argument("--no-browser", action="store_true", help="Print the link instead of opening a browser")
    ap.add_argument("--port", type=int, default=8765, help="Local port Google redirects back to")
    args = ap.parse_args()

    if args.status:
        from app.config import settings

        default_tok = settings.youtube_token_path or TOKEN_PATH
        tok_path = args.token or default_tok
        try:
            report = inspect_token(tok_path, slug=args.channel)
        except SystemExit as exc:
            msg = str(exc)
            if args.as_json:
                print(json.dumps({"ok": False, "error": msg}))
            else:
                print(msg)
            return 1
        return _print_status_report(report, as_json=args.as_json)

    args.token = args.token or TOKEN_PATH

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
