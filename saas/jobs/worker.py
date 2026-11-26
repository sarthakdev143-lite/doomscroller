"""The job worker: one job at a time per process (metering keeps a process-wide fallback meter).

    python -m saas.jobs.worker            # or scripts/saas_serve.py --workers N

* Claim: `UPDATE jobs SET status='fetching' ... WHERE id=:id AND status='queued'`; the rowcount
  decides, so two workers can never take the same job. Higher plan priority first, then oldest.
* Heartbeat: a thread stamps `heartbeat_at` every SAAS_JOB_HEARTBEAT_S while the engine runs.
* Stuck jobs (no heartbeat for SAAS_JOB_STUCK_MINUTES, e.g. a killed worker) go back to the queue
  until SAAS_JOB_MAX_ATTEMPTS, then fail with their credits released.
* Finishing goes through one guarded transition (`WHERE status IN running`), and the ledger allows
  one finalisation per job, so a recovered job and a late-finishing worker can't both charge.
* SIGINT/SIGTERM: stop claiming, finish (or cancel at the next stage boundary) the current job, exit.
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Optional

from sqlalchemy import or_, update
from sqlalchemy.orm import Session, sessionmaker

from ..billing import ledger
from ..common import utcnow
from ..config import settings
from ..db import SessionLocal
from ..models.core import User
from ..models.jobs import RUNNING_STATUSES, Clip, Job, Upload
from ..auth.errors import ApiError
from . import brand, service, ssrf, storage
from .engine import Engine, EngineError, JobCanceled, get_engine

log = logging.getLogger("saas.jobs.worker")

_SAFE_ERRORS = {"canceled", "source_unavailable", "source_private", "source_too_long", "no_clips", "engine_unavailable",
                "engine_crashed", "timeout", "worker_lost", "source_not_allowed", "clip_failed", "no_speech",
                "download_failed", "invalid_media", "fetch_failed", "unreadable_source",
                "no_moments", "render_failed", "media_error"}


class Worker:
    def __init__(self, engine: Optional[Engine] = None, *, session_factory: sessionmaker = SessionLocal,
                 worker_id: Optional[str] = None, stop_event: Optional[threading.Event] = None):
        self.engine = engine or get_engine()
        self.sessions = session_factory
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self.stop = stop_event or threading.Event()
        self._last_maintenance = 0.0
        self._last_retention = 0.0

    # ---- queue ------------------------------------------------------------------------------------

    def claim(self) -> Optional[str]:
        db: Session = self.sessions()
        try:
            candidates = (db.query(Job.id).filter(Job.status == "queued", Job.cancel_requested.is_(False))
                          .order_by(Job.priority.desc(), Job.created_at.asc()).limit(5).all())
            now = utcnow()
            for (job_id,) in candidates:
                res = db.execute(update(Job).where(Job.id == job_id, Job.status == "queued")
                                 .values(status="fetching", stage="fetching", worker_id=self.worker_id,
                                         claimed_at=now, heartbeat_at=now, started_at=now,
                                         attempts=Job.attempts + 1, progress=0.0))
                db.commit()
                if res.rowcount == 1:
                    return job_id
            return None
        finally:
            db.close()

    def run_one(self) -> Optional[str]:
        job_id = self.claim()
        if job_id is not None:
            self.process(job_id)
        return job_id

    # ---- one job ----------------------------------------------------------------------------------

    def _update(self, job_id: str, **values) -> int:
        db = self.sessions()
        try:
            res = db.execute(update(Job).where(Job.id == job_id, Job.status.in_(RUNNING_STATUSES)).values(**values))
            db.commit()
            return int(res.rowcount or 0)
        finally:
            db.close()

    def _cancel_requested(self, job_id: str) -> bool:
        db = self.sessions()
        try:
            row = db.query(Job.cancel_requested, Job.status).filter(Job.id == job_id).first()
            return bool(row is None or row[0] or row[1] not in RUNNING_STATUSES)
        finally:
            db.close()

    def process(self, job_id: str) -> None:
        db = self.sessions()
        try:
            job = db.get(Job, job_id)
            user = db.get(User, job.user_id) if job else None
            if job is None or user is None:
                return
            source = self._source(db, job)
            jdir = storage.job_dir(job.user_id, job.id)
            out_dir = jdir / "out"
            storage.remove_tree(out_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            memes = service.recent_memes(db, job.user_id)
            options = dict(job.options or {})
            watermark = settings.brand_name if job.watermark else None
            logo_path, custom_memes = brand.engine_assets(db, job.user_id, options)
            started = time.monotonic()
        except EngineError as exc:
            db.close()
            self._fail(job_id, exc.code, exc.message)
            return
        except ApiError as exc:  # e.g. the source host now resolves to a private address
            db.close()
            self._fail(job_id, exc.code, exc.message)
            return
        except Exception:
            db.close()
            log.exception("job %s could not start", job_id)
            self._fail(job_id, "engine_crashed", "Something went wrong while starting your job.")
            return
        db.close()

        hb_stop = threading.Event()
        hb = threading.Thread(target=self._heartbeat, args=(job_id, hb_stop, started), daemon=True,
                              name=f"hb-{job_id[-6:]}")
        hb.start()

        def on_stage(stage: str, progress: float) -> None:
            if stage not in RUNNING_STATUSES:
                stage = "rendering"
            values = {"stage": stage, "progress": max(0.0, min(1.0, float(progress))), "heartbeat_at": utcnow()}
            if stage in RUNNING_STATUSES:
                values["status"] = stage
            self._update(job_id, **values)
            if self._cancel_requested(job_id):
                raise JobCanceled()

        try:
            result = self.engine.run(source=source, out_dir=out_dir, options=options, brand_name=None,
                                     watermark_text=watermark, recent_memes=memes, on_stage=on_stage,
                                     logo_path=logo_path, custom_memes=custom_memes)
            self._succeed(job_id, result, out_dir)
        except JobCanceled:
            self._finish_without_clips(job_id, "canceled", "canceled", "Canceled.")
        except EngineError as exc:
            self._fail(job_id, exc.code, exc.message)
        except Exception:
            log.exception("job %s crashed", job_id)
            self._fail(job_id, "engine_crashed", "Something went wrong while making your clips.")
        finally:
            hb_stop.set()
            hb.join(timeout=5)
            storage.remove_tree(out_dir.parent / "work")

    def _source(self, db: Session, job: Job) -> str:
        if job.source_kind == "url":
            return ssrf.validate_url(job.source_url or "")  # re-checked: DNS may have changed since creation
        up = db.query(Upload).filter(Upload.id == job.upload_id, Upload.user_id == job.user_id).one_or_none()
        path = storage.resolve(up.path) if up is not None else None
        if path is None or not path.is_file():
            raise EngineError("source_unavailable", "The uploaded file is gone. Upload it again.")
        return str(path)

    def _heartbeat(self, job_id: str, stop: threading.Event, started: float) -> None:
        limit = settings.job_timeout_minutes * 60
        while not stop.wait(settings.job_heartbeat_s):
            try:
                self._update(job_id, heartbeat_at=utcnow())
            except Exception:
                log.exception("heartbeat failed for %s", job_id)
            if time.monotonic() - started > limit:
                log.error("job %s exceeded %s minutes; failing it and restarting the worker", job_id,
                          settings.job_timeout_minutes)
                self._fail(job_id, "timeout", "Your job took too long and was stopped. Your credits were returned.")
                if os.environ.get("SAAS_WORKER_EXIT_ON_TIMEOUT", "1") == "1":
                    os._exit(3)  # the engine thread can't be interrupted; the supervisor restarts us
                return

    # ---- endings ----------------------------------------------------------------------------------

    def _succeed(self, job_id: str, result: dict, out_dir: Path) -> None:
        clips_in = result.get("clips") if isinstance(result, dict) else None
        if not clips_in:
            self._fail(job_id, "no_clips", "We couldn't find any good moments in that video. Your credits were returned.",
                       meter=(result or {}).get("meter"))
            return
        db = self.sessions()
        try:
            job = db.get(Job, job_id)
            if job is None or job.status not in RUNNING_STATUSES:
                return
            rows = []
            for i, c in enumerate(clips_in):
                vid = Path(str(c.get("path", "")))
                if not vid.is_file() or not storage.within(vid, out_dir):
                    log.error("job %s: engine returned a path outside its folder: %s", job_id, vid)
                    continue
                thumb = Path(str(c.get("thumbnail_path") or ""))
                thumb_rel = storage.relative(thumb) if c.get("thumbnail_path") and thumb.is_file() and \
                    storage.within(thumb, out_dir) else None
                rows.append(Clip(job_id=job.id, user_id=job.user_id, idx=i, path=storage.relative(vid),
                                 thumbnail_path=thumb_rel, size_bytes=vid.stat().st_size,
                                 duration_s=float(c.get("duration_s") or 0), start_s=float(c.get("start_s") or 0),
                                 end_s=float(c.get("end_s") or 0), title=str(c.get("title") or "")[:200],
                                 description=str(c.get("description") or "")[:5000],
                                 hashtags=[str(h)[:60] for h in (c.get("hashtags") or [])][:30],
                                 score=float(c.get("score") or 0), language=str(c.get("language") or "")[:8],
                                 memes_used=[str(m)[:200] for m in (c.get("memes_used") or [])][:20]))
            if not rows:
                db.close()
                self._fail(job_id, "no_clips", "We couldn't produce clips from that video. Your credits were returned.")
                return
            src = result.get("source") or {}
            try:
                actual_s = float(src.get("duration_s") or 0)
            except (TypeError, ValueError):
                actual_s = 0.0
            charge = job.credits_reserved
            if actual_s > 0:
                charge = min(job.credits_reserved, service.credits_for(actual_s))
            shorts = min(len(rows), job.shorts_reserved) if job.funding == "free" else 0
            now = utcnow()
            res = db.execute(update(Job).where(Job.id == job_id, Job.status.in_(RUNNING_STATUSES))
                             .values(status="done", stage="done", progress=1.0, finished_at=now, credits_charged=charge,
                                     shorts_charged=shorts,
                                     meter=_safe_meter(result.get("meter")),
                                     source_title=(str(src.get("title"))[:300] if src.get("title") else job.source_title)))
            if res.rowcount != 1:
                db.rollback()
                return
            for r in rows:
                db.add(r)
            ledger.settle(db, job_id, charge, shorts=len(rows))
            db.commit()
        except Exception:
            db.rollback()
            log.exception("job %s: saving results failed", job_id)
            db.close()
            self._fail(job_id, "engine_crashed", "Something went wrong while saving your clips.")
            return
        finally:
            db.close()

    def _finish_without_clips(self, job_id: str, status: str, code: str, message: str,
                              meter: Optional[dict] = None) -> None:
        db = self.sessions()
        try:
            now = utcnow()
            res = db.execute(update(Job).where(Job.id == job_id, Job.status.in_(RUNNING_STATUSES + ("queued",)))
                             .values(status=status, stage=status, finished_at=now, credits_charged=0,
                                     error_code=code, error_message=message[:500], meter=_safe_meter(meter)))
            if res.rowcount == 1:
                ledger.release(db, job_id, note="Job canceled" if status == "canceled" else "Job failed")
            db.commit()
        except Exception:
            db.rollback()
            log.exception("job %s: could not record %s", job_id, status)
        finally:
            db.close()

    def _fail(self, job_id: str, code: str, message: str, meter: Optional[dict] = None) -> None:
        if code not in _SAFE_ERRORS:
            log.info("job %s failed with engine code %s", job_id, code)
        self._finish_without_clips(job_id, "failed", code[:40], message, meter)

    # ---- maintenance ------------------------------------------------------------------------------

    def recover_stuck(self) -> dict:
        db = self.sessions()
        out = {"requeued": 0, "failed": 0}
        try:
            cutoff = utcnow() - timedelta(minutes=settings.job_stuck_minutes)
            stuck = (db.query(Job).filter(Job.status.in_(RUNNING_STATUSES),
                                          or_(Job.heartbeat_at.is_(None), Job.heartbeat_at < cutoff)).limit(100).all())
            for job in stuck:
                old_hb = job.heartbeat_at
                cond = [Job.id == job.id, Job.status.in_(RUNNING_STATUSES)]
                cond.append(Job.heartbeat_at.is_(None) if old_hb is None else Job.heartbeat_at == old_hb)
                if job.attempts < settings.job_max_attempts and not job.cancel_requested:
                    res = db.execute(update(Job).where(*cond).values(status="queued", stage="queued", worker_id=None,
                                                                     progress=0.0))
                    out["requeued"] += int(res.rowcount or 0)
                else:
                    res = db.execute(update(Job).where(*cond).values(
                        status="failed", stage="failed", finished_at=utcnow(), credits_charged=0,
                        error_code="worker_lost", error_message="Processing stopped unexpectedly. Your credits were returned."))
                    if res.rowcount == 1:
                        ledger.release(db, job.id, note="Worker lost the job")
                        out["failed"] += 1
                db.commit()
        except Exception:
            db.rollback()
            log.exception("stuck-job recovery failed")
        finally:
            db.close()
        return out

    def maintenance(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last_maintenance >= 60:
            self._last_maintenance = now
            self.recover_stuck()
            db = self.sessions()
            try:
                from ..billing import service as billing

                billing.grant_due_subscription_credits(db)
                billing.expire_ended_subscriptions(db)
                billing.release_stale_promo_holds(db)
                db.commit()
            except Exception:
                db.rollback()
                log.exception("billing maintenance failed")
            finally:
                db.close()
        if force or now - self._last_retention >= 3600:
            self._last_retention = now
            db = self.sessions()
            try:
                service.cleanup_retention(db)
            except Exception:
                db.rollback()
                log.exception("retention cleanup failed")
            finally:
                db.close()

    def run_forever(self) -> None:
        log.info("worker %s started (engine=%s)", self.worker_id, getattr(self.engine, "name", "?"))
        while not self.stop.is_set():
            try:
                self.maintenance()
                if self.run_one() is None:
                    self.stop.wait(settings.worker_poll_s)
            except Exception:
                log.exception("worker loop error")
                self.stop.wait(5)
        log.info("worker %s stopped", self.worker_id)


def _safe_meter(meter) -> dict:
    return meter if isinstance(meter, dict) else {}


def main(argv: Optional[list[str]] = None) -> int:
    os.environ.setdefault("DOOMSCROLLER_ENV_FILE", "")  # before anything can import app.config
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from ..db import init_db

    init_db()
    stop = threading.Event()

    def _stop(_sig, _frame) -> None:
        log.info("shutdown requested; finishing the current job")
        stop.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _stop)  # Windows console close / CTRL_BREAK
    if os.environ.get("SAAS_WORKER_WATCH_STDIN") == "1":
        def _watch_parent() -> None:
            import sys

            try:
                sys.stdin.buffer.read()  # blocks until the supervisor's end of the pipe closes
            except Exception:
                pass
            log.info("supervisor gone; stopping after the current job")
            stop.set()

        threading.Thread(target=_watch_parent, daemon=True, name="parent-watch").start()
    Worker(stop_event=stop).run_forever()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
