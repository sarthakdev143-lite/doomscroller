"""The owner's admin panel at /admin. Server-rendered pages + a small JSON API for actions.

Every route depends on `guard.admin_user` (IP allowlist -> core require_admin: role + TOTP -> role
and status re-check). State-changing routes live under /admin/api and are CSRF-protected by the
security middleware (admin.js sends X-CSRF-Token); each one writes an AuditLog row. Viewing a
replay and exporting the waitlist are audited too: both expose personal data.
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..common import utcnow
from ..config import settings
from ..db import get_db
from ..models.security import AdminNote, AuditLog, Ban
from ..models.tracking import Visitor
from ..models.waitlist import InviteBatch, WaitlistEntry
from ..security import bans as bans_mod
from ..security import events
from ..templating import SAAS_DIR, templates
from ..tracking import replay as replay_mod
from ..tracking import retention
from ..waitlist import emails as wl_emails
from ..waitlist import service as wl
from . import charts, core_adapter, csvutil, guard, queries

router = APIRouter(route_class=guard.AdminRoute, dependencies=[Depends(guard.admin_user)])

PAGE = 50
NAV = [("overview", "/admin", "Overview"), ("live", "/admin/live", "Live"), ("visitors", "/admin/visitors", "Visitors"),
       ("pages", "/admin/pages", "Pages"), ("funnel", "/admin/funnel", "Funnel"), ("sources", "/admin/sources", "Sources"),
       ("waitlist", "/admin/waitlist", "Waitlist"), ("users", "/admin/users", "Users"),
       ("security", "/admin/security", "Security"), ("audit", "/admin/audit", "Audit log")]


# ---- rendering helpers ---------------------------------------------------------------------------
def _ago(ts: datetime | None) -> str:
    if ts is None:
        return "—"
    s = (utcnow() - ts).total_seconds()
    if s < 0:
        return "just now"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= size:
            return f"{int(s // size)}{unit} ago"
    return f"{int(s)}s ago"


def _num(v, digits: int = 0) -> str:
    if v is None:
        return "—"
    if isinstance(v, float) and digits:
        return f"{v:,.{digits}f}"
    try:
        return f"{int(round(float(v))):,}"
    except (TypeError, ValueError):
        return str(v)


def _dur(ms) -> str:
    s = int((ms or 0) / 1000)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {(s % 3600) // 60:02d}m"


def _money(minor: int | None, cur: str) -> str:
    if minor is None:
        return "—"
    zero_decimal = {"JPY", "KRW", "VND", "CLP", "ISK", "UGX"}
    val = minor if cur.upper() in zero_decimal else minor / 100
    return f"{cur.upper()} {val:,.0f}" if cur.upper() in zero_decimal else f"{cur.upper()} {val:,.2f}"


def _flag(cc: str | None) -> str:
    if not cc or len(cc) != 2 or not cc.isalpha():
        return ""
    return "".join(chr(0x1F1E6 + ord(c) - 65) for c in cc.upper())


def _qs(request: Request, **changes) -> str:
    params = dict(request.query_params)
    for k, v in changes.items():
        if v is None or v == "":
            params.pop(k, None)  # None removes a parameter (e.g. reset paging when a filter changes)
        else:
            params[k] = v
    return "?" + urlencode(params) if params else "?"


H = SimpleNamespace(ago=_ago, num=_num, dur=_dur, money=_money, flag=_flag, sparkline=charts.sparkline)


def _tokens_css() -> bool:
    return (SAAS_DIR / "web" / "static" / "css" / "tokens.css").is_file()


def render(request: Request, name: str, ctx: dict, status: int = 200) -> HTMLResponse:
    base = {"h": H, "nav": NAV, "admin": getattr(request.state, "admin", None), "tokens_css": _tokens_css(),
            "qs": lambda **c: _qs(request, **c), "brand": settings.brand_name, "now": utcnow()}
    resp = templates.TemplateResponse(request, f"admin/{name}", base | ctx, status_code=status)
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": code, "message": message}, status_code=status)


async def _body(request: Request) -> dict:
    try:
        if "json" in request.headers.get("content-type", ""):
            data = await request.json()
        else:
            data = dict(await request.form())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# ---- pages ---------------------------------------------------------------------------------------
@router.get("/admin", response_class=HTMLResponse)
def overview(request: Request, range: str = "30d", db: Session = Depends(get_db)):
    return render(request, "overview.html", {"active": "overview", "o": queries.overview(db, range)})


@router.get("/admin/live", response_class=HTMLResponse)
def live(request: Request, bots: int = 0, db: Session = Depends(get_db)):
    return render(request, "live.html", {"active": "live", "rows": queries.live_sessions(db, include_bots=bool(bots)),
                                         "bots": bots})


@router.get("/admin/api/live")
def live_json(bots: int = 0, db: Session = Depends(get_db)):
    rows = queries.live_sessions(db, include_bots=bool(bots))
    return {"count": len({r["visitor"] for r in rows}), "rows": rows}


@router.get("/admin/visitors", response_class=HTMLResponse)
def visitors(request: Request, range: str = "30d", country: str = "", device: str = "", source: str = "",
             label: str = "", min_score: int = 0, consent: str = "", bots: str = "no", q: str = "",
             sort: str = "recent", page: int = Query(1, ge=1, le=500), db: Session = Depends(get_db)):
    key, since = queries.window(range)
    rows, total = queries.visitors(db, since=since, country=country, device=device, source=source, label=label,
                                   min_score=min_score, consent=consent, bots=bots, q=q, sort=sort, limit=PAGE,
                                   offset=(page - 1) * PAGE)
    facets = {
        "countries": [c for c, in db.execute(select(Visitor.country).where(Visitor.country.is_not(None))
                                            .group_by(Visitor.country).order_by(func.count().desc()).limit(60)).all()],
        "sources": [s for s, in db.execute(select(Visitor.first_source).where(Visitor.first_source.is_not(None))
                                          .group_by(Visitor.first_source).order_by(func.count().desc()).limit(60)).all()],
    }
    return render(request, "visitors.html", {"active": "visitors", "rows": rows, "total": total, "page": page,
                                             "pages": max(1, -(-total // PAGE)), "range": key, "facets": facets,
                                             "f": {"country": country, "device": device, "source": source,
                                                   "label": label, "min_score": min_score, "consent": consent,
                                                   "bots": bots, "q": q, "sort": sort}})


@router.get("/admin/visitors/{vid}", response_class=HTMLResponse)
def visitor(request: Request, vid: str, db: Session = Depends(get_db)):
    d = queries.visitor_detail(db, vid)
    if d is None:
        return render(request, "missing.html", {"active": "visitors", "what": "visitor"}, 404)
    notes = db.execute(select(AdminNote).where(AdminNote.subject_kind == "visitor", AdminNote.subject_id == vid)
                       .order_by(AdminNote.created_at.desc())).scalars().all()
    return render(request, "visitor.html", {"active": "visitors", "d": d, "notes": notes})


@router.get("/admin/replay/{page_id}", response_class=HTMLResponse)
def replay_page(request: Request, page_id: str, db: Session = Depends(get_db)):
    from ..models.tracking import PageView

    pv = db.get(PageView, page_id)
    if pv is None or not pv.has_replay:
        return render(request, "missing.html", {"active": "visitors", "what": "recording"}, 404)
    guard.audit(db, request, "replay_view", target_kind="pageview", target_id=page_id,
                detail={"visitor": pv.visitor_id, "path": pv.path}, commit=True)
    return render(request, "replay.html", {"active": "visitors", "pv": pv, "player_js": replay_mod.PLAYER_JS,
                                           "player_css": replay_mod.PLAYER_CSS})


@router.get("/admin/api/replay/{page_id}")
def replay_events(page_id: str, db: Session = Depends(get_db)):
    events_ = replay_mod.load_events(db, page_id)
    if not events_:
        return _err(404, "not_found", "No recording for that page view.")
    return Response(json.dumps(events_, separators=(",", ":")), media_type="application/json")


@router.get("/admin/pages", response_class=HTMLResponse)
def pages(request: Request, range: str = "30d", db: Session = Depends(get_db)):
    key, since = queries.window(range)
    return render(request, "pages.html", {"active": "pages", "rows": queries.pages(db, since), "range": key})


@router.get("/admin/pages/view", response_class=HTMLResponse)
def page_view(request: Request, path: str = "/", range: str = "30d", db: Session = Depends(get_db)):
    key, since = queries.window(range)
    return render(request, "page.html", {"active": "pages", "p": queries.page_detail(db, path[:512], since),
                                         "range": key})


@router.get("/admin/funnel", response_class=HTMLResponse)
def funnel(request: Request, range: str = "30d", db: Session = Depends(get_db)):
    key, since = queries.window(range)
    return render(request, "funnel.html", {"active": "funnel", "f": queries.funnel(db, since), "range": key})


@router.get("/admin/sources", response_class=HTMLResponse)
def sources(request: Request, range: str = "30d", db: Session = Depends(get_db)):
    key, since = queries.window(range)
    return render(request, "sources.html", {"active": "sources", "s": queries.sources(db, since), "range": key})


def _waitlist_query(q: str, status: str, flagged: str):
    stmt = select(WaitlistEntry)
    if q:
        like = f"%{q.strip().lower()[:200]}%"
        stmt = stmt.where(or_(WaitlistEntry.email.like(like), WaitlistEntry.referral_code == q.strip().lower(),
                              WaitlistEntry.id == q.strip(), WaitlistEntry.invite_code == q.strip().upper()))
    if status:
        stmt = stmt.where(WaitlistEntry.status == status)
    else:
        stmt = stmt.where(WaitlistEntry.status != "removed")
    if flagged == "yes":
        stmt = stmt.where(WaitlistEntry.flagged.is_(True))
    elif flagged == "no":
        stmt = stmt.where(WaitlistEntry.flagged.is_(False))
    return stmt


@router.get("/admin/waitlist", response_class=HTMLResponse)
def waitlist(request: Request, q: str = "", status: str = "", flagged: str = "", sort: str = "rank",
             page: int = Query(1, ge=1, le=1000), db: Session = Depends(get_db)):
    stmt = _waitlist_query(q, status, flagged)
    total = int(db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())
    order = (WaitlistEntry.created_at.desc(),) if sort == "recent" else (WaitlistEntry.rank_key, WaitlistEntry.seq)
    rows = db.execute(stmt.order_by(*order).limit(PAGE).offset((page - 1) * PAGE)).scalars().all()
    positions = {e.id: wl.position(db, e) for e in rows if e.status == "confirmed"}
    batches = db.execute(select(InviteBatch).order_by(InviteBatch.created_at.desc()).limit(10)).scalars().all()
    return render(request, "waitlist.html", {
        "active": "waitlist", "rows": rows, "total": total, "page": page, "pages": max(1, -(-total // PAGE)),
        "positions": positions, "counts": queries.waitlist_counts(db), "batches": batches,
        "f": {"q": q, "status": status, "flagged": flagged, "sort": sort},
        # Google-only mode sends no email: invites become links the admin copies to each person.
        "mail_enabled": wl_emails.mail_allowed(), "invite_url": wl.invite_url})


@router.get("/admin/waitlist/export.csv")
def waitlist_export(request: Request, q: str = "", status: str = "", flagged: str = "",
                    db: Session = Depends(get_db)):
    rows = db.execute(_waitlist_query(q, status, flagged).order_by(WaitlistEntry.rank_key, WaitlistEntry.seq)
                      .limit(100_000)).scalars().all()
    guard.audit(db, request, "waitlist_export", target_kind="waitlist", detail={"rows": len(rows), "q": q,
                                                                                "status": status}, commit=True)
    header = ["id", "email", "status", "role", "channel_url", "joined_at", "confirmed_at", "referral_code",
              "referrals", "referred_by", "referral_status", "country", "source", "risk_score", "risk_reasons",
              "flagged", "invite_code", "invited_at", "redeemed_at"]
    body = csvutil.to_csv(header, ([e.id, e.email, e.status, e.role, e.channel_url, e.created_at, e.confirmed_at,
                                    e.referral_code, e.referrals_counted, e.referred_by_id, e.referral_status,
                                    e.country, (e.source or {}).get("source"), e.risk_score,
                                    " ".join(e.risk_reasons or []), e.flagged, e.invite_code, e.invited_at,
                                    e.redeemed_at] for e in rows))
    stamp = utcnow().strftime("%Y%m%d-%H%M")
    return Response(body, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="waitlist-{stamp}.csv"'})


@router.get("/admin/users", response_class=HTMLResponse)
def users(request: Request, q: str = "", status: str = "", plan: str = "", page: int = Query(1, ge=1, le=1000),
          db: Session = Depends(get_db)):
    rows = core_adapter.list_users(db, q=q, status=status, plan=plan, limit=PAGE, offset=(page - 1) * PAGE)
    available = rows is not None
    rows = rows or []
    return render(request, "users.html", {"active": "users", "rows": rows, "available": available, "page": page,
                                          "balances": core_adapter.balances(db, [u.id for u in rows]),
                                          "total": core_adapter.count_users(db),
                                          "f": {"q": q, "status": status, "plan": plan}})


@router.get("/admin/users/{user_id}", response_class=HTMLResponse)
def user(request: Request, user_id: str, db: Session = Depends(get_db)):
    from ..tracking import identity

    u = core_adapter.get_user(db, user_id)
    if u is None:
        return render(request, "missing.html", {"active": "users", "what": "user"}, 404)
    vids = identity.visitors_for(db, user_id=user_id)
    visitors_ = db.execute(select(Visitor).where(Visitor.id.in_(vids))).scalars().all() if vids else []
    notes = db.execute(select(AdminNote).where(AdminNote.subject_kind == "user", AdminNote.subject_id == user_id)
                       .order_by(AdminNote.created_at.desc())).scalars().all()
    sharing = [x for x in core_adapter.users_sharing(db, ip=getattr(u, "signup_ip", None),
                                                     fingerprint=getattr(u, "signup_fingerprint", None))
               if x.id != user_id]
    wl_entry = db.execute(select(WaitlistEntry).where(WaitlistEntry.redeemed_user_id == user_id)).scalar_one_or_none()
    return render(request, "user.html", {"active": "users", "u": u, "balance": core_adapter.balance(db, user_id),
                                         "ledger": core_adapter.user_ledger(db, user_id),
                                         "jobs": core_adapter.user_jobs(db, user_id),
                                         "payments": core_adapter.user_payments(db, user_id),
                                         "visitors": visitors_, "notes": notes, "sharing": sharing,
                                         "wl": wl_entry})


@router.get("/admin/security", response_class=HTMLResponse)
def security(request: Request, kind: str = "", severity: str = "", ip: str = "", db: Session = Depends(get_db)):
    return render(request, "security.html", {"active": "security",
                                             "s": queries.security(db, kind=kind, severity=severity, ip=ip),
                                             "f": {"kind": kind, "severity": severity, "ip": ip},
                                             "ban_kinds": bans_mod.KINDS})


@router.get("/admin/audit", response_class=HTMLResponse)
def audit_log(request: Request, action: str = "", actor: str = "", page: int = Query(1, ge=1, le=1000),
              db: Session = Depends(get_db)):
    stmt = select(AuditLog)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if actor:
        stmt = stmt.where(or_(AuditLog.actor_email == actor, AuditLog.actor_id == actor))
    total = int(db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())
    rows = db.execute(stmt.order_by(AuditLog.created_at.desc()).limit(PAGE).offset((page - 1) * PAGE)).scalars().all()
    actions = [a for a, in db.execute(select(AuditLog.action).group_by(AuditLog.action)).all()]
    return render(request, "audit.html", {"active": "audit", "rows": rows, "total": total, "page": page,
                                          "pages": max(1, -(-total // PAGE)), "actions": actions,
                                          "f": {"action": action, "actor": actor}})


# ---- actions (JSON, CSRF-protected by the middleware, all audited) -------------------------------
@router.post("/admin/api/bans")
async def add_ban(request: Request, db: Session = Depends(get_db)):
    data = await _body(request)
    kind, value = str(data.get("kind") or ""), str(data.get("value") or "")
    hours = data.get("hours")
    try:
        hours_f = float(hours) if hours not in (None, "") else None
        ban = bans_mod.add(db, kind, value, str(data.get("reason") or "")[:500],
                           created_by=getattr(request.state.admin, "id", None), hours=hours_f)
    except (ValueError, TypeError) as exc:
        return _err(400, "invalid_ban", str(exc))
    guard.audit(db, request, "ban_add", target_kind=kind, target_id=ban.value,
                detail={"reason": ban.reason, "hours": hours_f})
    events.log(db, "ban_added", "info", ip=ban.value if kind == "ip" else None,
               fingerprint=ban.value if kind == "fingerprint" else None, detail={"kind": kind, "value": ban.value},
               commit=False)
    db.commit()
    return {"ok": True, "id": ban.id, "kind": ban.kind, "value": ban.value}


@router.delete("/admin/api/bans/{ban_id}")
def remove_ban(ban_id: str, request: Request, db: Session = Depends(get_db)):
    ban = bans_mod.remove(db, ban_id)
    if ban is None:
        return _err(404, "not_found", "No such ban.")
    guard.audit(db, request, "ban_remove", target_kind=ban.kind, target_id=ban.value)
    db.commit()
    return {"ok": True}


@router.post("/admin/api/notes")
async def add_note(request: Request, db: Session = Depends(get_db)):
    data = await _body(request)
    kind, sid, body = str(data.get("subject_kind") or ""), str(data.get("subject_id") or ""), str(data.get("body") or "")
    if kind not in ("user", "visitor", "waitlist", "ip", "fingerprint") or not sid or not body.strip():
        return _err(400, "invalid_note", "subject_kind, subject_id and body are required.")
    admin = request.state.admin
    note = AdminNote(subject_kind=kind, subject_id=sid[:160], body=body.strip()[:5000],
                     author_id=getattr(admin, "id", None), author_email=getattr(admin, "email", None),
                     created_at=utcnow())
    db.add(note)
    guard.audit(db, request, "note_add", target_kind=kind, target_id=sid)
    db.commit()
    return {"ok": True, "id": note.id}


@router.post("/admin/api/visitors/{vid}/forget")
def forget_visitor(vid: str, request: Request, db: Session = Depends(get_db)):
    v = db.get(Visitor, vid)
    if v is None:
        return _err(404, "not_found", "No such visitor.")
    out = retention.forget_visitor(db, vid)
    v.stats, v.interests, v.lead_reasons, v.client = {}, None, None, None
    v.lead_score, v.lead_label = 0, None
    guard.audit(db, request, "visitor_forget", target_kind="visitor", target_id=vid, detail=out)
    db.commit()
    return {"ok": True, **out}


def _entry(db: Session, entry_id: str) -> WaitlistEntry | None:
    return db.get(WaitlistEntry, entry_id)


@router.post("/admin/api/waitlist/invite")
async def waitlist_invite_batch(request: Request, db: Session = Depends(get_db)):
    data = await _body(request)
    try:
        count = int(data.get("count") or 0)
    except (TypeError, ValueError):
        count = 0
    if not 1 <= count <= 1000:
        return _err(400, "invalid_count", "Invite between 1 and 1000 people at a time.")
    include_flagged = str(data.get("include_flagged") or "").lower() in ("1", "true", "on", "yes")
    batch = wl.invite_batch(db, count, actor_id=getattr(request.state.admin, "id", None),
                            note=str(data.get("note") or "")[:500], include_flagged=include_flagged)
    guard.audit(db, request, "waitlist_invite_batch", target_kind="invite_batch", target_id=batch.id,
                detail={"requested": count, "invited": batch.invited, "include_flagged": include_flagged}, commit=True)
    out = {"ok": True, "batch": batch.id, "invited": batch.invited}
    if not wl_emails.mail_allowed():
        links = [{"email": e.email, "invite_url": wl.invite_url(e)}
                 for e in db.execute(select(WaitlistEntry).where(WaitlistEntry.invite_batch_id == batch.id)
                                     .order_by(WaitlistEntry.rank_key, WaitlistEntry.seq)).scalars()]
        out.update(links=links, message=f"Created {batch.invited} invite links. Nothing was emailed: copy each "
                                        "person's link from the list (status: invited) and send it to them.")
    return out


@router.post("/admin/api/waitlist/{entry_id}/{action}")
def waitlist_action(entry_id: str, action: str, request: Request, db: Session = Depends(get_db)):
    e = _entry(db, entry_id)
    if e is None:
        return _err(404, "not_found", "No such waitlist entry.")
    result: dict = {"ok": True}
    if action == "invite":
        if e.status not in ("confirmed", "pending", "invited"):
            return _err(409, "bad_state", f"Cannot invite an entry that is {e.status}.")
        wl.invite(db, e)
        db.commit()
        if not wl_emails.mail_allowed():
            result.update(invite_url=wl.invite_url(e),
                          message=f"Invite link created for {e.email}. Nothing was emailed: copy it from the row.")
    elif action == "resend":
        result["sent"] = wl.resend(db, e)
        if result["sent"] == "link":
            result.update(invite_url=wl.invite_url(e), message="Nothing is emailed here: copy the link from the row.")
    elif action in ("flag", "unflag"):
        e.flagged = action == "flag"
        db.commit()
    elif action == "remove":
        e.status = "removed"
        db.commit()
    elif action == "restore":
        e.status = "confirmed" if e.confirmed_at else "pending"
        db.commit()
    else:
        return _err(404, "unknown_action", "Unknown action.")
    guard.audit(db, request, f"waitlist_{action}", target_kind="waitlist", target_id=entry_id,
                detail={"email": e.email}, commit=True)
    return result


@router.post("/admin/api/users/{user_id}/status")
async def user_status(user_id: str, request: Request, db: Session = Depends(get_db)):
    data = await _body(request)
    status = str(data.get("status") or "")
    if status not in ("active", "suspended", "banned"):
        return _err(400, "invalid_status", "status must be active, suspended or banned.")
    u = core_adapter.get_user(db, user_id)
    if u is None:
        return _err(404, "not_found", "No such user.")
    if getattr(u, "role", None) == "admin" and status != "active":
        return _err(409, "is_admin", "Demote an admin before suspending or banning them.")
    before = getattr(u, "status", None)
    u.status = status
    revoked = None
    if status != "active":
        revoked = core_adapter.revoke_sessions(db, user_id, reason=f"admin_{status}")
    extra_bans = []
    if status == "banned" and str(data.get("ban_signals") or "").lower() in ("1", "true", "on", "yes"):
        for kind, value in (("fingerprint", getattr(u, "signup_fingerprint", None)),
                            ("ip", getattr(u, "signup_ip", None))):
            if value:
                try:
                    bans_mod.add(db, kind, value, f"banned with user {user_id}",
                                 created_by=getattr(request.state.admin, "id", None))
                    extra_bans.append(kind)
                except ValueError:
                    pass
    guard.audit(db, request, "user_status", target_kind="user", target_id=user_id,
                detail={"from": before, "to": status, "sessions_revoked": revoked, "bans": extra_bans})
    db.commit()
    return {"ok": True, "status": status, "sessions_revoked": revoked, "bans": extra_bans}


@router.post("/admin/api/users/{user_id}/credits")
async def user_credits(user_id: str, request: Request, db: Session = Depends(get_db)):
    data = await _body(request)
    try:
        amount = int(data.get("amount"))
    except (TypeError, ValueError):
        return _err(400, "invalid_amount", "amount must be a whole number of credits.")
    note = str(data.get("note") or "").strip()[:300]
    if not amount or abs(amount) > 100_000 or not note:
        return _err(400, "invalid_amount", "Give a non-zero amount (max 100000) and a reason.")
    if core_adapter.get_user(db, user_id) is None:
        return _err(404, "not_found", "No such user.")
    key = str(data.get("idempotency_key") or f"admin:{user_id}:{secrets.token_hex(8)}")[:120]
    ok = core_adapter.adjust_credits(db, user_id, amount, note=note,
                                     actor_id=getattr(request.state.admin, "id", None), key=key)
    if not ok:
        return _err(503, "ledger_unavailable", "The credit ledger is not available.")
    guard.audit(db, request, "user_credits", target_kind="user", target_id=user_id,
                detail={"amount": amount, "note": note})
    db.commit()
    return {"ok": True, "balance": core_adapter.balance(db, user_id)}


@router.post("/admin/api/users/{user_id}/billing-hold")
async def user_billing_hold(user_id: str, request: Request, db: Session = Depends(get_db)):
    data = await _body(request)
    u = core_adapter.get_user(db, user_id)
    if u is None or not hasattr(u, "billing_hold"):
        return _err(404, "not_found", "No such user.")
    hold = str(data.get("hold") or "").lower() in ("1", "true", "on", "yes")
    u.billing_hold = hold
    guard.audit(db, request, "user_billing_hold", target_kind="user", target_id=user_id, detail={"hold": hold})
    db.commit()
    return {"ok": True, "billing_hold": hold}


@router.post("/admin/api/maintenance")
def run_maintenance(request: Request, db: Session = Depends(get_db)):
    from ..security import limits

    out = retention.cleanup(db)
    out["rate_counters_deleted"] = limits.limiter.cleanup()
    guard.audit(db, request, "maintenance_run", detail=out)
    db.commit()
    return {"ok": True, **out}


@router.get("/admin/api/bans")
def list_bans(db: Session = Depends(get_db)):
    rows = db.execute(select(Ban).where(Ban.active.is_(True)).order_by(Ban.created_at.desc()).limit(500)).scalars()
    return {"bans": [{"id": b.id, "kind": b.kind, "value": b.value, "reason": b.reason,
                      "expires_at": b.expires_at.isoformat() if b.expires_at else None} for b in rows]}


ADMIN_STATIC = Path(__file__).resolve().parent / "static"
