"""Streamlit dashboard for Doomscroller.

Run with: streamlit run dashboard/app.py --server.port 8501
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Force the project root onto sys.path *before* anything else,
# and aggressively evict anything that shadowed the real `app` package
# (Streamlit sometimes injects the script dir at the front of sys.path).
for _p in list(sys.path):
    if Path(_p).resolve() == (PROJECT_ROOT / "dashboard").resolve():
        sys.path.remove(_p)
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Drop cached modules that Streamlit may have pre-loaded from the wrong
# location so the fresh import resolves against the real package root.
for _mod in ("app", "app.config", "app.db", "app.models", "app.quota",
             "app.analytics", "tasks", "tasks.pipeline"):
    sys.modules.pop(_mod, None)

# Make sure this script always runs with the project root as cwd so
# relative imports and file paths stay consistent regardless of how
# Streamlit is invoked.
try:
    os.chdir(PROJECT_ROOT)
except OSError:
    pass

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

    if st.sidebar.button("✂️ Chop long to Shorts now"):
        with st.sidebar.spinner("Chopping long videos into Shorts clips…"):
            out = _api("POST", "/scheduler/run/chop_long")
        if out:
            st.sidebar.success(f"Chop: {out}")

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


def render_video_card(v: Video, prefix: str = "main"):
    status = _status_badge(v.status.value if hasattr(v.status, "value") else str(v.status))
    col_main, col_meta, col_actions = st.columns([6, 3, 5])

    with col_main:
        title = v.title or "(no title)"
        st.markdown(f"**{title}**")
        st.caption(f"By {v.author or 'unknown'} · {v.source} · {v.duration_sec}s")
        # Thumbnail or local video preview
        try:
            if v.thumbnail_url:
                st.image(v.thumbnail_url, width=320)
            elif v.local_path and os.path.exists(v.local_path):
                st.video(v.local_path)
        except Exception:
            pass
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
            if c1.button("✅ Approve", key=f"{prefix}_approve_{v.id}"):
                with session_scope() as db:
                    row = db.get(Video, v.id)
                    row.status = VideoStatus.APPROVED
                    row.curated_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
                st.success("Approved — will be transformed automatically.")
                st.rerun()
            if c2.button("🔴 Reject", key=f"{prefix}_reject_{v.id}"):
                with session_scope() as db:
                    row = db.get(Video, v.id)
                    row.status = VideoStatus.REJECTED
                    row.curated_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
                st.rerun()

        elif v.status == VideoStatus.APPROVED:
            if st.button("🎨 Transform now", key=f"{prefix}_transform_{v.id}"):
                with st.spinner("Transforming — this can take 30–120s depending on video size…"):
                    out = _api("POST", f"/videos/{v.id}/transform", json={})
                if out:
                    if out.get("queued"):
                        st.success(f"Queued! Task ID: {out.get('task_id', 'N/A')} — auto-refreshing…")
                    elif out.get("result", {}).get("ok"):
                        st.success("Transform complete!")
                    else:
                        st.error(f"Transform error: {out}")
                import time as _time
                _time.sleep(1)
                st.rerun()
            # Long video option
            if v.duration_sec and v.duration_sec > 60:
                if st.button("✂️ Generate Shorts clips", key=f"{prefix}_chop_{v.id}"):
                    with st.spinner(f"Analyzing {v.duration_sec}s video and chopping into Shorts…"):
                        chop_out = _api("POST", f"/videos/{v.id}/chop")
                    if chop_out:
                        st.json(chop_out)
                        st.rerun()

        elif v.status == VideoStatus.TRANSFORMED:
            # Show metadata preview + edit + publish
            with st.expander("📝 Metadata (editable)", expanded=True):
                md = load_metadata(v.id)
                if md is None:
                    if st.button("Generate metadata with LLM", key=f"{prefix}_gen_{v.id}"):
                        with st.spinner("LLM generating metadata…"):
                            out = _api("POST", f"/metadata/{v.id}/generate")
                        if out and out.get("ok"):
                            st.success(f"Generated: {out.get('title', '')[:60]}")
                        else:
                            st.error(f"Metadata generation failed: {out}")
                        st.rerun()
                else:
                    new_title = st.text_input("Title", value=md["title"], key=f"{prefix}_t_{v.id}")
                    new_desc = st.text_area("Description", value=md["description"],
                                            height=100, key=f"{prefix}_d_{v.id}")
                    new_tags = st.text_input("Tags (comma-separated)",
                                             value=", ".join(md["tags"]),
                                             key=f"{prefix}_tags_{v.id}")
                    col_a, col_b = st.columns(2)
                    if col_a.button("💾 Save metadata", key=f"{prefix}_save_{v.id}"):
                        tags_list = [t.strip() for t in new_tags.split(",") if t.strip()]
                        _api("PUT", f"/metadata/{v.id}", json={
                            "title": new_title, "description": new_desc, "tags": tags_list,
                        })
                        st.success("Saved")
                    if col_b.button("🔄 Regenerate", key=f"{prefix}_regen_{v.id}"):
                        with st.spinner("Regenerating…"):
                            out = _api("POST", f"/metadata/{v.id}/generate")
                        if out and out.get("ok"):
                            st.success("Regenerated")
                        else:
                            st.error(f"Regenerate failed: {out}")
                        st.rerun()
            if st.button("📺 Publish now", key=f"{prefix}_publish_{v.id}"):
                with st.spinner("Publishing to YouTube… (can take 1–2 min)"):
                    out = _api("POST", f"/videos/{v.id}/publish", json={})
                if out:
                    if out.get("queued"):
                        st.success(f"Publish queued! Task ID: {out.get('task_id', 'N/A')}")
                    elif out.get("ok") or out.get("result", {}).get("ok"):
                        st.success("Published!")
                    else:
                        st.error(f"Publish failed: {out}")
                import time as _time_pub
                _time_pub.sleep(1)
                st.rerun()

        elif v.status == VideoStatus.PUBLISHED:
            st.success(f"Published: {v.youtube_video_id}")
            if v.youtube_video_id and not v.youtube_video_id.startswith("MOCK_"):
                st.markdown(f"[Watch on YouTube](https://youtube.com/watch?v={v.youtube_video_id})")

        elif v.status == VideoStatus.FAILED:
            st.error(v.error or "Failed")
            if st.button("Retry transform", key=f"{prefix}_retry_{v.id}"):
                with st.spinner("Retrying transform…"):
                    out = _api("POST", f"/videos/{v.id}/transform", json={})
                if out:
                    if out.get("queued"):
                        st.success(f"Transform queued! Task ID: {out.get('task_id', 'N/A')}")
                    elif out.get("result", {}).get("ok"):
                        st.success("Transform complete!")
                    else:
                        st.error(f"Transform failed: {out}")
                import time as _time_retry
                _time_retry.sleep(1)
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

    tabs = st.tabs(["📥 Pending", "✅ Approved", "📺 Ready to publish", "🚀 Pipeline", "✂️ Clips", "✅ Published", "👥 Creators", "🗂 All"])

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
            render_video_card(v, prefix="pending")

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
            render_video_card(v, prefix="approved")

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
            render_video_card(v, prefix="ready")

    with tabs[3]:
        st.subheader("🚀 Pipeline — ready to upload")
        with session_scope() as db:
            pending_count = db.scalar(
                select(func.count(Video.id)).where(Video.status == VideoStatus.PENDING)
            ) or 0
            approved_count = db.scalar(
                select(func.count(Video.id)).where(Video.status == VideoStatus.APPROVED)
            ) or 0
            transforming_count = db.scalar(
                select(func.count(Video.id)).where(Video.status == VideoStatus.TRANSFORMING)
            ) or 0
            transformed_count = db.scalar(
                select(func.count(Video.id)).where(Video.status == VideoStatus.TRANSFORMED)
            ) or 0
            failed_pipeline = db.scalar(
                select(func.count(Video.id)).where(Video.status == VideoStatus.FAILED)
            ) or 0
        ready = approved_count + transforming_count + transformed_count
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Pending", pending_count)
        c2.metric("Approved", approved_count)
        c3.metric("Transforming", transforming_count)
        c4.metric("Transformed", transformed_count)
        c5.metric("Ready to upload", ready)
        if failed_pipeline:
            st.warning(f"{failed_pipeline} failed — retry or discard")
        # Show all pipeline items (pending through transformed)
        with session_scope() as db:
            rows = db.execute(
                select(Video)
                .where(Video.status.in_([
                    VideoStatus.PENDING,
                    VideoStatus.APPROVED,
                    VideoStatus.TRANSFORMING,
                    VideoStatus.TRANSFORMED,
                    VideoStatus.FAILED,
                ]))
                .order_by(desc(Video.velocity_score), desc(Video.engagement_score))
                .limit(100)
            ).scalars().all()
        if not rows:
            st.info("Pipeline is empty. Scrape some videos first!")
        for v in rows:
            render_video_card(v, prefix="pipeline")

    with tabs[4]:
        st.subheader("✂️ Auto-generated Shorts clips from long videos")
        st.caption("These were chopped automatically from long-form videos using hot-section heuristics.")
        with session_scope() as db:
            rows = db.execute(
                select(Video)
                .where(
                    Video.meta.isnot(None),
                    Video.meta != "",
                )
                .order_by(desc(Video.scraped_at))
                .limit(100)
            ).scalars().all()
        clips = []
        for v in rows:
            try:
                meta = json.loads(v.meta or "{}")
                if "parent_video_id" in meta:
                    clips.append((v, meta))
            except Exception:
                pass
        if not clips:
            st.info("No clips yet. Approve a long video and click ✂️ Generate Shorts clips to create them.")
        for v, meta in clips:
            st.markdown(f"**{v.title}**  ·  {v.duration_sec}s  ·  score: {meta.get('clip_score', 0):.2f}")
            st.caption(
                f"Parent: {meta.get('parent_video_id', '')[:12]}… · "
                f"segment: {meta.get('clip_start', 0):.0f}s → {meta.get('clip_end', 0):.0f}s  ·  "
                f"{meta.get('clip_reason', '')}"
            )
            # Reuse render_video_card so clips get Approve/Transform/Publish buttons too
            render_video_card(v, prefix="clips_item_" + v.id[:8])
            st.divider()

    with tabs[5]:
        st.subheader("Published to YouTube")
        with session_scope() as db:
            rows = db.execute(
                select(Video).where(Video.status == VideoStatus.PUBLISHED)
                .order_by(desc(Video.published_at)).limit(50)
            ).scalars().all()
        if not rows:
            st.info("Nothing published yet.")
        for v in rows:
            render_video_card(v, prefix="published")

    with tabs[6]:
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
            st.dataframe(data, width="stretch")
        else:
            st.info("No creators yet. Add seeds to TARGET_HANDLES in .env, or run discovery.")

    with tabs[7]:
        st.subheader("All videos")
        with session_scope() as db:
            rows = db.execute(
                select(Video).order_by(desc(Video.scraped_at)).limit(100)
            ).scalars().all()
        for v in rows:
            render_video_card(v, prefix="all")


if __name__ == "__main__":
    main()
