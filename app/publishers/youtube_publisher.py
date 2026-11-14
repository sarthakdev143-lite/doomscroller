"""YouTube Data API v3 publisher using OAuth2 user credentials."""
from __future__ import annotations

import errno
import json
import math
import os
import re
import tempfile
from typing import Any, Optional

from .base import PublishResult, Publisher, TokenProblem, UploadOutcomeUnknown


UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
READ_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
ANALYTICS_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"
SCOPES = [UPLOAD_SCOPE, READ_SCOPE, ANALYTICS_SCOPE]
_LOCAL_ERRNOS = {
    errno.ENOSPC, errno.EDQUOT, errno.EROFS, errno.EACCES, errno.EPERM,
    errno.ENOENT, errno.EISDIR, errno.ENOTDIR, errno.EIO,
}
_REAUTH = "scripts/youtube_oauth.py --force"
_REFUSALS = (
    (
        ("accessnotconfigured", "service_disabled", "has not been used", "it is disabled"),
        "the {api} is not switched on for this Google Cloud project. Enable it under APIs & Services > Library, "
        "wait a few minutes and try again.",
    ),
    (
        ("insufficient", "insufficient_scope"),
        "this token lacks the permission the {api} needs, so a box was probably left unticked. "
        f"Run {_REAUTH} and tick every box.",
    ),
    (
        ("uploadlimitexceeded", "exceeded the number of videos"),
        "this YouTube channel hit its daily upload limit (24 hours). Wait for the rolling window to clear "
        "(or complete channel verification in YouTube Studio) and try again.",
    ),
    (
        ("quota", "ratelimit", "userratelimitexceeded"),
        "the {api} quota is used up for today. It resets at midnight Pacific, so try again then.",
    ),
)
_ANGLE_BRACKETS = re.compile(r"[<>]")
_VALID_PRIVACIES = frozenset({"public", "unlisted", "private"})
_YT_TAGS_CHAR_BUDGET = 480


def _extract_oauth_error_text(exc: Any) -> str:
    raw = getattr(exc, "content", b"")
    if not raw:
        return ""
    try:
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        data = json.loads(text)
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    err = data.get("error")
    desc = data.get("error_description")
    if isinstance(err, str):
        return f"{err} {desc or ''}".strip()
    return ""


def explain_api_error(exc: Exception, api: str = "YouTube Data API v3") -> str:
    """A plain cause and the next step, for someone reading a setup check rather than a log."""
    from google.auth.exceptions import TransportError
    from googleapiclient.errors import HttpError
    from httplib2 import HttpLib2Error

    text = str(exc)
    if "invalid_grant" in text:
        return (
            "Google refused the token (invalid_grant): it was revoked, or it expired, because tokens die after "
            "7 days while the OAuth consent screen is in Testing. Publish the app to production under Audience "
            f"in Google Auth Platform, then run {_REAUTH}."
        )
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
    oauth_extra = _extract_oauth_error_text(exc)
    said = f"{exc.reason} {exc.error_details} {oauth_extra}".lower()
    if "invalid_grant" in said:
        return (
            "Google refused the token (invalid_grant): it was revoked, or it expired, because tokens die after "
            "7 days while the OAuth consent screen is in Testing. Publish the app to production under Audience "
            f"in Google Auth Platform, then run {_REAUTH}."
        )
    for words, advice in _REFUSALS:
        if any(word in said for word in words):
            return advice.format(api=api)
    if exc.resp.status == 401:
        return f"Google no longer accepts this token. Run {_REAUTH}."
    if exc.resp.status == 429:
        return f"Google rate-limited the {api} request (HTTP 429: too many requests). Wait a few minutes and try again."
    if exc.resp.status >= 500:
        return f"Google had a problem on its side ({exc.resp.status}), so try again in a few minutes."
    owner = " Check this account owns the channel." if exc.resp.status == 403 else ""
    reason = exc.reason
    if not reason or str(reason).strip().lower() == "ok":
        reason = oauth_extra or f"HTTP {exc.resp.status}"
    return f"Google refused the {api} request: {reason}.{owner}"


