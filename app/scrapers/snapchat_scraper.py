"""Snapchat Spotlight scraper.

Snapchat's public web pages are a Next.js app that embeds a complete, fully
hydrated data payload in a ``<script id="__NEXT_DATA__">`` tag. That payload is
served on a *plain HTTP GET* -- no browser, no JS execution, no Playwright --
and it contains everything we need per video::

    props.pageProps.spotlightFeed.spotlightStories[]
      .story.storyId.value                          stable id
      .story.snapList[0].snapUrls.mediaUrl          direct MP4
      .story.snapList[0].timestampInSec.value       upload time
      .metadata.videoMetadata.name / .description   title / caption
      .metadata.videoMetadata.contentUrl            direct MP4
      .metadata.videoMetadata.width/.height/.durationMs
      .metadata.videoMetadata.creator.personCreator handle + display name
      .metadata.engagementStats.*                   views/boosts/comments/shares
      .metadata.llmTitle / .llmDescription          Snapchat's own SEO copy
      .metadata.cuSignals[]                         keywords + detected language

Two entry points:

*   **Spotlight crawl** (primary) -- ``/spotlight`` returns 25 hydrated stories.
    Requesting ``/spotlight/<storyId>`` returns another 25 related stories, so we
    breadth-first crawl story ids to build a pool as large as we need.
*   **Profile crawl** (targeted) -- ``/@<handle>`` returns ``spotlightHighlights``
    (snap ids only, no stats) plus the creator's follower count. Each highlight
    id is resolved through ``/spotlight/<id>`` to hydrate its full metadata.

Playwright is retained only as an optional fallback for the case where the plain
HTTP fetch is challenged; it is never required for normal operation.
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from datetime import datetime, timezone
from typing import Any, Iterable

from .base import ScrapedItem, Scraper

log = logging.getLogger(__name__)

_BASE = "https://www.snapchat.com"
_SPOTLIGHT_URL = f"{_BASE}/spotlight"
_PROFILE_URL_TEMPLATE = f"{_BASE}/@{{user}}"
_STORY_URL_TEMPLATE = f"{_BASE}/spotlight/{{story_id}}"
_HANDLE_IN_URL_RE = re.compile(
    r"https?://(?:www\.)?snapchat\.com/(?:add/|@)(?P<user>[^/?#]+)", re.I
)
_VALID_HANDLE_RE = re.compile(r"^[A-Za-z0-9_.\-]{2,64}$")
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}
# The CDN rejects requests that do not look like they came from snapchat.com.
_CDN_HEADERS = {
    "User-Agent": _UA,
    "Referer": f"{_SPOTLIGHT_URL}/",
    "Origin": _BASE,
    "Accept": "*/*",
}

# Snapchat's snapMediaType value for video snaps.
_MEDIA_TYPE_VIDEO = 1
# Placeholder strings Snapchat uses when a creator supplied no title/description.
_PLACEHOLDER_TITLES = {"spotlight snap", "snapchat", ""}
_PLACEHOLDER_DESC_PREFIX = "another spotlight snap brought to you"


# ---------------------------------------------------------------------------
# Coercion helpers -- Snapchat mixes ints, numeric strings and None in the same
# fields, and wraps many scalars in a {"value": ...} box.
# ---------------------------------------------------------------------------

def _num(value: Any, default: int = 0) -> int:
    """Coerce an int-or-numeric-string-or-None field to an int."""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except (TypeError, ValueError):
            return default
    return default


def _flt(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _unwrap(value: Any) -> Any:
    """Unwrap Snapchat's ``{"value": x}`` scalar boxes."""
    if isinstance(value, dict) and "value" in value and len(value) == 1:
        return value["value"]
    return value


def _text(value: Any) -> str:
    value = _unwrap(value)
    return value.strip() if isinstance(value, str) else ""


