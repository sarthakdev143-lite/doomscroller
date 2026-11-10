"""Show what the loop knows, and whether it can run at all."""
from __future__ import annotations

import json
import os
import shutil
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.clipper import ai
from app.config import settings
from app.db import session_scope
from app.models import ClipJob, GrowthEvent, GrowthState, Video, VideoStatus
from app.publishers.youtube_publisher import ANALYTICS_SCOPE, READ_SCOPE, UPLOAD_SCOPE, explain_api_error

from . import channels, collect, safety, supply
from .autopilot import explore_rate, ready_clips, started_at
from .factors import channel_zone
from .learn import matured_results
from .youtube import get_insights, insights_for

# Each of these fires once for an incident that matters for weeks, while routine events arrive about seven a day.
_PROBLEM_KINDS = ("missing", "strike", "paused", "account_hold", "publish_failed", "publish_stuck", "collect_failed",
                  "analytics_failed", "finding_retracted", "starved", "missed", "transcribe_failed", "feed_empty",
                  "clip_held")
# What ended a problem stays beside it, or a week-old strike reads as still open and its advice does nothing.
_RESOLUTION_KINDS = ("strike_cleared", "resumed", "unblocked")
_LISTED_KINDS = _PROBLEM_KINDS + _RESOLUTION_KINDS
# 10,000 quota units a day per Cloud project, about 1,600 per upload.
_PROJECT_UPLOADS = 6
PROBLEM_WINDOW = timedelta(days=30)


def _check(ok: bool, name: str, detail: str, *, required: bool = True) -> dict:
    return {"ok": ok, "name": name, "detail": detail, "required": required}


def _token_checks(path: Optional[str] = None) -> list[dict]:
    path = path or settings.youtube_token_path
    if not os.path.exists(path):
        return [_check(False, "token", f"missing at {path}. Run: scripts/youtube_oauth.py")]
    try:
        with open(path, encoding="utf-8") as fh:
            token = json.load(fh)
    except (OSError, ValueError) as exc:
        return [_check(False, "token", f"unreadable: {exc}")]
    granted = set(token.get("scopes") or [])
    return [
        _check(bool(token.get("refresh_token")), "refresh token",
               "present" if token.get("refresh_token") else "absent, so uploads stop within the hour. Re-run with --force"),
        _check(UPLOAD_SCOPE in granted, "upload permission", "granted" if UPLOAD_SCOPE in granted else "not granted"),
        _check(READ_SCOPE in granted, "read permission", "granted" if READ_SCOPE in granted else "not granted, so no view counts"),
        _check(ANALYTICS_SCOPE in granted, "analytics permission",
               "granted" if ANALYTICS_SCOPE in granted else "not granted, so no retention data. Re-run with --force",
               required=False),
    ]


def _youtube_checks(insights) -> list[dict]:
    """A dead token or a switched-off API looks fine from the files, and only shows once a window has been lost."""
    try:
        insights.verify_token()
    except Exception as exc:
        return [_check(False, "token refresh", explain_api_error(exc))]
    checks = [_check(True, "token refresh", "Google accepted the token")]
    try:
        name = insights.channel_name()
        checks.append(_check(bool(name), "channel", f"connected to {name}" if name else
                             "this Google account has no YouTube channel. Create one, then run scripts/youtube_oauth.py --force"))
    except Exception as exc:
        checks.append(_check(False, "channel", explain_api_error(exc)))
    try:
        insights.probe_analytics()
        checks.append(_check(True, "analytics read", "YouTube Analytics answered"))
    except Exception as exc:
        checks.append(_check(False, "analytics read", explain_api_error(exc, api="YouTube Analytics API")))
    return checks


def _feed_check(feed: str) -> dict:
    name = f"feed {feed}"
    try:
        link = supply.first_link(feed)
    except Exception as exc:
        return _check(False, name, f"could not be read: {exc}. Check the link and the connection")
    if link:
        return _check(True, name, f"lists videos, such as {link}")
    if supply.lists_nothing(feed):
        return _check(False, name, "lists no videos. Check the link")
    # A block is meant to keep this source off the channel, so it must not be something to undo before the loop is ready.
    return {**_check(False, name, "skipped, because a takedown block covers its videos. "
                     "To use it again: scripts/growth.py unblock <name>", required=False), "blocked_feed": feed}


def _live_checks() -> list[dict]:
    insights = get_insights()
    youtube = _youtube_checks(insights) if insights else [
        _check(False, "live YouTube checks", "skipped: USE_MOCK or an empty YOUTUBE_CLIENT_SECRETS means no real connection",
               required=False)]
    return youtube + [_feed_check(feed) for feed in settings.growth_feeds_list]


