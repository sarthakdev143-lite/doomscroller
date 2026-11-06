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


def show_doctor(live: bool) -> int:
    result = doctor(live=live)
    for check in result["checks"]:
        mark = "ok  " if check["ok"] else ("FAIL" if check["required"] else "warn")
        print(f"{mark}  {check['name']}: {check['detail']}")
    print("\nReady." if result["ready"] else "\nNot ready: fix the FAIL lines.")
    return 0 if result["ready"] else 1


def show_resume() -> int:
    print("Resumed. Strikes still count until they are 90 days old." if resume() else "Not paused.")
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
            print(f"  {problem['at'][:16]} {problem['kind']}: {problem['summary']}")
    for kind, total in totals.items():
        if total > _PROBLEMS_PER_KIND:
            print(f"  and {total - _PROBLEMS_PER_KIND} more {kind}")


def show_report() -> int:
    report = build_report()
    if report["paused"]:
        print(f"PAUSED: {report['paused']['reason']}. Clear it with: scripts/growth.py resume")
    if report["hold"]:
        print(f"On hold until {report['hold']['until']}: {report['hold']['reason']}")
    if report["blocks"]:
        print("Takedown blocks, whose feeds are skipped. Lift with: scripts/growth.py unblock <name>")
        _show_blocks(report["blocks"])
    for warning in report["warnings"]:
        print(f"WARNING: {warning}")
    print(f"Day {report['day']}, {report['phase']}, {report['explore_rate']:.0%} of choices are tests")
    print(f"Published {report['published']}, judged {report['matured']}, {report['stock']} clips in stock")
    print(f"Last successful collect: {(report['collect']['last_success'] or 'never')[:16].replace('T', ' ')}")
    _show_problems(report["problems"])
    if report["focus"]:
        print("Testing harder: " + ", ".join(report["focus"]))
    for post in report["plan"].get("posts", []):
        print(f"  today {post['slot']}: {post['state']}")
    for factor, rows in report["factors"].items():
        print(f"\n{factor}")
        for row in rows:
            score = f"  score {row['score']['multiplier']}x" if "score" in row else ""
            print(f"  {row['arm']:<24} views {row['multiplier']:>5}x  ({row['low']}-{row['high']}x)  "
                  f"P(best) {row['p_best']:.0%}{score}  n={row['n']}")
    print("\nRecent")
    for event in report["events"][:15]:
        print(f"  {event['at'][:16]} {event['kind']}: {event['summary']}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("doctor", "report", "resume", "unblock"))
    parser.add_argument("name", nargs="?", help="unblock: the author, source or clip parent to lift")
    parser.add_argument("--offline", action="store_true", help="doctor: skip asking Google and reading feeds")
    args = parser.parse_args()
    init_db()
    if args.command == "unblock":
        sys.exit(show_unblock(args.name))
    if args.command == "doctor":
        sys.exit(show_doctor(live=not args.offline))
    sys.exit({"report": show_report, "resume": show_resume}[args.command]())
