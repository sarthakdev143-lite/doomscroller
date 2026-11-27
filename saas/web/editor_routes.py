"""The clip editor page: GET /app/clips/{clip_id}/edit (saas.web, editor agent).

Captions are burned into the finished MP4 by libass, so the browser never exports video. The page is a
preview-and-spec editor: the customer changes an edit spec (trim, caption fixes, hook, logo, memes, sound),
sees an approximate preview drawn over the clip, and asks the server to re-render it
(POST /api/clips/{id}/render). Everything the page shows comes from GET /api/clips/{id}/edit in the browser
(saas/web/static/js/editor/main.js); this route only decides who may open it.

The integrator includes `router` in saas.main.create_app (it is not part of saas.web.routes.router).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..auth.deps import require_user
from ..db import get_db
from ..models.core import User
from ..models.jobs import Clip

router = APIRouter(include_in_schema=False)

_MAX_ID = 80  # public ids are short and opaque; anything longer is not one of ours


def _own_clip(db: Session, user: User, clip_id: str) -> Clip | None:
    if not clip_id or len(clip_id) > _MAX_ID:
        return None
    return (db.query(Clip)
            .filter(Clip.id == clip_id, Clip.user_id == user.id, Clip.deleted_at.is_(None))
            .one_or_none())


@router.get("/app/clips/{clip_id}/edit")
def clip_editor(request: Request, clip_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    clip = _own_clip(db, user, clip_id)
    if clip is None:
        # Another account's clip answers exactly like a missing one, so ids cannot be probed.
        raise HTTPException(status_code=404)
    from .app_pages import _app_page  # late: app_pages registers its routes on saas.web.routes.router

    return _app_page(request, db, user, "app/editor.html", "clips",
                     clip_id=clip.id, job_id=clip.job_id, clip_number=(clip.idx or 0) + 1,
                     clip_title=(clip.title or "").strip())
