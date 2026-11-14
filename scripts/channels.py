"""Set up and manage the channels the loop runs, without editing channels.json by hand.

    venv/bin/python scripts/channels.py                  # every channel, and the next step for each
    venv/bin/python scripts/channels.py add tech --name "Tech & Science" --feed https://www.youtube.com/@mkbhd
    venv/bin/python scripts/channels.py connect tech     # sign in to YouTube, check it, switch it on
    venv/bin/python scripts/channels.py feed tech --add https://www.youtube.com/@MarkRober --remove <link>
    venv/bin/python scripts/channels.py set tech --uploads 4 --brand @techclips
    venv/bin/python scripts/channels.py clone tech tech-2 --name "Tech Secondary"
    venv/bin/python scripts/channels.py export --out channels_backup.json
    venv/bin/python scripts/channels.py import channels_backup.json --dry-run
    venv/bin/python scripts/channels.py disable tech     # stop one channel, leave the rest running

A new channel is two commands: add, then connect. Every change is checked, written in one piece and
picked up by the running loop on its next tick, so nothing needs a restart.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.db import init_db, session_scope
from app.growth import channels

# Google's part needs a person in a browser, so it is spelled out once, in order.
_GOOGLE = """
{slug} needs its own Google project (about 10 minutes, once). In a browser:
  1. Create the YouTube channel.
  2. console.cloud.google.com: create a project for it.
  3. Enable "YouTube Data API v3" and "YouTube Analytics API".
  4. OAuth consent screen: set it up, then press "Publish app" so it reads "In production".
     Skip that and the sign-in dies after 7 days and the channel quietly stops.
  5. Credentials: create an OAuth client of type "Desktop app" and download its JSON.
  6. Save that file here as: {secrets}

Then:  venv/bin/python scripts/channels.py connect {slug}
"""


def _applied(result: dict) -> int:
    if result.get("error"):
        print(f"Saved, but not applied: {result['error']}")
        return 1
    print(f"Done. Running now: {', '.join(result['active']) or 'no channels'}.")
    return 0


def show_list() -> int:
    with session_scope() as db:
        channels.sync_channels(db)
        problem = channels.config_health(db).get("error")
        rows = [(c, len(channels.feeds_for(db, c)), channels.next_step(db, c)) for c in channels.all_channels(db)]
    if problem:
        print(f"channels.json has a problem, so the last good setup is still running:\n  {problem}\n")
    if not rows:
        print("No channels yet. Start one with:\n  venv/bin/python scripts/channels.py add <slug> --name \"...\" --feed <link>")
        return 0
    todo = 0
    for channel, feeds, step in rows:
        state = "running" if channel.enabled and not step else ("on" if channel.enabled else "off")
        sources = "from .env" if channels.is_default(channel) else f"{feeds} source{'s' if feeds != 1 else ''}"
        print(f"{channel.slug:<14} {(channel.name or '')[:24]:<24} {state:<8} "
              f"{channels.uploads_per_day(channel)}/day  {sources}")
        if step:
            todo += 1
            print(f"{'':<14} next: {step}")
    print(f"\n{len(rows) - todo} of {len(rows)} ready." + ("" if not todo else " Do each \"next\" line, then run this again."))
    return 0


def _report(row: dict) -> list[str]:
    """What still stops this channel, in the owner's words."""
    return [f"{check['name']}: {check['detail']}" for check in row["checks"] if check["required"] and not check["ok"]]


