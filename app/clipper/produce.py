"""One chosen moment in, one finished clip out, with no database and no channel state.

Both the owner's queue (``job.clip_source``) and the SaaS seam (``standalone.clip_to_directory``)
call :func:`produce_clip`, so a fix to how a clip is edited, decorated or rendered reaches both.
Everything here reads ``settings`` at call time: callers apply their overrides (a growth recipe,
a customer's options) around the call.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from app.config import settings

from . import music, sfx
from .broll import StockSession, credit_line
from .edit import EditPlan, attach_memes, attach_stock, build_clean_vlog_edit, build_edit, trim_to
from .ingest import SourceVideo
from .media import MediaError, MediaInfo
from .memes import MemeSession, credit_line as meme_credit_line, identity_keys
from .moments import Moment, fallback_hook
from .render import _STING_PAD_SEC, ClipPlan, render_moment
from .signals import Signals
from .transcribe import Word

log = logging.getLogger(__name__)

# A Short stays clear of the one-minute mark even with an ending sting held after the last word.
DEFAULT_MAX_SEC = 31.5


@dataclass
class Steps:
    """The stages produce_clip runs. Callers (and tests) may swap any of them."""

    build_edit: Callable = build_edit
    build_clean_vlog_edit: Callable = build_clean_vlog_edit
    attach_stock: Callable = attach_stock
    attach_memes: Callable = attach_memes
    render_moment: Callable = render_moment


@dataclass
class Produced:
    plan: ClipPlan
    edit: Optional[EditPlan]
    credit: str = ""
    # Canonical ids of the memes placed, and each one's identity keys for the history.
    memes_used: list[str] = field(default_factory=list)
    meme_keys: list[list[str]] = field(default_factory=list)


def _ending_sting_seconds(moment: Moment, stings: dict) -> float:
    """How long the ending sting plays (0 without one): the render holds the last frame that long."""
    sting_path = stings.get(moment.sound) if moment.sound else None
    if not sting_path:
        return 0.0
    sound = sfx.sting(str(sting_path))
    return (len(sound) / sfx.RATE) if sound is not None else sfx._STING_MAX_SEC


def edit_budget(moment: Moment, stings: dict, max_sec: float = DEFAULT_MAX_SEC) -> float:
    """How long the edit may play: room is left for an ending sting held after the last word."""
    cap = max(10.0, float(max_sec))
    if not moment.sound:
        return cap
    sting_sec = _ending_sting_seconds(moment, stings) or sfx._STING_MAX_SEC
    return min(cap, max(15.0, cap + 0.4 - (sting_sec + 0.8)))


def produce_clip(src: SourceVideo, info: MediaInfo, signals: Signals, moment: Moment, words: Sequence[Word],
                 out_path: str, *, timed: bool, language: str, burned_in: bool, stings: dict,
                 stock: StockSession, mstock: MemeSession, warn: Callable[[str], None],
                 track=None, brand: Optional[str] = None, watermark: str = "",
                 max_sec: float = DEFAULT_MAX_SEC, captions: Optional[bool] = None,
                 steps: Optional[Steps] = None) -> Produced:
    """Edit, decorate and render one moment. Raises MediaError when the clip cannot be rendered at all."""
    run = steps or Steps()
    max_edit_sec = edit_budget(moment, stings, max_sec)
    if moment.kind == "speech" and timed:
        edit = run.build_edit(moment, words, title=src.title)
    elif moment.kind == "speech":
        edit = run.build_clean_vlog_edit(moment, src.path, speed=min(1.12, settings.clip_speed),
                                     max_sec=min(max_edit_sec, max(31.0, float(max_sec) - 0.5)))
    else:
        edit = None
    if edit is not None:
        trim_to(edit, max_edit_sec)
        ending = str(stings[moment.sound]) if moment.sound in stings else ""
        if ending and info.has_audio and settings.clip_sfx:
            edit.tail_room = round(_ending_sting_seconds(moment, stings) + _STING_PAD_SEC, 3)
        if timed:
            for message in run.attach_stock(edit, stock):
                warn(message)
        # Only marked reactions get a meme; Hindi clean-vlog edits have no marks, so they get none.
        for message in run.attach_memes(edit, mstock, language=language, has_audio=info.has_audio,
                                        ending_sound=ending if edit.tail_room else ""):
            warn(message)

    from app.transformers.ffmpeg_transformer import is_renderable

    if (not moment.hook or not is_renderable(moment.hook)) and moment.title:
        moment.hook = fallback_hook(moment.title)
    extras: dict = {}
    if brand is not None:
        extras["brand"] = brand
    if language:
        extras["language"] = language
    if watermark:
        extras["watermark"] = watermark
    if moment.sound in stings:
        extras["sting"] = str(stings[moment.sound])
    if edit is not None and not edit.words and not edit.opening_words and moment.kind == "speech" and words:
        edit.opening_words = [w for w in words if w.end > moment.start and w.start < moment.end]
    show_captions = timed if captions is None else (timed and captions)
    render_words = list(words) if timed else []

    def render() -> ClipPlan:
        kwargs = dict(extras)
        try:
            return run.render_moment(src.path, info, signals, moment, render_words, out_path, edit=edit,
                                     creator=src.author_name or src.author, music=track, captions=show_captions,
                                     source_captioned=burned_in, **kwargs)
        except TypeError as exc:
            if "language" not in str(exc) or "language" not in kwargs:
                raise
            kwargs.pop("language")  # a render stand-in written before clips had a language
            return run.render_moment(src.path, info, signals, moment, render_words, out_path, edit=edit,
                                     creator=src.author_name or src.author, music=track, captions=show_captions,
                                     source_captioned=burned_in, **kwargs)

    try:
        plan = render()
    except MediaError as exc:
        if not (edit and (edit.cutaways or edit.meme_overlays)):
            raise
        # B-roll and memes are optional, so shots that will not render cost only themselves.
        lost = [n for n, items in (("B-roll", edit.cutaways), ("meme overlays", edit.meme_overlays)) if items]
        warn(f"the clip lost its {' and '.join(lost)}, which would not render: {exc}")
        edit.cutaways = []
        edit.meme_overlays = []
        plan = render()
    if not plan.tail:
        moment.sound = ""  # an ending sound that did not load is not credited either
    credit = ""
    if edit and edit.cutaways:
        credit = credit_line((c.clip.author for c in edit.cutaways), edit.cutaways[0].clip.provider)
    overlays = [m.clip for m in edit.meme_overlays if m.clip is not None] if edit else []
    if overlays:
        credit = f"{credit} {meme_credit_line([c.title for c in overlays], overlays[0].provider)}".strip()
    if plan.music and track is not None:
        credit = f"{credit} {music.credit(track)}".strip()
    from .memes import canonical_id

    return Produced(plan=plan, edit=edit, credit=credit,
                    memes_used=[canonical_id(c.meme_id) for c in overlays],
                    meme_keys=[identity_keys(c) for c in overlays])
