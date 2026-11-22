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
_SHORTS_TITLE_MAX = 70
_DESCRIPTION_MAX = 5000
_TAG_MAX_CHARS = 30
_TAGS_MAX_COUNT = 15
_TAGS_MAX_TOTAL_CHARS = 480  # the real cap is 500; leave headroom

_ANGLE_BRACKETS = re.compile(r"[<>]")  # YouTube refuses these in a title or description
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")
_WORD_RE = re.compile(r"[a-zA-Z]+")
_HINGLISH_WORDS = frozenset({
    "hai", "hain", "ho", "tha", "thi", "kya", "kyu", "kyun", "kyon", "kaise", "kaun",
    "aur", "nahi", "nahin", "yaar", "bhai", "dost", "ghar", "dekho", "gaya", "gayi",
    "gaye", "raha", "rahi", "rahe", "hua", "hui", "hue", "mein", "se", "ko", "ka", "ki",
    "ke", "pe", "bahut", "bohot", "kuch", "sab", "sach", "jhooth", "pyaar", "shaadi",
    "diwali", "patakha", "patakhe", "dhamaka", "aaj", "abhi", "yahan", "wahan", "mera",
    "meri", "mere", "tera", "teri", "apna", "apni", "hum", "aap", "karke", "liya",
    "diya", "mila", "mili", "nikla", "nikli", "karo", "baat", "batao", "chalo", "zyada",
    "itna", "itni", "wala", "wali", "wale", "khana", "paani", "laddu", "didi", "bhaiya",
    "aage", "kutta", "khilaya", "chhoti", "gaiyon", "khapli", "atta", "bina", "lekin",
    "kyunki", "fir", "phir", "kahan", "kab", "kitna", "kitni", "jab", "tab", "kaafi",
    "bilkul", "sirf", "waise", "samajh", "paisa", "paise", "zindagi", "duniya", "sapna",
    "mazaa", "maza", "dar", "raaz", "sachchi", "bawal", "dhoka", "natak", "kaam", "waqt",
    "subah", "shaam", "raat", "saal", "mahina", "hadh", "kaunsa", "sabse", "aapko",
    "ne", "yeh", "bol", "end",
})
# Keep "end" and English homographs ("ho", "hum", "hue", "mere", "tab") out of Hinglish triggers.
_HINGLISH_WORDS = _HINGLISH_WORDS - {"end", "ho", "hum", "hue", "mere", "tab"}
_HINGLISH_PARTICLES = frozenset({
    "ka", "ki", "ke", "ko", "se", "pe", "mein", "hai", "nahi",
    "kya", "kyun", "kaise", "aur", "yaar", "bhai", "wala", "wali",
})
_RISKY_TITLE_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:kachche|kachcha|underwears?|lingeries?|bikinis?)\b", re.IGNORECASE), "kapde"),
    (re.compile(r"\baccidents?\b", re.IGNORECASE), "scene"),
    (re.compile(r"\b(?:bhoot(?:on)?|ghosts?|haunted)\b", re.IGNORECASE), "raaz"),
    (re.compile(r"\b(?:khoon|bloods?|bloody)\b", re.IGNORECASE), "shock"),
    (re.compile(r"\b(?:murders?|murdered|murdering|kills?|killed|killing|deaths?|dead)\b", re.IGNORECASE), "drama"),
    (re.compile(r"\b(?:horrors?|darawna|darawni|darawne)\b", re.IGNORECASE), "suspense"),
    (re.compile(r"\b(?:gandi|ganda|gande)\b", re.IGNORECASE), "ajeeb"),
    (re.compile(r"भूत(?:ों)?"), "राज़"),
)


def _strip_unsafe(value: Optional[str]) -> str:
    """Strip angle brackets, null bytes, and C0/C1 control chars, normalising CRLF to LF."""
    if not value:
        return ""
    normalized = str(value).replace("\r\n", "\n").replace("\r", "\n")
    return _CONTROL_CHARS.sub("", _ANGLE_BRACKETS.sub("", normalized))

