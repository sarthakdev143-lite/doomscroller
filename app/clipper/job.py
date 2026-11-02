"""The whole clipper run, and the queue that lets it run unattended."""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.config import settings
from app.db import session_scope
from app.models import ClipJob, Video, VideoStatus

from .broll import StockSession, credit_line
from .edit import attach_stock, build_edit
from .ingest import IngestError, SourceVideo, fetch
from .media import Interrupted, MediaError, probe, require_tools
from .moments import Moment, clashes, pick_speech, pick_visual
from .render import HEIGHT, WIDTH, render_moment
from .signals import analyse
from .transcribe import TranscribeError, transcribe

log = logging.getLogger(__name__)

Progress = Callable[[str], None]

# Rendering is CPU-heavy, so one job at a time. Holding this also proves that any
# row still marked running was orphaned by a crash or restart.
_queue_lock = threading.Lock()
# A job stopped this many times is probably what is stopping things.
_MAX_ATTEMPTS = 3
_FINISH_RETRY_SECONDS = (1, 3, 10)


def _clip_id(src: SourceVideo, moment: Moment) -> str:
    return f"{src.source_id}_{int(moment.start)}_{int(moment.end)}"


def _find_row(db, src: SourceVideo, moment: Moment) -> Optional[Video]:
    return db.execute(
        select(Video).where(Video.source == src.source, Video.source_id == _clip_id(src, moment))
    ).scalar_one_or_none()


def _earlier_clips(src: SourceVideo) -> list[tuple[str, Moment]]:
    """Clips already made from this source. A rejected one counts: that footage was seen."""
    with session_scope() as db:
        rows = db.execute(
            select(Video).where(
                Video.source == src.source,
                Video.source_id.startswith(f"{src.source_id}_", autoescape=True),
                Video.status != VideoStatus.FAILED,
            )
        ).scalars().all()
        earlier = []
        for row in rows:
            meta = row.meta or {}
            if meta.get("clip_parent") == src.source_id and "clip_start" in meta:
                earlier.append((row.source_id, Moment(meta["clip_start"], meta["clip_end"])))
        return earlier


def _existing_row(src: SourceVideo, moment: Moment) -> tuple[Optional[VideoStatus], Optional[str], Optional[dict]]:
    """Status, id and saved summary of the row for this exact moment, if any."""
    with session_scope() as db:
        row = _find_row(db, src, moment)
        if row is None:
            return None, None, None
        path = row.transformed_path
        return row.status, row.id, {
            "path": path, "exists": bool(path and os.path.exists(path)),
            "title": row.title, "duration": row.duration_sec,
        }


def _save_clip(src: SourceVideo, moment: Moment, path: str, duration: float, *, language: str,
               auto_queue: bool, credit: str = "") -> tuple[str, VideoStatus]:
    """Record a finished render without undoing anything a person decided."""
    now = datetime.now(timezone.utc)
    description = f"{moment.description or src.description[:2000]} {credit}".strip()
    # Empty unless the AI wrote one, so the metadata step falls back to the source's.
    written = f"{moment.description} {credit}".strip() if moment.description else ""
    with session_scope() as db:
        video = _find_row(db, src, moment)
        if video is not None and video.status not in (VideoStatus.FAILED, VideoStatus.PENDING):
            if video.status not in (VideoStatus.REJECTED, VideoStatus.PUBLISHED):
                # A lost file was re-rendered: the status stands, but the credit and
                # length must describe the file that exists now.
                video.local_path = path
                video.transformed_path = path
                video.duration_sec = int(round(duration))
                video.description = description
                video.meta = {**(video.meta or {}), "llm_description": written}
            return video.id, video.status
        if video is None:
            video = Video(source=src.source, source_id=_clip_id(src, moment))
            db.add(video)
        video.source_url = src.url or Path(src.path).name
        video.author = src.author
        video.author_name = src.author_name
        video.title = moment.title or src.title or "Clip"
        video.description = description
        video.language = language
        video.keywords = list(moment.tags) + list(src.keywords)
        video.local_path = path
        video.transformed_path = path
        video.duration_sec = int(round(duration))
        video.width, video.height = WIDTH, HEIGHT
        video.status = VideoStatus.TRANSFORMED if auto_queue else VideoStatus.PENDING
        video.transformed_at = now if auto_queue else None
        video.error = None
        video.meta = {
            "clip_rendered": True,
            "clip_kind": moment.kind,
            "clip_start": moment.start,
            "clip_end": moment.end,
            "clip_score": moment.score,
            "clip_reason": moment.reason,
            "clip_hook": moment.hook,
            "clip_parent": src.source_id,
            "llm_title": moment.title,
            "llm_description": written,
        }
        db.flush()
        return video.id, video.status


