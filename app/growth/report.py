"""Show what the loop knows, and whether it can run at all."""
from __future__ import annotations

import json
import os
import re
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

from . import approval, channels, collect, safety, supply
from .autopilot import _past_grace, explore_rate, ready_clips, started_at
from .factors import aware, channel_zone
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


def _secret_detail(path: Optional[str], *, fallback: str) -> tuple[bool, str]:
    if not path:
        return False, fallback
    if os.path.exists(path):
        return True, path
    return False, f"missing at {path}"


def _ts_key(raw: object) -> datetime:
    if isinstance(raw, str) and raw:
        try:
            return aware(datetime.fromisoformat(raw))
        except ValueError:
            pass
    return datetime.min.replace(tzinfo=timezone.utc)


def _token_checks(path: Optional[str] = None, *, slug: Optional[str] = None) -> list[dict]:
    path = path or settings.youtube_token_path
    connect_cmd = f"scripts/channels.py connect {slug}" if slug else "scripts/youtube_oauth.py"
    force_hint = f"Re-run: scripts/youtube_oauth.py --channel {slug} --force" if slug else "Re-run with --force"
    if not os.path.exists(path):
        return [_check(False, "token", f"missing at {path}. Run: {connect_cmd}")]
    try:
        with open(path, encoding="utf-8") as fh:
            token = json.load(fh)
    except (OSError, ValueError) as exc:
        return [_check(False, "token", f"unreadable: {exc}")]
    granted = set(token.get("scopes") or [])
    return [
        _check(bool(token.get("refresh_token")), "refresh token",
               "present" if token.get("refresh_token") else f"absent, so uploads stop within the hour. {force_hint}"),
        _check(UPLOAD_SCOPE in granted, "upload permission", "granted" if UPLOAD_SCOPE in granted else "not granted"),
        _check(READ_SCOPE in granted, "read permission", "granted" if READ_SCOPE in granted else "not granted, so no view counts"),
        _check(ANALYTICS_SCOPE in granted, "analytics permission",
               "granted" if ANALYTICS_SCOPE in granted else f"not granted, so no retention data. {force_hint}",
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


def _channel_rows(live: bool, only: Optional[str] = None) -> list[dict]:
    """One block per managed named channel (enabled and disabled), so each channel's own setup can be checked.

    Disabled channels are listed but never gate overall readiness. `only` limits it to one slug, so
    connecting a channel does not wait on Google for every other one.
    """
    with session_scope() as db:
        named = [c for c in channels.all_channels(db) if not channels.is_default(c) and only in (None, c.slug)]
        info = []
        for c in named:
            feeds = channels.feeds_for(db, c)
            stock = len(ready_clips(db, channels.video_scope(c)))
            gap = db.get(GrowthState, channels.state_key(c, supply.SUPPLY_GAP))
            info.append((c, feeds, stock, (gap.value or {}).get("cause") if gap else None,
                         insights_for(c) if (live and c.enabled) else None))
    rows = []
    for channel, feeds, stock, gap_cause, insights in info:
        has_creds, secret_detail = _secret_detail(
            channel.client_secrets,
            fallback="not set, so this channel's uploads would be fake",
        )
        if not channel.enabled:
            rows.append(_disabled_row(channel, has_creds and os.path.exists(channel.token_path)))
            continue
        checks = [
            _check(has_creds, "client_secrets", secret_detail),
            *_token_checks(channel.token_path, slug=channel.slug),
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


def check_channel(slug: str, *, live: bool = True) -> Optional[dict]:
    """One channel's readiness, asked of Google when `live=True`. What `channels.py connect` shows before leaving it on."""
    return next(iter(_channel_rows(live, only=slug)), None)


def doctor(*, live: bool = False) -> dict:
    """Live also asks Google and reads every feed, so it comes first: what it finds about a feed counts below."""
    with session_scope() as db:
        all_chs = channels.all_channels(db)
        active = channels.active_channels(db)
    has_named = any(not channels.is_default(c) for c in all_chs)
    legacy_active = any(channels.is_default(c) for c in active)
    live_checks = _live_checks() if (live and legacy_active) else []
    channel_rows = _channel_rows(live)
    barred = {c["blocked_feed"] for c in live_checks if "blocked_feed" in c}
    barred |= {c["blocked_feed"] for r in channel_rows for c in r["checks"] if "blocked_feed" in c}
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
    # With named channels (and no active legacy default), each has its own token below; global YOUTUBE_* creds are unused.
    named_only = has_named and not legacy_active
    has_global_creds, global_secret_detail = _secret_detail(
        secrets,
        fallback="per channel below" if named_only else "not set, so uploads are fake",
    )
    auth_checks = [
        _check(has_global_creds, "YOUTUBE_CLIENT_SECRETS", global_secret_detail),
        *_token_checks(),
    ]
    if named_only:
        auth_checks = [{**c, "required": False} for c in auth_checks]
    if config.get("error"):
        cfg_ok, cfg_detail = False, config["error"]
    elif has_named and not active:
        cfg_ok, cfg_detail = False, f"{config.get('count', 0)} channels declared, none enabled — run: scripts/channels.py connect <slug>"
    else:
        cfg_ok, cfg_detail = True, f"{config.get('count', 0)} channels declared"
    # An inbox file's title is only its filename, so without AI its clips never reach the shelf on their own.
    needs_ai = files > 0 and not ai.available()
    # The dashboard runs this on every refresh, so it goes by what supply last found rather than asking YouTube.
    feeds = [feed for feed in all_feeds if feed not in empty_feeds and feed not in barred]
    no_links = sorted(set(all_feeds) & empty_feeds)
    checks = [
        _check(cfg_ok, "channels.json", cfg_detail),
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
    waiting = sum(1 for v in approval.awaiting_review(db, scope) if not (v.meta or {}).get("growth_flagged"))
    steps = [f"{active} clip jobs are being cut."] if active else []
    if held:
        steps.append(f"{_held_note(held)}.")
    if waiting > 0:
        steps.append(f"{waiting} clip(s) waiting for approval on the Review tab.")
    if not steps:  # adding sources is only the answer when they are what ran out
        gap_key = supply.SUPPLY_GAP if channel is None else channels.state_key(channel, supply.SUPPLY_GAP)
        state = db.get(GrowthState, gap_key)
        gap = state.value if state else {}
        named = channel is not None and not channels.is_default(channel)
        fallback = (f"Add a creator link with: scripts/channels.py feed {channel.slug} --add <link>"
                    if named else supply.ADD_SOURCES)
        steps.append(f"Supply stopped: {gap['cause']}. {gap['step']}" if gap else fallback)
    return " ".join(["No finished clips in stock, so nothing can post.", *steps])


def _events(db, *where, limit: Optional[int] = None) -> list[dict]:
    names = {c.id: (c.name or c.slug) for c in channels.all_channels(db)}
    rows = select(GrowthEvent).where(*where).order_by(GrowthEvent.created_at.desc()).limit(limit)
    return [{"at": e.created_at.isoformat(), "kind": e.kind, "summary": e.summary,
             "channel": names.get(e.channel_id, "")} for e in db.execute(rows).scalars()]


def _section(db, channel, now: datetime) -> dict:
    """What one channel has done and learned. With no channel, the whole legacy loop, as before."""
    now = aware(now)
    scope = None if channel is None else channels.video_scope(channel)
    key = (lambda name: name) if channel is None else (lambda name: channels.state_key(channel, name))
    started = started_at(db, channel)
    paused, plan, focus, summary = (db.get(GrowthState, key(name)) for name in ("paused", "plan", "focus", "summary"))
    results = matured_results(db, scope=scope, zone=channel_zone(channel))
    diagnoses = {}
    if results:
        for video in db.execute(select(Video).where(Video.id.in_([r.video_id for r in results]))).scalars():
            diagnoses[video.id] = (video.meta or {}).get("growth_diagnosis", {})
    ranked = sorted(results, key=lambda r: -r.views)
    row = lambda r: {"id": r.video_id, "title": r.title, "views": r.views, "retention": r.retention,
                     "arms": r.arms, "diagnosis": diagnoses.get(r.video_id, {}).get("reason", "")}
    best_rows = ranked[:5]
    rest = ranked[len(best_rows):]
    stock = len(ready_clips(db, scope))
    collected = collect.health(db, channel)
    warnings = [_stock_warning(db, stock, channel), collect.analytics_warning(collected, now)]
    posted = [Video.status == VideoStatus.PUBLISHED] + ([scope] if scope is not None else [])
    return {
        "day": max(1, (now - started).days + 1) if started else 0,
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
        "factors": summary.value if (summary and results) else {},
        "best": [row(r) for r in best_rows],
        "worst": [row(r) for r in rest[-5:][::-1]],
    }


def _fleet(sections: dict[str, dict]) -> dict:
    """The top of a multi-channel report: totals, and anything a channel needs the owner for."""
    parts = list(sections.values())
    collects = [s["collect"]["last_success"] for s in parts if s["collect"]["last_success"]]
    all_best = [{**r, "channel": s["name"]} for s in parts for r in s["best"]]
    warnings = ([f"{s['name']}: {w}" for s in parts for w in s["warnings"]] if parts
                else (["No channels are enabled. Run: scripts/channels.py connect <slug>"]
                      if settings.growth_enabled else []))
    return {
        "day": max((s["day"] for s in parts), default=0),
        "phase": "per channel",
        "explore_rate": settings.growth_explore_rate,
        "paused": next((s["paused"] for s in parts if s["paused"]), None),
        "hold": next((s["hold"] for s in parts if s["hold"]), None),
        "published": sum(s["published"] for s in parts),
        "matured": sum(s["matured"] for s in parts),
        "stock": sum(s["stock"] for s in parts),
        "collect": {"last_success": max(collects, key=_ts_key) if collects else None,
                    "analytics_failing_since": None, "analytics_error": None},
        "warnings": warnings,
        "plan": {}, "focus": [], "factors": {},
        "best": sorted(all_best, key=lambda r: -r["views"])[:5],
        "worst": [],
    }


def _build_report(db, now: datetime, channel_slug: Optional[str]) -> dict:
    if channel_slug and channel_slug.strip():
        all_chs = channels.all_channels(db)
        target = channels.channel_by_slug(db, channel_slug.strip())
        if target is None:
            known = ", ".join(c.slug for c in all_chs) or "none"
            raise LookupError(f"unknown channel '{channel_slug.strip()}' (known: {known})")
        ev_filter = GrowthEvent.channel_id.is_(None) if channels.is_default(target) else GrowthEvent.channel_id == target.id
        shared = {
            "blocks": safety.block_list(db),
            "problems": _events(db, GrowthEvent.kind.in_(_LISTED_KINDS), GrowthEvent.created_at >= now - PROBLEM_WINDOW, ev_filter),
            "events": _events(db, GrowthEvent.kind.not_in(_LISTED_KINDS), ev_filter, limit=40),
        }
        sec_ch = None if (len(all_chs) == 1 and channels.is_default(target)) else target
        return {"slug": target.slug, "name": target.name or target.slug, "enabled": target.enabled,
                **_section(db, sec_ch, now), **shared}
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


def build_report(db=None, *, channel_slug: Optional[str] = None) -> dict:
    """One channel reports flat, as it always has. Several report totals plus a section per channel.

    Pass `db` to read inside a caller's session; with none, the report opens its own.
    """
    now = datetime.now(timezone.utc)
    if db is not None:
        return _build_report(db, now, channel_slug)
    with session_scope() as own:
        return _build_report(own, now, channel_slug)


# ---------- Markdown digest ----------

_MD_SPECIAL = re.compile(r"([\\`*\[\]<>|#~]|(?<!\w)_|_(?!\w))")  # intraword underscores (yt_9) are not emphasis in GFM
_MD_SPACE = re.compile(r"[\s\x00-\x1f\x7f]+")
_MD_CAP = 160
_MD_LISTED = 5


def _md(value: object, limit: int = _MD_CAP) -> str:
    """Untrusted text (titles, event summaries) made safe for a Markdown table cell: no secrets, no markup."""
    from .notify import _redact

    text = _MD_SPACE.sub(" ", _redact(str(value if value is not None else ""))).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return _MD_SPECIAL.sub(r"\\\1", text)


def _md_pct(value: object) -> str:
    return f"{float(value):.0%}" if isinstance(value, (int, float)) and not isinstance(value, bool) else "—"


def _md_table(header: list[str], rows: list[list[str]]) -> list[str]:
    return ["| " + " | ".join(header) + " |", "|" + "|".join(" --- " for _ in header) + "|",
            *("| " + " | ".join(row) + " |" for row in rows)]


def _md_winners(best: list[dict], *, with_channel: bool) -> list[str]:
    horizon = max(1, settings.growth_horizon_hours)
    header = ["#", "Short", "Views", f"Views/h (first {horizon}h)", "Retention"] + (["Channel"] if with_channel else [])
    rows = []
    for i, r in enumerate(best, 1):
        views = int(r.get("views") or 0)
        row = [str(i), _md(r.get("title") or "(untitled)"), f"{views:,}", f"{views / horizon:.1f}", _md_pct(r.get("retention"))]
        if with_channel:
            row.append(_md(r.get("channel", "")))
        rows.append(row)
    return _md_table(header, rows)


def _md_preferences(factors: dict) -> list[str]:
    out: list[str] = []
    for factor, rows in factors.items():
        if not rows:
            continue
        out += ["", f"**{_md(factor)}**", ""]
        table = [[_md(r.get("arm", "")), f"{r.get('multiplier', 1.0)}x", f"{r.get('low', 1.0)}–{r.get('high', 1.0)}x",
                  _md_pct(r.get("p_best")), str(r.get("n", 0))] for r in rows[:_MD_LISTED]]
        out += _md_table(["Choice", "Views vs typical", "Range", "P(best)", "Posts"], table)
        if len(rows) > _MD_LISTED:
            out.append(f"\n_and {len(rows) - _MD_LISTED} more._")
    return out


def _md_state(sec: dict) -> list[str]:
    status = ["Running"]
    if sec.get("enabled") is False:
        status = ["DISABLED"]
    if sec.get("paused"):
        status = [f"PAUSED — {_md((sec['paused'] or {}).get('reason') or 'paused')}"]
    if sec.get("hold"):
        hold = sec["hold"]
        status.append(f"on hold until {_md(str(hold.get('until', ''))[:16].replace('T', ' '))}: {_md(hold.get('reason', ''))}")
    lines = [f"- **Status:** {'; '.join(status)}",
             f"- **Loop:** day {sec.get('day', 0)}, {_md(sec.get('phase', ''))}, "
             f"{float(sec.get('explore_rate') or 0):.0%} of choices are tests",
             f"- **Posts:** {sec.get('published', 0)} published, {sec.get('matured', 0)} judged, "
             f"{sec.get('stock', 0)} clips in stock"]
    last = ((sec.get("collect") or {}).get("last_success") or "never")
    lines.append(f"- **Last analytics collect:** {_md(str(last)[:16].replace('T', ' '))}")
    return lines


def _md_section(sec: dict, level: str) -> list[str]:
    out = _md_state(sec)
    for warning in sec.get("warnings", []):
        out.append(f"- ⚠️ {_md(warning, 400)}")
    out += ["", f"{level} Top winning Shorts", ""]
    if sec.get("best"):
        out += _md_winners(sec["best"], with_channel=False)
    else:
        out.append("_No Shorts have matured yet — results appear once a post is old enough to judge._")
    if sec.get("focus"):
        out += ["", "**Testing harder:** " + ", ".join(_md(f) for f in sec["focus"])]
    posts = (sec.get("plan") or {}).get("posts", [])
    if posts:
        out += ["", "**Today's plan:** " + ", ".join(f"{_md(p.get('slot', ''))} ({_md(p.get('state', ''))})" for p in posts)]
    prefs = _md_preferences(sec.get("factors") or {})
    out += ["", f"{level} Learned preferences", ""]
    out += prefs[1:] if prefs else ["_Nothing learned yet._"]
    return out


def _md_shared(rep: dict) -> list[str]:
    out: list[str] = []
    blocks = rep.get("blocks") or []
    out += ["", "## Safety", ""]
    if blocks:
        out += ["**Takedown blocks** (their feeds are skipped; lift with `scripts/growth.py unblock <name>`):", ""]
        for b in blocks:
            who = ", ".join(_md(a) for a in b.get("authors", [])) or _md(b.get("source", ""))
            out.append(f"- {who}: {_md(b.get('reason', ''))} on {_md(str(b.get('at', ''))[:10])}")
    else:
        out.append("_No takedown blocks._")
    problems = rep.get("problems") or []
    out += ["", "**Problems in the last 30 days**", ""]
    if problems:
        shown: Counter = Counter()
        totals = Counter(p.get("kind", "") for p in problems)
        for p in problems:
            kind = p.get("kind", "")
            shown[kind] += 1
            if shown[kind] <= _MD_LISTED:
                tag = f"[{_md(p['channel'])}] " if p.get("channel") else ""
                out.append(f"- {_md(str(p.get('at', ''))[:16].replace('T', ' '))} {tag}`{_md(kind)}`: {_md(p.get('summary', ''), 300)}")
        out += [f"- and {n - _MD_LISTED} more `{_md(kind)}`" for kind, n in totals.items() if n > _MD_LISTED]
    else:
        out.append("_None._")
    events = (rep.get("events") or [])[:10]
    out += ["", "## Recent", ""]
    if events:
        for e in events:
            tag = f"[{_md(e['channel'])}] " if e.get("channel") else ""
            out.append(f"- {_md(str(e.get('at', ''))[:16].replace('T', ' '))} {tag}`{_md(e.get('kind', ''))}`: {_md(e.get('summary', ''), 300)}")
    else:
        out.append("_Nothing yet._")
    return out


def render_markdown(rep: dict, *, now: Optional[datetime] = None) -> str:
    """A GitHub-Markdown digest of a `build_report` result. Untrusted text is escaped and secrets are scrubbed."""
    stamp = aware(now) if now is not None else datetime.now(timezone.utc)
    out = ["# Doomscroller growth report", "", f"_Generated {stamp:%Y-%m-%d %H:%M} UTC_", ""]
    sections = rep.get("channels")
    if sections is None:
        title = f"## {_md(rep['name'])} (`{_md(rep['slug'])}`)" if rep.get("slug") else "## Overview"
        out += [title, "", *_md_section(rep, "###")]
    else:
        out += ["## Fleet summary", "",
                f"- **Channels:** {len(sections)}",
                f"- **Posts:** {rep.get('published', 0)} published, {rep.get('matured', 0)} judged, "
                f"{rep.get('stock', 0)} clips in stock"]
        for warning in rep.get("warnings", []):
            out.append(f"- ⚠️ {_md(warning, 400)}")
        if rep.get("best"):
            out += ["", "### Top winning Shorts across channels", "", *_md_winners(rep["best"], with_channel=True)]
        for slug, sec in sections.items():
            out += ["", f"## {_md(sec.get('name') or slug)} (`{_md(slug)}`)", "", *_md_section(sec, "###")]
    out += _md_shared(rep)
    return "\n".join(out).rstrip() + "\n"


def export_markdown_report(db=None, *, channel_slug: str = "", now: Optional[datetime] = None) -> str:
    """The report as a Markdown digest, for the whole fleet or one channel. Raises LookupError for an unknown slug."""
    return render_markdown(build_report(db, channel_slug=channel_slug or None), now=now)


def build_status(*, now: Optional[datetime] = None, channel_slug: Optional[str] = None) -> dict:
    """Quick operational snapshot: stock, review queue, active pauses/holds/blocks, and next due post per channel."""
    now = aware(now) if now is not None else datetime.now(timezone.utc)
    inbox_files = len(supply.open_inbox_files())
    with session_scope() as db:
        all_chs = channels.all_channels(db)
        if channel_slug:
            target = channels.channel_by_slug(db, channel_slug.strip())
            if target is None:
                known = ", ".join(c.slug for c in all_chs) or "none"
                raise LookupError(f"unknown channel '{channel_slug}' (known: {known})")
            listed = [target]
        else:
            listed = [c for c in all_chs if c.enabled or not channels.is_default(c)]
        legacy_single = not channel_slug and len(listed) == 1 and channels.is_default(listed[0])
        rows = []
        candidates: list[dict] = []
        for ch in listed:
            scope = None if legacy_single else channels.video_scope(ch)
            cid = channels.event_channel(ch)
            job_scope = ClipJob.channel_id.is_(None) if cid is None else ClipJob.channel_id == cid
            stock = len(ready_clips(db, scope))
            waiting_review = len(approval.awaiting_review(db, scope))
            active_jobs = db.scalar(select(func.count(ClipJob.id)).where(
                ClipJob.status.in_(("queued", "running")), job_scope)) or 0
            paused = safety.pause_state(db, None if legacy_single else ch)
            hold = safety.active_hold(db, now, None if legacy_single else ch)
            plan_key = "plan" if legacy_single else channels.state_key(ch, "plan")
            plan_state = db.get(GrowthState, plan_key)
            posts = (plan_state.value or {}).get("posts", []) if plan_state and isinstance(plan_state.value, dict) else []
            posted_today = sum(1 for p in posts if p.get("state") == "posted")
            waiting_posts = sorted(
                (p for p in posts if p.get("state") == "waiting" and not _past_grace(p, now)),
                key=lambda p: _ts_key(p.get("due")),
            )
            ch_next = ({"slot": waiting_posts[0].get("slot"), "due": waiting_posts[0].get("due"),
                        "ends": waiting_posts[0].get("ends")} if waiting_posts else None)
            if ch.enabled and paused is None and ch_next is not None:
                candidates.append(ch_next)
            rows.append({
                "slug": ch.slug,
                "name": ch.name or ch.slug,
                "enabled": ch.enabled,
                "stock": stock,
                "awaiting_review": waiting_review,
                "active_jobs": active_jobs,
                "posted_today": posted_today,
                "uploads_per_day": ch.uploads_per_day,
                "paused": paused,
                "hold": hold,
                "next_post": ch_next,
            })
        blocks = safety.block_list(db)
    counted = [r for r in rows if r["enabled"]] or rows
    earliest = min(candidates, key=lambda p: _ts_key(p.get("due")), default=None)
    return {
        "growth_enabled": settings.growth_enabled,
        "enable_publish": settings.enable_publish,
        "use_mock": settings.use_mock,
        "stock": sum(r["stock"] for r in counted),
        "awaiting_review": sum(r["awaiting_review"] for r in counted),
        "active_jobs": sum(r["active_jobs"] for r in counted),
        "posted_today": sum(r["posted_today"] for r in counted),
        "inbox_files": inbox_files,
        "paused_channels": [r["slug"] for r in rows if r["paused"] is not None],
        "held_channels": [r["slug"] for r in rows if r["hold"] is not None],
        "blocks": len(blocks),
        "next_post": earliest["due"] if earliest else None,
        "channels": rows,
    }
