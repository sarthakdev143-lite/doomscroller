"""Check and inspect the growth loop.

    venv/bin/python scripts/growth.py doctor    can it really run? Asks Google and reads feeds; --offline skips that
    venv/bin/python scripts/growth.py report    what has it learned?
    venv/bin/python scripts/growth.py resume    post again after a takedown pause
    venv/bin/python scripts/growth.py unblock   list takedown blocks; with a name, lift one
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import init_db, session_scope
from app.growth.report import build_report, doctor
from app.growth.safety import block_list, resume, unblock


_PROBLEMS_PER_KIND = 5


def _print_checks(checks: list[dict], indent: str = "") -> None:
    for check in checks:
        mark = "ok  " if check["ok"] else ("FAIL" if check["required"] else "warn")
        print(f"{indent}{mark}  {check['name']}: {check['detail']}")


def show_doctor(live: bool) -> int:
    result = doctor(live=live)
    _print_checks(result["checks"])
    for channel in result.get("channels", []):
        state = "ready" if channel["ready"] else "NOT READY"
        print(f"\nchannel {channel['slug']} ({channel['name']}) — {state}")
        _print_checks(channel["checks"], indent="  ")
    if result["ready"]:
        print("\nReady.")
        return 0
    bad = [c["name"] for c in result["checks"] if c["required"] and not c["ok"]]
    bad += [f"channel {ch['slug']}" for ch in result.get("channels", []) if not ch["ready"]]
    print(f"\nNot ready — fix: {', '.join(bad)}" if bad else "\nNot ready: fix the FAIL lines.")
    return 1


def show_reload() -> int:
    from app.db import init_db
    from app.growth.channels import reload_channels

    init_db()
    result = reload_channels()
    if result.get("error"):
        print(f"channels.json NOT applied — the running loop keeps its channels: {result['error']}")
        return 1
    print(f"Reloaded. Active channels: {', '.join(result['active']) or 'none'}. "
          "The running loop picks this up on its next tick (no restart needed).")
    return 0


def show_resume(slug: str | None) -> int:
    where = f" {slug}" if slug else ""
    if resume(slug):
        print(f"Resumed{where}. Strikes still count until they are 90 days old.")
    else:
        print(f"Not paused{where}." if slug else "Not paused.")
    return 0


def _show_blocks(blocks: list[dict]) -> None:
    for block in blocks:
        print(f"  {', '.join(block['authors']) or block['source']}: {block['reason']} on {block['at'][:10]}")


def show_unblock(name: str | None) -> int:
    if name is None:
        with session_scope() as db:
            blocks = block_list(db)
        _show_blocks(blocks)
        print("Lift one with: scripts/growth.py unblock <name>" if blocks else "No blocks.")
        return 0
    lifted = unblock(name)
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
    resume = f"scripts/growth.py resume {slug}".rstrip()
    if state["paused"]:
        print(f"PAUSED: {state['paused']['reason']}. Clear it with: {resume}")
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


def show_report() -> int:
    report = build_report()
    if report["blocks"]:
        print("Takedown blocks, whose feeds are skipped. Lift with: scripts/growth.py unblock <name>")
        _show_blocks(report["blocks"])
    sections = report.get("channels")
    if sections is None:
        _show_state(report)
        _show_problems(report["problems"])
        _show_learning(report)
    else:
        # Several channels: the totals, then each channel's own state and what it has learned.
        print(f"{len(sections)} channels. Published {report['published']}, judged {report['matured']}, "
              f"{report['stock']} clips in stock")
        _show_problems(report["problems"])
        for slug, section in sections.items():
            print(f"\n== {section['name']} ({slug}) ==")
            _show_state(section, slug)
            _show_learning(section)
    print("\nRecent")
    for event in report["events"][:15]:
        print(f"  {event['at'][:16]} {_tag(event)}{event['kind']}: {event['summary']}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("doctor", "report", "resume", "unblock", "reload"))
    parser.add_argument("name", nargs="?", help="unblock: the author/source/parent to lift; resume: a channel slug")
    parser.add_argument("--offline", action="store_true", help="doctor: skip asking Google and reading feeds")
    args = parser.parse_args()
    init_db()
    if args.command == "unblock":
        sys.exit(show_unblock(args.name))
    if args.command == "reload":
        sys.exit(show_reload())
    if args.command == "resume":
        sys.exit(show_resume(args.name))
    if args.command == "doctor":
        sys.exit(show_doctor(live=not args.offline))
    sys.exit(show_report())
