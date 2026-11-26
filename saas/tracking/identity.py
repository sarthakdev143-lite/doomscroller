"""The first-party visitor id and the links from it to waitlist entries and users.

`vid` is an HttpOnly cookie the server sets (from `GET /api/t/config`) only once analytics are
allowed for the browser. Server-set rather than `document.cookie`, so Safari's 7-day cap on
script-written cookies does not reset returning visitors every week.
"""
from __future__ import annotations

import re
import secrets

from fastapi import Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..common import utcnow
from ..config import settings
from ..models.tracking import Visitor, VisitorLink

COOKIE = "vid"
COOKIE_MAX_AGE = 400 * 24 * 3600  # the most browsers allow
_VID_RE = re.compile(r"^v_[a-f0-9]{32}$")


def new_visitor_id() -> str:
    return "v_" + secrets.token_hex(16)


def valid(vid: object) -> bool:
    return isinstance(vid, str) and bool(_VID_RE.match(vid))


def visitor_id(request: Request) -> str | None:
    """The browser's visitor id from its cookie, or None (no consent yet, cookies off, forged)."""
    vid = request.cookies.get(COOKIE)
    return vid if valid(vid) else None


def set_cookie(response: Response, vid: str) -> None:
    response.set_cookie(COOKIE, vid, max_age=COOKIE_MAX_AGE, path="/", samesite="lax",
                        secure=settings.is_prod, httponly=True)


def clear_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE, path="/")


def link(db: Session, visitor_id: str | None, *, user_id: str | None = None,
         waitlist_id: str | None = None) -> bool:
    """Tie a visitor to a user and/or waitlist entry. Idempotent; never raises; caller commits.

    The core agent calls this on signup and login so the admin panel shows a user's whole journey,
    across every device they used.
    """
    if not valid(visitor_id) or not (user_id or waitlist_id):
        return False
    try:
        exists = db.execute(select(VisitorLink.id).where(
            VisitorLink.visitor_id == visitor_id,
            VisitorLink.user_id.is_(None) if user_id is None else VisitorLink.user_id == user_id,
            VisitorLink.waitlist_id.is_(None) if waitlist_id is None else VisitorLink.waitlist_id == waitlist_id,
        )).first()
        pending = any(isinstance(o, VisitorLink) and (o.visitor_id, o.user_id, o.waitlist_id)
                      == (visitor_id, user_id, waitlist_id) for o in db.new)
        if not exists and not pending:
            db.add(VisitorLink(visitor_id=visitor_id, user_id=user_id, waitlist_id=waitlist_id, created_at=utcnow()))
        v = db.get(Visitor, visitor_id)
        if v is not None:
            if user_id:
                v.user_id = user_id
            if waitlist_id:
                v.waitlist_id = waitlist_id
            from . import scoring

            scoring.rescore(v)
        # No flush here: a flush failure would poison the caller's session. Their commit writes it.
        return True
    except Exception:
        return False


def visitors_for(db: Session, *, user_id: str | None = None, waitlist_id: str | None = None) -> list[str]:
    q = select(VisitorLink.visitor_id)
    if user_id:
        q = q.where(VisitorLink.user_id == user_id)
    elif waitlist_id:
        q = q.where(VisitorLink.waitlist_id == waitlist_id)
    else:
        return []
    return list(dict.fromkeys(db.execute(q).scalars().all()))
