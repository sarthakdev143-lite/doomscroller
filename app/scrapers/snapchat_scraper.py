"""Playwright-based Snapchat scraper.

Fetches video / Spotlight URLs from a Snapchat public profile page or the
Snapchat Discover spotlight page. Returns ScrapedItems with source_url set to
the direct video URL; the YtDlpScraper (or the pipeline caller) is responsible
for downloading the actual MP4 via yt-dlp.

Requirements:
  pip install playwright
  playwright install chromium

Note: Snapchat does not expose a public API. This scraper reads the client-
rendered DOM of the public profile page. If Snapchat changes their frontend
markup, selectors will need to be updated accordingly.
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

from .base import ScrapedItem, Scraper

log = logging.getLogger(__name__)

# Public URL patterns
_PROFILE_URL_RE = re.compile(r"https?://(?:www\.)?snapchat\.com/add/(?P<user>[^/?#]+)")
_URL_TEMPLATE = "https://www.snapchat.com/add/{user}"
_SPOTLIGHT_URL = "https://www.snapchat.com/spotlight"


class SnapchatScraper(Scraper):
    """Scrape video URLs from public Snapchat profiles or the Spotlight page."""

    def __init__(self) -> None:
        self._browser: Any = None
        self._playwright: Any = None

    # ------------------------------------------------------------------
    # Browser helpers
    # ------------------------------------------------------------------
    def _get_browser(self) -> Any:
        """Return a launched Chromium browser instance (lazy init)."""
        if self._browser is not None:
            return self._browser
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "playwright is required for Snapchat scraping. "
                "Install with: pip install playwright && playwright install chromium"
            ) from exc

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        return self._browser

    def _new_context(self):
        browser = self._get_browser()
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        # Mask certain webdriver signals
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        return context

    # ------------------------------------------------------------------
    # URL parsing
    # ------------------------------------------------------------------
    def _normalize_handle(self, handle: str) -> str | None:
        """Extract the Snapchat username from a handle or URL.

        Accepts:
          - ``username``
          - ``@username``
          - ``https://www.snapchat.com/add/username``
        """
        h = handle.strip().lstrip("@")
        m = _PROFILE_URL_RE.match(h)
        if m:
            return m.group("user")
        # Plain username
        if re.match(r"^[A-Za-z0-9_.\-]+$", h):
            return h
        return None

    def _profile_url(self, username: str) -> str:
        return _URL_TEMPLATE.format(user=username)

    # ------------------------------------------------------------------
    # Core extraction
    # ------------------------------------------------------------------
    def _extract_video_urls_from_page(
        self, page: Any, base_source: str, base_id_prefix: str
    ) -> list[ScrapedItem]:
        """Walk the page's JSON data for Spotlight/Story video URLs."""
        items: list[ScrapedItem] = []

        # Strategy 1: look for <script type="application/json"> blobs
        json_blobs = page.evaluate(
            """
            () => {
              const out = [];
              document.querySelectorAll('script[type="application/json"], script[type="application/ld+json"]')
                .forEach(s => out.push(s.textContent));
              return out;
            }
            """
        )
        for blob in json_blobs:
            items.extend(self._parse_snapchat_json(blob, base_source, base_id_prefix))
            if len(items) >= 20:
                break

        # Strategy 2: scan anchors / data-src attributes in the DOM
        if not items:
            found_urls: set[str] = set()
            page_links = page.evaluate(
                """
                () => {
                  const urls = new Set();
                  document.querySelectorAll('a[href*=".mp4"], source[src*=".mp4"], video[src]')
                    .forEach(el => {
                      const u = el.getAttribute('href') || el.getAttribute('src');
                      if (u) urls.add(u);
                    });
                  // Also scan for data-* attributes that contain video URLs
                  document.querySelectorAll('[data-url], [data-src], [data-video-url]')
                    .forEach(el => {
                      const attr = el.getAttribute('data-url')
                                || el.getAttribute('data-src')
                                || el.getAttribute('data-video-url');
                      if (attr && attr.includes('.mp4')) urls.add(attr);
                    });
                  return [...urls];
                }
                """
            )
            for url in page_links:
                if url not in found_urls:
                    found_urls.add(url)
                    items.append(
                        ScrapedItem(
                            source=base_source,
                            source_id=f"{base_id_prefix}_{len(items) + 1}",
                            source_url=url,
                            meta={"scrape_method": "dom_scan"},
                        )
                    )

        return self._dedup(items)

    def _parse_snapchat_json(
        self, blob: str, base_source: str, base_id_prefix: str
    ) -> list[ScrapedItem]:
        """Best-effort extraction of video URLs from Snapchat JSON blobs."""
        import json

        items: list[ScrapedItem] = []
        try:
            data = json.loads(blob)
        except (json.JSONDecodeError, TypeError):
            return items

        def _walk(obj: Any, path: str = "") -> None:
            if isinstance(obj, dict):
                for k, v in obj.items():
                    _walk(v, f"{path}.{k}")
            elif isinstance(obj, list):
                for elem in obj:
                    _walk(elem, path)
            elif isinstance(obj, str):
                if ".mp4" in obj or "snapcat" in obj or "cdn-media" in obj:
                    url = obj.strip().strip('"').strip("'")
                    if url.startswith("http"):
                        items.append(
                            ScrapedItem(
                                source=base_source,
                                source_id=f"{base_id_prefix}_{len(items) + 1}",
                                source_url=url,
                                meta={"scrape_method": "json_blob", "json_path": path},
                            )
                        )

        _walk(data)
        return items

    def _scan_page(
        self, url: str, source: str, id_prefix: str, max_results: int = 20
    ) -> list[ScrapedItem]:
        """Open a URL, wait for content, and extract video links."""
        items: list[ScrapedItem] = []
        context = self._new_context()
        try:
            page = context.new_page()
            page.set_default_timeout(30_000)
            page.goto(url, wait_until="domcontentloaded")
            # Give client-side JS up to 10s to render
            page.wait_for_timeout(10_000)
            items = self._extract_video_urls_from_page(page, source, id_prefix)
        except Exception as exc:
            log.error("Snapchat page scan failed for %r: %s", url, exc)
        finally:
            try:
                context.close()
            except Exception:
                pass
        return items[:max_results]

    # ------------------------------------------------------------------
    # Public scraper interface
    # ------------------------------------------------------------------
    def scrape_handles(
        self, handles: list[str], max_per_run: int, download: bool = True
    ) -> list[ScrapedItem]:
        """Scrape public video URLs from Snapchat profile pages.

        When **download** is True the discovered video URLs are passed through
        yt-dlp so the resulting :class:`ScrapedItem` carries a real
        ``local_path`` pointing at a downloaded MP4.
        """
        out: list[ScrapedItem] = []
        handles = [h for h in handles if h]
        if not handles:
            return out
        per = max(1, max_per_run // len(handles))
        downloader = None
        if download:
            try:
                from .yt_dlp_scraper import YtDlpScraper as _DL
                downloader = _DL()
            except Exception as exc:
                log.warning("yt-dlp downloader unavailable for Snapchat downloads: %s", exc)

        for h in handles:
            username = self._normalize_handle(h)
            if not username:
                log.warning("Could not parse Snapchat handle: %r", h)
                continue
            url = self._profile_url(username)
            items = self._scan_page(
                url=url,
                source="snapchat",
                id_prefix=username,
                max_results=per,
            )
            for it in items:
                it.author = username
                if download and downloader and not it.local_path:
                    try:
                        path = downloader._download(it)
                        if path:
                            it.local_path = path
                    except Exception as exc:
                        log.warning("yt-dlp snap download failed for %r: %s", it.source_url, exc)
            out.extend(items)
        return self._dedup(out)[:max_per_run]

    def scrape_hashtags(
        self, hashtags: list[str], max_per_run: int, download: bool = True
    ) -> list[ScrapedItem]:
        """Scrape from the Snapchat Spotlight page (public trending videos)."""
        out: list[ScrapedItem] = []
        hashtags = [t for t in hashtags if t]
        if not hashtags:
            return out
        per = max(1, max_per_run // max(len(hashtags), 1))
        downloader = None
        if download:
            try:
                from .yt_dlp_scraper import YtDlpScraper as _DL
                downloader = _DL()
            except Exception as exc:
                log.warning("yt-dlp downloader unavailable for Snapchat downloads: %s", exc)

        for tag in hashtags:
            tag_clean = tag.lstrip("#")
            items = self._scan_page(
                url=_SPOTLIGHT_URL,
                source="snapchat",
                id_prefix=f"spotlight_{tag_clean}",
                max_results=per,
            )
            for it in items:
                it.meta = {**(it.meta or {}), "hashtag": tag_clean}
                if download and downloader and not it.local_path:
                    try:
                        path = downloader._download(it)
                        if path:
                            it.local_path = path
                    except Exception as exc:
                        log.warning("yt-dlp snap download failed for %r: %s", it.source_url, exc)
            out.extend(items)
        return self._dedup(out)[:max_per_run]

    # ------------------------------------------------------------------
    # Snapchat CDN downloader
    # ------------------------------------------------------------------
    def _download_snapchat_cdn(
        self, items: list[ScrapedItem], tag: str = "spotlight"
    ) -> None:
        """Download snapchat CDN video URLs via Playwright + direct HTTP.

        ``yt-dlp`` does not understand snapchat's CDN host names
        (``bolt-gcdn.sc-cdn.net``, ``cf-st.sc-cdn.net``), so we intercept the
        network requests ourselves and then ``GET`` the stream URL directly with
        the correct ``Referer`` header.
        """
        if not items:
            return
        to_download = [it for it in items if not it.local_path and it.source_url]
        if not to_download:
            return

        try:
            from .yt_dlp_scraper import YtDlpScraper  # noqa: F401
            content_dir = YtDlpScraper().content_dir
        except Exception:
            try:
                from app.config import settings
                content_dir = settings.content_path
            except Exception:
                content_dir = "."

        _HEADERS = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.snapchat.com/spotlight",
            "Origin": "https://www.snapchat.com",
        }

        try:
            from playwright.sync_api import sync_playwright as _sp
        except ImportError:
            log.debug("playwright not installed for snapchat CDN downloader")
            return

        found: list[str] = []
        browser = None
        ctx = None
        page = None
        try:
            browser = self._get_browser()
            ctx = self._new_context()
            page = ctx.new_page()
            page.set_default_timeout(30000)

            seen: set[str] = set()
            for it in to_download:
                src_url = it.source_url or ""
                if "snapchat.com/" in src_url:
                    page_url = src_url
                else:
                    page_url = _SPOTLIGHT_URL
                tag_clean = (it.meta or {}).get("hashtag", tag) if isinstance(it.meta, dict) else tag

                def _on_req(req):
                    u = req.url
                    if ("sc-cdn.net" not in u) and ("bolt-gcdn" not in u):
                        return
                    # skip image / snapshot CDN requests
                    path = u.split("?")[0]
                    if "/bk/" in path:
                        return
                    if u not in seen:
                        seen.add(u)
                        found.append(u)

                page.on("request", _on_req)
                try:
                    page.goto(page_url, wait_until="domcontentloaded")
                except Exception:
                    pass
                page.wait_for_timeout(5000)
                page.remove_listener("request", _on_req)

                # try to match found URLs against the item we just scanned
                if found:
                    it.source_url = found[-1]
                time.sleep(1)
        except Exception as exc:
            log.debug("snap CDN browser open failed: %s", exc)
        finally:
            if ctx:
                try:
                    ctx.close()
                except Exception:
                    pass

        # HTTP-download the captured CDN URLs
        import requests as _requests

        dest_map: dict[str, str] = {}  # source_url -> local_path
        for url in found:
            if url in dest_map:
                continue
            try:
                r = _requests.get(url, headers=_HEADERS, timeout=25, allow_redirects=True, stream=True)
                if r.status_code != 200 or len(r.content) < 50_000:
                    continue
                fname = f"snap_{int(time.time())}_{len(dest_map)}.mp4"
                dest = os.path.join(content_dir, fname)
                with open(dest, "wb") as fh:
                    for chunk in r.iter_content(64 * 1024):
                        if chunk:
                            fh.write(chunk)
                dest_map[url] = dest
            except Exception as exc:
                log.debug("snap CDN GET failed %r: %s", url[:60], exc)

        # Map back to items and populate minimal metadata via ffprobe
        for it in items:
            if it.local_path:
                continue
            mapped = dest_map.get(it.source_url)
            if not mapped:
                continue
            it.local_path = mapped
            try:
                import subprocess as _subprocess
                _out = _subprocess.run(
                    [
                        "ffprobe", "-v", "error",
                        "-select_streams", "v:0",
                        "-show_entries", "stream=duration,width,height,bit_rate",
                        "-of", "default=noprint_wrappers=1",
                        mapped,
                    ],
                    capture_output=True, text=True, check=True,
                )
                for line in _out.stdout.splitlines():
                    if "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    if k.strip() == "duration":
                        try:
                            it.duration_sec = max(0, int(float(v.strip())))
                        except Exception:
                            pass
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def close(self) -> None:
        """Release browser resources."""
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass