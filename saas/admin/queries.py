"""Analytics queries for the admin panel.

Portable SQL only (SQLite and Postgres): grouping by day happens in Python over the narrow columns
it needs, which is plenty fast at the volumes a launching product sees and avoids per-dialect date
functions. Bots are excluded from every audience number unless a page asks for them.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from sqlalchemy import and_, distinct, func, or_, select
from sqlalchemy.orm import Session

from ..common import utcnow
from ..models.security import Ban, SecurityEvent, SeenSignal
from ..models.tracking import PageView, TrackEvent, TrackSession, Visitor, VisitorLink
from ..models.waitlist import WaitlistEntry
from . import charts, core_adapter

RANGES = {"1d": 1, "7d": 7, "30d": 30, "90d": 90}
LIVE_WINDOW = timedelta(minutes=3)
_PRICING = re.compile(r"pric|plan|tier|credit", re.I)
_FORMSTART = re.compile(r"wait|join|signup|sign-up|register|email", re.I)


def window(range_key: str | None, default: str = "30d") -> tuple[str, datetime]:
    key = range_key if range_key in RANGES else default
    return key, utcnow() - timedelta(days=RANGES[key])


def _human_bots(q, model=TrackSession):
    return q.where(model.is_bot.is_(False))


# ---- overview ----------------------------------------------------------------------------------
def live_count(db: Session) -> int:
    return int(db.execute(_human_bots(select(func.count(distinct(TrackSession.visitor_id)))
                                      .where(TrackSession.last_seen_at >= utcnow() - LIVE_WINDOW))).scalar_one())


def audience(db: Session, since: datetime) -> dict:
    rows = db.execute(_human_bots(select(TrackSession.visitor_id, TrackSession.pageviews, TrackSession.active_ms)
                                  .where(TrackSession.started_at >= since))).all()
    sessions = len(rows)
    bounces = sum(1 for _, pv, ms in rows if (pv or 0) <= 1 and (ms or 0) < 10_000)
    return {"visitors": len({r[0] for r in rows}), "sessions": sessions,
            "bounce": round(100 * bounces / sessions, 1) if sessions else None,
            "avg_active_s": round(sum((r[2] or 0) for r in rows) / sessions / 1000, 1) if sessions else None,
            "pages_per_session": round(sum((r[1] or 0) for r in rows) / sessions, 2) if sessions else None}


def waitlist_counts(db: Session, since: datetime | None = None) -> dict:
    def c(*conds):
        return int(db.execute(select(func.count(WaitlistEntry.id)).where(*conds)).scalar_one())

    out = {"total": c(WaitlistEntry.status != "removed"), "waiting": c(WaitlistEntry.status == "confirmed"),
           "invited": c(WaitlistEntry.status == "invited"), "joined": c(WaitlistEntry.status == "joined"),
           "pending": c(WaitlistEntry.status == "pending"), "flagged": c(WaitlistEntry.flagged.is_(True))}
    if since is not None:
        out["joins"] = c(WaitlistEntry.created_at >= since)
        out["confirms"] = c(WaitlistEntry.confirmed_at.is_not(None), WaitlistEntry.confirmed_at >= since)
        out["confirm_rate"] = round(100 * out["confirms"] / out["joins"], 1) if out["joins"] else None
    return out


def daily_series(db: Session, since: datetime) -> dict:
    now = utcnow()
    day_list = charts.days(since, now)
    sess = db.execute(_human_bots(select(TrackSession.started_at, TrackSession.visitor_id)
                                  .where(TrackSession.started_at >= since))).all()
    by_day_visitors: dict = defaultdict(set)
    for ts, vid in sess:
        by_day_visitors[ts.date()].add(vid)
    joins = db.execute(select(WaitlistEntry.created_at).where(WaitlistEntry.created_at >= since)).scalars().all()
    confirms = db.execute(select(WaitlistEntry.confirmed_at).where(WaitlistEntry.confirmed_at >= since)).scalars().all()
    labels = [d.strftime("%b %d") for d in day_list]
    visitors = [len(by_day_visitors.get(d, ())) for d in day_list]
    sessions = charts.bucket_daily([s[0] for s in sess], day_list)
    return {"labels": labels, "visitors": visitors, "sessions": sessions,
            "joins": charts.bucket_daily(list(joins), day_list),
            "confirms": charts.bucket_daily(list(confirms), day_list)}


def top_pages(db: Session, since: datetime, limit: int = 8) -> list:
    return db.execute(select(PageView.path, func.count(PageView.id), func.count(distinct(PageView.visitor_id)))
                      .join(Visitor, Visitor.id == PageView.visitor_id)
                      .where(PageView.started_at >= since, Visitor.is_bot.is_(False))
                      .group_by(PageView.path).order_by(func.count(PageView.id).desc()).limit(limit)).all()


def breakdown(db: Session, since: datetime, column, limit: int = 8) -> list:
    return db.execute(select(column, func.count(Visitor.id)).where(Visitor.last_seen_at >= since,
                                                                  Visitor.is_bot.is_(False))
                      .group_by(column).order_by(func.count(Visitor.id).desc()).limit(limit)).all()


def top_sources(db: Session, since: datetime, limit: int = 8) -> list:
    return db.execute(_human_bots(select(TrackSession.source, func.count(TrackSession.id),
                                         func.count(distinct(TrackSession.visitor_id)))
                                  .where(TrackSession.started_at >= since))
                      .group_by(TrackSession.source).order_by(func.count(TrackSession.id).desc()).limit(limit)).all()


def top_leads(db: Session, since: datetime, limit: int = 10) -> list[Visitor]:
    return db.execute(select(Visitor).where(Visitor.last_seen_at >= since, Visitor.is_bot.is_(False),
                                            Visitor.lead_score > 0)
                      .order_by(Visitor.lead_score.desc(), Visitor.last_seen_at.desc()).limit(limit)).scalars().all()


def overview(db: Session, range_key: str) -> dict:
    key, since = window(range_key)
    now = utcnow()
    windows = {label: audience(db, now - timedelta(days=d)) for label, d in (("24h", 1), ("7d", 7), ("30d", 30))}
    series = daily_series(db, now - timedelta(days=29))
    chart = charts.series_chart([{"name": "Visitors", "values": series["visitors"], "kind": "area"},
                                 {"name": "Waitlist joins", "values": series["joins"], "kind": "line"}],
                                series["labels"])
    jobs = core_adapter.jobs_summary(db, since)
    return {
        "range": key, "live": live_count(db), "windows": windows, "chart": chart, "series": series,
        "waitlist": waitlist_counts(db, since),
        "signups": core_adapter.count_users(db, since), "signups_total": core_adapter.count_users(db),
        "paid": core_adapter.count_paid(db, since), "paid_total": core_adapter.count_paid(db),
        "revenue": core_adapter.revenue(db, since), "credits": core_adapter.credits_flow(db, since),
        "jobs": jobs,
        "pages": charts.bars(top_pages(db, since)), "sources": charts.bars(top_sources(db, since)),
        "countries": charts.bars(breakdown(db, since, Visitor.country)),
        "devices": charts.bars(breakdown(db, since, Visitor.device_type)),
        "labels": charts.bars(breakdown(db, since, Visitor.lead_label)),
        "leads": top_leads(db, since),
        "security": db.execute(select(SecurityEvent).where(SecurityEvent.severity.in_(("medium", "high", "critical")))
                               .order_by(SecurityEvent.created_at.desc()).limit(6)).scalars().all(),
        "recent_joins": db.execute(select(WaitlistEntry).order_by(WaitlistEntry.created_at.desc()).limit(6)).scalars().all(),
    }


# ---- live ---------------------------------------------------------------------------------------
def live_sessions(db: Session, *, include_bots: bool = False) -> list[dict]:
    q = select(TrackSession, Visitor).join(Visitor, Visitor.id == TrackSession.visitor_id).where(
        TrackSession.last_seen_at >= utcnow() - LIVE_WINDOW)
    if not include_bots:
        q = q.where(TrackSession.is_bot.is_(False))
    rows = db.execute(q.order_by(TrackSession.last_seen_at.desc()).limit(200)).all()
    out = []
    for s, v in rows:
        out.append({"visitor": v.id, "session": s.id, "path": s.current_path, "title": s.current_title,
                    "country": v.country, "city": v.city, "device": v.device_type, "browser": v.browser,
                    "os": v.os, "source": s.source, "pageviews": s.pageviews,
                    "active_s": int((s.active_ms or 0) / 1000), "started_at": s.started_at.isoformat(),
                    "last_seen_at": s.last_seen_at.isoformat(), "score": v.lead_score, "label": v.lead_label,
                    "bot": bool(v.is_bot), "waitlist": bool(v.waitlist_id), "user": bool(v.user_id)})
    return out


# ---- visitors -----------------------------------------------------------------------------------
def visitors(db: Session, *, since: datetime, country: str = "", device: str = "", source: str = "",
             label: str = "", min_score: int = 0, consent: str = "", bots: str = "no", q: str = "",
             sort: str = "recent", limit: int = 50, offset: int = 0) -> tuple[list[Visitor], int]:
    stmt = select(Visitor).where(Visitor.last_seen_at >= since)
    if country:
        stmt = stmt.where(Visitor.country == country.upper()[:2])
    if device:
        stmt = stmt.where(Visitor.device_type == device)
    if source:
        stmt = stmt.where(Visitor.first_source == source)
    if label:
        stmt = stmt.where(Visitor.lead_label == label)
    if min_score:
        stmt = stmt.where(Visitor.lead_score >= min_score)
    if consent:
        stmt = stmt.where(Visitor.consent == consent)
    if bots == "no":
        stmt = stmt.where(Visitor.is_bot.is_(False))
    elif bots == "only":
        stmt = stmt.where(Visitor.is_bot.is_(True))
    if q:
        q = q.strip()[:200]
        wl_ids = select(WaitlistEntry.id).where(WaitlistEntry.email.like(f"%{q.lower()}%"))
        stmt = stmt.where(or_(Visitor.id == q, Visitor.last_ip == q, Visitor.fingerprint == q.lower(),
                              Visitor.user_id == q, Visitor.waitlist_id.in_(wl_ids)))
    total = int(db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())
    order = Visitor.lead_score.desc() if sort == "score" else Visitor.last_seen_at.desc()
    rows = db.execute(stmt.order_by(order).limit(limit).offset(offset)).scalars().all()
    return rows, total


def visitor_detail(db: Session, vid: str) -> dict | None:
    from ..tracking import replay as replay_mod

    v = db.get(Visitor, vid)
    if v is None:
        return None
    links = db.execute(select(VisitorLink).where(VisitorLink.visitor_id == vid)).scalars().all()
    wl_ids = {x for x in [v.waitlist_id] + [link.waitlist_id for link in links] if x}
    user_ids = {x for x in [v.user_id] + [link.user_id for link in links] if x}
    waitlist = db.execute(select(WaitlistEntry).where(WaitlistEntry.id.in_(wl_ids))).scalars().all() if wl_ids else []
    users = list(core_adapter.users_by_ids(db, list(user_ids)).values())
    # Other browsers of the same person (linked through their user / waitlist entry).
    sibling_q = select(VisitorLink.visitor_id).where(or_(
        VisitorLink.user_id.in_(user_ids) if user_ids else False,
        VisitorLink.waitlist_id.in_(wl_ids) if wl_ids else False), VisitorLink.visitor_id != vid)
    siblings = sorted(set(db.execute(sibling_q).scalars().all())) if (user_ids or wl_ids) else []

    sessions = db.execute(select(TrackSession).where(TrackSession.visitor_id == vid)
                          .order_by(TrackSession.started_at.desc()).limit(30)).scalars().all()
    events = db.execute(select(TrackEvent).where(TrackEvent.visitor_id == vid)
                        .order_by(TrackEvent.ts.desc()).limit(1500)).scalars().all()
    by_session: dict[str, list] = defaultdict(list)
    for e in reversed(events):
        by_session[e.session_id].append(e)
    pages = db.execute(select(PageView).where(PageView.visitor_id == vid)
                       .order_by(PageView.started_at.desc()).limit(200)).scalars().all()
    timeline = []
    for s in sessions:
        timeline.append({"session": s, "events": by_session.get(s.id, [])[:300],
                         "recordings": replay_mod.recordings_for_session(db, s.id) if s.has_replay else []})

    # Every subject that shared an IP or fingerprint with this visitor.
    ips = set(db.execute(select(SeenSignal.value).where(SeenSignal.kind == "ip", SeenSignal.subject_kind == "visitor",
                                                         SeenSignal.subject_id == vid)).scalars().all())
    fps = {v.fingerprint} if v.fingerprint else set()
    fps |= set(db.execute(select(SeenSignal.value).where(SeenSignal.kind == "fp", SeenSignal.subject_kind == "visitor",
                                                          SeenSignal.subject_id == vid)).scalars().all())
    cluster = []
    if ips or fps:
        conds = []
        if ips:
            conds.append(and_(SeenSignal.kind == "ip", SeenSignal.value.in_(ips)))
        if fps:
            conds.append(and_(SeenSignal.kind == "fp", SeenSignal.value.in_(fps)))
        rows = db.execute(select(SeenSignal).where(or_(*conds), or_(SeenSignal.subject_kind != "visitor",
                                                                    SeenSignal.subject_id != vid))
                          .order_by(SeenSignal.last_seen.desc()).limit(100)).scalars().all()
        cluster = rows
    sections = sorted(((k, val) for k, val in ((v.stats or {}).get("sections") or {}).items()), key=lambda x: -x[1])
    return {"v": v, "waitlist": waitlist, "users": users, "siblings": siblings, "timeline": timeline,
            "pages": pages, "sections": charts.bars(sections[:15]), "cluster": cluster,
            "ips": sorted(ips), "fps": sorted(fps),
            "bans": db.execute(select(Ban).where(Ban.active.is_(True), or_(
                and_(Ban.kind == "visitor", Ban.value == vid),
                and_(Ban.kind == "fingerprint", Ban.value.in_(fps or {""})),
                and_(Ban.kind == "ip", Ban.value.in_(ips or {""}))))).scalars().all()}


# ---- pages --------------------------------------------------------------------------------------
def pages(db: Session, since: datetime) -> list[dict]:
    rows = db.execute(select(PageView.path, func.count(PageView.id), func.count(distinct(PageView.visitor_id)),
                             func.avg(PageView.active_ms), func.avg(PageView.max_scroll))
                      .join(Visitor, Visitor.id == PageView.visitor_id)
                      .where(PageView.started_at >= since, Visitor.is_bot.is_(False))
                      .group_by(PageView.path).order_by(func.count(PageView.id).desc()).limit(200)).all()
    clicks = dict(db.execute(select(TrackEvent.path, func.count(TrackEvent.id))
                             .where(TrackEvent.ts >= since, TrackEvent.type == "click")
                             .group_by(TrackEvent.path)).all())
    top = max((r[1] for r in rows), default=0) or 1
    return [{"path": p, "views": n, "visitors": u, "avg_active_s": round((a or 0) / 1000, 1),
             "avg_scroll": round(s or 0), "clicks": clicks.get(p, 0), "pct": round(100 * n / top, 1)}
            for p, n, u, a, s in rows]


def page_detail(db: Session, path: str, since: datetime) -> dict:
    base = (select(PageView).join(Visitor, Visitor.id == PageView.visitor_id)
            .where(PageView.path == path, PageView.started_at >= since, Visitor.is_bot.is_(False)))
    views = db.execute(base.order_by(PageView.started_at.desc()).limit(5000)).scalars().all()
    depths = [pv.max_scroll or 0 for pv in views]
    curve = charts.scroll_curve(depths)
    layout = next((pv.layout for pv in views if pv.layout), None)

    ev = (TrackEvent.path == path, TrackEvent.ts >= since)
    dwell = db.execute(select(TrackEvent.name, func.sum(TrackEvent.value), func.count(distinct(TrackEvent.page_id)))
                       .where(*ev, TrackEvent.type == "dwell").group_by(TrackEvent.name)
                       .order_by(func.sum(TrackEvent.value).desc()).limit(30)).all()
    n_views = len(views) or 1
    dwell_rows = [(name, round((total or 0) / 1000 / n_views, 1), round((total or 0) / 1000), seen)
                  for name, total, seen in dwell]

    clicks = db.execute(select(TrackEvent.type, TrackEvent.x, TrackEvent.y, TrackEvent.name, TrackEvent.section)
                        .where(*ev, TrackEvent.type.in_(("click", "rage", "dead")), TrackEvent.x.is_not(None))
                        .order_by(TrackEvent.ts.desc()).limit(4000)).all()
    heat_rows = db.execute(select(TrackEvent.props).where(*ev, TrackEvent.type == "heat")
                           .order_by(TrackEvent.ts.desc()).limit(800)).scalars().all()
    heat = [p for props in heat_rows for p in ((props or {}).get("p") or [])][:8000]

    imps = dict(db.execute(select(TrackEvent.name, func.count(distinct(TrackEvent.page_id)))
                           .where(*ev, TrackEvent.type == "imp").group_by(TrackEvent.name)).all())
    clk = dict(db.execute(select(TrackEvent.name, func.count(distinct(TrackEvent.page_id)))
                          .where(*ev, TrackEvent.type == "click", TrackEvent.name.is_not(None))
                          .group_by(TrackEvent.name)).all())
    hovers = dict(db.execute(select(TrackEvent.name, func.count(distinct(TrackEvent.page_id)))
                             .where(*ev, TrackEvent.type == "hover").group_by(TrackEvent.name)).all())
    ctas = sorted(({"name": n, "impressions": imps.get(n, 0), "clicks": clk.get(n, 0), "hovers": hovers.get(n, 0),
                    "ctr": round(100 * clk.get(n, 0) / imps[n], 1) if imps.get(n) else None}
                   for n in set(imps) | set(clk) if n and not n.startswith(("a:", "button:"))
                   or (n and imps.get(n))),
                  key=lambda r: (-(r["clicks"]), -(r["impressions"])))[:40]
    frustration = Counter((t, name or "?", section or "") for t, _x, _y, name, section in clicks if t in ("rage", "dead"))

    vitals = defaultdict(list)
    for name, val in db.execute(select(TrackEvent.name, TrackEvent.value).where(*ev, TrackEvent.type == "vital")
                                .limit(20000)).all():
        if val is not None:
            vitals[name].append(val)
    p75 = {k: sorted(vs)[int(0.75 * (len(vs) - 1))] for k, vs in vitals.items() if vs}
    errors = db.execute(select(TrackEvent.name, func.count(TrackEvent.id)).where(*ev, TrackEvent.type == "error")
                        .group_by(TrackEvent.name).order_by(func.count(TrackEvent.id).desc()).limit(10)).all()
    forms = db.execute(select(TrackEvent.type, TrackEvent.name, func.count(distinct(TrackEvent.page_id)))
                       .where(*ev, TrackEvent.type.in_(("focus", "submit", "abandon")))
                       .group_by(TrackEvent.type, TrackEvent.name)).all()
    form_stats: dict = defaultdict(lambda: {"focus": 0, "submit": 0, "abandon": 0})
    for t, name, n in forms:
        form_stats[name or "form"][t] = n
    return {"path": path, "views": len(views), "visitors": len({pv.visitor_id for pv in views}),
            "avg_active_s": round(sum(pv.active_ms or 0 for pv in views) / n_views / 1000, 1),
            "curve": charts.path_for_curve(curve), "curve_points": curve, "layout": layout or [],
            "dwell": charts.bars(dwell_rows), "clicks": clicks, "heat": heat, "ctas": ctas,
            "frustration": frustration.most_common(12), "vitals": p75, "errors": errors,
            "forms": dict(form_stats)}


# ---- funnel -------------------------------------------------------------------------------------
def funnel(db: Session, since: datetime) -> dict:
    cohort = set(db.execute(_human_bots(select(distinct(TrackSession.visitor_id))
                                        .where(TrackSession.started_at >= since))).scalars().all())

    def visitors_with(*conds) -> set[str]:
        return set(db.execute(select(distinct(TrackEvent.visitor_id)).where(TrackEvent.ts >= since, *conds))
                   .scalars().all()) & cohort

    landing = set(db.execute(select(distinct(PageView.visitor_id)).where(PageView.started_at >= since,
                                                                         PageView.path == "/")).scalars().all()) & cohort
    pricing_views = set(db.execute(select(distinct(PageView.visitor_id)).where(
        PageView.started_at >= since, PageView.path.like("/pricing%"))).scalars().all())
    pricing_dwell = {vid for vid, name in db.execute(select(TrackEvent.visitor_id, TrackEvent.name).where(
        TrackEvent.ts >= since, TrackEvent.type == "dwell")).all() if name and _PRICING.search(name)}
    pricing = (pricing_views | pricing_dwell) & cohort
    form_start = {vid for vid, name in db.execute(select(TrackEvent.visitor_id, TrackEvent.name).where(
        TrackEvent.ts >= since, TrackEvent.type == "focus")).all() if _FORMSTART.search(name or "")} & cohort
    wl_rows = db.execute(select(VisitorLink.visitor_id, WaitlistEntry.status, WaitlistEntry.confirmed_at)
                         .join(WaitlistEntry, WaitlistEntry.id == VisitorLink.waitlist_id)
                         .where(VisitorLink.waitlist_id.is_not(None))).all()
    joined = {vid for vid, _s, _c in wl_rows} & cohort
    confirmed = {vid for vid, _s, c in wl_rows if c is not None} & cohort
    user_links = db.execute(select(VisitorLink.visitor_id, VisitorLink.user_id).where(VisitorLink.user_id.is_not(None))).all()
    signed_up = {vid for vid, _u in user_links} & cohort
    users = core_adapter.users_by_ids(db, list({u for _v, u in user_links}))
    paid = {vid for vid, u in user_links if getattr(users.get(u), "first_paid_at", None)} & cohort
    steps = [("Visited", len(cohort)), ("Saw the landing page", len(landing)), ("Looked at pricing", len(pricing)),
             ("Started the waitlist form", len(form_start)), ("Joined the waitlist", len(joined)),
             ("Confirmed email", len(confirmed)), ("Signed up", len(signed_up)), ("Paid", len(paid))]
    all_joins = waitlist_counts(db, since)
    return {"steps": charts.funnel(steps), "untracked_joins": max(0, all_joins.get("joins", 0) - len(joined)),
            "all_joins": all_joins.get("joins", 0)}


# ---- sources ------------------------------------------------------------------------------------
def sources(db: Session, since: datetime) -> dict:
    rows = db.execute(_human_bots(select(TrackSession.source, TrackSession.medium, TrackSession.campaign,
                                         TrackSession.visitor_id, TrackSession.pageviews, TrackSession.active_ms)
                                  .where(TrackSession.started_at >= since))).all()
    joined = set(db.execute(select(VisitorLink.visitor_id).where(VisitorLink.waitlist_id.is_not(None))).scalars().all())
    signed = set(db.execute(select(VisitorLink.visitor_id).where(VisitorLink.user_id.is_not(None))).scalars().all())
    scores = dict(db.execute(select(Visitor.id, Visitor.lead_score).where(Visitor.last_seen_at >= since)).all())
    agg: dict = defaultdict(lambda: {"sessions": 0, "visitors": set(), "active": 0})
    camp: dict = defaultdict(lambda: {"sessions": 0, "visitors": set()})
    for src, med, cmp_, vid, _pv, ms in rows:
        k = (src or "direct", med or "none")
        agg[k]["sessions"] += 1
        agg[k]["visitors"].add(vid)
        agg[k]["active"] += ms or 0
        if cmp_:
            camp[(src or "?", cmp_)]["sessions"] += 1
            camp[(src or "?", cmp_)]["visitors"].add(vid)
    table = []
    for (src, med), a in agg.items():
        vs = a["visitors"]
        table.append({"source": src, "medium": med, "sessions": a["sessions"], "visitors": len(vs),
                      "joins": len(vs & joined), "signups": len(vs & signed),
                      "conv": round(100 * len(vs & joined) / len(vs), 1) if vs else 0,
                      "avg_score": round(sum(scores.get(v, 0) for v in vs) / len(vs)) if vs else 0,
                      "avg_active_s": round(a["active"] / a["sessions"] / 1000, 1) if a["sessions"] else 0})
    table.sort(key=lambda r: -r["sessions"])
    top = max((r["sessions"] for r in table), default=0) or 1
    for r in table:
        r["pct"] = round(100 * r["sessions"] / top, 1)
    referrers = db.execute(_human_bots(select(TrackSession.referrer_host, func.count(TrackSession.id))
                                       .where(TrackSession.started_at >= since, TrackSession.referrer_host.is_not(None)))
                           .group_by(TrackSession.referrer_host).order_by(func.count(TrackSession.id).desc())
                           .limit(30)).all()
    campaigns = sorted(({"source": s, "campaign": c, "sessions": a["sessions"], "visitors": len(a["visitors"]),
                         "joins": len(a["visitors"] & joined)} for (s, c), a in camp.items()),
                       key=lambda r: -r["sessions"])[:30]
    return {"table": table, "referrers": charts.bars(referrers), "campaigns": campaigns}


# ---- security -----------------------------------------------------------------------------------
def security(db: Session, *, kind: str = "", severity: str = "", ip: str = "", limit: int = 100) -> dict:
    q = select(SecurityEvent)
    if kind:
        q = q.where(SecurityEvent.kind == kind)
    if severity:
        q = q.where(SecurityEvent.severity == severity)
    if ip:
        q = q.where(SecurityEvent.ip == ip)
    stream = db.execute(q.order_by(SecurityEvent.created_at.desc()).limit(limit)).scalars().all()
    day = utcnow() - timedelta(days=1)
    week = utcnow() - timedelta(days=7)
    kinds = db.execute(select(SecurityEvent.kind, func.count(SecurityEvent.id)).where(SecurityEvent.created_at >= week)
                       .group_by(SecurityEvent.kind).order_by(func.count(SecurityEvent.id).desc())).all()
    offenders = db.execute(select(SecurityEvent.ip, func.count(SecurityEvent.id), func.max(SecurityEvent.created_at))
                           .where(SecurityEvent.created_at >= week, SecurityEvent.ip.is_not(None),
                                  SecurityEvent.severity.in_(("medium", "high", "critical")))
                           .group_by(SecurityEvent.ip).order_by(func.count(SecurityEvent.id).desc()).limit(15)).all()
    limited = db.execute(select(SecurityEvent.ip, SecurityEvent.detail, SecurityEvent.created_at)
                         .where(SecurityEvent.kind == "rate_limited", SecurityEvent.created_at >= day)
                         .order_by(SecurityEvent.created_at.desc()).limit(50)).all()
    bans = db.execute(select(Ban).where(Ban.active.is_(True)).order_by(Ban.created_at.desc()).limit(300)).scalars().all()
    return {"stream": stream, "kinds": charts.bars(kinds), "offenders": offenders, "limited": limited, "bans": bans,
            "clusters": clusters(db), "signup_clusters": core_adapter.signup_clusters(db),
            "referrals": db.execute(select(WaitlistEntry.referral_reason, func.count(WaitlistEntry.id))
                                    .where(WaitlistEntry.referral_status.in_(("rejected", "held")))
                                    .group_by(WaitlistEntry.referral_reason)).all()}


def clusters(db: Session, limit: int = 40) -> list[dict]:
    """IPs / fingerprints shared by 2+ identities (waitlist entries or users): multi-account rings."""
    rows = db.execute(select(SeenSignal.kind, SeenSignal.value, func.count(distinct(SeenSignal.subject_id)))
                      .where(SeenSignal.subject_kind.in_(("waitlist", "user")), SeenSignal.kind.in_(("ip", "fp")))
                      .group_by(SeenSignal.kind, SeenSignal.value)
                      .having(func.count(distinct(SeenSignal.subject_id)) > 1)
                      .order_by(func.count(distinct(SeenSignal.subject_id)).desc()).limit(limit)).all()
    out = []
    for kind, value, n in rows:
        members = db.execute(select(SeenSignal.subject_kind, SeenSignal.subject_id, SeenSignal.last_seen)
                             .where(SeenSignal.kind == kind, SeenSignal.value == value,
                                    SeenSignal.subject_kind.in_(("waitlist", "user")))
                             .order_by(SeenSignal.last_seen.desc()).limit(12)).all()
        wl = {m.subject_id for m in members if m.subject_kind == "waitlist"}
        emails = dict(db.execute(select(WaitlistEntry.id, WaitlistEntry.email).where(WaitlistEntry.id.in_(wl))).all()) if wl else {}
        users = core_adapter.users_by_ids(db, [m.subject_id for m in members if m.subject_kind == "user"])
        out.append({"kind": kind, "value": value, "count": int(n), "members": [
            {"kind": m.subject_kind, "id": m.subject_id, "last_seen": m.last_seen,
             "email": emails.get(m.subject_id) or getattr(users.get(m.subject_id), "email", None)} for m in members]})
    return out