def _no_screen() -> bool:
    """A server reached over SSH has no browser to open, and waiting for one would hang the sign-in."""
    if os.environ.get("SSH_CONNECTION"):
        return True
    return sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def connect(slug: str, *, again: bool = False, no_browser: bool = False, port: int = 0) -> int:
    """Sign in, check the result against Google, and only then switch the channel on. Safe to run again."""
    from app.growth import report

    no_browser = no_browser or _no_screen()
    with session_scope() as db:
        channels.sync_channels(db)
        channel = channels.channel_by_slug(db, slug)
        if channel is None:
            known = ", ".join(c.slug for c in channels.all_channels(db)) or "none"
            print(f"No channel {slug!r}. Known: {known}. Add it first: scripts/channels.py add {slug}")
            return 1
        if channels.is_default(channel):
            print("That is the channel from your .env. Connect it with: venv/bin/python scripts/youtube_oauth.py")
            return 1
        secrets, token = channel.client_secrets, channel.token_path
    if not secrets or not os.path.exists(secrets):
        print(_GOOGLE.format(slug=slug, secrets=secrets or f"secrets/{slug}_client_secret.json"))
        return 1

    if again or not os.path.exists(token):
        print(f"Sign in with the Google account that owns the {slug} YouTube channel, and pick THAT channel.")
        command = [sys.executable, str(Path(__file__).with_name("youtube_oauth.py")), "--channel", slug, "--force"]
        command += ["--no-browser"] if no_browser else []
        command += ["--port", str(port)] if port else []
        if subprocess.call(command) or not os.path.exists(token):
            print(f"\nNot connected. Fix what it said above, then run this again:\n"
                  f"  venv/bin/python scripts/channels.py connect {slug}")
            return 1

    # On first, because a channel that is off is not checked against Google.
    switched = channels.set_enabled(slug, True)
    if switched.get("error"):
        print(f"Could not switch {slug} on: {switched['error']}")
        return 1
    print("Checking it with Google...")
    row = report.check_channel(slug)
    problems = _report(row) if row else ["it could not be checked"]
    if problems:
        channels.set_enabled(slug, False)  # a channel that cannot post would only miss every window
        print(f"\n{slug} is connected but not switched on yet:")
        for line in problems:
            print(f"  - {line}")
        print(f"Fix that, then run this again:\n  venv/bin/python scripts/channels.py connect {slug}")
        return 1
    name = next((c["detail"] for c in row["checks"] if c["name"] == "channel"), "")
    print(f"\n{slug} is on, {name or 'connected'}. It starts cutting clips within the hour.")
    print("Clips wait for your approval on the review page before anything is posted.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("list", help="every channel and its next step (the default)")
    add = commands.add_parser("add", help="declare a new channel (off until connected)")
    add.add_argument("slug", help="a short name for it, such as tech")
    add.add_argument("--name", default="")
    add.add_argument("--niche", default="")
    add.add_argument("--brand", default="", help="the @handle shown on its clips")
    add.add_argument("--feed", action="append", default=[], help="a creator's channel or playlist link; repeatable")
    add.add_argument("--uploads", type=int, default=3, help="posts a day (a Cloud project allows about 6)")
    add.add_argument("--timezone", default="")
    link = commands.add_parser("connect", help="sign in to YouTube, check it, and switch the channel on")
    link.add_argument("slug")
    link.add_argument("--again", action="store_true", help="sign in again even though a sign-in is saved")
    link.add_argument("--no-browser", action="store_true", help="print the link (for a server reached over SSH)")
    link.add_argument("--port", type=int, default=0)
    edit = commands.add_parser("set", help="change a channel's name, handle, pace, timezone or approval policy")
    edit.add_argument("slug")
    edit.add_argument("--name")
    edit.add_argument("--niche")
    edit.add_argument("--brand", dest="brand_name", help="the @handle shown on its clips")
    edit.add_argument("--uploads", dest="uploads_per_day", type=int, help="posts a day")
    edit.add_argument("--timezone")
    edit.add_argument(
        "--require-approval",
        dest="require_approval",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="whether a person must approve clips before upload",
    )
    edit.add_argument(
        "--auto-approve-score",
        dest="auto_approve_score",
        type=float,
        default=None,
        help="AI score threshold (0-1) for unattended clip approval; 0 turns it off",
    )
    edit.add_argument(
        "--cadence-backstop",
        dest="cadence_backstop",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="approve the best waiting clip rather than miss a posting slot",
    )
    for name, text in (("enable", "switch a channel on without checking it"), ("disable", "stop one channel")):
        toggle = commands.add_parser(name, help=text)
        toggle.add_argument("slug")
        if name == "enable":
            toggle.add_argument("--force", action="store_true", help="enable even with no sign-in saved")
    feed = commands.add_parser("feed", help="add or remove a channel's source links")
    feed.add_argument("slug")
    feed.add_argument("--add", action="append", default=[])
    feed.add_argument("--remove", action="append", default=[])
    exp = commands.add_parser("export", help="export portable channel settings and feeds as JSON (no secrets)")
    exp.add_argument("--out", default="", help="write snapshot to FILE instead of stdout")
    exp.add_argument("--json", action="store_true", dest="as_json", help="machine-readable JSON output")
    imp = commands.add_parser("import", help="import channels and feeds from a portable JSON snapshot")
    imp.add_argument("file", help="path to exported channels JSON file")
    imp.add_argument("--dry-run", action="store_true", help="validate without writing changes")
    imp.add_argument("--json", action="store_true", dest="as_json", help="machine-readable JSON output")
    cln = commands.add_parser("clone", help="clone a channel's settings and feeds into a new unlinked slug")
    cln.add_argument("source_slug", help="existing channel slug to copy from")
    cln.add_argument("new_slug", help="new channel slug to create")
    cln.add_argument("--name", default="", help="display name for the cloned channel")
    cln.add_argument("--json", action="store_true", dest="as_json", help="machine-readable JSON output")
    args = parser.parse_args(argv)

    init_db()
    try:
        if args.command in (None, "list"):
            return show_list()
        if args.command == "add":
            result = channels.add_channel(args.slug, name=args.name, niche=args.niche, brand_name=args.brand,
                                          feeds=tuple(args.feed), uploads_per_day=args.uploads,
                                          timezone=args.timezone)
            print(f"Added {args.slug} (off for now).")
            if channels.DEFAULT_SLUG in result.get("active", []):
                print("Your existing channel keeps running as it was.")
            if not args.feed:
                print(f"It has no creators to clip yet: scripts/channels.py feed {args.slug} --add <link>")
            if result.get("error"):
                print(f"Saved, but not applied: {result['error']}")
                return 1
            secrets = f"secrets/{args.slug}_client_secret.json"
            # Someone who already saved the file goes straight on; otherwise Google's part comes next.
            if os.path.exists(secrets) and args.feed:
                return connect(args.slug)
            print(_GOOGLE.format(slug=args.slug, secrets=secrets))
            return 0
        if args.command == "connect":
            return connect(args.slug, again=args.again, no_browser=args.no_browser, port=args.port)
        if args.command == "set":
            editable_keys = (
                "name",
                "niche",
                "brand_name",
                "uploads_per_day",
                "timezone",
                "require_approval",
                "auto_approve_score",
                "cadence_backstop",
            )
            fields = {key: getattr(args, key) for key in editable_keys}
            if all(value is None for value in fields.values()):
                print(
                    "Nothing to change. Give at least one of --name --niche --brand --uploads --timezone "
                    "--require-approval/--no-require-approval --auto-approve-score --cadence-backstop/--no-cadence-backstop."
                )
                return 1
            return _applied(channels.update_channel(args.slug, **fields))
        if args.command == "enable":
            with session_scope() as db:
                channels.sync_channels(db)
                channel = channels.channel_by_slug(db, args.slug)
                token = channel.token_path if channel else ""
            if channel is not None and not args.force and not (token and os.path.exists(token)):
                print(f"{args.slug} is not signed in to YouTube yet, so it could not post. Run:\n"
                      f"  venv/bin/python scripts/channels.py connect {args.slug}")
                return 1
            return _applied(channels.set_enabled(args.slug, True))
        if args.command == "disable":
            return _applied(channels.set_enabled(args.slug, False))
        if args.command == "export":
            with session_scope() as db:
                channels.sync_channels(db)
                snap = channels.export_channels_snapshot(db)
            rendered = json.dumps(snap, indent=2, ensure_ascii=False)
            if args.out:
                out_path = Path(args.out)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                temp = out_path.with_name(out_path.name + ".tmp")
                temp.write_text(rendered + "\n", encoding="utf-8")
                os.replace(temp, out_path)
                if args.as_json:
                    print(json.dumps({"ok": True, "out": str(out_path), "count": snap["count"]}, indent=2))
                else:
                    print(f"Exported {snap['count']} channel(s) to {out_path}.")
            else:
                print(rendered)
            return 0
        if args.command == "import":
            in_path = Path(args.file)
            try:
                raw_text = in_path.read_text(encoding="utf-8")
            except OSError as exc:
                msg = f"cannot read {in_path}: {exc}"
                print(json.dumps({"ok": False, "error": msg}, indent=2) if args.as_json else f"ERROR: {msg}")
                return 1
            try:
                payload = json.loads(raw_text)
            except json.JSONDecodeError as exc:
                msg = f"{in_path} is not valid JSON ({exc})"
                print(json.dumps({"ok": False, "error": msg}, indent=2) if args.as_json else f"ERROR: {msg}")
                return 1
            with session_scope() as db:
                result = channels.import_channels_snapshot(db, payload, dry_run=args.dry_run)
            if args.as_json:
                print(json.dumps(result, indent=2))
            elif args.dry_run:
                print(
                    f"Dry run OK: {len(result['created'])} to create, {len(result['updated'])} to update "
                    f"({result['feeds']} feed(s)). No changes written."
                )
            else:
                running = ", ".join(result.get("active", [])) or "no channels"
                print(
                    f"Imported {result['count']} channel(s) "
                    f"({len(result['created'])} created, {len(result['updated'])} updated). "
                    f"Running now: {running}."
                )
            return 0
        if args.command == "clone":
            with session_scope() as db:
                cloned = channels.clone_channel(db, args.source_slug, args.new_slug, name=args.name)
                feed_count = len(channels.feeds_for(db, cloned))
                slug = cloned.slug
                cname = cloned.name or slug
                enabled = bool(cloned.enabled)
                secrets = cloned.client_secrets or f"secrets/{slug}_client_secret.json"
            if args.as_json:
                print(
                    json.dumps(
                        {
                            "ok": True,
                            "slug": slug,
                            "cloned_from": args.source_slug,
                            "name": cname,
                            "enabled": enabled,
                            "feeds": feed_count,
                        },
                        indent=2,
                    )
                )
            else:
                print(f"Cloned {args.source_slug} -> {slug} ({cname}, {feed_count} source(s), off for now).")
                print(_GOOGLE.format(slug=slug, secrets=secrets))
            return 0
        return _applied(channels.change_feeds(args.slug, add=tuple(args.add), remove=tuple(args.remove)))
    except channels.ChannelsConfigError as exc:
        if getattr(args, "as_json", False):
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        else:
            print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
