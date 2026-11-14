"""Seed the database with mock viral videos for local development and testing."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import select

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import discovery  # noqa: E402
from app.analytics import recompute_scores  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.models import Video, VideoStatus  # noqa: E402
from app.scrapers.mock_scraper import MockScraper  # noqa: E402

_ALLOWED_SEED_STATUSES = {
    "pending": VideoStatus.PENDING,
    "approved": VideoStatus.APPROVED,
    "transformed": VideoStatus.TRANSFORMED,
}


def seed_videos(
    count: int = 20,
    *,
    channel_slug: Optional[str] = None,
    status: str = "pending",
) -> dict:
    """Seed up to ``count`` deduplicated mock videos with complete metadata and scores."""
    if count < 1 or count > 500:
        raise ValueError("count must be between 1 and 500")
    norm_status = status.strip().lower()
    if norm_status not in _ALLOWED_SEED_STATUSES:
        valid = ", ".join(sorted(_ALLOWED_SEED_STATUSES))
        raise ValueError(f"invalid status {status!r} (valid: {valid})")
    target_status = _ALLOWED_SEED_STATUSES[norm_status]

    init_db()
    channel_id: Optional[str] = None
    resolved_slug: Optional[str] = None
    if channel_slug and channel_slug.strip():
        from app.growth import channels

        cleaned_slug = channel_slug.strip()
        with session_scope() as db:
            channels._ensure_seeded(db)
            ch = channels.channel_by_slug(db, cleaned_slug)
            if ch is None:
                known = ", ".join(c.slug for c in channels.all_channels(db)) or "default"
                raise ValueError(f"unknown channel {cleaned_slug!r} (known: {known})")
            channel_id = channels.event_channel(ch)
            resolved_slug = ch.slug

    scraper = MockScraper()
    handles = settings.handles_list or ["@viralbyte", "@dailyfyp"]
    hashtags = settings.hashtags_list or ["fyp", "shorts"]
    handle_quota = count if count == 1 else max(1, (count * 2 + 2) // 3)
    hashtag_quota = max(1, count - handle_quota) if count > 1 else 0

    candidates = list(scraper.scrape_handles(handles=handles, max_per_run=handle_quota))
    if hashtag_quota > 0:
        candidates.extend(scraper.scrape_hashtags(hashtags=hashtags, max_per_run=hashtag_quota))
    if len(candidates) < count:
        candidates.extend(scraper.scrape_handles(handles=[], max_per_run=count))

    created = 0
    skipped = 0
    now = datetime.now(timezone.utc)
    seen_keys: set[tuple[str, str]] = set()
    new_rows: list[Video] = []
    seed_platform = (settings.enabled_platforms_list[0].lower() if settings.enabled_platforms_list else "snapchat")

    with session_scope() as db:
        discovery.ensure_seed_creators(db, handles, platform=seed_platform)
        for it in candidates:
            if created >= count:
                break
            key = (it.source, it.source_id)
            if key in seen_keys:
                skipped += 1
                continue
            seen_keys.add(key)

            existing = db.execute(
                select(Video).where(Video.source == it.source, Video.source_id == it.source_id)
            ).scalar_one_or_none()
            if existing is not None:
                skipped += 1
                continue

            v = Video(
                source=it.source,
                source_id=it.source_id,
                source_url=it.source_url,
                author=it.author,
                author_name=it.author_name or "",
                title=it.title,
                description=it.description,
                thumbnail_url=it.thumbnail_url,
                media_url=it.media_url,
                local_path=it.local_path,
                transformed_path=it.local_path if target_status == VideoStatus.TRANSFORMED else None,
                language=it.language or "",
                keywords=list(it.keywords or []),
                views=it.views,
                likes=it.likes,
                comments=it.comments,
                shares=it.shares,
                duration_sec=it.duration_sec,
                width=it.width or 0,
                height=it.height or 0,
                status=target_status,
                channel_id=channel_id,
                meta=dict(it.meta or {}),
                source_published_at=it.published_at,
                scraped_at=now,
                curated_at=now if target_status in (VideoStatus.APPROVED, VideoStatus.TRANSFORMED) else None,
                transformed_at=now if target_status == VideoStatus.TRANSFORMED else None,
            )
            new_rows.append(v)
            db.add(v)
            created += 1

        if new_rows:
            recompute_scores(new_rows, now=now)

    out: dict = {
        "ok": True,
        "created": created,
        "skipped": skipped,
        "requested": count,
        "status": norm_status,
    }
    if resolved_slug:
        out["channel"] = resolved_slug
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("count", nargs="?", type=int, default=20, help="number of mock videos to seed (1..500)")
    parser.add_argument("--channel", type=str, default=None, help="target channel slug (e.g. default, tech)")
    parser.add_argument(
        "--status",
        type=str,
        default="pending",
        help="initial status for seeded rows: pending, approved, or transformed (default: pending)",
    )
    parser.add_argument("--json", action="store_true", help="print result as JSON")
    return parser


def main(n: int | list[str] | None = 20) -> int:
    if isinstance(n, list):
        parser = build_parser()
        args = parser.parse_args(n)
        try:
            summary = seed_videos(args.count, channel_slug=args.channel, status=args.status)
        except ValueError as exc:
            if args.json:
                print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
            else:
                print(f"error: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(summary, indent=2))
        else:
            chan_note = f" for [{summary['channel']}]" if summary.get("channel") else ""
            print(f"Seeded {summary['created']} mock videos{chan_note} (status={summary['status']}).")
        return 0

    count = 20 if n is None else int(n)
    try:
        summary = seed_videos(count)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Seeded {summary['created']} mock videos.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
