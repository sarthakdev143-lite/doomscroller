"""Sliding-window rate limits shared by every worker through the database.

Algorithm: the sliding-window *counter* (two fixed windows, the previous one weighted by how much of
it still overlaps the sliding window). One row per (bucket, key, window) in `rate_counters`, bumped
with an atomic upsert, so the count survives restarts and is shared across processes.

Two modes:
- strict (`sync_s=0`, the default for `enforce`/`hit`): every hit goes to the DB. For sensitive,
  low-volume buckets: signups, logins, waitlist joins, password resets.
- write-behind (`sync_s>0`, the middleware's global per-IP limit): hits are counted in-process and
  flushed to the DB at most every `sync_s` seconds per key, pulling the other workers' total back.
  The decision never waits on I/O it does not need.

The in-process fast path: a key the DB has already put over its limit is refused locally until the
window slides, without touching the DB again. If the DB is unreachable the limiter fails open on
the DB but keeps enforcing with its local counts.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from fastapi import HTTPException, Request

from ..common import client_ip

log = logging.getLogger("saas.security.limits")

_MAX_KEYS = 50_000


class RateLimited(HTTPException):
    """429 with the contract's error shape. `install(app)` registers the JSON handler for it; as an
    HTTPException subclass it still becomes a 429 in an app that never installed the handler."""

    def __init__(self, bucket: str, retry_after: int):
        self.bucket = bucket
        self.retry_after = max(1, int(retry_after))
        super().__init__(status_code=429, detail={"error": "rate_limited",
                                                  "message": "Too many requests. Slow down and try again shortly."},
                         headers={"Retry-After": str(self.retry_after)})


@dataclass
class _KeyState:
    window_start: int
    prev: float = 0.0  # previous window's total (DB view when synced, else local)
    cur_db: int = 0  # current window total in the DB as of the last sync (includes our flushed hits)
    pending: int = 0  # local hits not yet flushed
    last_sync: float = 0.0
    blocked_until: float = 0.0


class Limiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: OrderedDict[tuple[str, str, int], _KeyState] = OrderedDict()
        self._last_cleanup = 0.0

    def reset(self) -> None:
        with self._lock:
            self._state.clear()

    # -- internals -------------------------------------------------------------------------------
    @staticmethod
    def _estimate(st: _KeyState, now: float, window_s: int) -> float:
        elapsed = now - st.window_start
        weight = max(0.0, (window_s - elapsed) / window_s)
        return st.prev * weight + st.cur_db + st.pending

    def _get_state(self, sk: tuple[str, str, int], ws: int, window_s: int
                   ) -> tuple[_KeyState, tuple[int, int] | None]:
        """State for the current window, plus (old_window, pending) still owed to the DB, if any."""
        st = self._state.get(sk)
        owed = None
        if st is None:
            st = _KeyState(window_start=ws)
            self._state[sk] = st
            if len(self._state) > _MAX_KEYS:
                self._state.popitem(last=False)
        elif st.window_start != ws:
            adjacent = st.window_start == ws - window_s
            if st.pending:
                owed = (st.window_start, st.pending)
            st = _KeyState(window_start=ws, prev=float(st.cur_db + st.pending) if adjacent else 0.0,
                           blocked_until=st.blocked_until)
            self._state[sk] = st
        self._state.move_to_end(sk)
        return st, owed

    def _sync(self, bucket: str, key: str, st: _KeyState, window_s: int, now: float) -> None:
        from ..db import engine
        from . import dbutil

        delta = st.pending
        try:
            with engine.begin() as conn:
                if delta:
                    st.cur_db = dbutil.add_to_counter(conn, bucket, key, st.window_start, delta,
                                                      st.window_start + 2 * window_s)
                else:
                    st.cur_db = dbutil.read_counter(conn, bucket, key, st.window_start)
                st.prev = float(dbutil.read_counter(conn, bucket, key, st.window_start - window_s))
            st.pending -= delta
            st.last_sync = now
        except Exception as exc:  # DB down or table missing: keep counting locally
            log.debug("rate limiter DB sync failed for %s: %s", bucket, exc)
            st.last_sync = now

    def _flush_old(self, bucket: str, key: str, owed: tuple[int, int], window_s: int) -> None:
        from ..db import engine
        from . import dbutil

        window, pending = owed
        try:
            with engine.begin() as conn:
                dbutil.add_to_counter(conn, bucket, key, window, pending, window + 2 * window_s)
        except Exception as exc:
            log.debug("rate limiter flush failed for %s: %s", bucket, exc)

    # -- public ----------------------------------------------------------------------------------
    def hit(self, bucket: str, key: str, limit: int, window_s: int, *, sync_s: float = 0.0,
            cost: int = 1, now: float | None = None) -> tuple[bool, int]:
        """Count one hit. Returns (allowed, retry_after_seconds)."""
        if limit <= 0:
            return True, 0
        now = time.time() if now is None else now
        window_s = max(1, int(window_s))
        key = (key or "-")[:160]
        ws = int(now // window_s) * window_s
        sk = (bucket, key, window_s)
        with self._lock:
            st, owed = self._get_state(sk, ws, window_s)
            if owed is not None:
                self._flush_old(bucket, key, owed, window_s)
            st.pending += cost
            if st.blocked_until > now:
                # Fast path: already over the limit; the DB has nothing to add to that answer.
                if sync_s and now - st.last_sync >= max(sync_s, 5.0):
                    self._sync(bucket, key, st, window_s, now)
                return False, max(1, math.ceil(st.blocked_until - now))
            if not sync_s or now - st.last_sync >= sync_s or self._estimate(st, now, window_s) > limit:
                self._sync(bucket, key, st, window_s, now)
            est = self._estimate(st, now, window_s)
            if est <= limit:
                return True, 0
            # Over. If this window alone is full, wait for the next one; otherwise wait until the
            # previous window's weight has decayed enough to admit one more hit.
            cur = st.cur_db + st.pending
            if cur > limit:
                # Next window starts with this one as its fully weighted "previous" window.
                wait = (ws + window_s - now) + window_s * (1 - limit / cur)
            elif st.prev > 0:
                needed_weight = (limit - cur) / st.prev
                wait = window_s * (1 - needed_weight) - (now - ws)
            else:
                wait = (ws + window_s) - now
            wait = max(1.0, min(2.0 * window_s, wait))
            st.blocked_until = now + wait
            return False, max(1, math.ceil(wait))

    def cleanup(self) -> int:
        """Delete expired windows. Cheap; safe to call often."""
        from sqlalchemy import delete

        from ..db import engine
        from ..models.security import RateCounter

        try:
            with engine.begin() as conn:
                res = conn.execute(delete(RateCounter).where(RateCounter.expires_at < int(time.time())))
                return int(res.rowcount or 0)
        except Exception as exc:
            log.warning("rate counter cleanup failed: %s", exc)
            return 0


limiter = Limiter()


def reset_state() -> None:
    limiter.reset()


def _key_for(request: Request | None, key: str | None) -> str:
    if key:
        return key
    return client_ip(request) if request is not None else "-"


def hit(request: Request | None, bucket: str, limit: int, window_s: int, key: str | None = None) -> bool:
    """Non-raising: True when this hit is within the limit."""
    allowed, _ = limiter.hit(bucket, _key_for(request, key), limit, window_s)
    return allowed


def enforce(request: Request | None, bucket: str, limit: int, window_s: int, key: str | None = None) -> None:
    """Raise 429 `{"error": "rate_limited"}` when over. Key defaults to the client IP.

    Call it before the route writes anything: the DB counter uses its own short transaction.
    """
    allowed, retry = limiter.hit(bucket, _key_for(request, key), limit, window_s)
    if not allowed:
        _note_limited(request, bucket, key)
        raise RateLimited(bucket, retry)


_noted: dict[tuple[str, str], float] = {}
_noted_lock = threading.Lock()


def _note_limited(request: Request | None, bucket: str, key: str | None) -> None:
    """One `rate_limited` security event per (bucket, key) per 10 minutes, not one per refused hit."""
    k = (bucket, _key_for(request, key))
    now = time.time()
    with _noted_lock:
        if _noted.get(k, 0) > now - 600:
            return
        _noted[k] = now
        if len(_noted) > 10_000:
            _noted.clear()
    try:
        from ..db import session_scope
        from . import events

        ip = client_ip(request) if request is not None else None
        with session_scope() as db:
            events.log(db, "rate_limited", "low", ip=ip, detail={"bucket": bucket, "key": k[1][:64]},
                       path=request.url.path if request is not None else None)
    except Exception as exc:
        log.debug("could not record rate_limited event: %s", exc)