def clip_source(source: str | SourceVideo, *, count: Optional[int] = None,
                auto_queue: Optional[bool] = None, progress: Optional[Progress] = None) -> dict:
    """Turn one long video into finished clips.

    Never raises for a bad input or a failed clip, and keeps any clips already
    made. Only :class:`Interrupted` escapes, because shutting down is not a failure.
    """
    from tasks.pipeline import generate_metadata_for_video

    report = progress or (lambda stage: log.info("clipper: %s", stage))
    count = count or settings.max_clips_per_video
    auto_queue = settings.clip_auto_queue if auto_queue is None else auto_queue
    warnings: list[str] = []
    clips: list[dict] = []
    statuses: list[VideoStatus] = []
    label, kind = str(source), "visual"

    def outcome(ok: bool, error: Optional[str] = None) -> dict:
        result = {
            "ok": ok, "source": label, "kind": kind,
            "queued": VideoStatus.TRANSFORMED in statuses, "clips": clips, "warnings": warnings,
        }
        if error:
            result["error"] = error
        return result

    def warn(message: str) -> None:
        warnings.append(message)
        log.warning("clipper: %s", message)

    try:
        require_tools()
        report("downloading")
        src = source if isinstance(source, SourceVideo) else fetch(source)
        label = src.title or src.path
        info = probe(src.path)

        report("transcribing")
        transcript = None
        try:
            transcript = transcribe(src.path, info)
        except (TranscribeError, MediaError) as exc:
            warn(f"transcription failed, so no captions: {exc}")
        speech = transcript is not None and transcript.is_english_speech
        # A cached transcript or an audio track can run past the end of the picture.
        words = [w for w in transcript.words if w.end <= info.duration] if speech else []
        language = "en" if speech else ""
        if info.has_audio and transcript is None and not settings.deepgram_api_key:
            warn("DEEPGRAM_API_KEY is not set, so speech gets no captions")

        report("analysing")
        signals = analyse(src.path, info)

        report("picking moments")
        moments = pick_speech(words, signals, count=count, title=src.title) if speech else []
        if speech and not moments:
            warn("too little continuous speech for a speech clip, so cutting on motion and sound instead")
            speech = False
        if not speech:
            moments = pick_visual(src.path, info, signals, count=count, title=src.title)
        kind = "speech" if speech else "visual"
        if not moments:
            return outcome(False, "no usable moments found")

        # A retry or a re-paste may pick slightly different sentences for the same
        # idea. Those must not go out as a second, nearly identical clip.
        earlier = _earlier_clips(src)
        fresh = []
        for moment in moments:
            repeat = next((cid for cid, old in earlier if cid != _clip_id(src, moment) and clashes(moment, old)), None)
            if repeat:
                warn(f"skipped {moment.start:.1f}-{moment.end:.1f}s: it repeats clip {repeat}")
            else:
                fresh.append(moment)

        out_dir = settings.content_path / "clips" / f"{src.source}_{src.source_id}"
        failed = 0
        stock = StockSession()
        for number, moment in enumerate(fresh, 1):
            status, row_id, saved = _existing_row(src, moment)
            if status in (VideoStatus.REJECTED, VideoStatus.PUBLISHED):
                warn(f"clip {number} is already {status.value}; left as it is")
                continue
            if status in (VideoStatus.APPROVED, VideoStatus.TRANSFORMED, VideoStatus.PUBLISHING) and saved["exists"]:
                # Someone chose this exact file. Keep it rather than swap in a new render.
                statuses.append(status)
                clips.append({
                    "id": row_id, "path": saved["path"], "title": saved["title"], "hook": moment.hook,
                    "start": round(moment.start, 2), "end": round(moment.end, 2),
                    "duration": float(saved["duration"] or 0), "score": round(moment.score, 2),
                    "kind": moment.kind, "reason": moment.reason,
                })
                continue

            report(f"rendering clip {number} of {len(fresh)}")
            out_path = str(out_dir / f"{int(moment.start)}_{int(moment.end)}.mp4")
            try:
                edit = build_edit(moment, words, title=src.title) if moment.kind == "speech" else None
                if edit:
                    for message in attach_stock(edit, stock):
                        if message not in warnings:
                            warn(message)
                try:
                    plan = render_moment(src.path, info, signals, moment, words, out_path, edit=edit)
                except MediaError as exc:
                    if not (edit and edit.cutaways):
                        raise
                    # B-roll is optional, so a shot that will not render costs only itself.
                    warn(f"clip {number} lost its B-roll, which would not render: {exc}")
                    edit.cutaways = []
                    plan = render_moment(src.path, info, signals, moment, words, out_path, edit=edit)
                credit = ""
                if edit and edit.cutaways:
                    credit = credit_line((c.clip.author for c in edit.cutaways), edit.cutaways[0].clip.provider)
                video_id, saved_status = _save_clip(src, moment, out_path, plan.duration, language=language,
                                                    auto_queue=auto_queue, credit=credit)
            except Interrupted:
                raise
            except Exception as exc:
                failed += 1
                warn(f"clip {number} failed: {exc}")
                continue
            statuses.append(saved_status)
            try:
                generate_metadata_for_video(video_id)
            except Exception as exc:  # metadata can be redone at publish time
                log.warning("clipper: metadata failed for %s: %s", video_id, exc)
            clips.append({
                "id": video_id, "path": out_path, "title": moment.title, "hook": moment.hook,
                "start": round(moment.start, 2), "end": round(moment.end, 2),
                "duration": round(plan.duration, 1), "score": round(moment.score, 2),
                "kind": moment.kind, "reason": moment.reason,
            })
        if failed and not clips:
            return outcome(False, "every clip failed to render")
        return outcome(True)
    except Interrupted:
        raise
    except (IngestError, MediaError) as exc:
        return outcome(bool(clips), str(exc))
    except Exception as exc:
        log.exception("clipper: unexpected failure")
        return outcome(bool(clips), str(exc))


