"""Manage the channels the loop runs, without editing channels.json by hand.

    venv/bin/python scripts/channels.py list
    venv/bin/python scripts/channels.py add tech --name "Tech & Science" --feed https://www.youtube.com/@mkbhd
    venv/bin/python scripts/channels.py feed tech --add https://www.youtube.com/@MarkRober --remove <link>
    venv/bin/python scripts/channels.py enable tech      # once its token is in place
    venv/bin/python scripts/channels.py disable tech     # stop one channel, leave the rest running

Every change is checked, written in one piece and applied to the running loop on its next tick.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.db import init_db, session_scope
from app.growth import channels

_SETUP = """
Next, for this channel (about 10 minutes, once):
  1. Create the YouTube channel, and a Google Cloud project of its own.
  2. Enable "YouTube Data API v3" and "YouTube Analytics API".
  3. Set up the OAuth consent screen, then press "Publish app" so it reads "In production".
     Skip that and the token dies after 7 days and the channel quietly stops.
  4. Create a Desktop OAuth client and save its JSON as: {secrets}
  5. venv/bin/python scripts/youtube_oauth.py --channel {slug} --force
     (pick THIS channel on Google's screen)
  6. venv/bin/python scripts/channels.py enable {slug}
  7. venv/bin/python scripts/growth.py doctor
"""


def _applied(result: dict) -> int:
    if result.get("error"):
        print(f"Saved, but not applied: {result['error']}")
        return 1
    print(f"Applied. Running channels: {', '.join(result['active']) or 'none'}.")
    return 0


def show_list() -> int:
    with session_scope() as db:
        channels.sync_channels(db)
        rows = channels.all_channels(db)
        if not rows:
            print("No channels.")
        for channel in rows:
            feeds = channels.feeds_for(db, channel)
            token = "token ok" if channel.token_path and os.path.exists(channel.token_path) else "no token"
            if channels.is_default(channel):
                token = "settings from .env"
            print(f"{'on ' if channel.enabled else 'off'}  {channel.slug:<16} {channel.name:<26} "
                  f"{channels.uploads_per_day(channel)}/day  {len(feeds)} feeds  {token}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="every channel, whether it is on, and whether its token is in place")
    add = commands.add_parser("add", help="declare a new channel (off until enabled)")
    add.add_argument("slug")
    add.add_argument("--name", default="")
    add.add_argument("--niche", default="")
    add.add_argument("--brand", default="", help="the @handle shown as the on-screen credit")
    add.add_argument("--feed", action="append", default=[], help="a source channel or playlist link; repeatable")
    add.add_argument("--uploads", type=int, default=3, help="posts a day (a Cloud project allows about 6)")
    add.add_argument("--timezone", default="")
    for name, text in (("enable", "start posting on a channel"), ("disable", "stop one channel")):
        toggle = commands.add_parser(name, help=text)
        toggle.add_argument("slug")
        if name == "enable":
            toggle.add_argument("--force", action="store_true", help="enable even with no token on disk")
    feed = commands.add_parser("feed", help="add or remove a channel's source links")
    feed.add_argument("slug")
    feed.add_argument("--add", action="append", default=[])
    feed.add_argument("--remove", action="append", default=[])
    args = parser.parse_args()

    init_db()
    try:
        if args.command == "list":
            return show_list()
        if args.command == "add":
            result = channels.add_channel(args.slug, name=args.name, niche=args.niche, brand_name=args.brand,
                                          feeds=tuple(args.feed), uploads_per_day=args.uploads,
                                          timezone=args.timezone)
            print(f"Added {args.slug} to {channels.settings.channels_path} (off for now).")
            if channels.DEFAULT_SLUG in result.get("active", []):
                print("Your existing channel from .env keeps running: it is listed as \"default\". "
                      "Remove that entry to retire it.")
            print(_SETUP.format(slug=args.slug, secrets=f"secrets/{args.slug}_client_secret.json"))
            return 1 if result.get("error") else 0
        if args.command == "enable":
            with session_scope() as db:
                channels.sync_channels(db)
                channel = channels.channel_by_slug(db, args.slug)
                token = channel.token_path if channel else ""
            if channel is not None and not args.force and not (token and os.path.exists(token)):
                print(f"{args.slug} has no token at {token or '(no token_path)'}, so it could not post. Run:\n"
                      f"  venv/bin/python scripts/youtube_oauth.py --channel {args.slug} --force\n"
                      "then enable it again (or pass --force).")
                return 1
            return _applied(channels.set_enabled(args.slug, True))
        if args.command == "disable":
            return _applied(channels.set_enabled(args.slug, False))
        return _applied(channels.change_feeds(args.slug, add=tuple(args.add), remove=tuple(args.remove)))
    except channels.ChannelsConfigError as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
