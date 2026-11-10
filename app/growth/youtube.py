"""Reads what YouTube knows about the channel's videos. Unlike the publisher's fetch_stats, failures raise."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from app.config import settings
from app.publishers import MockPublisher, YouTubePublisher, get_publisher

_BATCH_STATS = 50
_BATCH_ANALYTICS = 200
_UPLOADS_PER_PAGE = 50
_UPLOAD_PAGES = 20
_METRICS = ("views,engagedViews,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,"
            "likes,comments,shares,subscribersGained,subscribersLost")


def _rows(response: dict) -> list[dict]:
    names = [h["name"] for h in response.get("columnHeaders", [])]
    return [dict(zip(names, row)) for row in response.get("rows") or []]


class Insights:
    def __init__(self, publisher: YouTubePublisher) -> None:
        self._publisher = publisher

    def stats(self, video_ids: list[str]) -> dict[str, dict]:
        """Live counters. A video missing from the result was deleted or blocked."""
        client = self._publisher._client()
        out: dict[str, dict] = {}
        for start in range(0, len(video_ids), _BATCH_STATS):
            batch = video_ids[start:start + _BATCH_STATS]
            response = client.videos().list(part="statistics,status", id=",".join(batch)).execute()
            for item in response.get("items", []):
                stats, status = item.get("statistics", {}), item.get("status", {})
                out[item["id"]] = {
                    "views": int(stats.get("viewCount", 0)), "likes": int(stats.get("likeCount", 0)),
                    "comments": int(stats.get("commentCount", 0)),
                    "privacy": status.get("privacyStatus", ""),
                    "upload_status": status.get("uploadStatus", ""),
                    "rejection_reason": status.get("rejectionReason", ""),
                    "failure_reason": status.get("failureReason", ""),
                }
        return out

    def retention(self, video_ids: list[str], start: date, end: date) -> dict[str, dict]:
        """Per-video watch metrics. YouTube publishes these two or three days late."""
        client = self._publisher.analytics_client()
        out: dict[str, dict] = {}
        for first in range(0, len(video_ids), _BATCH_ANALYTICS):
            batch = video_ids[first:first + _BATCH_ANALYTICS]
            response = client.reports().query(
                ids="channel==MINE", startDate=start.isoformat(), endDate=end.isoformat(),
                metrics=_METRICS, dimensions="video", filters="video==" + ",".join(batch),
                sort="-views", maxResults=_BATCH_ANALYTICS,
            ).execute()
            for row in _rows(response):
                out[row.pop("video")] = row
        return out

    def verify_token(self) -> None:
        self._publisher.refresh_now()

    def channel_name(self) -> Optional[str]:
        items = self._publisher._client().channels().list(part="snippet", mine=True).execute().get("items")
        return items[0]["snippet"]["title"] if items else None

    def channel_identity(self) -> Optional[tuple[str, str]]:
        """(id, title) of the channel this token actually belongs to, for catching a wrong-channel token."""
        items = self._publisher._client().channels().list(part="snippet", mine=True).execute().get("items")
        return (items[0]["id"], items[0]["snippet"]["title"]) if items else None

    def probe_analytics(self) -> None:
        """One row is enough to show the API is on and this token may read it."""
        end = date.today() - timedelta(days=1)  # safely behind YouTube's Pacific calendar
        self._publisher.analytics_client().reports().query(
            ids="channel==MINE", startDate=(end - timedelta(days=30)).isoformat(), endDate=end.isoformat(),
            metrics="views",
        ).execute()

    def feed_share(self, video_id: str, start: date, end: date) -> Optional[float]:
        """Share of views from the Shorts feed: how much of its reach YouTube handed it."""
        response = self._publisher.analytics_client().reports().query(
            ids="channel==MINE", startDate=start.isoformat(), endDate=end.isoformat(),
            metrics="views", dimensions="insightTrafficSourceType", filters=f"video=={video_id}",
        ).execute()
        by_source = {r["insightTrafficSourceType"]: r["views"] for r in _rows(response)}
        total = sum(by_source.values())
        return round(by_source.get("SHORTS", 0) / total, 3) if total else None


    def uploads_since(self, since: Optional[datetime] = None) -> list[dict]:
        """The channel's uploads, newest first, back to `since`. A partial list raises, since a miss means upload again."""
        from googleapiclient.errors import HttpError

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
            if not page or (since and any(u["published_at"] and u["published_at"] < since for u in found)):
                return uploads
        raise RuntimeError(f"more than {_UPLOAD_PAGES} pages of uploads to read")


def _upload(item: dict) -> dict:
    snippet = item.get("snippet", {})
    try:
        published_at = datetime.fromisoformat(snippet.get("publishedAt", ""))
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
