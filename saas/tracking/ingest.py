"""Validation and processing of t.js batches (`POST /api/t`).

A batch is one page load's events since the last flush:

    {"v": 1, "pv": "p_<hex>", "now": <client ms>, "url": "/pricing?utm_source=x", "ref": "...",
     "title": "...", "fp": "<hex>", "sig": {...}, "layout": [{"n","t","h"}], "e": [event, ...]}
    event: {"t": type, "ts": <client ms>, "n": name, "v": number, "x": 0..1, "y": 0..1, "s": section,
            "d": {small props}}

Server side: the visitor comes from the HttpOnly `vid` cookie; sessions are cut here (30 minutes of
silence); timestamps are re-based on the server clock (client clocks lie); paths are stripped of
query strings and anything that looks like a secret token; nothing that could be a form value is
accepted (props are size-capped primitives, and t.js never sends values).
"""
from __future__ import annotations

import math
import re
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..common import client_ip, utcnow
from ..models.tracking import PageView, TrackEvent, TrackSession, Visitor
from ..security import bans, forms, netintel
from ..security.dbutil import touch_signal
from . import geo as geo_mod
from . import identity, scoring
from . import ua as ua_mod

SESSION_GAP = timedelta(minutes=30)
MAX_EVENTS = 300
MAX_AGE_MS = 30 * 60 * 1000

# t.js event types -> stored type. hb/scroll/leave only update aggregates.
STORED = {"pageview", "dwell", "click", "rage", "dead", "heat", "focus", "submit", "abandon", "vital",
          "error", "custom", "imp", "hover"}
AGGREGATE_ONLY = {"hb", "scroll", "leave", "vis"}
TYPES = STORED | AGGREGATE_ONLY
VITALS = {"LCP", "CLS", "INP", "FCP", "TTFB"}

_TOKENISH = re.compile(r"^[A-Za-z0-9_\-]{20,}$")
_SECRET_PATHS = re.compile(r"^/(waitlist/confirm|reset|verify-email|verify|invite|magic|auth/[^/]+/callback)(/|$)")
_KEEP_QUERY = ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "ref", "gclid",
               "fbclid", "msclkid", "ttclid", "li_fat_id", "via")


def _trunc(v: object, n: int) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s[:n] if s else None


def _num(v: object, lo: float, hi: float) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return min(hi, max(lo, f))


def _small_props(d: object, max_keys: int = 12) -> dict | None:
    """Primitives only, short strings, a few keys: room for metadata, no room for a pasted form."""
    if not isinstance(d, dict):
        return None
    out: dict = {}
    for k, v in list(d.items())[:max_keys]:
        k = str(k)[:40]
        if isinstance(v, bool) or v is None:
            out[k] = v
        elif isinstance(v, (int, float)):
            f = _num(v, -1e12, 1e12)
            if f is not None:
                out[k] = f
        elif isinstance(v, str):
            out[k] = v[:120]
        elif isinstance(v, list) and k in ("p", "fields"):
            out[k] = v[:150]
    return out or None


class EventIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    t: str = Field(max_length=16)
    ts: float | None = None
    n: str | None = None
    v: float | None = None
    x: float | None = None
    y: float | None = None
    s: str | None = None
    d: dict | None = None

    @field_validator("n", "s", mode="before")
    @classmethod
    def _short(cls, v):
        return None if v is None else str(v)[:128]


class Batch(BaseModel):
    model_config = ConfigDict(extra="ignore")
    v: int = 1
    pv: str = Field(pattern=r"^p_[a-z0-9]{8,40}$")
    now: float
    url: str = Field(max_length=4096)
    ref: str | None = Field(default=None, max_length=4096)
    title: str | None = None
    fp: str | None = Field(default=None, max_length=128)
    vid: str | None = Field(default=None, max_length=40)
    sig: dict | None = None
    layout: list | None = None
    e: list[EventIn] = Field(default_factory=list, max_length=MAX_EVENTS)

    @field_validator("title", mode="before")
    @classmethod
    def _title(cls, v):
        return None if v is None else str(v)[:200]

    @field_validator("layout")
    @classmethod
    def _layout(cls, v):
        if v is None:
            return None
        if len(v) > 80:
            raise ValueError("layout too long")
        return v


