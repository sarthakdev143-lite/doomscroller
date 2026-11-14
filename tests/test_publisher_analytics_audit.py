"""Wave 9 audit & TDD suite for publishers, OAuth CLI, analytics, discovery, and quota."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httplib2
import pytest
from googleapiclient.errors import HttpError

from app import analytics, discovery, quota
from app.config import settings
from app.db import init_db, session_scope
from app.models import Channel, Creator, QuotaUsage, Video, VideoStatus
from app.publishers.base import Publisher, TokenProblem
from app.publishers.mock_publisher import MockPublisher
from app.publishers.youtube_publisher import (
    ANALYTICS_SCOPE,
    READ_SCOPE,
    SCOPES,
    UPLOAD_SCOPE,
    YouTubePublisher,
    explain_api_error,
    write_token_file,
)


def _load_oauth_module():
    path = Path(__file__).resolve().parent.parent / "scripts" / "youtube_oauth.py"
    spec = importlib.util.spec_from_file_location("youtube_oauth_under_test", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ===========================================================================
# 1. YouTubePublisher.fetch_stats (>50 IDs batching, dedup, partial failures)
# ===========================================================================
def test_fetch_stats_batches_more_than_50_ids_dedups_and_skips_mock(monkeypatch):
    pub = YouTubePublisher(token_path="unused.json")
    queried_batches: list[list[str]] = []

    class FakeListCall:
        def __init__(self, batch_ids: list[str]):
            self._ids = batch_ids

        def execute(self):
            items = []
            for idx, vid in enumerate(self._ids):
                if idx == 0:
                    # Simulate hidden likes/comments on a video
                    items.append({"id": vid, "statistics": {"viewCount": "1200"}})
                else:
                    items.append({
                        "id": vid,
                        "statistics": {
                            "viewCount": str(100 + idx),
                            "likeCount": str(10 + idx),
                            "commentCount": None,
                        },
                    })
            return {"items": items}

    class FakeVideos:
        def list(self, *, part: str, id: str):
            assert part == "statistics"
            batch = id.split(",")
            assert len(batch) <= 50
            queried_batches.append(batch)
            return FakeListCall(batch)

    class FakeYT:
        def videos(self):
            return FakeVideos()

    client_builds = {"n": 0}

    def fake_client():
        client_builds["n"] += 1
        return FakeYT()

    monkeypatch.setattr(pub, "_client", fake_client)

    # Empty / MOCK-only input must not even build the OAuth client
    assert pub.fetch_stats([]) == {}
    assert pub.fetch_stats(["", "   ", "MOCK_12345"]) == {}
    assert client_builds["n"] == 0

    real_ids = [f"vid_{i:03d}" for i in range(120)]
    noisy_input = ["MOCK_skip", "", real_ids[0], *real_ids, real_ids[75]]
    stats = pub.fetch_stats(noisy_input)

    assert [len(b) for b in queried_batches] == [50, 50, 20]
    assert len(stats) == 120
    assert stats["vid_000"] == {"viewCount": 1200, "likeCount": 0, "commentCount": 0}
    assert stats["vid_119"]["viewCount"] == 100 + 19


def test_fetch_stats_preserves_successful_batches_when_later_batch_fails(monkeypatch):
    pub = YouTubePublisher(token_path="unused.json")
    call_num = {"n": 0}

    class FakeVideos:
        def list(self, *, part: str, id: str):
            call_num["n"] += 1
            batch = id.split(",")

            class Call:
                def execute(self_inner):
                    if call_num["n"] == 2:
                        raise RuntimeError("transient 503 on second page")
                    return {
                        "items": [
                            {"id": v, "statistics": {"viewCount": "42", "likeCount": "5", "commentCount": "1"}}
                            for v in batch
                        ]
                    }

            return Call()

    class FakeYT:
        def videos(self):
            return FakeVideos()

    monkeypatch.setattr(pub, "_client", lambda: FakeYT())
    ids = [f"v{i}" for i in range(65)]
    out = pub.fetch_stats(ids)
    assert call_num["n"] == 2
    assert len(out) == 50
    assert out["v0"]["viewCount"] == 42


# ===========================================================================
# 2. Token JSON validation, +00:00/corrupt expiry recovery & write_token_file
# ===========================================================================
@pytest.mark.parametrize("bad_payload", [
    "null",
    "[1, 2, 3]",
    "{}",
    json.dumps({
        "token": "still_looks_fresh",
        "expiry": "2099-01-01T00:00:00Z",
        "refresh_token": "",
        "client_id": "cid",
        "client_secret": "csec",
    }),
    json.dumps({
        "token": "old",
        "expiry": "2020-01-01T00:00:00Z",
        "refresh_token": "rtok",
        "client_id": "   ",
        "client_secret": "csec",
    }),
])
def test_credentials_rejects_non_dict_and_blank_field_tokens(tmp_path, monkeypatch, bad_payload):
    from google.oauth2.credentials import Credentials

    monkeypatch.setattr(
        Credentials,
        "refresh",
        lambda self, req: (_ for _ in ()).throw(AssertionError("must not call Google refresh on invalid token shape")),
    )
    tok = tmp_path / "bad_token.json"
    tok.write_text(bad_payload, encoding="utf-8")
    pub = YouTubePublisher(token_path=str(tok))
    with pytest.raises(TokenProblem):
        pub.check_ready()


@pytest.mark.parametrize("weird_expiry", [
    "2020-01-01T10:00:00+00:00",
    "not-a-timestamp",
    12345,
])
def test_credentials_recovers_token_with_plus00_or_corrupt_expiry_via_refresh(
    tmp_path, monkeypatch, weird_expiry
):
    from google.oauth2.credentials import Credentials

    def fake_refresh(self, _request):
        self.token = "renewed_access_token"
        self.expiry = datetime(2099, 1, 1)

    monkeypatch.setattr(Credentials, "refresh", fake_refresh)
    tok = tmp_path / "weird_expiry.json"
    tok.write_text(
        json.dumps({
            "token": "stale",
            "refresh_token": "valid_refresh",
            "client_id": "valid_cid",
            "client_secret": "valid_csec",
            "token_uri": "https://oauth2.googleapis.com/token",
            "expiry": weird_expiry,
        }),
        encoding="utf-8",
    )
    pub = YouTubePublisher(token_path=str(tok))
    creds = pub._credentials()
    assert creds.token == "renewed_access_token"


def test_write_token_file_creates_missing_parent_directory(tmp_path):
    nested = tmp_path / "deep" / "secrets" / "youtube_token.json"
    write_token_file(str(nested), '{"ok": true}')
    assert json.loads(nested.read_text(encoding="utf-8")) == {"ok": True}


# ===========================================================================
# 3. HTTP error classification & upload safeguards
# ===========================================================================
def test_explain_api_error_classifies_429_upload_limit_and_oauth_string_errors():
    err_429 = HttpError(httplib2.Response({"status": 429, "reason": "Ok"}), b"")
    msg_429 = explain_api_error(err_429)
    assert "Ok" not in msg_429
    assert "rate" in msg_429.lower() or "quota" in msg_429.lower() or "too many" in msg_429.lower()

    upload_cap_body = json.dumps({
        "error": {
            "code": 400,
            "message": "The user has exceeded the number of videos they may upload.",
            "errors": [{"reason": "uploadLimitExceeded", "message": "The user has exceeded the number of videos they may upload."}],
        }
    }).encode("utf-8")
    err_upload_cap = HttpError(httplib2.Response({"status": 400, "reason": "Bad Request"}), upload_cap_body)
    msg_cap = explain_api_error(err_upload_cap)
    assert "upload limit" in msg_cap.lower() or "24 hours" in msg_cap.lower()

    oauth_str_body = json.dumps({
        "error": "insufficient_scope",
        "error_description": "Request had insufficient authentication scopes.",
    }).encode("utf-8")
    err_oauth_str = HttpError(httplib2.Response({"status": 403, "reason": "Ok"}), oauth_str_body)
    msg_oauth = explain_api_error(err_oauth_str)
    assert "tick every box" in msg_oauth


def test_upload_terminates_on_idless_response_sanitizes_tags_and_checks_file(tmp_path, monkeypatch):
    pub = YouTubePublisher(token_path="unused.json")
    captured_bodies: list[dict] = []

    class IdlessRequest:
        resumable_uri = "https://upload/session"
        calls = 0

        def next_chunk(self):
            self.calls += 1
            if self.calls > 3:
                raise AssertionError("infinite loop on id-less upload response")
            return None, {}

    class FakeVideos:
        def insert(self, *, part: str, body: dict, media_body):
            captured_bodies.append(body)
            return IdlessRequest()

    class FakeYT:
        def videos(self):
            return FakeVideos()

    monkeypatch.setattr(pub, "_client", lambda: FakeYT())

    with pytest.raises(FileNotFoundError):
        pub.upload(
            file_path=str(tmp_path / "missing.mp4"),
            title="Title",
            description="Desc",
            tags=["shorts"],
            category=22,
            privacy="public",
        )

    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")
    huge_tags = ["<bad>", "shorts", "SHORTS", *[f"very_long_tag_number_{i:03d}" for i in range(60)]]
    res = pub.upload(
        file_path=str(clip),
        title="Title",
        description="Desc",
        tags=huge_tags,
        category=22,
        privacy="PUBLIC",
    )
    assert res.status == "failed"
    assert res.video_id == ""
    sent_tags = captured_bodies[-1]["snippet"]["tags"]
    assert all("<" not in t and ">" not in t for t in sent_tags)
    # YouTube Data API v3 total tag character budget <= 500
    yt_char_len = sum(len(t) + (2 if " " in t else 0) for t in sent_tags) + max(0, len(sent_tags) - 1)
    assert yt_char_len <= 500
    assert captured_bodies[-1]["status"]["privacyStatus"] == "public"

    assert Publisher().fetch_stats(["vid1"]) == {}
    mock_res = MockPublisher().upload(
        file_path=str(clip), title="T", description="D", tags=None, category=22, privacy="public"  # type: ignore[arg-type]
    )
    assert mock_res.raw["tags"] == []


# ===========================================================================
# 4. Analytics zero / negative / None / NaN / Inf / naive-now safety
# ===========================================================================
def test_analytics_handles_none_negative_nan_inf_and_naive_now():
    # Unsaved SQLAlchemy Video leaves integer fields as None until flush
    unsaved = Video(source="youtube", source_id="u1", source_url="https://x")
    assert analytics.engagement_score(unsaved) == 0.0
    assert analytics.reach_score(unsaved) == 0.0
    assert analytics.viral_score(unsaved) >= 0.0

    partial = Video(source="youtube", source_id="u2", source_url="https://x", views=1000)
    assert analytics.engagement_score(partial) == 0.0

    nan_vid = Video(
        source="youtube",
        source_id="u3",
        source_url="https://x",
        views=float("nan"),  # type: ignore[arg-type]
        likes=float("nan"),  # type: ignore[arg-type]
        comments=-10,
        shares=float("inf"),  # type: ignore[arg-type]
    )
    assert analytics.engagement_score(nan_vid) == 0.0
    assert analytics.reach_score(nan_vid) == 0.0

    neg_likes = Video(
        source="youtube", source_id="u4", source_url="https://x",
        views=1000, likes=-500, comments=0, shares=10,
    )
    # Negative sentinel likes must not cancel out real shares
    assert analytics.engagement_score(neg_likes) > 0.0

    frac_views = Video(
        source="youtube", source_id="u5", source_url="https://x",
        views=0.5,  # type: ignore[arg-type]
        likes=0, comments=0, shares=0,
    )
    assert 0.0 <= analytics.reach_score(frac_views) <= 1.0

    aware_scraped = datetime(2026, 11, 13, 10, 0, tzinfo=timezone.utc)
    naive_now = datetime(2026, 11, 13, 16, 0)  # offset-naive
    timed = Video(
        source="youtube", source_id="u6", source_url="https://x",
        views=50_000, likes=2_000, comments=100, shares=50,
        scraped_at=aware_scraped,
    )
    assert analytics.age_hours(timed, now=naive_now) == pytest.approx(6.0)
    analytics.recompute_scores([timed], now=naive_now)
    assert 0.0 <= timed.viral_score <= 1.0


# ===========================================================================
# 5. Discovery dedup, null/blank/@-prefixed authors & duplicate DB rows
# ===========================================================================
def test_discovery_dedups_handles_tolerates_null_authors_and_duplicate_db_rows():
    init_db()
    now = datetime.now(timezone.utc)
    with session_scope() as db:
        db.query(Creator).delete()
        db.query(Video).delete()
        db.flush()

        added = discovery.ensure_seed_creators(
            db,
            ["@khaby.lame", "khaby.lame", "  @KHABY.LAME  ", "", "   ", "@", None],  # type: ignore[list-item]
            platform="snapchat",
        )
        assert added == 1

        # Add duplicate legacy Creator rows to ensure MultipleResultsFound never triggers
        db.add_all([
            Creator(handle="dup_creator", platform="youtube", score=1.0),
            Creator(handle="dup_creator", platform="youtube", score=3.0),
        ])
        db.add_all([
            Video(
                source="snapchat", source_id="s1", source_url="https://x/1",
                author="khaby.lame", engagement_score=0.8, scraped_at=now,
            ),
            Video(
                source="snapchat", source_id="s2", source_url="https://x/2",
                author="   ", engagement_score=0.9, scraped_at=now,
            ),
            Video(
                source="snapchat", source_id="s3", source_url="https://x/3",
                author="@", engagement_score=0.95, scraped_at=now,
            ),
            Video(
                source="", source_id="s4", source_url="https://x/4",
                author="orphan_author", engagement_score=0.9, scraped_at=now,
            ),
            Video(
                source="snapchat", source_id="s5", source_url="https://x/5",
                author="@newstar", engagement_score=None, scraped_at=now,  # type: ignore[arg-type]
            ),
            Video(
                source="snapchat", source_id="s6", source_url="https://x/6",
                author="newstar", engagement_score=0.7, scraped_at=now,
            ),
        ])
        db.flush()

        assert discovery.discover_creators(db, days=7, top_n=0) == []
        discovered = discovery.discover_creators(db, days=7, top_n=10)
        # @khaby.lame already existed as seed so shouldn't be re-added; newstar is added once
        assert [c.handle.lstrip("@").lower() for c in discovered] == ["newstar"]

        # Boost handles both @-prefixed seed and duplicate legacy rows without crashing
        discovery.boost_creator(db, "snapchat", "khaby.lame", 2.5)
        discovery.boost_creator(db, "youtube", "dup_creator", 1.5)
        discovery.boost_creator(db, "youtube", "dup_creator", float("nan"))

        seed_row = db.query(Creator).filter(Creator.platform == "snapchat", Creator.is_seed.is_(True)).one()
        assert seed_row.score == pytest.approx(3.5)

        assert discovery.list_active_creators(db, limit=0) == []
        active = discovery.list_active_creators(db, limit=20)
        yt_dups = [c for c in active if c.platform == "youtube" and c.handle == "dup_creator"]
        assert len(yt_dups) == 1
        assert yt_dups[0].score == pytest.approx(4.5)


# ===========================================================================
# 6. Quota UTC boundary & long-prefix multi-channel isolation
# ===========================================================================
def test_quota_utc_midnight_boundary_and_long_channel_id_isolation():
    init_db()
    before_midnight_utc = datetime(2026, 11, 13, 23, 59, 59, tzinfo=timezone.utc)
    after_midnight_utc = datetime(2026, 11, 14, 0, 0, 0, tzinfo=timezone.utc)
    # 2026-11-14 02:00 IST (+05:30) is still 2026-11-13 20:30 UTC!
    ist_still_nov13 = datetime(2026, 11, 14, 2, 0, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))

    chan_alpha = "channel_shared_prefix_0001_alpha"
    chan_bravo = "channel_shared_prefix_0001_bravo"

    with session_scope() as db:
        db.query(QuotaUsage).delete()
        db.flush()

        quota.consume_one(db, note="alpha-1", channel_id=chan_alpha, now=before_midnight_utc)
        quota.consume_one(db, note="bravo-1", channel_id=chan_bravo, now=before_midnight_utc)
        quota.consume_one(db, note="bravo-2", channel_id=chan_bravo, now=ist_still_nov13)
        quota.consume_one(db, note="legacy-ws", channel_id="   ", now=before_midnight_utc)

        assert quota.snapshot(db, chan_alpha, now=before_midnight_utc)["uploads"] == 1
        assert quota.snapshot(db, chan_bravo, now=before_midnight_utc)["uploads"] == 2
        assert quota.snapshot(db, None, now=before_midnight_utc)["uploads"] == 1
        assert quota.snapshot(db, "   ", now=before_midnight_utc)["uploads"] == 1

        # Exact UTC midnight rollover resets daily counts cleanly
        assert quota.snapshot(db, chan_alpha, now=after_midnight_utc)["uploads"] == 0
        assert quota.snapshot(db, chan_bravo, now=after_midnight_utc)["uploads"] == 0
        assert quota.remaining(db, chan_bravo, now=after_midnight_utc) == settings.max_uploads_per_day


# ===========================================================================
# 7. scripts/youtube_oauth.py --status / --check offline token inspection
# ===========================================================================
def test_youtube_oauth_status_and_check_flags_run_offline_and_never_leak_secrets(
    tmp_path, monkeypatch, capsys
):
    from google_auth_oauthlib.flow import InstalledAppFlow

    monkeypatch.setattr(
        InstalledAppFlow,
        "from_client_secrets_file",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("--status/--check must stay 100% offline")),
    )

    init_db()
    oauth = _load_oauth_module()

    token_file = tmp_path / "tech_token.json"
    secret_refresh = "1//SUPER_SECRET_REFRESH_TOKEN_VALUE"
    secret_client = "GOCSPX-SUPER_SECRET_CLIENT_VALUE"
    write_token_file(
        str(token_file),
        json.dumps({
            "token": "ya29.SUPER_SECRET_ACCESS_TOKEN",
            "refresh_token": secret_refresh,
            "client_id": "12345.apps.googleusercontent.com",
            "client_secret": secret_client,
            "scopes": SCOPES,
            "expiry": "2026-12-01T00:00:00Z",
        }),
    )

    channels_json = tmp_path / "channels.json"
    channels_json.write_text(
        json.dumps({
            "channels": [{
                "slug": "tech",
                "name": "Tech Shorts",
                "token_path": str(token_file),
                "client_secrets": str(tmp_path / "nonexistent_client_secret.json"),
            }]
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "channels_file", str(channels_json))

    with session_scope() as db:
        from app.growth import channels as ch_mod

        ch_mod.sync_channels(db)
        ch = ch_mod.channel_by_slug(db, "tech")
        assert ch is not None
        ch.youtube_channel_id = "UC_BOUND_TECH_999"

    # 1) --status --channel tech --json (works even when client_secret.json does not exist!)
    monkeypatch.setattr(sys, "argv", ["youtube_oauth.py", "--channel", "tech", "--status", "--json"])
    rc = oauth.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert secret_refresh not in out
    assert secret_client not in out
    assert "ya29.SUPER_SECRET_ACCESS_TOKEN" not in out
    report = json.loads(out)
    assert report["ok"] is True
    assert report["has_refresh_token"] is True
    assert report["bound_channel_id"] == "UC_BOUND_TECH_999"
    assert set(SCOPES).issubset(set(report["scopes"]))
    assert "mode" in report["permissions"]

    # 2) --check alias in human-readable mode
    monkeypatch.setattr(sys, "argv", ["youtube_oauth.py", "--token", str(token_file), "--check"])
    rc_human = oauth.main()
    human_out = capsys.readouterr().out
    assert rc_human == 0
    assert "UC_BOUND_TECH_999" in human_out
    assert "present" in human_out.lower()
    assert secret_refresh not in human_out

    # 3) Missing refresh_token or missing upload scope returns exit code 1
    bad_token = tmp_path / "partial_token.json"
    bad_token.write_text(
        json.dumps({
            "token": "ya29.x",
            "refresh_token": "",
            "client_id": "cid",
            "client_secret": "csec",
            "scopes": [READ_SCOPE],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "argv", ["youtube_oauth.py", "--token", str(bad_token), "--status"])
    assert oauth.main() == 1
    capsys.readouterr()

    # 4) Corrupt "null" JSON in _show_existing and --status does not crash with AttributeError
    null_token = tmp_path / "null_token.json"
    null_token.write_text("null", encoding="utf-8")
    oauth._show_existing(str(null_token))
    monkeypatch.setattr(sys, "argv", ["youtube_oauth.py", "--token", str(null_token), "--check"])
    assert oauth.main() == 1
