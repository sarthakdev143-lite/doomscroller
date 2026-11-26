"""Retention: raw events `event_retention_days` (90), replays `replay_retention_days` (30).

Sessions and page views go with the raw events (they carry IPs). Visitors are deleted 400 days after
they were last seen unless linked to a user or waitlist entry (the vid cookie lives 400 days, so an
older row could never be matched again anyway). Security events and seen-signals: 365 days.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import and_, delete, update
from sqlalchemy.orm import Session

from ..common import utcnow
from ..config import settings
from ..models.security import SecurityEvent, SeenSignal
from ..models.tracking import PageView, ReplayChunk, TrackEvent, TrackSession, Visitor

VISITOR_DAYS = 400
SECURITY_DAYS = 365


def cleanup(db: Session) -> dict:
    now = utcnow()
    ev_cut = now - timedelta(days=settings.event_retention_days)
    rp_cut = now - timedelta(days=settings.replay_retention_days)
    out = {}
    out["replay_chunks"] = db.execute(delete(ReplayChunk).where(ReplayChunk.created_at < rp_cut)).rowcount or 0
    db.execute(update(PageView).where(PageView.has_replay.is_(True), PageView.started_at < rp_cut)
               .values(has_replay=False))
    db.execute(update(TrackSession).where(TrackSession.has_replay.is_(True), TrackSession.started_at < rp_cut)
               .values(has_replay=False))
    out["events"] = db.execute(delete(TrackEvent).where(TrackEvent.ts < ev_cut)).rowcount or 0
    out["pageviews"] = db.execute(delete(PageView).where(PageView.started_at < ev_cut)).rowcount or 0
    out["sessions"] = db.execute(delete(TrackSession).where(TrackSession.last_seen_at < ev_cut)).rowcount or 0
    out["visitors"] = db.execute(delete(Visitor).where(and_(
        Visitor.last_seen_at < now - timedelta(days=VISITOR_DAYS),
        Visitor.user_id.is_(None), Visitor.waitlist_id.is_(None)))).rowcount or 0
    sec_cut = now - timedelta(days=SECURITY_DAYS)
    out["security_events"] = db.execute(delete(SecurityEvent).where(SecurityEvent.created_at < sec_cut)).rowcount or 0
    out["seen_signals"] = db.execute(delete(SeenSignal).where(SeenSignal.last_seen < sec_cut)).rowcount or 0
    db.flush()
    return out


def forget_visitor(db: Session, visitor_id: str) -> dict:
    """Erase a visitor's behavioural data (consent withdrawn, or an erasure request)."""
    out = {
        "replay_chunks": db.execute(delete(ReplayChunk).where(ReplayChunk.visitor_id == visitor_id)).rowcount or 0,
        "events": db.execute(delete(TrackEvent).where(TrackEvent.visitor_id == visitor_id)).rowcount or 0,
        "pageviews": db.execute(delete(PageView).where(PageView.visitor_id == visitor_id)).rowcount or 0,
        "sessions": db.execute(delete(TrackSession).where(TrackSession.visitor_id == visitor_id)).rowcount or 0,
    }
    db.flush()
    return out