def _held_reasons(db, scope=None) -> Counter:
    """Why unattended clips were left for a person. They post nothing until someone looks."""
    conds = [Video.status == VideoStatus.PENDING]
    if scope is not None:
        conds.append(scope)
    metas = db.execute(select(Video.meta).where(*conds)).scalars()
    return Counter(meta["growth_flagged"] for meta in metas if (meta or {}).get("growth_flagged"))


def _held_note(held: Counter) -> str:
    return (f"{held.total()} clips will not post until someone approves them on the Pending tab. "
            f"Most often: {held.most_common(1)[0][0]}")


def _identity_check(channel, insights) -> list[dict]:
    """Catch a token wired to the wrong channel (an account-picker misclick at oauth time)."""
    if insights is None or not channel.youtube_channel_id:
        return []
    try:
        identity = insights.channel_identity()
    except Exception as exc:
        return [_check(False, "channel identity", explain_api_error(exc), required=False)]
    live_id = identity[0] if identity else None
    ok = live_id == channel.youtube_channel_id
    return [_check(ok, "channel identity",
                   f"token belongs to {identity[1]}" if ok else
                   f"token is wired to a DIFFERENT channel ({identity[1] if identity else live_id}) — "
                   f"re-run scripts/youtube_oauth.py --channel {channel.slug} --force")]


def _disabled_row(channel, configured: bool) -> dict:
    """A named channel left off. A fully-set-up one that is still off is the silent 'forgot enabled:true' trap."""
    note = ("configured (token + secret present) but enabled:false — set enabled:true and restart"
            if configured else "disabled")
    return {"slug": channel.slug, "name": channel.name, "enabled": False, "ready": True,
            "checks": [_check(True, "enabled", note, required=False)]}


def _channel_rows(live: bool) -> list[dict]:
    """One block per managed named channel (enabled and disabled), so each channel's own setup can be checked.

    Disabled channels are listed but never gate overall readiness.
    """
    with session_scope() as db:
        named = [c for c in channels.all_channels(db) if not channels.is_default(c)]
        info = []
        for c in named:
            feeds = channels.feeds_for(db, c)
            stock = len(ready_clips(db, channels.video_scope(c)))
            gap = db.get(GrowthState, channels.state_key(c, supply.SUPPLY_GAP))
            info.append((c, feeds, stock, (gap.value or {}).get("cause") if gap else None,
                         insights_for(c) if (live and c.enabled) else None))
    rows = []
    for channel, feeds, stock, gap_cause, insights in info:
        has_creds = bool(channel.client_secrets) and os.path.exists(channel.client_secrets)
        if not channel.enabled:
            rows.append(_disabled_row(channel, has_creds and os.path.exists(channel.token_path)))
            continue
        checks = [
            _check(has_creds, "client_secrets",
                   channel.client_secrets or "not set, so this channel's uploads would be fake"),
            *_token_checks(channel.token_path),
            _check(bool(feeds), "feeds", f"{len(feeds)} feeds" if feeds else "none set for this channel"),
            _check(not gap_cause, "stock",
                   f"{stock} clips ready" + (f"; supply gap: {gap_cause[:80]}" if gap_cause else ""), required=False),
            _check(channel.uploads_per_day <= _PROJECT_UPLOADS, "pace",
                   f"{channel.uploads_per_day} a day" + (
                       "" if channel.uploads_per_day <= _PROJECT_UPLOADS else
                       f": a Cloud project's default quota covers about {_PROJECT_UPLOADS} uploads a day, so the "
                       "rest will be refused unless this project has a raised quota"), required=False),
        ]
        if live:
            checks += _youtube_checks(insights) if insights else [
                _check(False, "live", "no connection: USE_MOCK or no client_secrets", required=False)]
            checks += _identity_check(channel, insights)
            checks += [_feed_check(feed) for feed in feeds]
        rows.append({"slug": channel.slug, "name": channel.name, "enabled": True,
                     "ready": all(c["ok"] for c in checks if c["required"]), "checks": checks})
    return rows


