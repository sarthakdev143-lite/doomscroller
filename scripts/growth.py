"""Check and inspect the growth loop.

    venv/bin/python scripts/growth.py status    quick snapshot: stock, pauses/blocks, next post (default)
    venv/bin/python scripts/growth.py doctor    can it really run? Asks Google and reads feeds; --offline skips that
    venv/bin/python scripts/growth.py report    what has it learned? (--markdown for a shareable digest, --out FILE to save it)
    venv/bin/python scripts/growth.py notify-test  send a test alert to your phone (ntfy / Telegram)
    venv/bin/python scripts/growth.py pause     stop posting on a channel (supply and learning keep running)
    venv/bin/python scripts/growth.py resume    post again after a pause
    venv/bin/python scripts/growth.py unblock   list takedown blocks; with a name, lift one
    venv/bin/python scripts/growth.py reload    apply channels.json changes without restarting

Add --json on status, report, doctor, pause, resume, unblock, reload, or notify-test for machine-readable output,
and --channel/-c <slug> on status, report, or doctor to inspect one channel.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import init_db, session_scope
from app.growth import channels as _channels, report as _report, safety as _safety
from app.growth.report import build_report, build_status, check_channel, doctor
from app.growth.safety import block_list, pause, resume, unblock


_PROBLEMS_PER_KIND = 5


def _fmt_moment(iso_str: str | None) -> str:
    if not iso_str:
        return "none scheduled"
    return iso_str[:16].replace("T", " ") + (" UTC" if "+00:00" in iso_str or iso_str.endswith("Z") else "")


def _print_checks(checks: list[dict], indent: str = "") -> None:
    for check in checks:
        mark = "ok  " if check["ok"] else ("FAIL" if check["required"] else "warn")
        print(f"{indent}{mark}  {check['name']}: {check['detail']}")


def show_status(*, as_json: bool = False, channel: str | None = None) -> int:
    try:
        st = _report.build_status(channel_slug=channel)
    except LookupError as exc:
        if as_json:
            print(json.dumps({"error": str(exc)}, indent=2))
        else:
            print(f"Error: {exc}")
        return 1
    if as_json:
        print(json.dumps(st, indent=2))
        return 0
    loop_state = "on" if st["growth_enabled"] else "OFF"
    pub_state = ("mock" if st["use_mock"] else "live") if st["enable_publish"] else "publish OFF"
    print(f"Growth: {loop_state} ({pub_state}) | {st['stock']} in stock, {st['awaiting_review']} awaiting review, "
          f"{st['active_jobs']} cutting, {st['blocks']} block(s)")
    print(f"Next post: {_fmt_moment(st['next_post'])}")
    for ch in st["channels"]:
        badges = []
        if not ch["enabled"]:
            badges.append("DISABLED")
        if ch["paused"]:
            badges.append(f"PAUSED: {ch['paused'].get('reason', 'paused')}")
        if ch["hold"]:
            badges.append(f"HOLD until {str(ch['hold'].get('until', ''))[:16].replace('T', ' ')}: {ch['hold'].get('reason', '')}")
        badge_str = f" [{' | '.join(badges)}]" if badges else ""
        nxt = ch["next_post"]
        nxt_str = f"next {nxt['slot']} at {_fmt_moment(nxt['due'])}" if nxt else "no window waiting"
        print(f"  {ch['slug']} ({ch['name']}){badge_str}: {ch['stock']} ready, {ch['awaiting_review']} review, "
              f"{ch['posted_today']}/{ch['uploads_per_day']} today — {nxt_str}")
    return 0


def show_doctor(live: bool, *, as_json: bool = False, channel: str | None = None) -> int:
    if channel:
        slug = channel.strip()
        row = _report.check_channel(slug, live=live)
        if row is None:
            with session_scope() as db:
                all_chs = _channels.all_channels(db)
            if slug == "default" and any(_channels.is_default(c) for c in all_chs):
                base = _report.doctor(live=live)
                row = {"slug": "default", "name": "default", "enabled": True,
                       "ready": base["ready"], "checks": base["checks"]}
            else:
                known = ", ".join(c.slug for c in all_chs) or "none"
                msg = f"Unknown channel '{slug}'. Known channels: {known}."
                print(json.dumps({"error": msg}, indent=2) if as_json else msg)
                return 1
        if as_json:
            print(json.dumps(row, indent=2))
            return 0 if row["ready"] else 1
        state = "ready" if row["ready"] else "NOT READY"
        print(f"channel {row['slug']} ({row['name']}) — {state}")
        _print_checks(row["checks"], indent="  ")
        return 0 if row["ready"] else 1

    result = _report.doctor(live=live)
    if as_json:
        print(json.dumps(result, indent=2))
        return 0 if result["ready"] else 1
    _print_checks(result["checks"])
    for ch in result.get("channels", []):
        state = "ready" if ch["ready"] else "NOT READY"
        print(f"\nchannel {ch['slug']} ({ch['name']}) — {state}")
        _print_checks(ch["checks"], indent="  ")
    if result["ready"]:
        print("\nReady.")
        return 0
    bad = [c["name"] for c in result["checks"] if c["required"] and not c["ok"]]
    bad += [f"channel {ch['slug']}" for ch in result.get("channels", []) if not ch["ready"]]
    print(f"\nNot ready — fix: {', '.join(bad)}" if bad else "\nNot ready: fix the FAIL lines.")
    return 1


def show_reload(*, as_json: bool = False) -> int:
    from app.growth.channels import reload_channels

    init_db()
    result = reload_channels()
    if as_json:
        print(json.dumps(result, indent=2))
        return 1 if result.get("error") else 0
    if result.get("error"):
        print(f"channels.json NOT applied — the running loop keeps its channels: {result['error']}")
        return 1
    print(f"Reloaded. Active channels: {', '.join(result['active']) or 'none'}. "
          "The running loop picks this up on its next tick (no restart needed).")
    return 0


def show_pause(slug: str | None, *, as_json: bool = False) -> int:
    target = (slug or "").strip()
    with session_scope() as db:
        all_chs = _channels.all_channels(db)
        known_slugs = [c.slug for c in all_chs]
    if not target:
        if len(known_slugs) == 1:
            target = known_slugs[0]
        else:
            msg = f"Specify a channel slug to pause ({', '.join(known_slugs) or 'none'})."
            print(json.dumps({"ok": False, "error": msg}, indent=2) if as_json else msg)
            return 1
    if target not in known_slugs:
        msg = f"Unknown channel '{target}'. Known channels: {', '.join(known_slugs) or 'none'}."
        print(json.dumps({"ok": False, "error": msg}, indent=2) if as_json else msg)
        return 1
    _safety.pause(target)
    if as_json:
        print(json.dumps({"ok": True, "paused": target}, indent=2))
        return 0
    resume_cmd = f"scripts/growth.py resume {target}" if target != "default" else "scripts/growth.py resume"
    print(f"Paused {target}. Resume when ready with: {resume_cmd}")
    return 0


def show_resume(slug: str | None, *, as_json: bool = False) -> int:
    target = slug.strip() if slug else None
    if target:
        with session_scope() as db:
            known_slugs = [c.slug for c in _channels.all_channels(db)]
        if target not in known_slugs:
            msg = f"Unknown channel '{target}'. Known channels: {', '.join(known_slugs) or 'none'}."
            print(json.dumps({"ok": False, "error": msg}, indent=2) if as_json else msg)
            return 1
    cleared = _safety.resume(target)
    if as_json:
        print(json.dumps({"ok": True, "resumed": cleared, "channel": target}, indent=2))
        return 0
    where = f" {target}" if target else ""
    if cleared:
        print(f"Resumed{where}. Strikes still count until they are 90 days old.")
    else:
        print(f"Not paused{where}." if target else "Not paused.")
    return 0


def _show_blocks(blocks: list[dict]) -> None:
    for block in blocks:
        print(f"  {', '.join(block['authors']) or block['source']}: {block['reason']} on {block['at'][:10]}")


def show_unblock(name: str | None, *, as_json: bool = False) -> int:
    if name is None:
        with session_scope() as db:
            blocks = block_list(db)
        if as_json:
            print(json.dumps({"blocks": blocks}, indent=2))
            return 0
        _show_blocks(blocks)
        print("Lift one with: scripts/growth.py unblock <name>" if blocks else "No blocks.")
        return 0
    lifted = unblock(name)
    if as_json:
        print(json.dumps({"ok": True, "name": name.strip(), "lifted": lifted}, indent=2))
        return 0
    print(f"Lifted {lifted} block(s). Their strikes still count until they are 90 days old." if lifted
          else f"No block names {name}.")
    return 0


def _show_problems(problems: list[dict]) -> None:
    """Capped per kind: a long outage must not push one takedown off the screen."""
    if not problems:
        return
    print("\nProblems in the last 30 days")
    totals = Counter(p["kind"] for p in problems)
    shown: Counter = Counter()
    for problem in problems:
        shown[problem["kind"]] += 1
        if shown[problem["kind"]] <= _PROBLEMS_PER_KIND:
            print(f"  {problem['at'][:16]} {_tag(problem)}{problem['kind']}: {problem['summary']}")
    for kind, total in totals.items():
        if total > _PROBLEMS_PER_KIND:
            print(f"  and {total - _PROBLEMS_PER_KIND} more {kind}")


def _tag(event: dict) -> str:
    return f"[{event['channel']}] " if event.get("channel") else ""


def _show_state(state: dict, slug: str = "") -> None:
    """The pause, warnings and numbers of the whole loop, or of one channel."""
    resume_cmd = f"scripts/growth.py resume {slug}".rstrip()
    if state["paused"]:
        print(f"PAUSED: {state['paused']['reason']}. Clear it with: {resume_cmd}")
    if state["hold"]:
        print(f"On hold until {state['hold']['until']}: {state['hold']['reason']}")
    for warning in state["warnings"]:
        print(f"WARNING: {warning}")
    print(f"Day {state['day']}, {state['phase']}, {state['explore_rate']:.0%} of choices are tests")
    print(f"Published {state['published']}, judged {state['matured']}, {state['stock']} clips in stock")
    print(f"Last successful collect: {(state['collect']['last_success'] or 'never')[:16].replace('T', ' ')}")


def _show_learning(state: dict) -> None:
    if state["focus"]:
        print("Testing harder: " + ", ".join(state["focus"]))
    for post in state["plan"].get("posts", []):
        print(f"  today {post['slot']}: {post['state']}")
    for factor, rows in state["factors"].items():
        print(f"\n{factor}")
        for row in rows:
            score = f"  score {row['score']['multiplier']}x" if "score" in row else ""
            print(f"  {row['arm']:<24} views {row['multiplier']:>5}x  ({row['low']}-{row['high']}x)  "
                  f"P(best) {row['p_best']:.0%}{score}  n={row['n']}")


def _write_atomic(path: Path, text: str) -> None:
    """Whole file or nothing: a half-written weekly digest is worse than none."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    try:
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def show_report(*, as_json: bool = False, channel: str | None = None, markdown: bool = False,
                out: str | None = None) -> int:
    """Plain text by default. `markdown` prints a shareable digest, and `out` writes a file instead of printing
    (Markdown, or JSON with `as_json`)."""
    try:
        rep = _report.build_report(channel_slug=channel) if channel else _report.build_report()
    except LookupError as exc:
        if as_json:
            print(json.dumps({"error": str(exc)}, indent=2))
        else:
            print(f"Error: {exc}")
        return 1
    if out:
        target = Path(out)
        text = (json.dumps(rep, indent=2) if as_json else _report.render_markdown(rep)).rstrip() + "\n"
        try:
            _write_atomic(target, text)
        except OSError as exc:
            msg = f"cannot write {target}: {exc}"
            print(json.dumps({"ok": False, "error": msg}, indent=2) if as_json else f"Error: {msg}")
            return 1
        print(json.dumps({"ok": True, "out": str(target), "format": "json" if as_json else "markdown"}, indent=2)
              if as_json else f"Wrote {'JSON' if as_json else 'Markdown'} report to {target}.")
        return 0
    if as_json:
        print(json.dumps(rep, indent=2))
        return 0
    if markdown:
        print(_report.render_markdown(rep), end="")
        return 0
    if rep["blocks"]:
        print("Takedown blocks, whose feeds are skipped. Lift with: scripts/growth.py unblock <name>")
        _show_blocks(rep["blocks"])
    if channel:
        print(f"== {rep['name']} ({rep['slug']}) ==")
        _show_state(rep, "" if rep["slug"] == "default" else rep["slug"])
        _show_problems(rep["problems"])
        _show_learning(rep)
    else:
        sections = rep.get("channels")
        if sections is None:
            _show_state(rep)
            _show_problems(rep["problems"])
            _show_learning(rep)
        else:
            # Several channels: the totals, then each channel's own state and what it has learned.
            print(f"{len(sections)} channels. Published {rep['published']}, judged {rep['matured']}, "
                  f"{rep['stock']} clips in stock")
            _show_problems(rep["problems"])
            for slug, section in sections.items():
                print(f"\n== {section['name']} ({slug}) ==")
                _show_state(section, slug)
                _show_learning(section)
    print("\nRecent")
    for event in rep["events"][:15]:
        print(f"  {event['at'][:16]} {_tag(event)}{event['kind']}: {event['summary']}")
    return 0


