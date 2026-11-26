"""Lead score (0-100), intent label and inferred interests: a transparent rules model.

Every point comes with a human-readable reason, shown in the admin panel next to the score, so the
owner can see *why* someone is a "ready to buy" and argue with the rule rather than a black box.
Inputs are the rolling aggregates ingest keeps in `Visitor.stats`:

    paths {path: views}            page_ms {path: active ms}     scroll {path: max %}
    sections {name: dwell ms}      cta {name: clicks}            hover {name: count}
    forms {start, submit, abandon} rage, dead, errors, clicks, moves
"""
from __future__ import annotations

import re

LABELS = ("ready to buy", "comparing prices", "developer / API", "browsing", "bot")

_CTA_RE = re.compile(r"(join|waitlist|signup|sign-up|sign_up|register|start|get-started|try|buy|checkout|"
                     r"subscribe|upgrade|plan|pricing|cta|purchase|pay)", re.I)
_PRICING_RE = re.compile(r"(pric|plan|tier|credit|billing|pack|cost|compare)", re.I)
_DEV_RE = re.compile(r"(developer|docs|api|mcp|sdk|webhook|integration|automation|cli)", re.I)
_COMPETITOR_RE = re.compile(r"(opus ?clip|opusclip|vizard|klap|submagic|2short|munch|vidyo|descript|"
                            r"captions\.ai|kapwing|veed|invideo|quso|ssemble|alternative|\bvs\b|versus|"
                            r"pricing|price|cheap|cheaper|best .*clip)", re.I)
_SEARCH_HOSTS = ("google.", "bing.", "duckduckgo.", "yahoo.", "yandex.", "baidu.", "ecosia.", "brave.")
_COMMUNITY_HOSTS = ("producthunt.", "news.ycombinator.", "reddit.", "indiehackers.", "x.com", "twitter.",
                    "linkedin.", "youtube.")

# Section / path words -> what the visitor is probably looking for.
_TOPICS = [
    (re.compile(r"(pric|plan|tier|credit|billing|pack)", re.I), "pricing & plans"),
    (re.compile(r"(api|mcp|developer|docs|sdk|webhook|integration)", re.I), "API / automation"),
    (re.compile(r"(caption|subtitle|transcri)", re.I), "captions"),
    (re.compile(r"(b-?roll|stock|cutaway)", re.I), "B-roll"),
    (re.compile(r"(meme|reaction)", re.I), "memes / reactions"),
    (re.compile(r"(music|sfx|sound|audio)", re.I), "sound design"),
    (re.compile(r"(hindi|language|multilingual|translat)", re.I), "Hindi / other languages"),
    (re.compile(r"(faq|question)", re.I), "answers before buying (FAQ)"),
    (re.compile(r"(testimonial|review|social|proof|logos|customers)", re.I), "social proof"),
    (re.compile(r"(demo|example|showcase|sample|gallery|before|after|output|video)", re.I), "output quality"),
    (re.compile(r"(compare|vs|alternative|comparison)", re.I), "comparisons"),
    (re.compile(r"(privacy|security|rights|copyright|legal|terms)", re.I), "rights & privacy"),
    (re.compile(r"(schedul|youtube|post|publish|channel)", re.I), "auto-posting to YouTube"),
    (re.compile(r"(how|works|steps|process|feature)", re.I), "how it works"),
]


def _sum_matching(d: dict | None, rx: re.Pattern) -> float:
    return float(sum(v for k, v in (d or {}).items() if isinstance(v, (int, float)) and rx.search(str(k))))


