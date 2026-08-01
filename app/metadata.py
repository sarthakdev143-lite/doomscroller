"""LLM-based YouTube metadata generation using the z-ai CLI.

Calls `z-ai chat` as a subprocess, returns JSON-structured metadata that
preserves the source video's language. Falls back to rule-based metadata
on any failure.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional

from .config import settings

log = logging.getLogger(__name__)


@dataclass
class GeneratedMetadata:
    title: str
    description: str
    tags: list[str]
    detected_language: str
    source: str  # "llm" | "fallback"


# ---------- Language detection ----------
def detect_language(text: str) -> str:
    """Detect the dominant language of a text. Returns ISO 639-1 code."""
    if not text or not text.strip():
        return "en"
    try:
        from langdetect import detect
        return detect(text) or "en"
    except Exception:
        return "en"


# ---------- LLM call ----------
def _call_llm(system_prompt: str, user_prompt: str) -> Optional[str]:
    """Call z-ai CLI, return the assistant message content or None on failure."""
    if not shutil.which("z-ai"):
        log.warning("z-ai CLI not found on PATH; falling back to rules.")
        return None
    cmd = [
        "z-ai", "chat",
        "--prompt", user_prompt,
        "--system", system_prompt,
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=settings.llm_timeout_sec,
        )
        if proc.returncode != 0:
            log.error("z-ai CLI failed: %s", proc.stderr[-500:])
            return None
        # CLI output is JSON prefixed with a couple of status lines. Find the JSON.
        out = proc.stdout
        # Strip ANSI / status lines — find first '{'
        idx = out.find("{")
        if idx < 0:
            return None
        data = json.loads(out[idx:])
        return (data.get("choices") or [{}])[0].get("message", {}).get("content")
    except subprocess.TimeoutExpired:
        log.error("z-ai CLI timed out after %ss", settings.llm_timeout_sec)
        return None
    except Exception as e:
        log.error("z-ai CLI error: %s", e)
        return None


# ---------- Prompt ----------
_NICHE_HINTS = {
    "general_viral": "general viral short-form content (any niche)",
    "tech": "tech, coding, AI, and programming content",
    "comedy": "comedy, pranks, fails, and life-hack content",
    "education": "educational explainers, how-tos, and life tips",
}

_SYSTEM = (
    "You are a YouTube Shorts metadata writer. You write click-worthy but "
    "not spammy titles, SEO-friendly descriptions with 3-5 relevant hashtags, "
    "and 5-10 relevant tags. You MUST respond in the same language as the "
    "source content. You MUST respond as a single minified JSON object with "
    "exactly these keys: title, description, tags. No other text."
)

def _user_prompt(*, source_title: str, source_desc: str, author: str,
                 platform: str, language: str, niche: str) -> str:
    niche_hint = _NICHE_HINTS.get(niche, _NICHE_HINTS["general_viral"])
    return (
        f"Source platform: {platform}\n"
        f"Author: {author}\n"
        f"Detected language: {language}\n"
        f"Channel niche: {niche_hint}\n\n"
        f"Source title: {source_title!r}\n"
        f"Source description: {source_desc[:1000]!r}\n\n"
        f"Generate YouTube Shorts metadata in {language}. The title must be "
        f"<= 90 characters, attention-grabbing, and faithful to the source. "
        f"The description should be 1-3 sentences plus 3-5 hashtags. Tags must "
        f"be lowercase, 1-3 words each, 5-10 items. Respond ONLY with the JSON."
    )


# ---------- JSON extraction ----------
def _extract_json(text: str) -> Optional[dict]:
    if not text:
        return None
    # Try direct parse first
    try:
        return json.loads(text)
    except Exception:
        pass
    # Try to find a {...} block
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


# ---------- Public API ----------
def generate_metadata(*, source_title: str, source_desc: str, author: str,
                      platform: str) -> GeneratedMetadata:
    """Generate YouTube metadata for a video, preserving source language."""
    text_for_lang = f"{source_title} {source_desc}"
    language = detect_language(text_for_lang)

    if settings.use_llm_metadata:
        user = _user_prompt(
            source_title=source_title, source_desc=source_desc,
            author=author, platform=platform,
            language=language, niche=settings.channel_niche,
        )
        raw = _call_llm(_SYSTEM, user)
        if raw:
            data = _extract_json(raw) or {}
            title = str(data.get("title") or "").strip()
            desc = str(data.get("description") or "").strip()
            tags = data.get("tags") or []
            if not isinstance(tags, list):
                tags = [tags]
            tags = [str(t).strip().lower() for t in tags if str(t).strip()]
            # Always include shorts tag
            if "shorts" not in tags:
                tags.insert(0, "shorts")
            if title and desc:
                return GeneratedMetadata(
                    title=title[:100],
                    description=desc[:5000],
                    tags=tags[:15],
                    detected_language=language,
                    source="llm",
                )

    # Fallback: rule-based
    return _fallback_metadata(
        source_title=source_title, source_desc=source_desc,
        author=author, language=language,
    )


def _fallback_metadata(*, source_title: str, source_desc: str,
                       author: str, language: str) -> GeneratedMetadata:
    title = (source_title or "Amazing Short")[:90]
    if not title.lower().endswith("#shorts"):
        title = f"{title} #shorts"
    desc = (source_desc or "Watch till the end! Subscribe for more.")[:4000]
    desc = desc + "\n\n#shorts #viral #fyp"
    tags = ["shorts", "viral", "fyp", "trending", author.lstrip("@").lower()[:30]]
    tags = [t for t in tags if t]
    return GeneratedMetadata(
        title=title, description=desc, tags=tags[:10],
        detected_language=language, source="fallback",
    )
