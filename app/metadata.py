"""YouTube metadata generation.

Metadata is assembled from the best source available, in this order:

1.  **platform** -- Snapchat already runs its own captioning model over every
    Spotlight video and exposes the result (``llmTitle``, ``llmDescription``,
    keywords, detected language). It is written for search, in the video's own
    language, and it costs nothing. When present it wins.
2.  **llm** -- the ``z-ai`` CLI, if that binary is on PATH.
3.  **fallback** -- a rule-based composer over whatever text we do have.

Whichever supplies the raw copy, everything is then pushed through the same
finishing pass (:func:`compose`), which enforces YouTube's limits, preserves the
source language, builds the tag list, and appends the creator credit. That
credit matters: this tool republishes other people's videos, and attribution is
both the decent thing to do and the cheapest strike-avoidance there is.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Optional, Sequence

from .config import settings

log = logging.getLogger(__name__)

# YouTube hard limits.
_TITLE_MAX = 100
_DESCRIPTION_MAX = 5000
_TAG_MAX_CHARS = 30
_TAGS_MAX_COUNT = 15
_TAGS_MAX_TOTAL_CHARS = 480  # the real cap is 500; leave headroom

_SHORTS_HASHTAG = "#Shorts"
_BASE_TAGS = ("shorts", "viral", "trending", "reels")
# Tags that add nothing and dilute relevance.
_TAG_STOPWORDS = frozenset({
    "video", "videos", "snap", "snapchat", "spotlight", "clip", "clips", "the",
})


@dataclass
class GeneratedMetadata:
    title: str
    description: str
    tags: list[str] = field(default_factory=list)
    detected_language: str = "en"
    source: str = "fallback"  # platform | llm | fallback


# ---------------------------------------------------------------------------
# Language
# ---------------------------------------------------------------------------
def detect_language(text: str) -> str:
    """Detect the dominant language of a text. Returns an ISO 639-1 code."""
    if not text or not text.strip():
        return "en"
    try:
        from langdetect import detect

        return detect(text) or "en"
    except Exception:
        return "en"


# ---------------------------------------------------------------------------
# Finishing pass
# ---------------------------------------------------------------------------
def _clean_title(title: str) -> str:
    """Trim a title to YouTube's limit, leaving room for the Shorts hashtag."""
    title = re.sub(r"\s+", " ", (title or "").strip())
    if not title:
        return ""
    budget = _TITLE_MAX - len(_SHORTS_HASHTAG) - 1
    if len(title) <= budget:
        return title
    # Prefer cutting at a separator Snapchat likes to use ("A | B").
    for separator in (" | ", " - ", ": "):
        head = title.split(separator)[0].strip()
        if head and len(head) <= budget:
            return head
    # Otherwise cut on a word boundary.
    clipped = title[:budget].rsplit(" ", 1)[0].strip()
    return clipped or title[:budget].strip()


def _slug_tag(raw: str) -> str:
    """Normalise one tag: lowercase, no punctuation, length-capped."""
    tag = re.sub(r"[^\w\s]", "", str(raw or ""), flags=re.UNICODE).strip().lower()
    tag = re.sub(r"\s+", " ", tag)
    return tag[:_TAG_MAX_CHARS].strip()


def build_tags(keywords: Sequence[str], author: str = "") -> list[str]:
    """Build a deduped, length-capped YouTube tag list."""
    candidates: list[str] = []
    for keyword in list(keywords or []) + list(_BASE_TAGS):
        tag = _slug_tag(keyword)
        if not tag or len(tag) < 2 or tag in _TAG_STOPWORDS:
            continue
        candidates.append(tag)
    if author:
        author_tag = _slug_tag(author)
        if author_tag and len(author_tag) >= 2:
            candidates.append(author_tag)

    tags: list[str] = []
    total = 0
    for tag in dict.fromkeys(candidates):  # order-preserving dedupe
        if len(tags) >= _TAGS_MAX_COUNT:
            break
        # Tags containing spaces are quoted by the API, costing two extra chars.
        cost = len(tag) + (2 if " " in tag else 0) + 1
        if total + cost > _TAGS_MAX_TOTAL_CHARS:
            continue
        tags.append(tag)
        total += cost
    return tags


def _hashtags_from(tags: Sequence[str], limit: int = 4) -> list[str]:
    """Turn the leading tags into description hashtags."""
    out: list[str] = []
    for tag in tags:
        if " " in tag:
            continue  # hashtags cannot contain spaces
        candidate = f"#{tag}"
        if candidate.lower() != _SHORTS_HASHTAG.lower():
            out.append(candidate)
        if len(out) >= limit:
            break
    return out


def compose(
    *,
    title: str,
    description: str,
    keywords: Sequence[str],
    author: str = "",
    author_name: str = "",
    source_url: str = "",
    platform: str = "",
    language: str = "en",
    source: str = "fallback",
    require_credit: bool = False,
) -> GeneratedMetadata:
    """Apply YouTube limits, tags, hashtags and creator credit to raw copy.

    ``require_credit`` is set when rendering crops away the platform's
    burned-in attribution. The description credit is then the only attribution
    the viewer gets, so it is emitted even when the creator handle is unknown.
    """
    author = (author or "").lstrip("@").strip()
    tags = build_tags(keywords, author=author)

    clean_title = _clean_title(title)
    if not clean_title:
        clean_title = _clean_title(description) or "Watch this"
    if _SHORTS_HASHTAG.lower() not in clean_title.lower():
        clean_title = f"{clean_title} {_SHORTS_HASHTAG}"

    body = re.sub(r"\s+", " ", (description or "").strip())
    tail_blocks: list[str] = []

    hashtags = _hashtags_from(tags)
    if hashtags:
        tail_blocks.append(" ".join([_SHORTS_HASHTAG, *hashtags]))
    else:
        tail_blocks.append(_SHORTS_HASHTAG)

    # Credit the original creator, with a link back where we have one.
    if author:
        display = author_name or author
        credit = f"Credit: {display} (@{author})"
        if platform:
            credit += f" on {platform.title()}"
        if source_url:
            credit += f"\n{source_url}"
        tail_blocks.append(credit)
    elif require_credit:
        # No handle, but the visible attribution was cropped out, so say what
        # we can rather than shipping an uncredited reupload.
        credit = f"Originally posted on {platform.title()}" if platform else "Reposted content"
        if source_url:
            credit += f"\n{source_url}"
        tail_blocks.append(credit)

    # Truncate the free-form body first so the mandatory tail (hashtags +
    # creator credit) always survives the description cap. Cutting the joined
    # string here would silently drop the attribution on long descriptions.
    tail = "\n\n".join(tail_blocks)
    if body:
        budget = _DESCRIPTION_MAX - len(tail) - len("\n\n")
        body = body[:max(0, budget)]
        blocks = ([body] if body else []) + tail_blocks
    else:
        blocks = tail_blocks

    description_text = "\n\n".join(blocks)[:_DESCRIPTION_MAX]

    return GeneratedMetadata(
        title=clean_title[:_TITLE_MAX],
        description=description_text,
        tags=tags,
        detected_language=language or "en",
        source=source,
    )


# ---------------------------------------------------------------------------
# Optional z-ai CLI provider
# ---------------------------------------------------------------------------
_NICHE_HINTS = {
    "general_viral": "general viral short-form content (any niche)",
    "tech": "tech, coding, AI, and programming content",
    "comedy": "comedy, pranks, fails, and life-hack content",
    "education": "educational explainers, how-tos, and life tips",
}

_SYSTEM = (
    "You are a YouTube Shorts metadata writer. You write click-worthy but "
    "not spammy titles, SEO-friendly descriptions, and relevant tags. You MUST "
    "respond in the same language as the source content. You MUST respond as a "
    "single minified JSON object with exactly these keys: title, description, "
    "tags. No other text."
)


def _user_prompt(*, source_title: str, source_desc: str, author: str,
                 platform: str, language: str, niche: str,
                 keywords: Sequence[str]) -> str:
    niche_hint = _NICHE_HINTS.get(niche, _NICHE_HINTS["general_viral"])
    return (
        f"Source platform: {platform}\n"
        f"Author: {author}\n"
        f"Detected language: {language}\n"
        f"Channel niche: {niche_hint}\n"
        f"Known keywords: {', '.join(keywords) or 'none'}\n\n"
        f"Source title: {source_title!r}\n"
        f"Source description: {source_desc[:1000]!r}\n\n"
        f"Generate YouTube Shorts metadata in {language}. The title must be "
        f"<= 80 characters, attention-grabbing, and faithful to the source. "
        f"The description should be 1-3 sentences. Tags must be lowercase, "
        f"1-3 words each, 5-10 items. Respond ONLY with the JSON."
    )