class SnapchatScraper(Scraper):
    """Scrape Snapchat Spotlight via the embedded ``__NEXT_DATA__`` payload."""

    def __init__(self, *, timeout: float = 20.0) -> None:
        self._timeout = timeout
        self._client: Any = None
        # Playwright handles, only created if the HTTP path is challenged.
        self._browser: Any = None
        self._playwright: Any = None

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------
    def _http(self) -> Any:
        """Lazily build a keep-alive HTTP client (connection reuse matters here)."""
        if self._client is None:
            import httpx

            from ..config import settings

            proxy = (settings.proxy_url or "").strip() or None
            self._client = httpx.Client(
                headers=_HEADERS,
                timeout=self._timeout,
                follow_redirects=True,
                proxy=proxy,
            )
        return self._client

    def _fetch_next_data(self, url: str, *, allow_browser: bool = True) -> dict | None:
        """GET ``url`` and return its parsed ``__NEXT_DATA__`` payload."""
        try:
            response = self._http().get(url)
        except Exception as exc:
            log.warning("snapchat: GET %s failed: %s", url, exc)
            return None
        if response.status_code != 200:
            log.warning("snapchat: GET %s -> HTTP %s", url, response.status_code)
            return None
        data = self._parse_next_data(response.text)
        if data is None and allow_browser:
            log.info("snapchat: no __NEXT_DATA__ in %s, retrying via browser", url)
            html = self._fetch_via_browser(url)
            if html:
                data = self._parse_next_data(html)
        return data

    @staticmethod
    def _parse_next_data(html: str) -> dict | None:
        match = _NEXT_DATA_RE.search(html or "")
        if not match:
            return None
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            log.warning("snapchat: __NEXT_DATA__ parse failed: %s", exc)
            return None

    @staticmethod
    def _page_props(data: dict | None) -> dict:
        if not isinstance(data, dict):
            return {}
        props = data.get("props")
        page_props = props.get("pageProps") if isinstance(props, dict) else None
        return page_props if isinstance(page_props, dict) else {}

    # ------------------------------------------------------------------
    # Optional Playwright fallback
    # ------------------------------------------------------------------
    def _fetch_via_browser(self, url: str) -> str | None:
        """Render ``url`` in Chromium and return its HTML. Best-effort."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            log.debug("snapchat: playwright unavailable, cannot fall back")
            return None
        playwright = browser = None
        try:
            playwright = sync_playwright().start()
            browser = playwright.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            context = browser.new_context(
                user_agent=_UA,
                viewport={"width": 1280, "height": 900},
                locale="en-US",
            )
            context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            )
            page = context.new_page()
            page.set_default_timeout(20_000)
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
            return page.content()
        except Exception as exc:
            log.warning("snapchat: browser fallback failed for %s: %s", url, exc)
            return None
        finally:
            for closer in (
                getattr(browser, "close", None),
                getattr(playwright, "stop", None),
            ):
                if closer is not None:
                    try:
                        closer()
                    except Exception:
                        pass

    # ------------------------------------------------------------------
    # Handle helpers
    # ------------------------------------------------------------------
    def _normalize_handle(self, handle: str) -> str | None:
        """Extract a bare username from a handle, @handle or profile URL."""
        raw = (handle or "").strip()
        if not raw:
            return None
        match = _HANDLE_IN_URL_RE.match(raw)
        if match:
            return match.group("user")
        candidate = raw.lstrip("@").strip("/")
        return candidate if _VALID_HANDLE_RE.match(candidate) else None

    def _profile_url(self, username: str) -> str:
        return _PROFILE_URL_TEMPLATE.format(user=username)

    # ------------------------------------------------------------------
    # Story parsing
    # ------------------------------------------------------------------
    def _parse_story(self, entry: dict, *, fallback_author: str = "") -> ScrapedItem | None:
        """Convert one ``spotlightStories`` entry into a :class:`ScrapedItem`."""
        if not isinstance(entry, dict):
            return None
        story = entry.get("story") if isinstance(entry.get("story"), dict) else {}
        metadata = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
        video_meta = metadata.get("videoMetadata")
        video_meta = video_meta if isinstance(video_meta, dict) else {}
        stats = metadata.get("engagementStats")
        stats = stats if isinstance(stats, dict) else {}

        snaps = story.get("snapList") if isinstance(story.get("snapList"), list) else []
        snap = snaps[0] if snaps and isinstance(snaps[0], dict) else {}

        story_id = _text(story.get("storyId") or {}) or _text(snap.get("snapId") or {})
        if not story_id:
            return None

        # Skip sponsored placements and non-video snaps outright.
        if _unwrap(snap.get("isSponsored")) is True:
            return None
        media_type = snap.get("snapMediaType")
        if media_type is not None and _num(media_type, _MEDIA_TYPE_VIDEO) != _MEDIA_TYPE_VIDEO:
            return None

        snap_urls = snap.get("snapUrls") if isinstance(snap.get("snapUrls"), dict) else {}
        media_url = _text(video_meta.get("contentUrl")) or _text(snap_urls.get("mediaUrl"))
        if not media_url:
            return None

        creator = video_meta.get("creator") if isinstance(video_meta.get("creator"), dict) else {}
        person = creator.get("personCreator") if isinstance(creator.get("personCreator"), dict) else {}
        author = _text(person.get("username")) or fallback_author
        author_name = _text(person.get("name"))
        if not author:
            # Context cards carry the creator when videoMetadata does not.
            for card in metadata.get("contextCards") or []:
                if isinstance(card, dict) and _text(card.get("subtitle")):
                    author = _text(card.get("subtitle"))
                    author_name = author_name or _text(card.get("title"))
                    break

        keywords, language = self._parse_cu_signals(metadata)
        if not keywords:
            keywords = [k for k in (video_meta.get("keywords") or []) if isinstance(k, str)]
        hashtags = [
            (h.strip() if isinstance(h, str) else _text(h)).lstrip("#")
            for h in (metadata.get("hashtags") or [])
        ]
        hashtags = [h for h in hashtags if h]

        # Prefer Snapchat's own SEO title, then the creator's caption.
        llm_title = _text(metadata.get("llmTitle"))
        raw_name = _text(video_meta.get("name"))
        if raw_name.lower() in _PLACEHOLDER_TITLES:
            raw_name = ""
        caption = _text(video_meta.get("embeddedTextCaption"))
        title = llm_title or raw_name or caption

        llm_desc = _text(metadata.get("llmDescription"))
        raw_desc = _text(video_meta.get("description"))
        if raw_desc.lower().startswith(_PLACEHOLDER_DESC_PREFIX):
            raw_desc = ""
        description = llm_desc or raw_desc or _text(metadata.get("description"))

        duration_ms = _num(video_meta.get("durationMs"))
        timestamp = _num(_unwrap(snap.get("timestampInSec")))
        if not timestamp:
            timestamp = _num(video_meta.get("uploadDateMs")) // 1000
        published_at = (
            datetime.fromtimestamp(timestamp, tz=timezone.utc) if timestamp else None
        )

        views = _num(stats.get("viewCount")) or _num(video_meta.get("viewCount"))
        # Snapchat has no "likes"; a Spotlight boost is the closest equivalent.
        boosts = _num(stats.get("boostCount"))
        recommends = _num(stats.get("recommendCount"))
        shares = _num(stats.get("shareCount")) or _num(video_meta.get("shareCount"))

        thumbnail = (
            _text(video_meta.get("thumbnailUrl"))
            or _text(story.get("thumbnailUrl") or {})
            or _text(snap_urls.get("mediaPreviewUrl") or {})
        )
        one_link = metadata.get("oneLinkParams")
        canonical = _text((one_link or {}).get("deepLinkUrl")) if isinstance(one_link, dict) else ""
        if not canonical:
            canonical = (
                f"{_BASE}/@{author}/spotlight/{story_id}"
                if author
                else _STORY_URL_TEMPLATE.format(story_id=story_id)
            )

        return ScrapedItem(
            source="snapchat",
            source_id=story_id,
            source_url=canonical,
            author=author,
            author_name=author_name,
            title=title,
            description=description,
            thumbnail_url=thumbnail or None,
            media_url=media_url,
            views=views,
            likes=boosts + recommends,
            comments=_num(stats.get("commentCount")),
            shares=shares,
            duration_sec=round(duration_ms / 1000) if duration_ms else 0,
            width=_num(video_meta.get("width")),
            height=_num(video_meta.get("height")),
            language=language,
            keywords=[k for k in keywords if k][:15],
            published_at=published_at,
            follower_count=_num(person.get("followerCount")),
            meta={
                "scrape_method": "next_data",
                "boost_count": boosts,
                "recommend_count": recommends,
                "hashtags": hashtags,
                "llm_title": llm_title,
                "llm_description": llm_desc,
                "caption": caption,
                "creator_url": _text(person.get("url")),
            },
        )

    @staticmethod
    def _parse_cu_signals(metadata: dict) -> tuple[list[str], str]:
        """Pull keywords and the detected language out of ``cuSignals``."""
        keywords: list[str] = []
        language = ""
        best_score = 0.0
        for wrapper in metadata.get("cuSignals") or []:
            if not isinstance(wrapper, dict):
                continue
            signal = wrapper.get("signal")
            if not isinstance(signal, dict):
                continue
            case = signal.get("$case")
            payload = signal.get(case) if isinstance(signal.get(case), dict) else {}
            if case == "textMetadata":
                for keyword in payload.get("keywords") or []:
                    if isinstance(keyword, str) and keyword.strip():
                        keywords.append(keyword.strip())
            elif case == "detectedLanguage":
                for entry in payload.get("languageScores") or []:
                    if not isinstance(entry, dict):
                        continue
                    score = _flt(entry.get("score"))
                    code = entry.get("languageCode")
                    if isinstance(code, str) and code and score > best_score:
                        best_score, language = score, code
        # Preserve order while dropping duplicates.
        return list(dict.fromkeys(keywords)), language

    def _stories_from(self, data: dict | None) -> list[dict]:
        feed = self._page_props(data).get("spotlightFeed")
        stories = feed.get("spotlightStories") if isinstance(feed, dict) else None
        if not isinstance(stories, list):
            return []
        return [s for s in stories if isinstance(s, dict)]

    # ------------------------------------------------------------------
    # Spotlight crawl
    # ------------------------------------------------------------------
    def scrape_spotlight(self, max_results: int, *, max_requests: int | None = None,
                         known_ids: "frozenset[str] | set[str] | None" = None) -> list[ScrapedItem]:
        """Breadth-first crawl the Spotlight feed until ``max_results`` uniques.

        ``/spotlight`` seeds the crawl; each story id then acts as a cursor via
        ``/spotlight/<id>``, which returns a fresh page of related stories.

        ``known_ids`` are stories the caller already has. They are still walked
        *through* -- a story we have seen before is just as good a cursor into
        the related-story graph as a new one -- but they do not count against
        ``max_results``. Without this the crawl spends its whole request budget
        re-collecting the same seed page every run and reports the result as
        "skipped": observed yield decayed 20 -> 10 -> 4 over three runs.
        """
        known = frozenset(known_ids or ())
        if max_requests is None:
            from ..config import settings
            max_requests = max(1, getattr(settings, "scrape_max_requests", 12))
        collected: dict[str, ScrapedItem] = {}
        seen: set[str] = set()
        frontier: list[str] = []
        visited: set[str] = set()
        requests_made = 0

        def ingest(data: dict | None) -> int:
            added = 0
            for entry in self._stories_from(data):
                item = self._parse_story(entry)
                if item is None or item.source_id in seen:
                    continue
                seen.add(item.source_id)
                # Navigate through every story, new or already-known.
                frontier.append(item.source_id)
                if item.source_id in known:
                    continue
                collected[item.source_id] = item
                added += 1
            return added

        seed = self._fetch_next_data(_SPOTLIGHT_URL)
        requests_made += 1
        added = ingest(seed)
        log.info("snapchat spotlight: seed page -> %d new items (%d parsed)", added, len(seen))
        # Bail only when the seed page parsed nothing at all. An empty
        # ``collected`` is normal and expected once the library is large -- it
        # just means every seeded story is already known, and those are exactly
        # the cursors we need to walk to reach unseen ones.
        if not frontier:
            log.warning("snapchat spotlight: seed page yielded nothing")
            return []

        # Follow story-id cursors until we have enough or run out of budget.
        while len(collected) < max_results and frontier and requests_made < max_requests:
            # Sample the frontier so repeated runs explore different branches.
            story_id = frontier.pop(random.randrange(len(frontier)))
            if story_id in visited:
                continue
            visited.add(story_id)
            data = self._fetch_next_data(
                _STORY_URL_TEMPLATE.format(story_id=story_id), allow_browser=False
            )
            requests_made += 1
            added = ingest(data)
            log.debug(
                "snapchat spotlight: cursor %s -> +%d (total %d)",
                story_id[:16], added, len(collected),
            )
            if added:
                time.sleep(0.4)  # be a polite client

        log.info(
            "snapchat spotlight: %d new items from %d requests (%d known stories walked)",
            len(collected), requests_made, len(seen) - len(collected),
        )
        return list(collected.values())[:max_results]

    # ------------------------------------------------------------------
    # Profile crawl
    # ------------------------------------------------------------------
    def scrape_profile(self, handle: str, max_results: int) -> list[ScrapedItem]:
        """Scrape one public profile's Spotlight highlights."""
        username = self._normalize_handle(handle)
        if not username:
            log.warning("snapchat: unparseable handle %r", handle)
            return []

        data = self._fetch_next_data(self._profile_url(username))
        page_props = self._page_props(data)
        if not page_props:
            return []

        profile = page_props.get("userProfile")
        info = profile.get("publicProfileInfo") if isinstance(profile, dict) else None
        info = info if isinstance(info, dict) else {}
        follower_count = _num(info.get("subscriberCount"))
        display_name = _text(info.get("title"))

        highlights = page_props.get("spotlightHighlights")
        highlights = highlights if isinstance(highlights, list) else []
        if not highlights:
            log.info("snapchat: @%s has no spotlight highlights", username)
            return []

        # Highlights carry ids but no stats, so hydrate each via its story page.
        items: dict[str, ScrapedItem] = {}
        for highlight in highlights[: max(max_results, 1) * 2]:
            if len(items) >= max_results:
                break
            if not isinstance(highlight, dict):
                continue
            snaps = highlight.get("snapList") or []
            snap = snaps[0] if snaps and isinstance(snaps[0], dict) else {}
            story_id = _text(highlight.get("storyId") or {}) or _text(snap.get("snapId") or {})
            if not story_id:
                continue
            hydrated = self._fetch_next_data(
                _STORY_URL_TEMPLATE.format(story_id=story_id), allow_browser=False
            )
            for entry in self._stories_from(hydrated):
                item = self._parse_story(entry, fallback_author=username)
                # Only keep the requested story. The rest of that page is a
                # related feed, and attributing it to this creator would be wrong.
                if item is None or item.source_id != story_id:
                    continue
                item.author = item.author or username
                item.author_name = item.author_name or display_name
                item.follower_count = item.follower_count or follower_count
                item.meta["from_profile"] = username
                items[item.source_id] = item
                break
            time.sleep(0.3)

        log.info("snapchat: @%s -> %d items", username, len(items))
        return list(items.values())

    # ------------------------------------------------------------------
    # Scraper interface
    # ------------------------------------------------------------------
    def scrape_handles(self, handles: list[str], max_per_run: int,
                       download: bool = True) -> list[ScrapedItem]:
        usernames = [u for u in (self._normalize_handle(h) for h in handles or []) if u]
        if not usernames or max_per_run <= 0:
            return []
        per_handle = max(1, max_per_run // len(usernames))
        items: list[ScrapedItem] = []
        for username in usernames:
            if len(items) >= max_per_run:
                break
            try:
                items.extend(self.scrape_profile(username, per_handle))
            except Exception as exc:
                log.warning("snapchat: profile @%s failed: %s", username, exc)
        items = self._dedup(items)[:max_per_run]
        if download and items:
            self.download(items)
        return items

    def scrape_hashtags(self, hashtags: list[str], max_per_run: int,
                        download: bool = True,
                        known_ids: "frozenset[str] | set[str] | None" = None) -> list[ScrapedItem]:
        """Scrape Spotlight, optionally biasing toward the given topics.

        Spotlight has no public hashtag index, so the tags *rank* the crawled
        feed via Snapchat's own keywords rather than querying it.
        """
        if max_per_run <= 0:
            return []
        tags = {t.lstrip("#").lower() for t in (hashtags or []) if t}
        # Over-fetch so tag ranking has something to choose between.
        items = self.scrape_spotlight(
            max_results=max_per_run * 2 if tags else max_per_run,
            known_ids=known_ids,
        )

        if tags:
            def tag_affinity(item: ScrapedItem) -> int:
                haystack = {k.lower() for k in item.keywords}
                haystack |= {h.lower() for h in item.meta.get("hashtags") or []}
                words = f"{item.title} {item.description}".lower()
                return len(haystack & tags) + sum(1 for t in tags if t in words)

            items.sort(key=tag_affinity, reverse=True)
            for item in items:
                item.meta.setdefault("hashtags_requested", sorted(tags))

        items = items[:max_per_run]
        if download and items:
            self.download(items)
        return items

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------
    def download(self, items: Iterable[ScrapedItem], *, max_workers: int = 4) -> int:
        """Download each item's media straight from Snapchat's CDN.

        Every item carries its own ``media_url``, so files map 1:1 onto their
        metadata -- no guessing and no cross-assignment between videos.
        """
        import concurrent.futures

        from ..config import settings

        pending = [item for item in items if not item.local_path and item.media_url]
        if not pending:
            return 0

        content_dir = str(settings.content_path)
        os.makedirs(content_dir, exist_ok=True)

        def fetch(item: ScrapedItem) -> bool:
            # Name the file after the stable story id so a retry overwrites
            # rather than accumulating duplicates.
            safe_id = re.sub(r"[^A-Za-z0-9_.-]", "", item.source_id)[:60] or "snap"
            dest = os.path.join(content_dir, f"snap_{safe_id}.mp4")
            if os.path.exists(dest) and os.path.getsize(dest) > 50_000:
                item.local_path = dest
                return True
            tmp = f"{dest}.part"
            try:
                import httpx

                written = 0
                with httpx.Client(
                    headers=_CDN_HEADERS, timeout=60.0, follow_redirects=True
                ) as client:
                    with client.stream("GET", item.media_url) as response:
                        if response.status_code != 200:
                            log.warning(
                                "snapchat: CDN HTTP %s for %s",
                                response.status_code, item.source_id[:16],
                            )
                            return False
                        with open(tmp, "wb") as handle:
                            for chunk in response.iter_bytes(64 * 1024):
                                handle.write(chunk)
                                written += len(chunk)
                if written < 50_000:
                    log.warning(
                        "snapchat: %s too small (%d bytes)", item.source_id[:16], written
                    )
                    return False
                os.replace(tmp, dest)
                item.local_path = dest
                item.meta["size_bytes"] = written
                return True
            except Exception as exc:
                log.warning("snapchat: download failed for %s: %s", item.source_id[:16], exc)
                return False
            finally:
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            results = list(pool.map(fetch, pending))
        downloaded = sum(1 for ok in results if ok)
        log.info("snapchat: downloaded %d/%d files", downloaded, len(pending))
        return downloaded

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    def __enter__(self) -> "SnapchatScraper":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
