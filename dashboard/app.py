"""Streamlit dashboard for Doomscroller.

Run with: streamlit run dashboard/app.py --server.port 8501
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import streamlit as st  # noqa: E402
import httpx  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.models import Video, VideoStatus, MetadataDraft, Creator  # noqa: E402
from app import quota  # noqa: E402
from app.analytics import recompute_scores  # noqa: E402
from tasks.pipeline import (  # noqa: E402
    scrape_task, transform_task, publish_task, generate_metadata_for_video,
)

from sqlalchemy import desc, func, select  # noqa: E402

API_BASE = "http://localhost:8000"

st.set_page_config(
    page_title=settings.dashboard_title,
    page_icon="🎬",
    layout="wide",
)


@st.cache_resource
def _ensure_db():
    init_db()
    return True


def _api(method: str, path: str, **kw):
    try:
        with httpx.Client(timeout=300) as c:
            r = c.request(method, f"{API_BASE}{path}", **kw)
            if r.status_code >= 400:
                st.error(f"API {method} {path} → {r.status_code}: {r.text[:300]}")
                return None
            return r.json()
    except httpx.ConnectError:
        st.warning(f"API not reachable at {API_BASE}. Running in offline mode.")
        return None
    except Exception as e:
        st.error(f"API error: {e}")
        return None


def _status_badge(status: str) -> str:
    colors = {
        "pending": "🟡", "approved": "🟢", "rejected": "🔴",
        "transforming": "🔵", "transformed": "🟣",
        "publishing": "🟠", "published": "✅", "failed": "❌",
    }
    return f"{colors.get(status, '⚪')} {status}"


# ============ Sidebar ============
def sidebar():
    st.sidebar.markdown(f"## {settings.dashboard_title}")
    st.sidebar.caption(f"mock_mode={settings.use_mock} · niche={settings.channel_niche}")
    st.sidebar.markdown(f"Uploads/day: **{settings.uploads_per_day}** (every {settings.upload_interval_hours}h)")

    with session_scope() as db:
        snap = quota.snapshot(db)
    st.sidebar.markdown("### Daily quota")
    used = snap["uploads"]
    cap = snap["cap"]
    remaining = snap["remaining"]
    st.sidebar.metric("Today", f"{used} / {cap}", delta=f"{remaining} left")
    st.sidebar.progress(min(1.0, used / max(1, cap)))

    # Scheduler status
    jobs = _api("GET", "/scheduler/jobs") or {}
    st.sidebar.markdown("### Scheduler")
    if jobs.get("running"):
        for j in jobs.get("jobs", []):
            next_run = j.get("next_run_time", "")
            if next_run:
                next_run = next_run[:19].replace("T", " ")
            st.sidebar.caption(f"• `{j['id']}` → {next_run or 'pending'}")
    else:
        st.sidebar.warning("Scheduler not running")

    st.sidebar.markdown("### Actions")
    col1, col2 = st.sidebar.columns(2)
    if col1.button("🧹 Scrape now"):
        with st.spinner("Scraping…"):
            out = _api("POST", "/scrape", json={})
            if out:
                st.sidebar.success(f"Scraped: {out.get('result', out)}")
    if col2.button("♻️ Recompute"):
        _api("POST", "/scores/recompute")
        st.sidebar.success("Scores updated")

    if st.sidebar.button("🔍 Discover creators"):
        out = _api("POST", "/scheduler/run/discovery")
        st.sidebar.success(f"Discovery: {out}")

    if st.sidebar.button("📺 Run publish slot"):
        out = _api("POST", "/scheduler/run/publish")
        st.sidebar.success(f"Publish: {out}")

    auto_publish = st.sidebar.checkbox(
        "Auto-publish at scheduled slots",
        value=settings.auto_publish,
        help="When enabled, the scheduler auto-publishes the next transformed video every "
             f"{settings.upload_interval_hours}h (cap {settings.uploads_per_day}/day).",
    )
    if auto_publish != settings.auto_publish:
        st.sidebar.info("Toggle AUTO_PUBLISH in .env to change this permanently.")


# ============ Video card ============
def load_metadata(video_id: str) -> dict | None:
    with session_scope() as db:
        md = db.execute(
            select(MetadataDraft).where(MetadataDraft.video_id == video_id)
        ).scalar_one_or_none()
        if md is None:
            return None
        return {
            "title": md.title, "description": md.description,
            "tags": md.tags, "language": md.detected_language,
            "edited": md.edited,
        }


def render_video_card(v: Video):
    status = _status_badge(v.status.value if hasattr(v.status, "value") else str(v.status))
    col_main, col_meta, col_actions = st.columns([6, 3, 5])

    with col_main:
        title = v.title or "(no title)"
        st.markdown(f"**{title}**")
        st.caption(f"By {v.author or 'unknown'} · {v.source} · {v.duration_sec}s · {v.source_url}")
        if v.description:
            with st.expander("Source description"):
                st.write(v.description[:800])

    with col_meta:
        c1, c2 = st.columns(2)
        c1.metric("Views", f"{v.views:,}")
        c2.metric("Likes", f"{v.likes:,}")
        c1.metric("Engagement", f"{v.engagement_score:.3f}")
        c2.metric("Velocity", f"{v.velocity_score:.3f}")
        st.caption(status)

    with col_actions:
        if v.status == VideoStatus.PENDING:
            c1, c2 = st.columns(2)
            if c1.button("✅ Approve", key=f"approve_{v.id}"):
                with session_scope() as db:
                    row = db.get(Video, v.id)
                    row.status = VideoStatus.APPROVED
                    row.curated_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
                st.success("Approved — will be transformed automatically.")
                st.rerun()
            if c2.button("🔴 Reject", key=f"reject_{v.id}"):
                with session_scope() as db:
                    row = db.get(Video, v.id)
                    row.status = VideoStatus.REJECTED
                    row.curated_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
                st.rerun()

        elif v.status == VideoStatus.APPROVED:
            if st.button("🎨 Transform now", key=f"transform_{v.id}"):
                with st.spinner("Transforming…"):
                    out = transform_task(v.id, {})
                st.write(out)
                st.rerun()

        elif v.status == VideoStatus.TRANSFORMED:
            # Show metadata preview + edit + publish
            with st.expander("📝 Metadata (editable)", expanded=True):
                md = load_metadata(v.id)
                if md is None:
                    if st.button("Generate metadata with LLM", key=f"gen_{v.id}"):
                        with st.spinner("LLM generating metadata…"):
                            out = generate_metadata_for_video(v.id)
                        st.write(out)
                        st.rerun()
                else:
                    new_title = st.text_input("Title", value=md["title"], key=f"t_{v.id}")
                    new_desc = st.text_area("Description", value=md["description"],
                                            height=100, key=f"d_{v.id}")
                    new_tags = st.text_input("Tags (comma-separated)",
                                             value=", ".join(md["tags"]),
                                             key=f"tags_{v.id}")
                    col_a, col_b = st.columns(2)
                    if col_a.button("💾 Save metadata", key=f"save_{v.id}"):
                        tags_list = [t.strip() for t in new_tags.split(",") if t.strip()]
                        _api("PUT", f"/metadata/{v.id}", json={
                            "title": new_title, "description": new_desc, "tags": tags_list,
                        })
                        st.success("Saved")
                    if col_b.button("🔄 Regenerate", key=f"regen_{v.id}"):
                        with st.spinner("Regenerating…"):
                            out = generate_metadata_for_video(v.id)
                        st.rerun()
            if st.button("📺 Publish now", key=f"publish_{v.id}"):
                with st.spinner("Publishing…"):
                    out = publish_task(v.id, {})
                st.write(out)
                st.rerun()

        elif v.status == VideoStatus.PUBLISHED:
            st.success(f"Published: {v.youtube_video_id}")
            if v.youtube_video_id and not v.youtube_video_id.startswith("MOCK_"):
                st.markdown(f"[Watch on YouTube](https://youtube.com/watch?v={v.youtube_video_id})")

        elif v.status == VideoStatus.FAILED:
            st.error(v.error or "Failed")
            if st.button("Retry transform", key=f"retry_{v.id}"):
                transform_task(v.id, {})
                st.rerun()

    st.divider()


# ============ Main ============
def main():
    _ensure_db()
    sidebar()

    st.title("🎬 Doomscroller")
    st.caption("Semi-automated YouTube Shorts factory · approve → autonomous publish")

    # Top stats
    with session_scope() as db:
        total = db.scalar(select(func.count(Video.id))) or 0
        pending = db.scalar(select(func.count(Video.id)).where(Video.status == VideoStatus.PENDING)) or 0
        approved = db.scalar(select(func.count(Video.id)).where(Video.status == VideoStatus.APPROVED)) or 0
        transformed = db.scalar(select(func.count(Video.id)).where(Video.status == VideoStatus.TRANSFORMED)) or 0
        published = db.scalar(select(func.count(Video.id)).where(Video.status == VideoStatus.PUBLISHED)) or 0
        creators = db.scalar(select(func.count(Creator.id))) or 0
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Total", total)
    c2.metric("Pending", pending)
    c3.metric("Approved", approved)
    c4.metric("Transformed", transformed)
    c5.metric("Published", published)
    c6.metric("Creators", creators)

    tabs = st.tabs(["📥 Pending", "✅ Approved", "📺 Ready to publish", "✅ Published", "👥 Creators", "🗂 All"])

    with tabs[0]:
        st.subheader("Pending curation — your only job: approve or reject")
        with session_scope() as db:
            rows = db.execute(
                select(Video).where(Video.status == VideoStatus.PENDING)
                .order_by(desc(Video.velocity_score), desc(Video.engagement_score))
                .limit(50)
            ).scalars().all()
        if not rows:
            st.info("No pending videos. The scraper will fetch more on its schedule, or click 'Scrape now' in the sidebar.")
        for v in rows:
            render_video_card(v)

    with tabs[1]:
        st.subheader("Approved — queued for transformation")
        with session_scope() as db:
            rows = db.execute(
                select(Video).where(Video.status == VideoStatus.APPROVED)
                .order_by(desc(Video.curated_at)).limit(50)
            ).scalars().all()
        if not rows:
            st.info("Nothing approved yet. Approve videos from the Pending tab.")
        for v in rows:
            render_video_card(v)

    with tabs[2]:
        st.subheader("Transformed — ready to publish. Review metadata then publish.")
        with session_scope() as db:
            rows = db.execute(
                select(Video).where(Video.status == VideoStatus.TRANSFORMED)
                .order_by(desc(Video.transformed_at)).limit(50)
            ).scalars().all()
        if not rows:
            st.info("No transformed videos. The scheduler transforms approved videos automatically.")
        for v in rows:
            render_video_card(v)

    with tabs[3]:
        st.subheader("Published to YouTube")
        with session_scope() as db:
            rows = db.execute(
                select(Video).where(Video.status == VideoStatus.PUBLISHED)
                .order_by(desc(Video.published_at)).limit(50)
            ).scalars().all()
        if not rows:
            st.info("Nothing published yet.")
        for v in rows:
            render_video_card(v)

    with tabs[4]:
        st.subheader("Discovered creators")
        with session_scope() as db:
            creators = db.execute(
                select(Creator).order_by(desc(Creator.score), desc(Creator.is_seed)).limit(100)
            ).scalars().all()
        if creators:
            data = [
                {
                    "handle": c.handle, "platform": c.platform,
                    "seed": "✅" if c.is_seed else "🔍",
                    "score": round(c.score, 3),
                    "videos_scraped": c.videos_scraped,
                    "discovered_from": c.discovered_from or "-",
                    "last_scraped": c.last_scraped_at.strftime("%Y-%m-%d %H:%M") if c.last_scraped_at else "-",
                }
                for c in creators
            ]
            st.dataframe(data, use_container_width=True)
        else:
            st.info("No creators yet. Add seeds to TARGET_HANDLES in .env, or run discovery.")

    with tabs[5]:
        st.subheader("All videos")
        with session_scope() as db:
            rows = db.execute(
                select(Video).order_by(desc(Video.scraped_at)).limit(100)
            ).scalars().all()
        for v in rows:
            render_video_card(v)


if __name__ == "__main__":
    main()
