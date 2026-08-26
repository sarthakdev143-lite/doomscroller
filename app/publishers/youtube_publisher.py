"""YouTube Data API v3 publisher using OAuth2 user credentials."""
from __future__ import annotations

import os
from typing import Any, Optional

from .base import PublishResult, Publisher


class YouTubePublisher(Publisher):
    def __init__(self) -> None:
        from ..config import settings
        self.client_secrets = settings.youtube_client_secrets
        self.token_path = settings.youtube_token_path
        self._youtube: Optional[Any] = None

    def _client(self):
        if self._youtube is not None:
            return self._youtube
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("Google API client libs not installed") from e

        scopes = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]
        creds: Optional[Credentials] = None
        if os.path.exists(self.token_path):
            try:
                creds = Credentials.from_authorized_user_file(self.token_path, scopes)
            except Exception:
                creds = None
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception as e:
                    raise RuntimeError(f"YouTube token refresh failed: {e}. Delete {self.token_path} and re-auth via: python scripts/youtube_oauth.py") from e
            else:
                if not os.path.exists(self.client_secrets):
                    raise RuntimeError(f"YouTube OAuth not configured. Place client_secret.json at {self.client_secrets} and run: python scripts/youtube_oauth.py (or visit http://localhost:8000 on first publish)")
                # Don't block headless phone with browser; fail fast with instructive error
                if os.environ.get("DOOMSCROLLER_HEADLESS", "0") == "1" or not os.path.exists(self.token_path):
                    # Try browser flow but with short timeout-friendly message
                    raise RuntimeError(f"YouTube token missing at {self.token_path}. On your PC/phone browser run: python scripts/youtube_oauth.py --secrets {self.client_secrets} --token {self.token_path}")
                flow = InstalledAppFlow.from_client_secrets_file(self.client_secrets, scopes)
                creds = flow.run_local_server(port=0, open_browser=True)
            os.makedirs(os.path.dirname(self.token_path) or ".", exist_ok=True)
            with open(self.token_path, "w") as fh:
                fh.write(creds.to_json())
        self._youtube = build("youtube", "v3", credentials=creds)
        return self._youtube

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

        yt = self._client()
        body = {
            "snippet": {
                "title": title[:100],
                "description": description[:5000],
                "tags": tags[:500],
                "categoryId": str(category),
            },
            "status": {"privacyStatus": privacy},
        }
        media = googleapiclient.http.MediaFileUpload(file_path, resumable=True, chunksize=-1)
        request = yt.videos().insert(part="snippet,status", body=body, media_body=media)

        response: dict = {}
        while response is None or "id" not in response:
            status, response = request.next_chunk()
        return PublishResult(
            video_id=response.get("id", ""),
            status="uploaded" if response.get("id") else "failed",
            raw=response,
        )
