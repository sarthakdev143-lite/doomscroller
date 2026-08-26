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
            page.set_default_timeout(15_000)
            page.goto(url, wait_until="domcontentloaded")
            try:
                page.wait_for_selector('script[type="application/json"]', timeout=4000)
            except Exception:
                pass
            page.wait_for_timeout(2500)
            items = self._extract_video_urls_from_page(page, source, id_prefix)
            if not items:
                try:
                    page.wait_for_selector('a[href], video', timeout=2000)
                    page.wait_for_timeout(1000)
                    items = self._extract_video_urls_from_page(page, source, id_prefix)
                except Exception:
                    pass
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
    def _scrape_single(self, url: str, source: str, prefix: str, per: int) -> list[ScrapedItem]:
        p = None
        b = None
        ctx = None
        try:
            from playwright.sync_api import sync_playwright as _sp
            p = _sp().start()
            b = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled","--no-sandbox","--disable-dev-shm-usage"])
            ctx = b.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", viewport={"width":1280,"height":900}, locale="en-US")
            ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
            page = ctx.new_page()
            page.set_default_timeout(12_000)
            page.goto(url, wait_until="domcontentloaded")
            try:
                page.wait_for_selector('script[type="application/json"]', timeout=2000)
            except Exception:
                pass
            page.wait_for_timeout(1200)
            items = self._extract_video_urls_from_page(page, source, prefix)
            return items[:per]
        except Exception as exc:
            # greenlet / EPIPE errors are common in threaded playwright, log debug only
            log.debug("Snapchat parallel scan failed for %r: %s", url, exc)
            return []
        finally:
            try:
                if ctx: ctx.close()
            except Exception:
                pass
            try:
                if b: b.close()
            except Exception:
                pass
            try:
                if p: p.stop()
            except Exception:
                pass

    def _scrape_urls_batch(self, urls: list[tuple[str, str, str]], max_results: int) -> list[ScrapedItem]:
        if not urls:
            return []
        if len(urls) == 1:
            url, source, prefix = urls[0]
            per = max_results
            return self._dedup(self._scrape_single(url, source, prefix, per))[:max_results]
        import concurrent.futures as _fut
        per = max(1, max_results // max(len(urls), 1))
        out: list[ScrapedItem] = []
        workers = min(len(urls), 4)
        with _fut.ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(self._scrape_single, u, s, p, per): (u,s,p) for u,s,p in urls}
            for fut in _fut.as_completed(futs):
                try:
                    items = fut.result()
                    out.extend(items)
                except Exception as exc:
                    log.error("parallel scrape future failed: %s", exc)
                if len(out) >= max_results:
                    break
        return self._dedup(out)[:max_results]

    def _scrape_urls_batch_parallel(self, urls: list[tuple[str, str, str]], max_results: int) -> list[ScrapedItem]:
        return self._scrape_urls_batch(urls, max_results)

    def scrape_handles(
        self, handles: list[str], max_per_run: int, download: bool = True
    ) -> list[ScrapedItem]:
        """Scrape public video URLs from Snapchat profile pages."""
        out: list[ScrapedItem] = []
        handles = [h for h in handles if h]
        if not handles:
            return out
        valid = []
        for h in handles:
            username = self._normalize_handle(h)
            if not username:
                log.warning("Could not parse Snapchat handle: %r", h)
                continue
            valid.append(username)
        if not valid:
            return out
        urls = [(self._profile_url(u), "snapchat", u) for u in valid]
        items = self._scrape_urls_batch(urls, max_results=max_per_run)
        if download and items:
            try:
                self._download_snapchat_cdn(items)
            except Exception as exc:
                log.warning("snap CDN download failed: %s", exc)
        return self._dedup(items)[:max_per_run]

    def scrape_hashtags(
        self, hashtags: list[str], max_per_run: int, download: bool = True
    ) -> list[ScrapedItem]:
        """Scrape from the Snapchat Spotlight page (public trending videos)."""
        hashtags = [t for t in hashtags if t]
        if not hashtags:
            return []
        unique_tags = list({t.lstrip("#") for t in hashtags})
        # Spotlight is a single page — scraping it N times is wasteful, do it once
        primary = unique_tags[0] if unique_tags else "spotlight"
        items = self._scrape_urls_batch(
            [(_SPOTLIGHT_URL, "snapchat", f"spotlight_{primary}")],
            max_results=max_per_run,
        )
        for it in items:
            it.meta = {**(it.meta or {}), "hashtag": primary, "hashtags": unique_tags}
        if download and items:
            try:
                self._download_snapchat_cdn(items)
            except Exception as exc:
                log.warning("snap CDN download failed: %s", exc)
        return self._dedup(items)[:max_per_run]

    # ------------------------------------------------------------------
    # Snapchat CDN downloader
    # ------------------------------------------------------------------
    def _download_snapchat_cdn(
        self, items: list[ScrapedItem], tag: str = "spotlight"
    ) -> None:
        """Download snapchat CDN video URLs via single-page intercept + parallel HTTP."""
        if not items:
            return
        to_download = [it for it in items if not it.local_path and it.source_url]
        if not to_download:
            return
        # If source_url already looks like direct CDN, skip intercept and download directly
        direct = [it for it in to_download if "sc-cdn.net" in (it.source_url or "")]
        intercept_needed = [it for it in to_download if "sc-cdn.net" not in (it.source_url or "")]

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
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Referer": "https://www.snapchat.com/spotlight",
            "Origin": "https://www.snapchat.com",
        }

        found: list[str] = [it.source_url for it in direct if it.source_url]

        if intercept_needed:
            try:
                ctx = self._new_context()
                page = ctx.new_page()
                page.set_default_timeout(15000)
                seen: set[str] = set(found)
                def _on_req(req):
                    u = req.url
                    if ("sc-cdn.net" not in u) and ("bolt-gcdn" not in u):
                        return
                    if "/bk/" in u.split("?")[0]:
                        return
                    if u not in seen:
                        seen.add(u)
                        found.append(u)
                page.on("request", _on_req)
                try:
                    page.goto(_SPOTLIGHT_URL, wait_until="domcontentloaded")
                    page.wait_for_timeout(1500)
                    try:
                        page.evaluate("window.scrollBy(0, 800)")
                    except Exception:
                        pass
                    page.wait_for_timeout(800)
                except Exception:
                    pass
                try:
                    ctx.close()
                except Exception:
                    pass
            except Exception as exc:
                log.debug("snap CDN intercept failed: %s", exc)
            # map intercepted CDN urls back to items needing it
            for idx, it in enumerate(intercept_needed):
                if idx < len(found):
                    it.source_url = found[idx]

        import requests as _requests
        import concurrent.futures as _fut

        # dedupe found
        found = list(dict.fromkeys([u for u in found if u and "sc-cdn.net" in u]))
        dest_map: dict[str, str] = {}

        def _fetch(url: str) -> tuple[str, str | None]:
            try:
                r = _requests.get(url, headers=_HEADERS, timeout=15, stream=True)
                if r.status_code != 200:
                    return url, None
                data = b"".join(r.iter_content(64 * 1024))
                if len(data) < 50_000:
                    return url, None
                fname = f"snap_{int(time.time()*1000)}_{abs(hash(url)) % 10000}.mp4"
                dest = os.path.join(str(content_dir), fname)
                with open(dest, "wb") as fh:
                    fh.write(data)
                return url, dest
            except Exception:
                return url, None

        urls_to_fetch = [u for u in found if u not in dest_map]
        # Also fetch direct source_urls that weren't in found
        for it in to_download:
            if it.source_url and it.source_url not in found and "sc-cdn.net" in it.source_url:
                urls_to_fetch.append(it.source_url)
        urls_to_fetch = list(dict.fromkeys(urls_to_fetch))[: max(len(to_download), len(found)) + 4]

        with _fut.ThreadPoolExecutor(max_workers=4) as ex:
            for url, dest in ex.map(_fetch, urls_to_fetch):
                if dest:
                    dest_map[url] = dest

        for it in items:
            if it.local_path:
                continue
            mapped = dest_map.get(it.source_url)
            if not mapped and found:
                # fallback: assign any unused dest
                unused = [d for u, d in dest_map.items() if d not in [x.local_path for x in items]]
                if unused:
                    mapped = unused[0]
                    it.source_url = [u for u, d in dest_map.items() if d == mapped][0]
            if not mapped:
                continue
            it.local_path = mapped
            try:
                import subprocess as _subprocess
                _out = _subprocess.run(
                    ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=duration,width,height,bit_rate", "-of", "default=noprint_wrappers=1", mapped],
                    capture_output=True, text=True, check=True, timeout=8,
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