_SHORTS_HASHTAG = "#Shorts"
_BASE_TAGS = ("shorts", "viral", "trending")
_LONG_BASE_TAGS = ("viral", "trending")
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
def has_devanagari(text: Optional[str]) -> bool:
    return bool(text and _DEVANAGARI_RE.search(text))


def is_hinglish_text(text: Optional[str]) -> bool:
    if not text:
        return False
    tokens = [t.lower() for t in _WORD_RE.findall(text)]
    if not tokens:
        return False
    unique_tokens = set(tokens)
    if len(tokens) <= 6 and (unique_tokens & _HINGLISH_PARTICLES):
        return True
    hits = len(unique_tokens & _HINGLISH_WORDS)
    return hits >= 2 or (hits >= 1 and "₹" in text)


def has_risky_title_words(text: Optional[str]) -> bool:
    if not text:
        return False
    return any(pattern.search(text) for pattern, _rep in _RISKY_TITLE_REPLACEMENTS)


def sanitize_shorts_title(title: Optional[str]) -> str:
    cleaned = str(title or "")
    for pattern, replacement in _RISKY_TITLE_REPLACEMENTS:
        cleaned = pattern.sub(replacement, cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def detect_language(text: str) -> str:
    """Detect the dominant language of a text. Returns an ISO 639-1 code."""
    if not text or not text.strip():
        return "en"
    if has_devanagari(text) or is_hinglish_text(text):
        return "hi"
    try:
        from langdetect import DetectorFactory, detect_langs

        DetectorFactory.seed = 0
        probs = detect_langs(text)
        code = probs[0].lang if probs else "en"
        # langdetect frequently misclassifies short Romanized Hinglish or English titles as Malay/Tagalog/Indonesian/Vietnamese,
        # or assigns partial probability to another Latin language when English is a plausible candidate.
        if text.isascii():
            if code in ("id", "tl", "ms", "so", "sw", "vi"):
                return "en"
            if any(p.lang == "en" and p.prob >= 0.20 for p in probs):
                return "en"
        return code
    except Exception:
        return "en"


# ---------------------------------------------------------------------------
# Finishing pass
# ---------------------------------------------------------------------------
def _clean_title(title: str, *, reserve_shorts: bool = False, max_chars: int = _TITLE_MAX) -> str:
    """Trim a title to YouTube's limit, optionally reserving room for the Shorts hashtag."""
    title = re.sub(r"\s+", " ", _strip_unsafe(title).strip())
    if not title:
        return ""
    budget = max_chars - len(_SHORTS_HASHTAG) - 1 if reserve_shorts else max_chars
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


def finished_title(title: str, *, shorts_tag: bool = False, max_chars: int = _TITLE_MAX) -> str:
    """A title as it goes out: clean and within YouTube's limit (no forced #Shorts unless requested)."""
    clean = _clean_title(title or "", reserve_shorts=shorts_tag, max_chars=max_chars) or "Watch this"
    if shorts_tag and _SHORTS_HASHTAG.lower() not in clean.lower():
        clean = f"{clean} {_SHORTS_HASHTAG}"
    return clean[:max_chars]


def youtube_title(title: Optional[str], *, max_chars: int = _TITLE_MAX) -> str:
    """The last word on a title: whatever it says, YouTube will accept it."""
    clean = " ".join(_strip_unsafe(title).split())[:max_chars].strip()
    return clean or finished_title("", max_chars=max_chars)


def youtube_description(description: Optional[str], limit: int = _DESCRIPTION_MAX) -> str:
    """YouTube counts bytes here, not characters, and Hindi runs to three bytes each."""
    if limit <= 0:
        return ""
    clean = _strip_unsafe(description)
    return clean.encode("utf-8", "ignore")[:limit].decode("utf-8", "ignore").strip()


def _slug_tag(raw: str) -> str:
    """Normalise one tag: lowercase, no punctuation, length-capped."""
    if raw is None:
        return ""
    cleaned = _strip_unsafe(str(raw))
    tag = re.sub(r"[^\w\s]", "", cleaned, flags=re.UNICODE).strip().lower()
    tag = re.sub(r"\s+", " ", tag)
    return tag[:_TAG_MAX_CHARS].strip()


def build_tags(keywords: Sequence[str] | str | None, author: str = "", *, is_short: bool = True) -> list[str]:
    """Build a deduped, length-capped YouTube tag list."""
    kw_items: list[object] = [keywords] if isinstance(keywords, str) else list(keywords or [])
    candidates: list[str] = []
    base = _BASE_TAGS if is_short else _LONG_BASE_TAGS
    for keyword in kw_items + list(base):
        if keyword is None:
            continue
        tag = _slug_tag(str(keyword))
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
    is_short: bool = True,
    title_shorts_tag: bool = False,
) -> GeneratedMetadata:
    """Apply YouTube limits, tags, hashtags and creator credit to raw copy.

    ``require_credit`` is set when rendering crops away the platform's
    burned-in attribution. The description credit is then the only attribution
    the viewer gets, so it is emitted even when the creator handle is unknown.
    """
    author = " ".join(_strip_unsafe(author).lstrip("@").split())
    author_name = " ".join(_strip_unsafe(author_name).split())
    platform = " ".join(_strip_unsafe(platform).split())
    source_url = "".join(_strip_unsafe(source_url).split())
    tags = build_tags(keywords, author=author, is_short=is_short)

    description = _strip_unsafe(description)
    max_chars = _SHORTS_TITLE_MAX if is_short else _TITLE_MAX
    use_title_tag = is_short and title_shorts_tag
    raw_title = sanitize_shorts_title(title or "") if is_short else (title or "")
    clean_title = _clean_title(raw_title, reserve_shorts=use_title_tag, max_chars=max_chars)
    if not clean_title:
        fallback_src = sanitize_shorts_title(description) if is_short else description
        clean_title = _clean_title(fallback_src, reserve_shorts=use_title_tag, max_chars=max_chars) or "Watch this"
    if use_title_tag and _SHORTS_HASHTAG.lower() not in clean_title.lower():
        clean_title = f"{clean_title} {_SHORTS_HASHTAG}"

    body = re.sub(r"\s+", " ", description.strip())
    blocks: list[str] = []

    hashtags = _hashtags_from(tags)
    if is_short:
        blocks.append(" ".join([_SHORTS_HASHTAG, *hashtags]) if hashtags else _SHORTS_HASHTAG)
    elif hashtags:
        blocks.append(" ".join(hashtags))

    # Credit the original creator, with a link back where we have one.
    if author:
        display = author_name or author
        credit = f"Credit: {display} (@{author})"
        if platform:
            credit += f" on {platform.title()}"
        if source_url:
            credit += f"\n{source_url}"
        blocks.append(credit)
    elif require_credit:
        # No handle, but the visible attribution was cropped out, so say what
        # we can rather than shipping an uncredited reupload.
        if platform:
            credit = f"Originally posted on {platform.title()}"
        else:
            credit = "Source" if source_url else "Reposted content"
        if source_url:
            credit += f"\n{source_url}"
        blocks.append(credit)

    # The body gives way, so a long one cannot push the credit past the limit.
    tail = "\n\n".join(blocks)
    room = _DESCRIPTION_MAX - len(tail.encode("utf-8", "ignore")) - len("\n\n")
    body = youtube_description(body, max(0, room))

    return GeneratedMetadata(
        title=youtube_title(clean_title, max_chars=max_chars),
        description=youtube_description(f"{body}\n\n{tail}" if body else tail),
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
    from app.winproc import background_subprocess_kwargs

    try:
        proc = subprocess.run(
            ["z-ai", "chat", "--prompt", user_prompt, "--system", system_prompt],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=settings.llm_timeout_sec,
            **background_subprocess_kwargs(low_priority=True),
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
    is_short: bool = True,
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
            is_short=is_short,
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
                is_short=is_short,
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
        is_short=is_short,
    )


def _fallback_metadata(*, source_title: str, source_desc: str,
                       author: str, language: str) -> GeneratedMetadata:
    """Rule-based metadata. Retained as a stable entry point for tests."""
    return compose(
        title=source_title, description=source_desc, keywords=[],
        author=author, language=language, source="fallback",
    )