def compute(stats: dict | None, *, sessions: int = 0, active_ms: int = 0, is_bot: bool = False,
            bot_reasons: list | None = None, waitlist: str | None = None, user: bool = False,
            referrer_host: str | None = None, source: str | None = None, term: str | None = None,
            campaign: str | None = None) -> tuple[int, str, list[str], list[dict]]:
    """-> (score, label, reasons, interests). `waitlist` is None | 'joined' | 'confirmed'."""
    s = stats or {}
    if is_bot:
        why = ", ".join(bot_reasons or []) or "automation detected"
        return 0, "bot", [f"bot: {why}"], []

    score = 0
    reasons: list[str] = []

    def add(points: int, why: str) -> None:
        nonlocal score
        if points:
            score += points
            reasons.append(f"+{points} {why}")

    paths = s.get("paths") or {}
    page_ms = s.get("page_ms") or {}
    sections = s.get("sections") or {}

    pricing_views = int(sum(v for k, v in paths.items() if k.startswith("/pricing")))
    # Time on /pricing and dwell on pricing sections overlap when the sections live on /pricing, so
    # take the larger rather than the sum.
    pricing_ms = max(_sum_matching({k: v for k, v in page_ms.items() if k.startswith("/pricing")}, re.compile(".")),
                     _sum_matching(sections, _PRICING_RE))
    if pricing_views:
        add(min(16, 8 * pricing_views), f"viewed pricing {pricing_views}x")
    if pricing_ms >= 60_000:
        add(18, f"spent {int(pricing_ms / 1000)}s on pricing")
    elif pricing_ms >= 20_000:
        add(10, f"spent {int(pricing_ms / 1000)}s on pricing")

    if sessions >= 4:
        add(18, f"came back {sessions - 1} times")
    elif sessions >= 2:
        add(10, "returning visitor")

    if active_ms >= 120_000:
        add(6, f"{int(active_ms / 60000)} min engaged")
    landing_scroll = max([v for k, v in (s.get("scroll") or {}).items() if k in ("/", "")] or [0])
    if landing_scroll >= 75:
        add(5, f"read {int(landing_scroll)}% of the landing page")

    cta = s.get("cta") or {}
    cta_intent = int(sum(v for k, v in cta.items() if _CTA_RE.search(k)))
    cta_other = int(sum(v for k, v in cta.items() if not _CTA_RE.search(k)))
    if cta_intent:
        add(min(18, 6 * cta_intent), f"clicked a call to action {cta_intent}x")
    if cta_other:
        add(min(4, cta_other), "clicked around")
    hovers = int(sum(v for k, v in (s.get("hover") or {}).items() if _CTA_RE.search(k)))
    if hovers:
        add(min(6, 2 * hovers), "hovered on buy/join buttons")

    forms = s.get("forms") or {}
    if forms.get("submit"):
        add(10, "submitted a form")
    elif forms.get("start"):
        add(8, "started filling a form")

    dev_views = int(sum(v for k, v in paths.items() if _DEV_RE.search(k)))
    dev_ms = max(_sum_matching({k: v for k, v in page_ms.items() if _DEV_RE.search(k)}, re.compile(".")),
                 _sum_matching(sections, _DEV_RE))
    if dev_views:
        add(6, "read the developer / API docs")
    if dev_ms >= 30_000:
        add(6, f"{int(dev_ms / 1000)}s on API / MCP content")

    intent_text = " ".join(x for x in (term, campaign, source) if x)
    comparing_ref = bool(intent_text and _COMPETITOR_RE.search(intent_text))
    host = (referrer_host or "").lower()
    if comparing_ref:
        add(8, f"arrived searching '{intent_text[:60]}'")
    elif any(h in host for h in _SEARCH_HOSTS):
        add(3, f"found us via search ({host})")
    elif any(h in host for h in _COMMUNITY_HOSTS):
        add(3, f"referred by {host}")

    if waitlist == "confirmed":
        add(28, "joined and confirmed the waitlist")
    elif waitlist == "joined":
        add(20, "joined the waitlist (unconfirmed)")
    if user:
        add(25, "signed up")

    if s.get("rage"):
        reasons.append(f"! {s['rage']} rage click(s): something frustrated them")
    if s.get("dead"):
        reasons.append(f"! {s['dead']} dead click(s): expected something clickable")

    score = max(0, min(100, score))

    pricing_signal = pricing_views >= 1 and (pricing_ms >= 20_000 or pricing_views >= 2)
    comparing = comparing_ref or pricing_views >= 2 or pricing_ms >= 45_000
    total_ms = max(1.0, float(sum(v for v in page_ms.values() if isinstance(v, (int, float)))))
    dev = (dev_views >= 1 and dev_ms / total_ms >= 0.3) or dev_ms >= 30_000
    acted = cta_intent >= 1 or forms.get("start") or waitlist or user
    if score >= 60 and acted and (pricing_signal or user):
        label = "ready to buy"
    elif dev:
        label = "developer / API"
    elif comparing:
        label = "comparing prices"
    else:
        label = "browsing"

    return score, label, reasons, interests(s)


def interests(stats: dict | None, limit: int = 5) -> list[dict]:
    """What the visitor is probably looking for, by dwell: [{"topic", "ms", "from": [sections]}]."""
    s = stats or {}
    weights: dict[str, dict] = {}
    sources = list((s.get("sections") or {}).items()) + list((s.get("page_ms") or {}).items())
    for name, ms in sources:
        if not isinstance(ms, (int, float)) or ms < 1500:
            continue
        for rx, topic in _TOPICS:
            if rx.search(str(name)):
                w = weights.setdefault(topic, {"topic": topic, "ms": 0, "from": []})
                w["ms"] += int(ms)
                if len(w["from"]) < 4 and name not in w["from"]:
                    w["from"].append(str(name)[:64])
                break
    return sorted(weights.values(), key=lambda w: -w["ms"])[:limit]


def rescore(v) -> None:
    """Recompute a Visitor's score in place (caller commits)."""
    wl = None
    if getattr(v, "waitlist_id", None):
        st = (v.stats or {}).get("waitlist")
        wl = "confirmed" if st == "confirmed" else "joined"
    score, label, reasons, ints = compute(
        v.stats, sessions=v.sessions or 0, active_ms=v.active_ms or 0, is_bot=bool(v.is_bot),
        bot_reasons=v.bot_reasons, waitlist=wl, user=bool(getattr(v, "user_id", None)),
        referrer_host=v.first_referrer_host, source=v.first_source, term=v.first_term,
        campaign=v.first_campaign)
    v.lead_score = score
    v.lead_label = label
    v.lead_reasons = reasons
    v.interests = ints