def _extract_json(text: str) -> Optional[dict]:
    """Pull the first JSON object out of a possibly chatty LLM response."""
    if not text:
        return None
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def _call_llm(system_prompt: str, user_prompt: str) -> Optional[str]:
    """Call the z-ai CLI; return the assistant message, or None on any failure."""
    if not shutil.which("z-ai"):
        log.debug("z-ai CLI not on PATH; skipping LLM provider")
        return None
    try:
        proc = subprocess.run(
            ["z-ai", "chat", "--prompt", user_prompt, "--system", system_prompt],
            capture_output=True, text=True, timeout=settings.llm_timeout_sec,
        )
    except subprocess.TimeoutExpired:
        log.warning("z-ai CLI timed out after %ss", settings.llm_timeout_sec)
        return None
    except Exception as exc:
        log.warning("z-ai CLI error: %s", exc)
        return None
    if proc.returncode != 0:
        log.warning("z-ai CLI failed: %s", (proc.stderr or "")[-400:])
        return None
    # The CLI prefixes its JSON with status lines.
    start = proc.stdout.find("{")
    if start < 0:
        return None
    try:
        payload = json.loads(proc.stdout[start:])
    except json.JSONDecodeError:
        return None
    choices = payload.get("choices") or [{}]
    return (choices[0].get("message") or {}).get("content")


def llm_available() -> bool:
    """Whether the optional LLM provider can actually be used."""
    return bool(settings.use_llm_metadata and shutil.which("z-ai"))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def generate_metadata(
    *,
    source_title: str = "",
    source_desc: str = "",
    author: str = "",
    author_name: str = "",
    platform: str = "",
    source_url: str = "",
    keywords: Sequence[str] | None = None,
    language: str = "",
    platform_title: str = "",
    platform_description: str = "",
    require_credit: bool = False,
) -> GeneratedMetadata:
    """Generate YouTube metadata for a video, preserving its source language.

    ``platform_title`` / ``platform_description`` are the source platform's own
    generated copy (for Snapchat, ``llmTitle`` / ``llmDescription``). They are
    preferred over calling out to an LLM ourselves: same job, already done, in
    the right language, for free.
    """
    keywords = list(keywords or [])
    language = language or detect_language(
        f"{platform_title or source_title} {platform_description or source_desc}"
    )

    # 1. The platform already wrote SEO copy for us.
    if platform_title or platform_description:
        return compose(
            title=platform_title or source_title,
            description=platform_description or source_desc,
            keywords=keywords,
            author=author, author_name=author_name,
            source_url=source_url, platform=platform,
            language=language, source="platform",
            require_credit=require_credit,
        )

    # 2. Ask the local LLM CLI, if there is one.
    if settings.use_llm_metadata:
        raw = _call_llm(
            _SYSTEM,
            _user_prompt(
                source_title=source_title, source_desc=source_desc,
                author=author, platform=platform, language=language,
                niche=settings.channel_niche, keywords=keywords,
            ),
        )
        data = _extract_json(raw or "") or {}
        title = str(data.get("title") or "").strip()
        description = str(data.get("description") or "").strip()
        if title:
            raw_tags = data.get("tags")
            if not isinstance(raw_tags, list):
                raw_tags = [raw_tags] if raw_tags else []
            return compose(
                title=title,
                description=description or source_desc,
                keywords=[*raw_tags, *keywords],
                author=author, author_name=author_name,
                source_url=source_url, platform=platform,
                language=language, source="llm",
                require_credit=require_credit,
            )

    # 3. Rule-based composition over whatever we have.
    return compose(
        title=source_title,
        description=source_desc,
        keywords=keywords,
        author=author, author_name=author_name,
        source_url=source_url, platform=platform,
        language=language, source="fallback",
        require_credit=require_credit,
    )


def _fallback_metadata(*, source_title: str, source_desc: str,
                       author: str, language: str) -> GeneratedMetadata:
    """Rule-based metadata. Retained as a stable entry point for tests."""
    return compose(
        title=source_title, description=source_desc, keywords=[],
        author=author, language=language, source="fallback",
    )