def show_notify_test(*, as_json: bool = False) -> int:
    """Send one test alert to every configured phone channel and say which got it. Exits non-zero only on a crash."""
    from app.growth import notify

    try:
        result = notify.send_test_notification()
    except Exception as exc:  # a delivery bug must not hide behind a traceback that might quote a token URL
        msg = notify._redact(f"{type(exc).__name__}: {exc}")
        print(json.dumps({"ok": False, "error": msg}, indent=2) if as_json else f"Error: notify test crashed: {msg}")
        return 1
    if as_json:
        print(json.dumps(result, indent=2))
        return 0
    if not result.get("configured", True):
        print("No phone alerts are set up, so nothing was sent. Set NTFY_TOPIC (install the ntfy app and subscribe "
              "to it), or TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, in .env, then restart.")
        return 0
    for name, status in (result.get("channels") or {}).items():
        print(f"{name}: ok" if status.get("ok") else f"{name}: FAILED ({notify._redact(str(status.get('error') or 'unknown error'))})")
    print("Delivered to at least one channel — check your phone." if result.get("ok")
          else "Nothing was delivered. Fix the FAILED line(s) above and run this again.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", default="status",
                        choices=("status", "doctor", "report", "pause", "resume", "unblock", "reload", "notify-test"))
    parser.add_argument("name", nargs="?",
                        help="pause/resume: a channel slug; unblock: the author/source/parent to lift")
    parser.add_argument("--offline", action="store_true", help="doctor: skip asking Google and reading feeds")
    parser.add_argument("--json", dest="as_json", action="store_true", help="emit machine-readable JSON output")
    parser.add_argument("--channel", "-c", default=None, help="filter status, report, or doctor to one channel slug")
    parser.add_argument("--markdown", "--md", dest="markdown", action="store_true",
                        help="report: print a shareable Markdown digest instead of plain text")
    parser.add_argument("--out", default=None, metavar="FILE",
                        help="report: write the digest to FILE (Markdown, or JSON with --json) instead of printing it")
    args = parser.parse_args(argv)
    if (args.out or args.markdown) and args.command != "report":
        parser.error("--out and --markdown only apply to the report command")
    init_db()
    target_slug = args.name or args.channel
    if args.command == "status":
        return show_status(as_json=args.as_json, channel=target_slug)
    if args.command == "unblock":
        return show_unblock(args.name, as_json=args.as_json)
    if args.command == "reload":
        return show_reload(as_json=args.as_json)
    if args.command == "pause":
        return show_pause(target_slug, as_json=args.as_json)
    if args.command == "resume":
        return show_resume(target_slug, as_json=args.as_json)
    if args.command == "doctor":
        return show_doctor(live=not args.offline, as_json=args.as_json, channel=target_slug)
    if args.command == "notify-test":
        return show_notify_test(as_json=args.as_json)
    return show_report(as_json=args.as_json, channel=target_slug, markdown=args.markdown, out=args.out)


if __name__ == "__main__":
    sys.exit(main())
