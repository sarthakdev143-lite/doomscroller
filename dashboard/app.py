"""Doomscroller — Operative Ultra Dashboard (fast, keyboard, bulk, live)"""
from __future__ import annotations
import os, sys, time, json
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
for _p in list(sys.path):
    if Path(_p).resolve() == (PROJECT_ROOT / "dashboard").resolve():
        sys.path.remove(_p)
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
for _mod in ("app","app.config","app.db","app.models"):
    sys.modules.pop(_mod, None)
try: os.chdir(PROJECT_ROOT)
except: pass

import streamlit as st
import httpx
from app.config import settings
from app.db import init_db, session_scope
from app.logging_config import configure_logging
from app.models import Video, VideoStatus, Creator
from sqlalchemy import desc, func, select, or_
from datetime import datetime, timezone

API_BASE = os.environ.get("DOOMSCROLLER_API_URL", "http://localhost:8000")
st.set_page_config(page_title="Doomscroller • Operative", page_icon="⚡", layout="wide", initial_sidebar_state="expanded")

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600&family=Inter:wght@400;600;700;800&display=swap');
html, body, [class*="st-"] { font-family: Inter, sans-serif; }
.stApp { background: #0a0e1a; background-image: radial-gradient(at 20% 0%, rgba(99,102,241,0.15) 0, transparent 50%), radial-gradient(at 90% 0%, rgba(139,92,246,0.12) 0, transparent 50%); }
h1 { font-weight:800; letter-spacing:-0.04em; font-size:32px; }
h2 { font-weight:700; font-size:20px; letter-spacing:-0.02em; }
h3 { font-weight:700; font-size:16px; }
[data-testid="stMetric"] { background: rgba(17,24,39,0.8); backdrop-filter: blur(16px); border:1px solid rgba(55,65,81,0.5); border-radius:14px; padding:14px 16px; box-shadow: 0 2px 12px rgba(0,0,0,0.25); transition: all 0.15s ease; }
[data-testid="stMetric"]:hover { border-color: rgba(99,102,241,0.5); transform: translateY(-1px); }
[data-testid="stMetricLabel"] { color:#9ca3af; font-size:10px; font-weight:700; letter-spacing:0.1em; text-transform:uppercase; }
[data-testid="stMetricValue"] { font-weight:800; font-size:20px; font-variant-numeric: tabular-nums; }
[data-testid="stSidebar"] { background: rgba(11,16,30,0.92); backdrop-filter: blur(24px); border-right:1px solid rgba(55,65,81,0.4); }
.stButton>button { border-radius:10px; font-weight:600; font-size:12.5px; padding:7px 14px; border:1px solid rgba(55,65,81,0.6); background: rgba(31,41,55,0.8); color:#e5e7eb; transition: all 0.12s ease; }
.stButton>button:hover { background: #6366f1; color:white; border-color:#6366f1; transform: translateY(-1px); box-shadow: 0 4px 16px rgba(99,102,241,0.4); }
.stButton>button[kind="primary"] { background: linear-gradient(135deg, #6366f1, #8b5cf6); border:none; color:white; }
.stButton>button[kind="primary"]:hover { background: linear-gradient(135deg, #4f46e5, #7c3aed); transform: translateY(-1px); }
hr { border-color: rgba(55,65,81,0.3); margin:12px 0; }
.video-card { background: rgba(17,24,39,0.6); backdrop-filter: blur(12px); border:1px solid rgba(55,65,81,0.4); border-radius:14px; padding:14px; margin:6px 0; transition: all 0.15s ease; }
.video-card:hover { background: rgba(31,41,55,0.85); border-color: rgba(99,102,241,0.35); box-shadow: 0 8px 24px rgba(0,0,0,0.35); }
.badge { display:inline-flex; align-items:center; gap:6px; padding:3px 10px; border-radius:20px; font-size:10.5px; font-weight:700; letter-spacing:0.06em; text-transform:uppercase; font-family: JetBrains Mono, monospace; }
.badge-pending { background: rgba(251,191,36,0.12); color:#fbbf24; border:1px solid rgba(251,191,36,0.3); }
.badge-approved { background: rgba(34,197,94,0.12); color:#22c55e; border:1px solid rgba(34,197,94,0.3); }
.badge-ready { background: rgba(168,85,247,0.12); color:#a855f7; border:1px solid rgba(168,85,247,0.3); }
.badge-published { background: rgba(6,182,212,0.12); color:#06b6d4; border:1px solid rgba(6,182,212,0.3); }
.kbd { display:inline-block; padding:2px 6px; background: rgba(55,65,81,0.6); border:1px solid rgba(75,85,99,0.5); border-bottom-width:2px; border-radius:6px; font-family: JetBrains Mono, monospace; font-size:10px; font-weight:600; color:#9ca3af; }
.pulse { width:7px; height:7px; background:#22c55e; border-radius:50%; display:inline-block; animation: pulse 1.8s infinite; }
@keyframes pulse { 0%,100% { opacity:1; box-shadow: 0 0 0 0 rgba(34,197,94,0.4); } 50% { opacity:0.8; box-shadow: 0 0 0 6px rgba(34,197,94,0); } }
::-webkit-scrollbar { width:5px; height:5px; } ::-webkit-scrollbar-thumb { background:#374151; border-radius:3px; }
[data-testid="stExpander"] { background: rgba(17,24,39,0.4); border:1px solid rgba(55,65,81,0.3); border-radius:10px; }
.stTextInput input, .stSelectbox div[data-baseweb="select"] { background: rgba(17,24,39,0.7) !important; border:1px solid rgba(55,65,81,0.4) !important; border-radius:10px !important; }
</style>
<script>
document.addEventListener('keydown', (e)=>{
  if(e.target.tagName==='INPUT' || e.target.tagName==='TEXTAREA') return;
  if(e.key==='k' && (e.ctrlKey||e.metaKey)) { e.preventDefault(); document.querySelector('input[placeholder*="Search"]')?.focus(); }
  if(e.key==='a' && !e.ctrlKey) { const b=document.querySelector('button[kind="primary"]'); }
});
</script>
""", unsafe_allow_html=True)

@st.cache_resource
def _ensure_db():
    configure_logging()
    init_db()
    return True
_ensure_db()

@st.cache_data(ttl=4, show_spinner=False)
def _cached_stats():
    with session_scope() as db:
        total=db.scalar(select(func.count(Video.id))) or 0
        pending=db.scalar(select(func.count(Video.id)).where(Video.status==VideoStatus.PENDING)) or 0
        approved=db.scalar(select(func.count(Video.id)).where(Video.status==VideoStatus.APPROVED)) or 0
        transformed=db.scalar(select(func.count(Video.id)).where(Video.status==VideoStatus.TRANSFORMED)) or 0
        published=db.scalar(select(func.count(Video.id)).where(Video.status==VideoStatus.PUBLISHED)) or 0
        creators=db.scalar(select(func.count(Creator.id))) or 0
        reach=db.scalar(select(func.sum(Video.views)).where(Video.status.in_([VideoStatus.PENDING,VideoStatus.APPROVED,VideoStatus.TRANSFORMED]))) or 0
        return {"total":total,"pending":pending,"approved":approved,"transformed":transformed,"published":published,"creators":creators,"reach":int(reach)}

def _api(method, path, **kw):
    timeout=4 if path in ("/scheduler/jobs","/stats","/health") else 40 if "scrape" in path else 10
    try:
        with httpx.Client(timeout=timeout) as c:
            r=c.request(method, f"{API_BASE}{path}", **kw)
            if r.status_code>=400:
                st.toast(f"{path} → {r.status_code}", icon="⚠️")
                return None
            return r.json()
    except: return None

def _badge(s):
    m={"pending":'<span class="badge badge-pending">● PENDING</span>',"approved":'<span class="badge badge-approved">● APPROVED</span>',"rejected":'<span class="badge" style="background:rgba(239,68,68,0.12);color:#ef4444;border:1px solid rgba(239,68,68,0.3)">● REJECTED</span>',"transformed":'<span class="badge badge-ready">● READY</span>',"published":'<span class="badge badge-published">● PUBLISHED</span>',"failed":'<span class="badge" style="background:rgba(239,68,68,0.12);color:#ef4444">● FAILED</span>'}
    return m.get(s, f'<span class="badge">{s}</span>')

# Sidebar fragment — doesn't rerun main
@st.fragment
def sidebar_frag():
    with st.sidebar:
        st.markdown("## ⚡ Doomscroller")
        st.caption(f"**{settings.uploads_per_day}/day** • {settings.upload_interval_hours}h ±{settings.upload_jitter_minutes}m • `{'auto' if settings.auto_approve else 'manual'}`")
        jobs=_api("GET","/scheduler/jobs") or {}
        if jobs.get("running"):
            with st.expander(f"⏰ Scheduler • {len(jobs.get('jobs',[]))} jobs", expanded=False):
                for j in jobs.get("jobs",[]):
                    nxt=j.get("next_run_time","")[:19].replace("T"," ") if j.get("next_run_time") else "pending"
                    st.caption(f"`{j['id']}` → {nxt}")
        st.divider()
        st.markdown("**⚡ Operate**")
        c1,c2=st.columns(2)
        if c1.button("🧹 Scrape", width="stretch", help="Fill to 20 pending"):
            with st.spinner("Scraping…"):
                out=_api("POST","/scrape", json={})
                if out: st.toast(f"+{out.get('result',{}).get('created',0)} videos", icon="✅")
                _cached_stats.clear()
        if c2.button("♻️ Recompute", width="stretch"):
            _api("POST","/scores/recompute"); st.toast("Scores updated", icon="✅"); _cached_stats.clear()
        if st.button("🤖 Auto-approve", width="stretch"):
            out=_api("POST","/scheduler/run/auto_approve"); st.toast(f"Approved {out.get('result',{}).get('approved',0)}" if out else "Done", icon="✅"); _cached_stats.clear()
        st.divider()
        st.markdown("**📤 Pacing**")
        uploads=st.slider("Shorts / day",1,12,int(settings.uploads_per_day),1)
        jitter=st.slider("Jitter ± min",0,90,int(settings.upload_jitter_minutes),5)
        auto_approve=st.checkbox("Auto-approve", value=bool(settings.auto_approve))
        if st.button("💾 Save pacing", type="primary", width="stretch"):
            out=_api("POST","/config/publish", json={"uploads_per_day":uploads,"upload_jitter_minutes":jitter,"auto_approve":auto_approve})
            if out and out.get("ok"):
                st.toast(f"Saved {uploads}/day", icon="✅"); time.sleep(0.4); st.rerun()
        st.divider()
        st.markdown("**🎯 Channel focus**")
        focus=_api("GET","/queue/focus") or {}
        langs=focus.get("target_languages") or []
        st.caption(f"Languages: `{', '.join(langs) if langs else 'any'}`")
        by_lang=focus.get("pending_by_language") or {}
        if by_lang:
            off=[f"{k} {v}" for k,v in sorted(by_lang.items(), key=lambda x:-x[1])
                 if langs and k not in langs]
            if off:
                st.caption(f"Off-focus pending: {', '.join(off)}")
        c1,c2=st.columns(2)
        if c1.button("🔍 Preview", width="stretch", help="Dry run: what the current filters would reject"):
            out=_api("POST","/queue/apply-filters?dry_run=true")
            if out:
                st.session_state["focus_preview"]=out
        if c2.button("🧽 Apply", width="stretch", help="Reject pending videos that fail the current filters"):
            out=_api("POST","/queue/apply-filters")
            if out:
                st.toast(f"Rejected {out.get('rejected',0)}, kept {out.get('kept',0)}", icon="🧽")
                _cached_stats.clear(); time.sleep(0.4); st.rerun()
        prev=st.session_state.get("focus_preview")
        if prev:
            st.caption(f"Would keep **{prev.get('kept',0)}**, reject **{prev.get('rejected',0)}** — {prev.get('reasons',{})}")
        st.divider()
        st.markdown("**🩺 Health**")
        hist=_api("GET","/scheduler/history?limit=1") or {}
        health=hist.get("health") or {}
        if health:
            failing=[j for j,h in health.items() if not h.get("ok", True)]
            with st.expander(f"Jobs • {len(health)} tracked" + (f" • {len(failing)} failing" if failing else ""), expanded=bool(failing)):
                for job,h in sorted(health.items()):
                    icon="✅" if h.get("ok", True) else "❌"
                    when=(h.get("timestamp") or "")[11:19]
                    st.caption(f"{icon} `{job}` {when} • {h.get('runs',0)} runs, {h.get('failures',0)} failed")
        else:
            st.caption("No job history yet.")
        over=(_api("GET","/scheduler/overdue") or {}).get("overdue") or []
        if over: st.warning(f"Overdue after downtime: {', '.join(over)}")
        store=_api("GET","/storage") or {}
        if store:
            st.caption(f"💾 {store.get('total_mb',0)} MB • {store.get('files',0)} files"
                       + (f" • {store.get('orphan_mb',0)} MB orphaned" if store.get('orphan_files') else ""))
            if st.button("🧹 Reclaim disk", width="stretch"):
                out=_api("POST","/storage/cleanup")
                res=(out or {}).get("result",{})
                st.toast(f"Reclaimed {res.get('mb_reclaimed',0)} MB", icon="💾"); time.sleep(0.4); st.rerun()
        st.divider()
        st.caption("Shortcuts: `Ctrl+K` search • `A` approve • `R` reject • `J/K` next/prev")

# Video card fragment — isolated, no full rerun
@st.fragment
def video_card_frag(v: Video, prefix: str):
    status=v.status.value if hasattr(v.status,"value") else str(v.status)
    badge=_status_badge=status
    # use html card
    st.markdown('<div class="video-card">', unsafe_allow_html=True)
    c1,c2,c3 = st.columns([6,3,5], gap="small")
    with c1:
        st.markdown(f"**{v.title or '(no title)'}**")
        dur=f"{v.duration_sec}s" if v.duration_sec else "?"
        who=v.author_name or v.author or "unknown"
        handle=f"@{v.author}" if v.author else ""
        res=f"{v.width}x{v.height}" if v.width and v.height else "?"
        lang=f" • {v.language}" if v.language else ""
        st.caption(f"**{who}** {handle} • {v.source} • {dur} • {res}{lang} • `{v.id[:8]}`")
        st.markdown(_badge(status), unsafe_allow_html=True)
        if v.keywords:
            kws=v.keywords if isinstance(v.keywords,list) else []
            if kws: st.caption("🏷 " + " · ".join(str(k) for k in kws[:5]))
        if v.thumbnail_url:
            st.image(v.thumbnail_url, width="stretch")
        elif v.local_path and os.path.exists(v.local_path):
            with st.expander("▶ Preview", expanded=False):
                st.video(v.local_path)
        if v.description:
            with st.expander("Description"):
                st.write(v.description[:380])
    with c2:
        m1,m2=st.columns(2)
        m1.metric("Views", f"{v.views:,}")
        m2.metric("Likes", f"{v.likes:,}")
        m1.metric("Viral", f"{(v.viral_score or 0):.3f}")
        m2.metric("Eng", f"{(v.engagement_score or 0):.3f}")
        if v.meta and isinstance(v.meta,dict) and v.meta.get("auto_approved"):
            st.caption("🤖 auto")
    with c3:
        sel_key=f"sel_{prefix}_{v.id}"
        is_sel=st.checkbox("Select", key=sel_key, value=v.id in st.session_state.get("selected",set()))
        sel=st.session_state.get("selected",set())
        if is_sel: sel.add(v.id)
        else: sel.discard(v.id)
        st.session_state["selected"]=sel
        if v.status==VideoStatus.PENDING:
            a,b=st.columns(2)
            if a.button("✅ Approve", key=f"{prefix}_a_{v.id}", type="primary", width="stretch"):
                with session_scope() as db:
                    r=db.get(Video,v.id); r.status=VideoStatus.APPROVED; r.curated_at=datetime.now(timezone.utc)
                st.toast("Approved", icon="✅"); _cached_stats.clear(); time.sleep(0.15); st.rerun()
            if b.button("✕ Reject", key=f"{prefix}_r_{v.id}", width="stretch"):
                with session_scope() as db:
                    r=db.get(Video,v.id); r.status=VideoStatus.REJECTED; r.curated_at=datetime.now(timezone.utc)
                st.toast("Rejected", icon="🗑️"); _cached_stats.clear(); time.sleep(0.15); st.rerun()
        elif v.status==VideoStatus.APPROVED:
            if st.button("🎨 Transform", key=f"{prefix}_t_{v.id}", type="primary", width="stretch"):
                with st.spinner("Transforming…"):
                    out=_api("POST",f"/videos/{v.id}/transform", json={})
                st.toast("Done" if out and out.get("result",{}).get("ok") else "Queued", icon="✅"); _cached_stats.clear(); time.sleep(0.6); st.rerun()
        elif v.status==VideoStatus.TRANSFORMED:
            st.success("✅ Ready 9:16", icon="✅")
            _var=(v.meta or {}).get("variation") if isinstance(v.meta,dict) else None
            if _var: st.caption(f"🎛 {_var}")
            try:
                from app.models import MetadataDraft as _MD
                with session_scope() as _db:
                    _d=_db.execute(select(_MD).where(_MD.video_id==v.id)).scalar_one_or_none()
                    if _d:
                        with st.expander("📝 YouTube metadata", expanded=False):
                            st.text_input("Title", value=_d.title, key=f"{prefix}_mdt_{v.id}", disabled=True)
                            st.caption(f"**Tags:** {', '.join(_d.tags or [])}")
                            st.caption(f"**Language:** {_d.detected_language}")
                            st.text_area("Description", value=_d.description, height=110, key=f"{prefix}_mdd_{v.id}", disabled=True)
            except Exception: pass
            if v.transformed_path and os.path.exists(v.transformed_path):
                with st.expander("▶ Ready", expanded=False):
                    st.video(v.transformed_path)
                try:
                    with open(v.transformed_path,"rb") as fh:
                        st.download_button("⬇️ Download", fh.read(), file_name=f"{v.id}.mp4", mime="video/mp4", key=f"{prefix}_dl_{v.id}", width="stretch", type="primary")
                except: pass
                if settings.enable_publish and st.button("📤 Publish", key=f"{prefix}_p_{v.id}", width="stretch"):
                    with st.spinner("Publishing…"):
                        out=_api("POST",f"/videos/{v.id}/publish", json={})
                    st.toast("Published!" if out and (out.get("ok") or out.get("result",{}).get("ok")) else "Failed", icon="🚀" if out and out.get("ok") else "❌"); _cached_stats.clear(); time.sleep(0.6); st.rerun()
        elif v.status==VideoStatus.PUBLISHED:
            st.success(f"🚀 {v.youtube_video_id}", icon="✅")
            if v.youtube_video_id and not v.youtube_video_id.startswith("MOCK_"):
                st.link_button("▶ YouTube", f"https://youtube.com/watch?v={v.youtube_video_id}", width="stretch")
        elif v.status==VideoStatus.FAILED:
            st.error(v.error or "Failed", icon="❌")
            if st.button("↻ Retry", key=f"{prefix}_rt_{v.id}", width="stretch"):
                _api("POST",f"/videos/{v.id}/transform", json={}); st.toast("Retrying", icon="🔄"); time.sleep(0.6); st.rerun()
    st.markdown('</div>', unsafe_allow_html=True)

@st.fragment(run_every=8)
def stats_frag():
    s=_cached_stats()
    c1,c2,c3,c4,c5,c6=st.columns(6)
    c1.metric("Total", s["total"])
    c2.metric("Pending", s["pending"], delta=f"{s['pending']}/20" if s["pending"] else None, delta_color="off")
    c3.metric("Approved", s["approved"])
    c4.metric("Ready", s["transformed"])
    c5.metric("Published", s["published"], delta="live" if s["published"] else None)
    reach=s.get("reach",0)
    c6.metric("Queue reach", f"{reach/1_000_000:.1f}M" if reach>=1_000_000 else f"{reach:,}",
              help="Combined source views of everything waiting in the queue")
    # progress to 20
    if s["pending"]<20:
        st.progress(s["pending"]/20, text=f"{s['pending']}/20 pending — scraper fills to 20")
    return s

def main():
    sidebar_frag()
    st.markdown('<div style="display:flex; align-items:center; gap:10px; margin:6px 0;"><span class="pulse"></span><span style="color:#9ca3af; font-size:11px; font-weight:700; letter-spacing:0.1em; text-transform:uppercase;">Operative • Live • 8/day ±45m</span><span style="margin-left:auto; color:#6b7280; font-size:11px;">Ctrl+K to search</span></div>', unsafe_allow_html=True)
    st.title("Doomscroller")
    st.caption(f"Snapchat → **{'Auto' if settings.auto_approve else 'Manual'}** → 1080x1920 • **{settings.uploads_per_day}/day** every {settings.upload_interval_hours}h")
    if "video_search" not in st.session_state: st.session_state["video_search"]=""
    if "selected" not in st.session_state: st.session_state["selected"]=set()
    top1,top2,top3=st.columns([3,1,1])
    with top1:
        search=st.text_input("Search", value=st.session_state["video_search"], placeholder="🔍 Search title, author, id — Ctrl+K", label_visibility="collapsed")
        if search!=st.session_state["video_search"]:
            st.session_state["video_search"]=search
            st.session_state["pending_page"]=0
    with top2:
        sort_by=st.selectbox("Sort", ["🔥 Viral","👁 Reach","❤️ Engagement","⚡ Velocity","🕒 Recent"], index=0, label_visibility="collapsed")
    with top3:
        density=st.segmented_control("View", ["Comfort","Compact"], default="Comfort", label_visibility="collapsed")
    sort_map={"🔥 Viral":(Video.viral_score,True),"👁 Reach":(Video.views,True),"❤️ Engagement":(Video.engagement_score,True),"⚡ Velocity":(Video.velocity_score,True),"🕒 Recent":(Video.scraped_at,True)}
    sort_col, sort_desc = sort_map[sort_by]

    s=stats_frag()
    # bulk bar
    sel_count=len(st.session_state.get("selected",set()))
    if sel_count>0:
        with st.container(border=True):
            b1,b2,b3,b4=st.columns([1,1,1,2])
            b1.metric("Selected", sel_count)
            if b2.button(f"✅ Approve {sel_count}", type="primary", width="stretch"):
                with session_scope() as db:
                    for vid in list(st.session_state["selected"]):
                        r=db.get(Video,vid)
                        if r and r.status==VideoStatus.PENDING:
                            r.status=VideoStatus.APPROVED; r.curated_at=datetime.now(timezone.utc)
                st.toast(f"Approved {sel_count}", icon="✅"); st.session_state["selected"]=set(); _cached_stats.clear(); time.sleep(0.3); st.rerun()
            if b3.button(f"✕ Reject {sel_count}", width="stretch"):
                with session_scope() as db:
                    for vid in list(st.session_state["selected"]):
                        r=db.get(Video,vid)
                        if r and r.status==VideoStatus.PENDING:
                            r.status=VideoStatus.REJECTED; r.curated_at=datetime.now(timezone.utc)
                st.toast(f"Rejected {sel_count}", icon="🗑️"); st.session_state["selected"]=set(); _cached_stats.clear(); time.sleep(0.3); st.rerun()
            if b4.button("Clear selection", width="stretch"):
                st.session_state["selected"]=set(); st.rerun()

    if st.button("🔄 Refresh", help="Clear cache"):
        _cached_stats.clear(); st.rerun()

    tabs=st.tabs(["📥 Pending","✅ Approved","⬇️ Ready","🚀 Pipeline","✅ Published","👥 Creators","🗂 All"])
    with tabs[0]:
        page=st.session_state.get("pending_page",0)
        per_page=6 if density=="Compact" else 8
        q=st.session_state["video_search"].strip().lower()
        with session_scope() as db:
            base=select(Video).where(Video.status==VideoStatus.PENDING)
            if q:
                base=base.where(or_(func.lower(Video.title).like(f"%{q}%"), func.lower(Video.author).like(f"%{q}%"), func.lower(Video.author_name).like(f"%{q}%"), func.lower(Video.source_id).like(f"%{q}%")))
            total=db.scalar(select(func.count()).select_from(base.subquery())) or 0
            order=desc(sort_col) if sort_desc else sort_col
            rows=db.execute(base.order_by(order).offset(page*per_page).limit(per_page)).scalars().all()
        if total==0:
            st.info("No pending. Scraper auto-fills to 20 every 6h — or hit Scrape.")
            if st.button("🧹 Scrape now (fill to 20)", type="primary"):
                with st.spinner("Scraping 17s parallel..."):
                    out=_api("POST","/scrape", json={})
                    if out: st.toast(f"+{out.get('result',{}).get('created',0)}", icon="✅")
                _cached_stats.clear(); time.sleep(0.5); st.rerun()
        else:
            st.caption(f"**{total}** pending • Page {page+1}/{(total+per_page-1)//per_page} • {per_page}/page • Sorted {sort_by} • <span class='kbd'>J/K</span> navigate • <span class='kbd'>A</span> approve • <span class='kbd'>R</span> reject", unsafe_allow_html=True)
            p1,p2,p3=st.columns([1,1,2])
            if p1.button("◀ Prev", disabled=page==0, key="pprev", width="stretch"):
                st.session_state["pending_page"]=max(0,page-1); st.rerun()
            if p2.button("Next ▶", disabled=(page+1)*per_page>=total, key="pnext", width="stretch"):
                st.session_state["pending_page"]=page+1; st.rerun()
            if p3.button(f"✅ Approve all {len(rows)} on page", width="stretch"):
                with session_scope() as db:
                    for v in rows:
                        r=db.get(Video,v.id); r.status=VideoStatus.APPROVED; r.curated_at=datetime.now(timezone.utc); r.meta={**(r.meta or {}),"bulk":True}
                st.toast(f"Approved {len(rows)}", icon="✅"); _cached_stats.clear(); time.sleep(0.3); st.rerun()
            for v in rows:
                video_card_frag(v, "pending")
    with tabs[1]:
        with session_scope() as db:
            rows=db.execute(select(Video).where(Video.status==VideoStatus.APPROVED).order_by(desc(Video.curated_at)).limit(12)).scalars().all()
        if not rows: st.info("Nothing approved.")
        else:
            for v in rows: video_card_frag(v, "approved")
    with tabs[2]:
        # Reviewing the ready queue means picking the best of it, so rank by
        # viral_score by default -- newest-first surfaces whatever was scraped
        # last, which is the weak tail of a run, not the pick of it.
        with session_scope() as db:
            total=db.scalar(select(func.count(Video.id)).where(Video.status==VideoStatus.TRANSFORMED)) or 0
        c1,c2=st.columns([2,1])
        with c1:
            order=st.selectbox("Sort",["Best score","Most views","Newest"],key="ready_sort",label_visibility="collapsed")
        with c2:
            show=st.selectbox("Show",[12,25,50,100,"All"],key="ready_show",label_visibility="collapsed")
        st.caption(f"{total} ready to upload")
        order_col={"Best score":desc(Video.viral_score),"Most views":desc(Video.views),"Newest":desc(Video.transformed_at)}[order]
        with session_scope() as db:
            q=select(Video).where(Video.status==VideoStatus.TRANSFORMED).order_by(order_col)
            if show!="All": q=q.limit(int(show))
            rows=db.execute(q).scalars().all()
        if not rows: st.info("No ready. Transform hourly.")
        else:
            for v in rows: video_card_frag(v, "ready")
    with tabs[3]:
        with session_scope() as db:
            pc=db.scalar(select(func.count(Video.id)).where(Video.status==VideoStatus.PENDING)) or 0
            ac=db.scalar(select(func.count(Video.id)).where(Video.status==VideoStatus.APPROVED)) or 0
            tc=db.scalar(select(func.count(Video.id)).where(Video.status==VideoStatus.TRANSFORMING)) or 0
            tr=db.scalar(select(func.count(Video.id)).where(Video.status==VideoStatus.TRANSFORMED)) or 0
            fc=db.scalar(select(func.count(Video.id)).where(Video.status==VideoStatus.FAILED)) or 0
        c1,c2,c3,c4,c5=st.columns(5)
        c1.metric("Pending",pc); c2.metric("Approved",ac); c3.metric("Transforming",tc); c4.metric("Ready",tr); c5.metric("Total",pc+ac+tc+tr+fc)
        if fc: st.warning(f"{fc} failed")
        with session_scope() as db:
            rows=db.execute(select(Video).where(Video.status.in_([VideoStatus.PENDING,VideoStatus.APPROVED,VideoStatus.TRANSFORMING,VideoStatus.TRANSFORMED,VideoStatus.FAILED])).order_by(desc(Video.viral_score)).limit(16)).scalars().all()
        for v in rows: video_card_frag(v, "pipeline")
    with tabs[4]:
        with session_scope() as db:
            rows=db.execute(select(Video).where(Video.status==VideoStatus.PUBLISHED).order_by(desc(Video.published_at)).limit(12)).scalars().all()
        if not rows: st.info("Nothing published yet.")
        else:
            for v in rows: video_card_frag(v, "published")
    with tabs[5]:
        with session_scope() as db:
            creators=db.execute(select(Creator).order_by(desc(Creator.score)).limit(30)).scalars().all()
        if creators:
            st.dataframe([{"handle":c.handle,"platform":c.platform,"seed":"✅" if c.is_seed else "🔍","score":round(c.score,3),"videos":c.videos_scraped,"from":c.discovered_from or "-","last":c.last_scraped_at.strftime("%Y-%m-%d %H:%M") if c.last_scraped_at else "-"} for c in creators], width="stretch", height=360)
        else: st.info("No creators.")
    with tabs[6]:
        q=st.session_state["video_search"].strip().lower()
        with session_scope() as db:
            base=select(Video)
            if q: base=base.where(or_(func.lower(Video.title).like(f"%{q}%"), func.lower(Video.author).like(f"%{q}%")))
            rows=db.execute(base.order_by(desc(Video.scraped_at)).limit(16)).scalars().all()
        for v in rows: video_card_frag(v, "all")

if __name__=="__main__":
    main()