def clip_existing_video(video_id: str) -> dict:
    """Clip a video that is already on disk and in the database."""
    with session_scope() as db:
        video = db.get(Video, video_id)
        if video is None:
            return {"ok": False, "error": "video not found"}
        src = SourceVideo(
            path=video.local_path or "", title=video.title or "",
            description=video.description or "", author=video.author or "",
            author_name=video.author_name or "", url=video.source_url or "",
            source=video.source or "local", source_id=video.source_id or video.id,
            keywords=list(video.keywords or []),
        )
    if not src.path or not Path(src.path).exists():
        return {"ok": False, "error": "source file missing"}
    return clip_source(src)


def enqueue(source: str, *, count: Optional[int] = None, auto_queue: Optional[bool] = None) -> str:
    with session_scope() as db:
        job = ClipJob(source=source.strip(), count=count, auto_queue=auto_queue)
        db.add(job)
        db.flush()
        return job.id


def _set_stage(job_id: str, stage: str) -> None:
    # Only a progress label, so a locked database must not fail the job.
    try:
        with session_scope() as db:
            job = db.get(ClipJob, job_id)
            if job is not None:
                job.stage = stage
    except OperationalError as exc:
        log.warning("clipper: could not record stage %r: %s", stage, exc)


def _next_job() -> Optional[tuple[str, str, Optional[int], Optional[bool]]]:
    with session_scope() as db:
        stale = db.execute(select(ClipJob).where(ClipJob.status == "running")).scalars().all()
        for job in stale:
            job.status = "queued"
        db.flush()
        while True:
            job = db.execute(
                select(ClipJob).where(ClipJob.status == "queued").order_by(ClipJob.created_at).limit(1)
            ).scalar_one_or_none()
            if job is None:
                return None
            job.attempts = (job.attempts or 0) + 1
            if job.attempts <= _MAX_ATTEMPTS:
                job.status = "running"
                job.started_at = datetime.now(timezone.utc)
                return job.id, job.source, job.count, job.auto_queue
            job.status = "failed"
            job.error = f"stopped {_MAX_ATTEMPTS} times without finishing"
            job.finished_at = datetime.now(timezone.utc)
            db.flush()


def _finish_job(job_id: str, result: dict) -> None:
    """Record the outcome, waiting out a briefly locked database."""
    for delay in (*_FINISH_RETRY_SECONDS, None):
        try:
            with session_scope() as db:
                job = db.get(ClipJob, job_id)
                job.status = "done" if result.get("ok") else "failed"
                job.stage = ""
                job.result = result
                job.error = result.get("error")
                job.finished_at = datetime.now(timezone.utc)
            return
        except OperationalError:
            if delay is None:
                raise
            time.sleep(delay)


def clip_queue_job() -> dict:
    """Work through queued clip jobs. Every outcome lands on the job's row."""
    if not _queue_lock.acquire(blocking=False):
        return {"skipped": "already running"}
    processed = failed = 0
    try:
        while True:
            claimed = _next_job()
            if claimed is None:
                break
            job_id, source, count, auto_queue = claimed
            try:
                result = clip_source(source, count=count, auto_queue=auto_queue,
                                     progress=lambda stage, jid=job_id: _set_stage(jid, stage))
            except Interrupted:
                # The app is stopping. Leave the job to run again after the restart.
                with session_scope() as db:
                    job = db.get(ClipJob, job_id)
                    job.status, job.stage = "queued", ""
                break
            except Exception as exc:
                log.exception("clip job %s crashed", job_id)
                result = {"ok": False, "error": str(exc)}
            _finish_job(job_id, result)
            processed += 1
            failed += 0 if result.get("ok") else 1
    finally:
        _queue_lock.release()
    return {"processed": processed, "failed": failed}