# ---- URL hygiene ---------------------------------------------------------------------------------
def clean_path(raw: str | None) -> str:
    """Path only, secrets redacted: '/reset/AbC...xyz?x=1' -> '/reset/:token'."""
    if not raw:
        return "/"
    try:
        p = urlparse(raw)
    except ValueError:
        return "/"
    path = p.path or "/"
    if not path.startswith("/"):
        path = "/" + path
    parts = path.split("/")
    secret_route = bool(_SECRET_PATHS.match(path))
    out = []
    for i, seg in enumerate(parts):
        if seg and (_TOKENISH.match(seg) or (secret_route and i >= 2 and len(seg) >= 8)):
            out.append(":token")
        else:
            out.append(seg[:80])
    path = "/".join(out)
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return path[:512] or "/"


def campaign_of(raw_url: str | None) -> dict:
    """UTM and click-id attribution from a page URL; everything else in the query is dropped."""
    try:
        q = parse_qs(urlparse(raw_url or "").query)
    except ValueError:
        return {}
    out = {k: q[k][0][:255] for k in _KEEP_QUERY if q.get(k) and q[k][0]}
    if "utm_source" not in out:
        if "gclid" in out:
            out.update(utm_source="google", utm_medium=out.get("utm_medium", "cpc"))
        elif "fbclid" in out:
            out.update(utm_source="facebook", utm_medium=out.get("utm_medium", "social"))
        elif "msclkid" in out:
            out.update(utm_source="bing", utm_medium=out.get("utm_medium", "cpc"))
        elif "ttclid" in out:
            out.update(utm_source="tiktok", utm_medium=out.get("utm_medium", "cpc"))
        elif "ref" in out:
            out.update(utm_source="waitlist-referral", utm_medium="referral", utm_campaign=out["ref"])
    return out


def referrer_of(raw: str | None, own_hosts: set[str]) -> tuple[str | None, str | None]:
    """(origin+path without query, host) for an external referrer; (None, None) for our own pages."""
    if not raw:
        return None, None
    try:
        p = urlparse(raw)
    except ValueError:
        return None, None
    host = (p.hostname or "").lower()
    if not host or host in own_hosts:
        return None, None
    return f"{p.scheme}://{host}{p.path or ''}"[:1024], host[:255]


_SEARCH = ("google.", "bing.", "duckduckgo.", "yahoo.", "yandex.", "baidu.", "ecosia.", "search.brave.")
_SOCIAL = ("facebook.", "instagram.", "t.co", "twitter.", "x.com", "linkedin.", "reddit.", "youtube.",
           "tiktok.", "news.ycombinator.", "producthunt.", "threads.net", "pinterest.", "discord.")


def source_of(campaign: dict, ref_host: str | None) -> tuple[str, str]:
    if campaign.get("utm_source"):
        return campaign["utm_source"].lower()[:128], (campaign.get("utm_medium") or "campaign").lower()[:128]
    if not ref_host:
        return "direct", "none"
    bare = ref_host[4:] if ref_host.startswith("www.") else ref_host
    if any(s in ref_host for s in _SEARCH):
        return bare, "organic"
    if any(s in ref_host for s in _SOCIAL):
        return bare, "social"
    return bare, "referral"


# ---- aggregates -----------------------------------------------------------------------------------
def _bump(d: dict, key: str, by: int, cap_keys: int = 60) -> None:
    """Add to a capped counter map; a new key past the cap is ignored (bounded JSON per visitor)."""
    if key in d or len(d) < cap_keys:
        d[key] = int(d.get(key, 0)) + int(by)


