"""Publish-ready packages: everything a customer needs to post one clip by hand.

We never post on a customer's behalf. Each finished clip gets:
* JSON (`GET /api/clips/{id}/package`): title, description, hashtags, per-platform copy already cut
  to each platform's limits (YouTube Shorts, Instagram Reels, TikTok), file links and suggested
  posting times in the customer's time zone (schedule.py, rules-v1).
* a zip (`GET /api/clips/{id}/package.zip`): the MP4, the thumbnail, package.json and one ready-to-paste
  text file per platform plus a README with the posting times.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

from ..config import settings
from ..models.jobs import Clip
from . import schedule, storage

YT_TITLE_MAX = 100
YT_DESC_MAX = 5000
YT_TAGS_MAX_CHARS = 500
IG_CAPTION_MAX = 2200
IG_HASHTAGS_MAX = 30
TT_CAPTION_MAX = 2200
TT_HASHTAGS_MAX = 8


def _hashtags(clip: Clip) -> list[str]:
    tags = []
    for t in clip.hashtags or []:
        t = "".join(ch for ch in str(t) if ch.isalnum() or ch == "_")
        if t and f"#{t}" not in tags:
            tags.append(f"#{t}")
    return tags


def _cut(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def platform_copy(clip: Clip) -> dict:
    title = (clip.title or "").strip() or "New clip"
    desc = (clip.description or "").strip()
    tags = _hashtags(clip)
    yt_tags = [t for t in tags if t.lower() != "#shorts"]
    yt_line = " ".join(["#Shorts"] + yt_tags[:4])
    keywords, used = [], 0
    for t in yt_tags:
        word = t[1:]
        if used + len(word) + 1 > YT_TAGS_MAX_CHARS or len(keywords) >= 15:
            break
        keywords.append(word)
        used += len(word) + 1
    ig_tags = " ".join(tags[:IG_HASHTAGS_MAX])
    tt_tags = " ".join(tags[:TT_HASHTAGS_MAX])
    return {
        "youtube_shorts": {"title": _cut(title, YT_TITLE_MAX),
                           "description": _cut(f"{desc}\n\n{yt_line}".strip(), YT_DESC_MAX),
                           "tags": keywords},
        "instagram_reels": {"caption": _cut(f"{title}\n\n{desc}\n\n{ig_tags}".strip(), IG_CAPTION_MAX)},
        "tiktok": {"caption": _cut(f"{title} {tt_tags}".strip(), TT_CAPTION_MAX)},
    }


def _links(clip: Clip, signed: bool) -> dict:
    if signed:
        video, exp = storage.signed_file_url(clip.id, "video")
        return {"video_url": video, "download_url": storage.signed_file_url(clip.id, "download")[0],
                "thumbnail_url": storage.signed_file_url(clip.id, "thumbnail")[0],
                "zip_url": storage.signed_file_url(clip.id, "package")[0], "links_expire_at": exp}
    return {"video_url": f"/api/clips/{clip.id}/video", "download_url": f"/api/clips/{clip.id}/download",
            "thumbnail_url": f"/api/clips/{clip.id}/thumbnail", "zip_url": f"/api/clips/{clip.id}/package.zip"}


def build(clip: Clip, tz: Optional[str], *, signed: bool = False) -> dict:
    copy = platform_copy(clip)
    times = schedule.suggest(tz, offset=max(0, int(clip.idx or 0)))
    platforms = {}
    for name, body in copy.items():
        platforms[name] = {**body, "label": times["platforms"][name]["label"],
                           "suggested_times": times["platforms"][name]["times"]}
    return {"clip_id": clip.id, "job_id": clip.job_id, "title": clip.title, "description": clip.description,
            "hashtags": _hashtags(clip), "duration_s": clip.duration_s, "format": "1080x1920 MP4 (9:16)",
            "timezone": times["timezone"], "schedule_model": times["model"], "schedule_note": times["note"],
            "platforms": platforms, "files": _links(clip, signed),
            "posting": "We don't post for you: download the MP4 and paste the copy for each platform."}


def _safe_stem(clip: Clip) -> str:
    stem = re.sub(r"[^A-Za-z0-9 _-]+", "", clip.title or "").strip().replace(" ", "_")[:60]
    return stem or f"clip_{clip.idx + 1}"


def build_zip(clip: Clip, tz: Optional[str]) -> Path:
    """Write the package zip to a temp file and return its path (the caller deletes it)."""
    video = storage.resolve(clip.path)
    if video is None or not video.is_file():
        raise FileNotFoundError(clip.id)
    thumb = storage.resolve(clip.thumbnail_path)
    pkg = build(clip, tz)
    stem = _safe_stem(clip)
    fd, name = tempfile.mkstemp(prefix="pkg_", suffix=".zip", dir=str(storage.tmp_dir()))
    os.close(fd)
    path = Path(name)
    lines = [f"{settings.brand_name} publish package", "", f"Clip: {clip.title}", f"Time zone: {pkg['timezone']}",
             "", "Suggested posting times (rules-v1):"]
    for p in pkg["platforms"].values():
        lines.append(f"  {p['label']}: " + "; ".join(t["local"] for t in p["suggested_times"]))
    lines += ["", pkg["schedule_note"], "", pkg["posting"]]
    with zipfile.ZipFile(path, "w") as z:
        z.write(video, f"{stem}.mp4", compress_type=zipfile.ZIP_STORED)  # already compressed
        if thumb is not None and thumb.is_file():
            z.write(thumb, f"{stem}_thumbnail.jpg", compress_type=zipfile.ZIP_STORED)
        z.writestr("package.json", json.dumps(pkg, indent=2, ensure_ascii=False), compress_type=zipfile.ZIP_DEFLATED)
        yt = pkg["platforms"]["youtube_shorts"]
        z.writestr("youtube_shorts.txt", f"TITLE\n{yt['title']}\n\nDESCRIPTION\n{yt['description']}\n\nTAGS\n"
                   f"{', '.join(yt['tags'])}\n", compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("instagram_reels.txt", pkg["platforms"]["instagram_reels"]["caption"] + "\n",
                   compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("tiktok.txt", pkg["platforms"]["tiktok"]["caption"] + "\n", compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("README.txt", "\n".join(lines) + "\n", compress_type=zipfile.ZIP_DEFLATED)
    return path


def zip_name(clip: Clip) -> str:
    return f"{_safe_stem(clip)}_package.zip"
