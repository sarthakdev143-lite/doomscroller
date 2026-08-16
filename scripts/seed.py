"""Seed the database with mock viral videos so the dashboard has content."""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.analytics import recompute_scores
from app.db import init_db, session_scope
from app.models import Video, VideoStatus
from app.scrapers.mock_scraper import MockScraper
from app.config import settings
from app import discovery


def main(n: int = 20) -> None:
    init_db()
    scraper = MockScraper()
    items = scraper.scrape_handles(
        handles=settings.handles_list or ["@viralbyte", "@dailyfyp"],
        max_per_run=n,
    )
    items += scraper.scrape_hashtags(
        hashtags=settings.hashtags_list or ["fyp", "shorts"],
        max_per_run=max(5, n // 2),
    )
    created = 0
    with session_scope() as db:
        # Ensure seed creators exist
        discovery.ensure_seed_creators(db, settings.handles_list or ["@viralbyte", "@dailyfyp"])
        for it in items:
            from sqlalchemy import select
            existing = db.execute(
                select(Video).where(Video.source == it.source,
                                    Video.source_id == it.source_id)
            ).scalar_one_or_none()
            if existing:
                continue
            v = Video(
                source=it.source,
                source_id=it.source_id,
                source_url=it.source_url,
                author=it.author,
                title=it.title,
                description=it.description,
                thumbnail_url=it.thumbnail_url,
                local_path=it.local_path,
                views=it.views,
                likes=it.likes,
                comments=it.comments,
                shares=it.shares,
                duration_sec=it.duration_sec,
                status=VideoStatus.PENDING,
                meta=it.meta,
            )
            recompute_scores([v])
            db.add(v)
            created += 1
    print(f"Seeded {created} mock videos.")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    main(n)
