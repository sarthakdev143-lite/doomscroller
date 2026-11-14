"""Reads what YouTube knows about the channel's videos. Unlike the publisher's fetch_stats, failures raise."""
from __future__ import annotations

import logging
import math
import re
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional

from app.config import settings
from app.publishers import MockPublisher, YouTubePublisher, get_publisher

from .factors import aware

log = logging.getLogger(__name__)

_BATCH_STATS = 50
_BATCH_ANALYTICS = 200
_UPLOADS_PER_PAGE = 50
_UPLOAD_PAGES = 20
_METRICS = ("views,engagedViews,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,"
            "likes,comments,shares,subscribersGained,subscribersLost")
_ISO_DURATION_RE = re.compile(
    r"^P(?:(?P<weeks>\d+(?:\.\d+)?)W)?(?:(?P<days>\d+(?:\.\d+)?)D)?"
    r"(?:T(?:(?P<hours>\d+(?:\.\d+)?)H)?(?:(?P<minutes>\d+(?:\.\d+)?)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$",
    re.IGNORECASE,
)
_QUERY_SECRET_RE = re.compile(
    r"([?&](?:key|api_key|access_token|refresh_token|client_secret|token)=)[^&#\s\"']+",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_JSON_SECRET_RE = re.compile(
    r"(\"(?:access_token|refresh_token|client_secret|api_key)\"\s*:\s*\")[^\"]+(\")",
    re.IGNORECASE,
)
_KV_SECRET_RE = re.compile(
    r"(\b(?:access_token|refresh_token|client_secret|api_key)=)[^\s&\"',;)]+",
    re.IGNORECASE,
)
_BOT_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")


def parse_duration(value: object) -> int:
    """Parse an ISO-8601 duration (PT45S, PT1M2S, P1DT2H, P0D) into whole seconds; 0 on malformed input."""
    if not isinstance(value, str):
        return 0
    s = value.strip()
    if not s or s.upper() in ("P", "PT") or s.upper().endswith("T"):
        return 0
    match = _ISO_DURATION_RE.match(s)
    if not match or all(v is None for v in match.groupdict().values()):
        return 0
    try:
        total = (
            float(match.group("weeks") or 0) * 604800
            + float(match.group("days") or 0) * 86400
            + float(match.group("hours") or 0) * 3600
            + float(match.group("minutes") or 0) * 60
            + float(match.group("seconds") or 0)
        )
    except (TypeError, ValueError):
        return 0
    return max(0, int(total)) if math.isfinite(total) else 0


parse_iso8601_duration = parse_duration


def scrub_secrets(text: str) -> str:
    """Remove API keys, OAuth bearer/refresh tokens, and configured secrets from text."""
    if not text:
        return ""
    out = _QUERY_SECRET_RE.sub(r"\1[REDACTED]", text)
    out = _BEARER_RE.sub(r"\1[REDACTED]", out)
    out = _JSON_SECRET_RE.sub(r"\1[REDACTED]\2", out)
    out = _KV_SECRET_RE.sub(r"\1[REDACTED]", out)
    out = _BOT_TOKEN_RE.sub("bot[REDACTED]", out)
    for secret in (
        getattr(settings, "youtube_api_key", ""),
        settings.review_token,
        settings.telegram_bot_token,
    ):
        clean = (secret or "").strip()
        if clean:
            out = out.replace(clean, "[REDACTED]")
    return out


def _scrub_exception(exc: BaseException, _depth: int = 0) -> None:
    if _depth > 4 or exc is None:
        return
    for attr in ("uri", "reason", "error_details"):
        val = getattr(exc, attr, None)
        if isinstance(val, str):
            scrubbed = scrub_secrets(val)
            if scrubbed != val:
                try:
                    setattr(exc, attr, scrubbed)
                except Exception:
                    pass
    if not isinstance(exc, OSError) and getattr(exc, "args", None):
        new_args = tuple(scrub_secrets(a) if isinstance(a, str) else a for a in exc.args)
        if new_args != exc.args:
            try:
                exc.args = new_args
            except Exception:
                pass
    if exc.__cause__ is not None:
        _scrub_exception(exc.__cause__, _depth + 1)
    if exc.__context__ is not None and exc.__context__ is not exc.__cause__:
        _scrub_exception(exc.__context__, _depth + 1)


def _clean_video_ids(video_ids: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in video_ids or ():
        vid = str(raw or "").strip()
        if not vid or vid.startswith("MOCK_") or vid in seen:
            continue
        seen.add(vid)
        out.append(vid)
    return out


def _safe_int(value: object) -> int:
    if value is None or isinstance(value, bool):
        return 0
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0
    return max(0, int(parsed)) if math.isfinite(parsed) else 0


def _rows(response: dict) -> list[dict]:
    names = [h["name"] for h in response.get("columnHeaders", [])]
    return [dict(zip(names, row)) for row in response.get("rows") or []]


class Insights:
    def __init__(self, publisher: YouTubePublisher) -> None:
        self._publisher = publisher

    def stats(self, video_ids: list[str]) -> dict[str, dict]:
        """Live counters. A video missing from the result was deleted or blocked."""
        clean_ids = _clean_video_ids(video_ids)
        if not clean_ids:
            return {}
        try:
            client = self._publisher._client()
            out: dict[str, dict] = {}
            for start in range(0, len(clean_ids), _BATCH_STATS):
                batch = clean_ids[start:start + _BATCH_STATS]
                response = client.videos().list(part="statistics,status", id=",".join(batch)).execute()
                for item in response.get("items", []):
                    item_id = item.get("id")
                    if not item_id:
                        continue
                    stats = item.get("statistics") or {}
                    status = item.get("status") or {}
                    row: dict = {
                        "views": _safe_int(stats.get("viewCount", 0)),
                        "likes": _safe_int(stats.get("likeCount", 0)),
                        "comments": _safe_int(stats.get("commentCount", 0)),
                        "privacy": status.get("privacyStatus", "") or "",
                        "upload_status": status.get("uploadStatus", "") or "",
                        "rejection_reason": status.get("rejectionReason", "") or "",
                        "failure_reason": status.get("failureReason", "") or "",
                    }
                    details = item.get("contentDetails")
                    if isinstance(details, dict) and "duration" in details:
                        row["duration"] = parse_duration(details.get("duration"))
                    out[item_id] = row
            return out
        except Exception as exc:
            _scrub_exception(exc)
            raise

    def retention(self, video_ids: list[str], start: date, end: date) -> dict[str, dict]:
        """Per-video watch metrics. YouTube publishes these two or three days late."""
        clean_ids = _clean_video_ids(video_ids)
        if not clean_ids:
            return {}
        try:
            client = self._publisher.analytics_client()
            out: dict[str, dict] = {}
            for first in range(0, len(clean_ids), _BATCH_ANALYTICS):
                batch = clean_ids[first:first + _BATCH_ANALYTICS]
                response = client.reports().query(
                    ids="channel==MINE", startDate=start.isoformat(), endDate=end.isoformat(),
                    metrics=_METRICS, dimensions="video", filters="video==" + ",".join(batch),
                    sort="-views", maxResults=_BATCH_ANALYTICS,
                ).execute()
                for row in _rows(response):
                    out[row.pop("video")] = row
            return out
        except Exception as exc:
            _scrub_exception(exc)
            raise

    def verify_token(self) -> None:
        try:
            self._publisher.refresh_now()
        except Exception as exc:
            _scrub_exception(exc)
            raise

    def channel_name(self) -> Optional[str]:
        try:
            items = self._publisher._client().channels().list(part="snippet", mine=True).execute().get("items")
            return items[0]["snippet"]["title"] if items else None
        except Exception as exc:
            _scrub_exception(exc)
            raise

    def channel_identity(self) -> Optional[tuple[str, str]]:
        """(id, title) of the channel this token actually belongs to, for catching a wrong-channel token."""
        try:
            items = self._publisher._client().channels().list(part="snippet", mine=True).execute().get("items")
            return (items[0]["id"], items[0]["snippet"]["title"]) if items else None
        except Exception as exc:
            _scrub_exception(exc)
            raise

    def probe_analytics(self) -> None:
        """One row is enough to show the API is on and this token may read it."""
        try:
            end = date.today() - timedelta(days=1)  # safely behind YouTube's Pacific calendar
            self._publisher.analytics_client().reports().query(
                ids="channel==MINE", startDate=(end - timedelta(days=30)).isoformat(), endDate=end.isoformat(),
                metrics="views",
            ).execute()
        except Exception as exc:
            _scrub_exception(exc)
            raise

    def feed_share(self, video_id: str, start: date, end: date) -> Optional[float]:
        """Share of views from the Shorts feed: how much of its reach YouTube handed it."""
        clean = str(video_id or "").strip()
        if not clean or clean.startswith("MOCK_"):
            return None
        try:
            response = self._publisher.analytics_client().reports().query(
                ids="channel==MINE", startDate=start.isoformat(), endDate=end.isoformat(),
                metrics="views", dimensions="insightTrafficSourceType", filters=f"video=={clean}",
            ).execute()
            by_source = {r["insightTrafficSourceType"]: r["views"] for r in _rows(response)}
            total = sum(by_source.values())
            return round(by_source.get("SHORTS", 0) / total, 3) if total else None
        except Exception as exc:
            _scrub_exception(exc)
            raise

    def uploads_since(self, since: Optional[datetime] = None) -> list[dict]:
        """The channel's uploads, newest first, back to `since`. A partial list raises, since a miss means upload again."""
        from googleapiclient.errors import HttpError

        since_utc = aware(since).astimezone(timezone.utc) if since is not None else None
        try:
            client = self._publisher._client()
            channels = client.channels().list(part="contentDetails", mine=True).execute().get("items")
            if not channels:
                raise RuntimeError("this token has no YouTube channel")
            playlist = channels[0]["contentDetails"]["relatedPlaylists"]["uploads"]
            uploads: list[dict] = []
            page = None
            for _ in range(_UPLOAD_PAGES):
                paging = {"pageToken": page} if page else {}
                try:
                    response = client.playlistItems().list(part="snippet", playlistId=playlist,
                                                           maxResults=_UPLOADS_PER_PAGE, **paging).execute()
                except HttpError as exc:
                    # A channel that has never uploaded answers with 404 for its uploads list.
                    if exc.resp.status == 404 and not page:
                        return []
                    raise
                found = [_upload(item) for item in response.get("items", [])]
                uploads.extend(found)
                page = response.get("nextPageToken")
                if not page or (since_utc and any(u["published_at"] and u["published_at"] < since_utc for u in found)):
                    return uploads
            raise RuntimeError(f"more than {_UPLOAD_PAGES} pages of uploads to read")
        except Exception as exc:
            _scrub_exception(exc)
            raise


YouTubeInsights = Insights


def _upload(item: dict) -> dict:
    snippet = item.get("snippet", {})
    try:
        parsed = datetime.fromisoformat(snippet.get("publishedAt", ""))
        published_at = aware(parsed).astimezone(timezone.utc)
    except (TypeError, ValueError):
        published_at = None
    return {"id": snippet.get("resourceId", {}).get("videoId", ""), "title": snippet.get("title", ""),
            "published_at": published_at}


def _publisher_for(channel):
    """The legacy channel (or none) uses the global publisher; a named one its own token."""
    from app.growth import channels

    if channel is None or channels.is_default(channel):
        return get_publisher()
    return channels.publisher_for(channel)


def insights_for(channel=None) -> Optional[Insights]:
    """A reader bound to a channel's token. None under mock mode or when the channel has no Cloud project."""
    publisher = _publisher_for(channel)
    return None if settings.use_mock or isinstance(publisher, MockPublisher) else Insights(publisher)


def get_insights() -> Optional[Insights]:
    """None under mock mode, where there is nothing real to read."""
    return insights_for(None)


def account_mismatch(channel, insights: Optional[Insights]) -> Optional[str]:
    """Why this token must not act for this channel, or None.

    Every Google sign-in looks alike, so a re-authorisation can land on another account. A clip posted
    there cannot be taken back, and reading there makes every video of this channel look deleted, which
    counts as strikes. Only a named channel that recorded its YouTube id at authorisation can be checked.
    Raises when Google cannot be asked: not knowing is not the same as a match.
    """
    from app.growth import channels

    if channel is None or channels.is_default(channel) or not channel.youtube_channel_id or insights is None:
        return None
    identity = insights.channel_identity()
    if identity and identity[0] == channel.youtube_channel_id:
        return None
    found = f"another YouTube channel ({identity[1]})" if identity else "an account with no YouTube channel"
    return (f"the token for channel {channel.slug} belongs to {found}. Re-run "
            f"scripts/youtube_oauth.py --channel {channel.slug} --force and pick the right account")