def doctor(*, live: bool = False) -> dict:
    """Live also asks Google and reads every feed, so it comes first: what it finds about a feed counts below."""
    live_checks = _live_checks() if live else []
    barred = {c["blocked_feed"] for c in live_checks if "blocked_feed" in c}
    secrets = settings.youtube_client_secrets
    try:
        ZoneInfo(settings.growth_timezone)
        zone_ok = True
    except Exception:
        zone_ok = False
    files = len(supply.open_inbox_files())
    with session_scope() as db:
        stock = len(ready_clips(db))
        empty_feeds = supply.empty_feeds(db)
        paused = safety.pause_reason(db)
        blocks = safety.block_list(db)
        held = _held_reasons(db)
        config = channels.config_health(db)
        dupes = channels.duplicate_credentials(db)
        active = channels.active_channels(db)
        # Every enabled channel's feeds. For a single legacy channel this is just GROWTH_FEEDS.
        all_feeds = [f for c in active for f in channels.feeds_for(db, c)]
    # With named channels, each has its own token below; the global YOUTUBE_* creds are legacy and unused.
    multi = any(not channels.is_default(c) for c in active)
    auth_checks = [
        _check(bool(secrets) and os.path.exists(secrets), "YOUTUBE_CLIENT_SECRETS",
               secrets or ("per channel below" if multi else "not set, so uploads are fake")),
        *_token_checks(),
    ]
    if multi:
        auth_checks = [{**c, "required": False} for c in auth_checks]
    # An inbox file's title is only its filename, so without AI its clips never reach the shelf on their own.
    needs_ai = files > 0 and not ai.available()
    # The dashboard runs this on every refresh, so it goes by what supply last found rather than asking YouTube.
    feeds = [feed for feed in all_feeds if feed not in empty_feeds and feed not in barred]
    no_links = sorted(set(all_feeds) & empty_feeds)
    checks = [
        _check("error" not in config, "channels.json",
               config["error"] if config.get("error") else f"{config.get('count', 0)} channels declared"),
        _check(not dupes, "channel credentials",
               "; ".join(dupes) + ". Each channel needs its own token/project." if dupes else "unique per channel"),
        _check(paused is None, "pause",
               f"{paused}. Check the channel, then clear it with: scripts/growth.py resume" if paused else "not paused"),
        _check(not blocks, "takedown blocks", safety.describe_blocks(blocks) if blocks else "none", required=False),
        _check(settings.growth_enabled, "GROWTH_ENABLED", "on" if settings.growth_enabled else "off"),
        _check(settings.enable_publish, "ENABLE_PUBLISH", "on" if settings.enable_publish else "off, so nothing posts"),
        _check(not settings.use_mock, "USE_MOCK", "real uploads" if not settings.use_mock else "true, so uploads are fake"),
        *auth_checks,
        _check(settings.youtube_default_privacy == "public", "YOUTUBE_DEFAULT_PRIVACY",
               f"{settings.youtube_default_privacy}: only public videos can get views to learn from"),
        _check(shutil.which("ffmpeg") is not None, "ffmpeg", "found" if shutil.which("ffmpeg") else "not on PATH"),
        _check(zone_ok, "GROWTH_TIMEZONE", settings.growth_timezone),
        _check(stock > 0 or (files > 0 and not needs_ai) or bool(feeds), "content source",
               f"{stock} clips ready; {files} inbox files ready; {len(feeds)} feeds"
               + (f"; no videos found at {', '.join(no_links)}" if no_links else "")
               + (f"; a takedown block covers {', '.join(sorted(barred))}" if barred else "")
               + ("; clips wait for review without an AI CLI" if needs_ai else "")
               + (f"; {held.total()} clips held for review" if held else "")),
        _check(not held, "held for review", _held_note(held) if held else "none", required=False),
        _check(bool(settings.deepgram_api_key), "DEEPGRAM_API_KEY", "set" if settings.deepgram_api_key else
               "not set, so every clip takes the no-speech path and no language is checked", required=False),
        # Required: a clip no AI has titled waits for review, so without one the shelf never fills.
        _check(ai.available(), "AI (OpenCode or Claude)",
               " and ".join(name for name, path in (("OpenCode", ai.opencode_path()), ("Claude", ai.claude_path())) if path)
               + " found" if ai.available() else "neither found, so every clip waits for review and nothing posts"),
        *live_checks,
    ]
    channel_rows = _channel_rows(live)
    ready = all(c["ok"] for c in checks if c["required"]) and all(r["ready"] for r in channel_rows)
    result = {"ready": ready, "checks": checks}
    if channel_rows:
        result["channels"] = channel_rows
    return result


def _stock_warning(db, stock: int, channel=None) -> Optional[str]:
    """Why nothing can post, and the step that fits. With a channel, judged on that channel's own shelf."""
    if stock or not settings.growth_enabled:
        return None
    scope = None if channel is None else channels.video_scope(channel)
    jobs = [ClipJob.status.in_(("queued", "running"))]
    if channel is not None:
        cid = channels.event_channel(channel)
        jobs.append(ClipJob.channel_id.is_(None) if cid is None else ClipJob.channel_id == cid)
    active = db.scalar(select(func.count(ClipJob.id)).where(*jobs)) or 0
    held = _held_reasons(db, scope)
    steps = [f"{active} clip jobs are being cut."] if active else []
    if held:
        steps.append(f"{_held_note(held)}.")
    if not steps:  # adding sources is only the answer when they are what ran out
        gap_key = supply.SUPPLY_GAP if channel is None else channels.state_key(channel, supply.SUPPLY_GAP)
        state = db.get(GrowthState, gap_key)
        gap = state.value if state else {}
        steps.append(f"Supply stopped: {gap['cause']}. {gap['step']}" if gap else supply.ADD_SOURCES)
    return " ".join(["No finished clips in stock, so nothing can post.", *steps])


