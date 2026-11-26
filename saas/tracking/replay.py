"""Session replay chunks (rrweb) - stored only with consent, for sampled visitors, inputs masked.

The server enforces what t.js is asked to do, so a modified client cannot plant recordings:
- analytics consent (granted, or implied in notice regions) for this request *and* on the visitor row;
- the visitor is sampled: a deterministic hash of the visitor id below `replay_sample_rate`;
- the page view belongs to that visitor; bots are never recorded;
- each chunk is <= 1 MB on the wire and <= 8 MB inflated (gzip bombs are cut off mid-stream), a
  JSON array of rrweb events; meta events have their URL reduced to a redacted path;
- at most `replay_max_session_mb` compressed per session and 500 chunks per page.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import zlib

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..common import utcnow
from ..config import settings
from ..models.tracking import PageView, ReplayChunk, TrackSession, Visitor
from .ingest import clean_path

MAX_INFLATED = 8 * 1024 * 1024
MAX_EVENTS_PER_CHUNK = 50_000
MAX_CHUNKS_PER_PAGE = 500

RECORD_JS = "/ts/vendor/rrweb/rrweb-record-2.2.2.min.js"
PLAYER_JS = "/ts/vendor/rrweb/rrweb-player-2.2.2.min.js"
PLAYER_CSS = "/ts/vendor/rrweb/rrweb-player-2.2.2.min.css"


class ReplayRejected(Exception):
    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code = code
        self.status = status


def sampled(visitor_id: str, rate: float | None = None) -> bool:
    rate = settings.replay_sample_rate if rate is None else rate
    if rate <= 0:
        return False
    if rate >= 1:
        return True
    h = int(hashlib.sha256(visitor_id.encode()).hexdigest()[:8], 16)
    return h / 0xFFFFFFFF < rate


def inflate(body: bytes, limit: int = MAX_INFLATED) -> bytes:
    """gzip or raw bytes -> raw bytes, refusing anything that inflates past `limit`."""
    if body[:2] != b"\x1f\x8b":
        if len(body) > limit:
            raise ReplayRejected("too_large", 413)
        return body
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        out = d.decompress(body, limit + 1)
    except zlib.error:
        raise ReplayRejected("bad_gzip") from None
    if len(out) > limit or d.unconsumed_tail:
        raise ReplayRejected("too_large", 413)
    return out


def _redact_events(events: list) -> list:
    for ev in events:
        # rrweb EventType.Meta == 4: {"href", "width", "height"}. Full URLs can carry reset tokens.
        if isinstance(ev, dict) and ev.get("type") == 4 and isinstance(ev.get("data"), dict):
            href = ev["data"].get("href")
            if isinstance(href, str):
                ev["data"]["href"] = clean_path(href)
    return events


def parse_chunk(raw: bytes) -> tuple[list, int | None, int | None]:
    try:
        events = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise ReplayRejected("bad_json") from None
    if not isinstance(events, list) or not events or len(events) > MAX_EVENTS_PER_CHUNK:
        raise ReplayRejected("bad_events")
    stamps = []
    for ev in events:
        if not isinstance(ev, dict) or not isinstance(ev.get("type"), int) \
                or not isinstance(ev.get("timestamp"), (int, float)):
            raise ReplayRejected("bad_events")
        stamps.append(int(ev["timestamp"]))
    return _redact_events(events), min(stamps), max(stamps)


def store_chunk(db: Session, *, visitor_id: str, page_id: str, seq: int, body: bytes) -> ReplayChunk:
    v = db.get(Visitor, visitor_id)
    if v is None or v.consent not in ("granted", "implied"):
        raise ReplayRejected("no_consent", 403)
    if v.is_bot:
        raise ReplayRejected("bot", 403)
    if not sampled(visitor_id):
        raise ReplayRejected("not_sampled", 403)
    pv = db.get(PageView, page_id)
    if pv is None or pv.visitor_id != visitor_id:
        raise ReplayRejected("unknown_page", 404)
    if not 0 <= seq < MAX_CHUNKS_PER_PAGE:
        raise ReplayRejected("bad_seq")
    sess = db.get(TrackSession, pv.session_id)
    cap = settings.replay_max_session_mb * 1024 * 1024
    if sess is not None and (sess.replay_bytes or 0) >= cap:
        raise ReplayRejected("session_full", 413)

    raw = inflate(body)
    events, first_ts, last_ts = parse_chunk(raw)
    payload = json.dumps(events, separators=(",", ":")).encode()
    gz = gzip.compress(payload, compresslevel=6)

    existing = db.execute(select(ReplayChunk).where(ReplayChunk.page_id == page_id, ReplayChunk.seq == seq)
                          ).scalar_one_or_none()
    if existing is not None:
        return existing  # a retried upload: first copy wins
    chunk = ReplayChunk(visitor_id=visitor_id, session_id=pv.session_id, page_id=page_id, seq=seq,
                        created_at=utcnow(), first_ts=first_ts, last_ts=last_ts, n_events=len(events),
                        size_raw=len(payload), size_gz=len(gz), data=gz)
    db.add(chunk)
    pv.has_replay = True
    if sess is not None:
        sess.has_replay = True
        sess.replay_bytes = (sess.replay_bytes or 0) + len(gz)
    db.commit()
    return chunk


def load_events(db: Session, page_id: str) -> list:
    rows = db.execute(select(ReplayChunk).where(ReplayChunk.page_id == page_id).order_by(ReplayChunk.seq)).scalars()
    out: list = []
    for r in rows:
        try:
            out.extend(json.loads(gzip.decompress(r.data)))
        except Exception:
            continue
    out.sort(key=lambda e: e.get("timestamp", 0) if isinstance(e, dict) else 0)
    return out


def recordings_for_session(db: Session, session_id: str) -> list[dict]:
    rows = db.execute(
        select(ReplayChunk.page_id, func.count(ReplayChunk.id), func.sum(ReplayChunk.size_gz),
               func.min(ReplayChunk.first_ts), func.max(ReplayChunk.last_ts))
        .where(ReplayChunk.session_id == session_id).group_by(ReplayChunk.page_id)
    ).all()
    out = []
    for page_id, n, size, first, last in rows:
        pv = db.get(PageView, page_id)
        out.append({"page_id": page_id, "chunks": n, "bytes": int(size or 0),
                    "duration_s": max(0, int(((last or 0) - (first or 0)) / 1000)),
                    "path": pv.path if pv else None, "started_at": pv.started_at if pv else None})
    out.sort(key=lambda r: r["started_at"] or utcnow())
    return out
