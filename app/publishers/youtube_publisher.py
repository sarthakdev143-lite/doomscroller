"""YouTube Data API v3 publisher using OAuth2 user credentials."""
from __future__ import annotations

import errno
import os
import tempfile
from typing import Any, Optional

from .base import PublishResult, Publisher, TokenProblem, UploadOutcomeUnknown


UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
READ_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
ANALYTICS_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"
SCOPES = [UPLOAD_SCOPE, READ_SCOPE, ANALYTICS_SCOPE]
_LOCAL_ERRNOS = {errno.ENOSPC, errno.EDQUOT, errno.EROFS, errno.EACCES, errno.EPERM, errno.ENOENT, errno.EISDIR,
                 errno.ENOTDIR, errno.EIO}
_REAUTH = "scripts/youtube_oauth.py --force"
_REFUSALS = (
    (("accessnotconfigured", "service_disabled", "has not been used", "it is disabled"),
     "the {api} is not switched on for this Google Cloud project. Enable it under APIs & Services > Library, "
     "wait a few minutes and try again."),
    (("insufficient",),
     "this token lacks the permission the {api} needs, so a box was probably left unticked. "
     f"Run {_REAUTH} and tick every box."),
    (("quota", "ratelimit"),
     "the {api} quota is used up for today. It resets at midnight Pacific, so try again then."),
)


def explain_api_error(exc: Exception, api: str = "YouTube Data API v3") -> str:
    """A plain cause and the next step, for someone reading a setup check rather than a log."""
    from google.auth.exceptions import TransportError
    from googleapiclient.errors import HttpError
    from httplib2 import HttpLib2Error

    text = str(exc)
    if "invalid_grant" in text:
        return ("Google refused the token (invalid_grant): it was revoked, or it expired, because tokens die after "
                "7 days while the OAuth consent screen is in Testing. Publish the app to production under Audience "
                f"in Google Auth Platform, then run {_REAUTH}.")
    if isinstance(exc, TokenProblem):
        return text
    cause = exc.__cause__ or exc
    # A refresh also saves the token, and sockets raise plain OSError too, so the errno is what tells them apart.
    if isinstance(cause, OSError) and cause.errno in _LOCAL_ERRNOS:
        return f"a problem on this machine, not at Google: {cause}. Check the disk space and the folder's permissions."
    if isinstance(cause, (OSError, HttpLib2Error, TransportError)):
        return f"could not reach Google: {cause}. Check the connection and try again."
    if not isinstance(exc, HttpError):
        return text
    # Not str(exc), which carries the URL and so can match a video id.
    said = f"{exc.reason} {exc.error_details}".lower()
    for words, advice in _REFUSALS:
        if any(word in said for word in words):
            return advice.format(api=api)
    if exc.resp.status == 401:
        return f"Google no longer accepts this token. Run {_REAUTH}."
    if exc.resp.status >= 500:
        return f"Google had a problem on its side ({exc.resp.status}), so try again in a few minutes."
    owner = " Check this account owns the channel." if exc.resp.status == 403 else ""
    return f"Google refused the {api} request: {exc.reason}.{owner}"


def write_token_file(path: str, contents: str) -> None:
    """A crash mid-write must leave the old token whole, and readers must never see half of one.
    A new file also means an old token's loose mode does not survive."""
    descriptor, temp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix="token-", suffix=".tmp")
    try:
        os.chmod(temp, 0o600)
        with os.fdopen(descriptor, "w") as fh:
            fh.write(contents)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, path)
    except BaseException:
        if os.path.exists(temp):
            os.unlink(temp)
        raise


def _send(request):
    """Once the file is on its way, a lost reply or a 5xx does not say whether YouTube kept it."""
    from googleapiclient.errors import HttpError

    try:
        return request.next_chunk()
    except Exception as exc:
        refused = isinstance(exc, HttpError) and exc.resp.status < 500
        if request.resumable_uri is None or refused:
            raise
        raise UploadOutcomeUnknown(f"upload outcome unknown: {exc}") from exc


class YouTubePublisher(Publisher):
    def __init__(self) -> None:
        from ..config import settings
        self.token_path = settings.youtube_token_path
        self._youtube: Optional[Any] = None
        self._analytics: Optional[Any] = None

    def _credentials(self, *, force_refresh: bool = False):
        """Never opens a browser: this runs on the scheduler thread, where nobody could log in and it would hang."""
        try:
            from google.auth.exceptions import RefreshError
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("Google API client libs not installed") from e

        try:
            # Scopes come from the file: asking for more than was granted fails the refresh,
            # and a token from before analytics was added can still upload.
            creds = Credentials.from_authorized_user_file(self.token_path)
        except Exception as e:
            raise self._token_invalid() from e
        if creds.valid and not force_refresh:
            return creds
        if not creds.refresh_token:
            raise self._token_invalid()
        try:
            creds.refresh(Request())
        except Exception as e:
            problem = TokenProblem if isinstance(e, RefreshError) else RuntimeError
            raise problem(f"YouTube token refresh failed: {e}. Tokens die after 7 days while the OAuth consent screen is in Testing, so publish it to production, then re-auth via: python scripts/youtube_oauth.py --force") from e
        write_token_file(self.token_path, creds.to_json())
        return creds

    def _token_invalid(self) -> TokenProblem:
        return TokenProblem(f"YouTube token missing or invalid at {self.token_path}, run scripts/youtube_oauth.py --force")

    def check_ready(self) -> None:
        self._credentials()

    def refresh_now(self) -> None:
        """Asks Google even when the last access token has time left, which hides a dead refresh token for up to an hour."""
        self._credentials(force_refresh=True)

    def _client(self):
        if self._youtube is None:
            from googleapiclient.discovery import build
            self._youtube = build("youtube", "v3", credentials=self._credentials(), cache_discovery=False)
        return self._youtube

    def analytics_client(self):
        if self._analytics is None:
            from googleapiclient.discovery import build
            self._analytics = build("youtubeAnalytics", "v2", credentials=self._credentials(), cache_discovery=False)
        return self._analytics

    def fetch_stats(self, video_ids: list[str]) -> dict:
        try:
            yt = self._client()
            resp = yt.videos().list(part="statistics", id=",".join(video_ids[:50])).execute()
            out: dict = {}
            for item in resp.get("items", []):
                vid = item.get("id")
                stats = item.get("statistics", {})
                out[vid] = {"viewCount": int(stats.get("viewCount", 0)), "likeCount": int(stats.get("likeCount", 0)), "commentCount": int(stats.get("commentCount", 0))}
            return out
        except Exception:
            return {}

    def upload(self, *, file_path: str, title: str, description: str,
               tags: list[str], category: int, privacy: str) -> PublishResult:
        import googleapiclient.http

        from ..config import settings
        from ..metadata import youtube_description, youtube_title
        yt = self._client()
        body = {
            "snippet": {
                "title": youtube_title(title),
                "description": youtube_description(description),
                "tags": tags[:500],
                "categoryId": str(category),
            },
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": settings.youtube_made_for_kids},
        }
        media = googleapiclient.http.MediaFileUpload(file_path, resumable=True, chunksize=-1)
        request = yt.videos().insert(part="snippet,status", body=body, media_body=media)

        response: dict = {}
        while response is None or "id" not in response:
            status, response = _send(request)
        return PublishResult(
            video_id=response.get("id", ""),
            status="uploaded" if response.get("id") else "failed",
            raw=response,
        )