def _client_signals(sig: dict | None) -> tuple[dict | None, list[str]]:
    if not isinstance(sig, dict):
        return None, []
    keep = {}
    for k in ("sw", "sh", "dpr", "cd", "tz", "lang", "langs", "hc", "dm", "touch", "plat", "vendor", "wgl",
              "pdf", "cookies", "vw", "vh"):
        v = sig.get(k)
        if isinstance(v, (int, float, bool)):
            keep[k] = v
        elif isinstance(v, str):
            keep[k] = v[:120]
        elif isinstance(v, list):
            keep[k] = [str(x)[:20] for x in v[:8]]
    auto = sig.get("auto")
    auto = [str(a)[:24] for a in auto[:12]] if isinstance(auto, list) else []
    if auto:
        keep["auto"] = auto
    return keep or None, auto


def _layout(raw: list | None) -> list | None:
    if not raw:
        return None
    out = []
    for item in raw[:60]:
        if not isinstance(item, dict):
            continue
        n = _trunc(item.get("n"), 64)
        t = _num(item.get("t"), 0, 1)
        h = _num(item.get("h"), 0, 1)
        if n and t is not None and h is not None:
            out.append({"n": n, "t": round(t, 4), "h": round(h, 4)})
    return out or None


def _enrich(db: Session, v: Visitor, request: Request, ip: str, *, new_ip: bool, new_ua: bool,
            automation: list[str]) -> None:
    if new_ip:
        g = geo_mod.lookup(ip, request)
        intel = netintel.inspect(ip, g)
        v.country, v.region, v.city, v.geo_timezone = g.country, g.region, g.city, g.timezone
        v.asn, v.asn_org = g.asn, g.asn_org
        v.is_datacenter, v.is_tor, v.is_vpn = intel.is_datacenter, intel.is_tor, intel.is_vpn
    if new_ua:
        info = ua_mod.parse(v.user_agent)
        v.browser, v.browser_version = info.browser, info.browser_version
        v.os, v.os_version = info.os, info.os_version
        v.device_type, v.device_brand, v.device_model = info.device_type, info.device_brand, info.device_model
    info = ua_mod.parse(v.user_agent)
    reasons = ua_mod.bot_reasons(info, automation=automation or (v.client or {}).get("auto"),
                                 datacenter=bool(v.is_datacenter), tor=bool(v.is_tor))
    stats = v.stats or {}
    if (v.pageviews or 0) >= 5 and not (stats.get("clicks") or stats.get("moves") or stats.get("scrolls")):
        reasons.append("no_interaction")
    v.bot_reasons = reasons or None
    v.is_bot = ua_mod.is_bot(reasons)
    if v.is_bot and v.device_type != "bot":
        v.device_type = "bot"
    flags = []
    if v.is_tor:
        flags.append("tor")
    if v.is_vpn:
        flags.append("vpn")
    elif v.is_datacenter:
        flags.append("datacenter")
    if v.is_bot:
        flags.append("bot")
    v.risk_flags = flags or None


def own_hosts(request: Request) -> set[str]:
    from ..config import settings

    hosts = {(urlparse(settings.public_url).hostname or "").lower()}
    h = (request.headers.get("host") or "").split(":")[0].lower()
    if h:
        hosts.add(h)
    return {x for x in hosts if x}


