"""Housekeeping for the tracking and security tables, plus threat-feed refresh.

`run_once()` is safe to call from anywhere (a worker, a cron, the admin panel). The middleware also
starts `start()` lazily on the first request: an hourly daemon thread. It never runs under pytest.
"""
from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger("saas.security.maintenance")

_thread: threading.Thread | None = None
_stop = threading.Event()


def run_once() -> dict:
    from ..db import session_scope
    from ..tracking import retention
    from . import limits, netintel

    out: dict = {}
    try:
        with session_scope() as db:
            out["tracking"] = retention.cleanup(db)
    except Exception as exc:
        log.warning("tracking retention failed: %s", exc)
        out["tracking_error"] = type(exc).__name__
    out["rate_counters_deleted"] = limits.limiter.cleanup()
    if netintel.network_allowed():
        out["tor_exits"] = netintel.refresh_tor_list()
    return out


def _loop(interval_s: float) -> None:
    # First pass shortly after boot, then every interval.
    if _stop.wait(30):
        return
    while not _stop.is_set():
        try:
            log.info("maintenance: %s", run_once())
        except Exception as exc:  # pragma: no cover
            log.warning("maintenance pass failed: %s", exc)
        if _stop.wait(interval_s):
            return


def start(interval_s: float = 3600.0) -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, args=(interval_s,), name="saas-security-maintenance", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()
    time.sleep(0)