def write_token_file(path: str, contents: str) -> None:
    """A crash mid-write must leave the old token whole, and readers must never see half of one.
    A new file also means an old token's loose mode does not survive."""
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    descriptor, temp = tempfile.mkstemp(dir=parent, prefix="token-", suffix=".tmp")
    try:
        os.chmod(temp, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as fh:
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


def _safe_stat_int(value: Any) -> int:
    if value is None or isinstance(value, bool):
        return 0
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(parsed) or parsed < 0:
        return 0
    return int(parsed)


def _sanitize_youtube_tags(tags: list[str] | None) -> list[str]:
    """Strip YouTube-illegal angle brackets, deduplicate, and stay inside YouTube's 500-char tag budget."""
    out: list[str] = []
    seen: set[str] = set()
    used_chars = 0
    for raw in tags or []:
        if not isinstance(raw, str):
            continue
        cleaned = _ANGLE_BRACKETS.sub("", raw).strip()
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        # YouTube counts quotes around multi-word tags plus comma separators toward its 500-char ceiling.
        tag_cost = len(cleaned) + (2 if " " in cleaned else 0) + (1 if out else 0)
        if used_chars + tag_cost > _YT_TAGS_CHAR_BUDGET:
            continue
        seen.add(key)
        out.append(cleaned)
        used_chars += tag_cost
    return out


class YouTubePublisher(Publisher):
    def __init__(self, token_path: Optional[str] = None) -> None:
        from ..config import settings
        # A per-channel token path lets several channels publish to their own accounts;
        # falling back to the global path keeps single-channel setups working.
        self.token_path = token_path or settings.youtube_token_path
        self._youtube: Optional[Any] = None
        self._analytics: Optional[Any] = None

    def _load_authorized_user_info(self) -> dict:
        if not self.token_path or not isinstance(self.token_path, str) or not os.path.isfile(self.token_path):
            raise self._token_invalid()
        try:
            with open(self.token_path, encoding="utf-8") as fh:
                info = json.load(fh)
        except Exception as e:
            raise self._token_invalid() from e
        if not isinstance(info, dict):
            raise self._token_invalid()
        for required in ("refresh_token", "client_id", "client_secret"):
            val = info.get(required)
            if not isinstance(val, str) or not val.strip():
                raise self._token_invalid()
        return info

    def _credentials(self, *, force_refresh: bool = False):
        """Never opens a browser: this runs on the scheduler thread, where nobody could log in and it would hang."""
        try:
            from google.auth.exceptions import RefreshError
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("Google API client libs not installed") from e

        info = self._load_authorized_user_info()
        clean_info = dict(info)
        if "expiry" in clean_info:
            exp = clean_info["expiry"]
            if isinstance(exp, str) and exp.endswith("+00:00"):
                clean_info["expiry"] = exp[:-6] + "Z"
            elif not isinstance(exp, str):
                clean_info.pop("expiry", None)
                clean_info.pop("token", None)

        try:
            # Scopes come from the file: asking for more than was granted fails the refresh,
            # and a token from before analytics was added can still upload.
            creds = Credentials.from_authorized_user_info(clean_info)
        except Exception:
            if "expiry" in clean_info:
                clean_info.pop("expiry", None)
                clean_info.pop("token", None)
                try:
                    creds = Credentials.from_authorized_user_info(clean_info)
                except Exception as e:
                    raise self._token_invalid() from e
            else:
                raise self._token_invalid()

        if creds.valid and not force_refresh:
            return creds
        if not creds.refresh_token or not str(creds.refresh_token).strip():
            raise self._token_invalid()
        try:
            creds.refresh(Request())
        except Exception as e:
            problem = TokenProblem if isinstance(e, RefreshError) else RuntimeError
            raise problem(
                f"YouTube token refresh failed: {e}. Tokens die after 7 days while the OAuth consent screen "
                "is in Testing, so publish it to production, then re-auth via: python scripts/youtube_oauth.py --force"
            ) from e
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
        clean_ids = list(
            dict.fromkeys(
                vid.strip()
                for vid in (video_ids or [])
                if isinstance(vid, str) and vid.strip() and not vid.strip().startswith("MOCK_")
            )
        )
        if not clean_ids:
            return {}
        try:
            yt = self._client()
        except Exception:
            return {}

        out: dict = {}
        for start in range(0, len(clean_ids), 50):
            batch = clean_ids[start:start + 50]
            try:
                resp = yt.videos().list(part="statistics", id=",".join(batch)).execute()
            except Exception:
                continue
            for item in (resp or {}).get("items") or []:
                if not isinstance(item, dict):
                    continue
                vid = item.get("id")
                if not isinstance(vid, str) or not vid:
                    continue
                stats = item.get("statistics") if isinstance(item.get("statistics"), dict) else {}
                out[vid] = {
                    "viewCount": _safe_stat_int(stats.get("viewCount", 0)),
                    "likeCount": _safe_stat_int(stats.get("likeCount", 0)),
                    "commentCount": _safe_stat_int(stats.get("commentCount", 0)),
                }
        return out

    def upload(self, *, file_path: str, title: str, description: str,
               tags: list[str], category: int, privacy: str) -> PublishResult:
        import googleapiclient.http

        from ..config import settings
        from ..metadata import youtube_description, youtube_title

        if not file_path or not os.path.isfile(file_path):
            raise FileNotFoundError(f"upload file not found: {file_path}")

        clean_privacy = (privacy or "public").strip().lower()
        if clean_privacy not in _VALID_PRIVACIES:
            clean_privacy = "private"

        yt = self._client()
        body = {
            "snippet": {
                "title": youtube_title(title),
                "description": youtube_description(description),
                "tags": _sanitize_youtube_tags(tags),
                "categoryId": str(category),
            },
            "status": {
                "privacyStatus": clean_privacy,
                "selfDeclaredMadeForKids": settings.youtube_made_for_kids,
            },
        }
        media = googleapiclient.http.MediaFileUpload(file_path, resumable=True, chunksize=-1)
        request = yt.videos().insert(part="snippet,status", body=body, media_body=media)

        response = None
        while response is None:
            _status, response = _send(request)
        if not isinstance(response, dict):
            response = {}
        vid = str(response.get("id") or "")
        return PublishResult(
            video_id=vid,
            status="uploaded" if vid else "failed",
            raw=response,
        )