def process(db: Session, request: Request, batch: Batch, *, consent_state: str) -> tuple[dict, str | None]:
    """Store a batch. Returns (response body, vid to set as a cookie if the browser has none)."""
    now = utcnow()
    ip = client_ip(request)
    vid = identity.visitor_id(request)
    set_vid = None
    if not vid:
        vid = batch.vid if identity.valid(batch.vid) else identity.new_visitor_id()
        set_vid = vid
    fp = forms.clean_fingerprint(batch.fp)
    banned = bans.check(db, fingerprint=fp, visitor_id=vid)
    if banned:
        return {"ok": True, "stored": 0}, None

    ua_str = (request.headers.get("user-agent") or "")[:512]
    v = db.get(Visitor, vid)
    created = v is None
    if created:
        v = Visitor(id=vid, created_at=now, last_seen_at=now, first_ip=ip, stats={}, sessions=0, pageviews=0,
                    events=0, active_ms=0)
        db.add(v)
    new_ip = created or v.last_ip != ip
    new_ua = created or v.user_agent != ua_str
    v.last_ip = ip
    v.user_agent = ua_str
    client, automation = _client_signals(batch.sig)
    if client:
        v.client = client
    if consent_state in ("granted", "implied") and v.consent != consent_state:
        v.consent = consent_state
        v.consent_at = now
    if fp and fp != v.fingerprint:
        v.fingerprint = fp
        touch_signal(db, "fp", fp, "visitor", vid, now)
    if new_ip:
        touch_signal(db, "ip", ip, "visitor", vid, now)

    path = clean_path(batch.url)
    camp = campaign_of(batch.url)
    ref_url, ref_host = referrer_of(batch.ref, own_hosts(request))

    # Session: the visitor's latest, unless it has been silent for 30 minutes.
    sess = db.execute(select(TrackSession).where(TrackSession.visitor_id == vid)
                      .order_by(TrackSession.last_seen_at.desc()).limit(1)).scalar_one_or_none()
    if sess is None or now - sess.last_seen_at > SESSION_GAP or (ref_host and camp.get("utm_source")
                                                               and sess.source != camp["utm_source"].lower()):
        src, medium = source_of(camp, ref_host)
        sess = TrackSession(visitor_id=vid, started_at=now, last_seen_at=now, ip=ip, referrer=ref_url,
                            referrer_host=ref_host, source=src, medium=medium, campaign=camp.get("utm_campaign"),
                            term=camp.get("utm_term"), content=camp.get("utm_content"), landing_path=path,
                            pageviews=0, events=0, active_ms=0, max_scroll=0)
        db.add(sess)
        db.flush()
        v.sessions = (v.sessions or 0) + 1
        if created or not v.first_source:
            v.first_referrer, v.first_referrer_host = ref_url, ref_host
            v.first_source, v.first_medium = src, medium
            v.first_campaign, v.first_term = camp.get("utm_campaign"), camp.get("utm_term")
            v.first_landing = path

    _enrich(db, v, request, ip, new_ip=new_ip, new_ua=new_ua, automation=automation)
    sess.ip, sess.country, sess.city = ip, v.country, v.city
    sess.browser, sess.os, sess.device_type = v.browser, v.os, v.device_type
    sess.is_bot = bool(v.is_bot)
    if fp:
        sess.fingerprint = fp

    pv = db.get(PageView, batch.pv)
    if pv is not None and pv.visitor_id != vid:
        db.rollback()
        return {"ok": False, "stored": 0}, None  # someone else's page id: drop, do not merge
    pv_created = pv is None
    if pv_created:
        # A page view counts once, when its id is first seen, however often a client says "pageview".
        pv = PageView(id=batch.pv, visitor_id=vid, session_id=sess.id, started_at=now, last_seen_at=now,
                      path=path, title=batch.title, active_ms=0, max_scroll=0)
        db.add(pv)
    pv.last_seen_at = now
    if batch.title:
        pv.title = batch.title
    lay = _layout(batch.layout)
    if lay:
        pv.layout = lay

    stats = dict(v.stats or {})
    for key in ("paths", "page_ms", "sections", "cta", "hover", "scroll", "forms"):
        stats[key] = dict(stats.get(key) or {})

    stored = 0
    errors_this_batch = 0
    pv_counted = False
    for e in batch.e:
        t = e.t
        if t not in TYPES:
            continue
        age = _num((batch.now or 0) - (e.ts or batch.now or 0), 0, MAX_AGE_MS) or 0
        ts = now - timedelta(milliseconds=age)
        name = _trunc(e.n, 128)
        section = _trunc(e.s, 64)
        val = _num(e.v, -1e9, 1e9)
        x, y = _num(e.x, 0, 1), _num(e.y, 0, 1)
        props = _small_props(e.d)

        if t == "pageview":
            if not pv_created or pv_counted:
                continue
            pv_counted = True
            sess.pageviews = (sess.pageviews or 0) + 1
            v.pageviews = (v.pageviews or 0) + 1
            _bump(stats["paths"], path, 1)
        elif t == "hb":
            ms = int(_num(e.v, 0, 30_000) or 0)
            pv.active_ms = (pv.active_ms or 0) + ms
            sess.active_ms = (sess.active_ms or 0) + ms
            v.active_ms = (v.active_ms or 0) + ms
            _bump(stats["page_ms"], path, ms)
            continue
        elif t == "scroll":
            depth = int(_num(e.v, 0, 100) or 0)
            pv.max_scroll = max(pv.max_scroll or 0, depth)
            sess.max_scroll = max(sess.max_scroll or 0, depth)
            stats["scroll"][path] = max(int(stats["scroll"].get(path, 0)), depth) if (
                path in stats["scroll"] or len(stats["scroll"]) < 60) else stats["scroll"].get(path, 0)
            stats["scrolls"] = int(stats.get("scrolls", 0)) + 1
            continue
        elif t in ("leave", "vis"):
            continue
        elif t == "dwell":
            if not name:
                continue
            val = _num(e.v, 0, 120_000)
            _bump(stats["sections"], name, int(val or 0), cap_keys=80)
            section = section or name
        elif t in ("click", "rage", "dead"):
            stats["clicks"] = int(stats.get("clicks", 0)) + 1
            if t == "click" and name:
                _bump(stats["cta"], name, 1)
            if t in ("rage", "dead"):
                stats[t] = int(stats.get(t, 0)) + 1
        elif t == "heat":
            pts = (props or {}).get("p") or []
            clean = []
            for p in pts[:150]:
                if isinstance(p, (list, tuple)) and len(p) >= 2:
                    px, py = _num(p[0], 0, 1), _num(p[1], 0, 1)
                    if px is not None and py is not None:
                        clean.append([round(px, 4), round(py, 4)])
            if not clean:
                continue
            props = {"p": clean}
            stats["moves"] = int(stats.get("moves", 0)) + 1
        elif t == "hover":
            if name:
                _bump(stats["hover"], name, 1)
        elif t == "focus":
            stats["forms"]["start"] = int(stats["forms"].get("start", 0)) + 1
        elif t == "submit":
            stats["forms"]["submit"] = int(stats["forms"].get("submit", 0)) + 1
        elif t == "abandon":
            stats["forms"]["abandon"] = int(stats["forms"].get("abandon", 0)) + 1
        elif t == "vital":
            if (name or "").upper() not in VITALS:
                continue
            name = name.upper()
        elif t == "error":
            errors_this_batch += 1
            if errors_this_batch > 10:
                continue
            stats["errors"] = int(stats.get("errors", 0)) + 1
        db.add(TrackEvent(visitor_id=vid, session_id=sess.id, page_id=pv.id, ts=ts, type=t, path=path,
                          name=name, section=section, value=val, x=x, y=y, props=props))
        stored += 1

    v.stats = stats
    v.events = (v.events or 0) + stored
    v.last_seen_at = now
    sess.events = (sess.events or 0) + stored
    sess.last_seen_at = now
    sess.current_path = path
    sess.current_title = batch.title or sess.current_title
    scoring.rescore(v)
    db.commit()
    return {"ok": True, "stored": stored}, set_vid


def ensure_visitor(db: Session, request: Request, vid: str, consent_state: str) -> Visitor:
    """Create (or update consent on) a visitor row without any events, e.g. on banner accept."""
    now = utcnow()
    v = db.get(Visitor, vid)
    if v is None:
        ip = client_ip(request)
        v = Visitor(id=vid, created_at=now, last_seen_at=now, first_ip=ip, last_ip=ip, stats={}, sessions=0,
                    pageviews=0, events=0, active_ms=0,
                    user_agent=(request.headers.get("user-agent") or "")[:512])
        db.add(v)
        _enrich(db, v, request, ip, new_ip=True, new_ua=True, automation=[])
    v.consent = consent_state
    v.consent_at = now
    return v