def _events(db, *where, limit: Optional[int] = None) -> list[dict]:
    names = {c.id: (c.name or c.slug) for c in channels.all_channels(db)}
    rows = select(GrowthEvent).where(*where).order_by(GrowthEvent.created_at.desc()).limit(limit)
    return [{"at": e.created_at.isoformat(), "kind": e.kind, "summary": e.summary,
             "channel": names.get(e.channel_id, "")} for e in db.execute(rows).scalars()]


def _section(db, channel, now: datetime) -> dict:
    """What one channel has done and learned. With no channel, the whole legacy loop, as before."""
    scope = None if channel is None else channels.video_scope(channel)
    key = (lambda name: name) if channel is None else (lambda name: channels.state_key(channel, name))
    started = started_at(db, channel)
    paused, plan, focus, summary = (db.get(GrowthState, key(name)) for name in ("paused", "plan", "focus", "summary"))
    results = matured_results(db, scope=scope, zone=channel_zone(channel))
    diagnoses = {}
    for video in db.execute(select(Video).where(Video.id.in_([r.video_id for r in results]))).scalars():
        diagnoses[video.id] = (video.meta or {}).get("growth_diagnosis", {})
    ranked = sorted(results, key=lambda r: -r.views)
    row = lambda r: {"id": r.video_id, "title": r.title, "views": r.views, "retention": r.retention,
                     "arms": r.arms, "diagnosis": diagnoses.get(r.video_id, {}).get("reason", "")}
    stock = len(ready_clips(db, scope))
    collected = collect.health(db, channel)
    warnings = [_stock_warning(db, stock, channel), collect.analytics_warning(collected, now)]
    posted = [Video.status == VideoStatus.PUBLISHED] + ([scope] if scope is not None else [])
    return {
        "day": (now - started).days + 1 if started else 0,
        "phase": "not started" if not started else (
            "testing" if explore_rate(started, now) == settings.growth_explore_rate else "exploiting"),
        "explore_rate": explore_rate(started, now) if started else settings.growth_explore_rate,
        "paused": paused.value if paused else None,
        "hold": safety.active_hold(db, now, channel),
        "published": db.scalar(select(func.count(Video.id)).where(*posted)) or 0,
        "matured": len(results),
        "stock": stock,
        "collect": collected,
        "warnings": [w for w in warnings if w],
        "plan": plan.value if plan else {},
        "focus": (focus.value or {}).get("factors", []) if focus else [],
        "factors": summary.value if summary else {},
        "best": [row(r) for r in ranked[:5]],
        "worst": [row(r) for r in ranked[-5:][::-1]] if len(ranked) > 5 else [],
    }


def _fleet(sections: dict[str, dict]) -> dict:
    """The top of a multi-channel report: totals, and anything a channel needs the owner for."""
    parts = list(sections.values())
    collects = [s["collect"]["last_success"] for s in parts if s["collect"]["last_success"]]
    return {
        "day": max((s["day"] for s in parts), default=0),
        "phase": "per channel",
        "explore_rate": settings.growth_explore_rate,
        "paused": next((s["paused"] for s in parts if s["paused"]), None),
        "hold": next((s["hold"] for s in parts if s["hold"]), None),
        "published": sum(s["published"] for s in parts),
        "matured": sum(s["matured"] for s in parts),
        "stock": sum(s["stock"] for s in parts),
        "collect": {"last_success": max(collects) if collects else None,
                    "analytics_failing_since": None, "analytics_error": None},
        "warnings": [f"{s['name']}: {w}" for s in parts for w in s["warnings"]],
        "plan": {}, "focus": [], "factors": {}, "best": [], "worst": [],
    }


def build_report() -> dict:
    """One channel reports flat, as it always has. Several report totals plus a section per channel."""
    now = datetime.now(timezone.utc)
    with session_scope() as db:
        active = channels.active_channels(db)
        shared = {
            "blocks": safety.block_list(db),
            "problems": _events(db, GrowthEvent.kind.in_(_LISTED_KINDS), GrowthEvent.created_at >= now - PROBLEM_WINDOW),
            "events": _events(db, GrowthEvent.kind.not_in(_LISTED_KINDS), limit=40),
        }
        if len(active) == 1 and channels.is_default(active[0]):
            return {**_section(db, None, now), **shared}
        sections = {c.slug: {"name": c.name or c.slug, **_section(db, c, now)} for c in active}
        return {**_fleet(sections), **shared, "channels": sections}
