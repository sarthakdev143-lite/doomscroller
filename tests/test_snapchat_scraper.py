"""Tests for the Snapchat __NEXT_DATA__ parser.

The fixture mirrors the real payload shape, including the parts that make it
awkward: numeric strings, ``{"value": x}`` scalar boxes, placeholder titles, and
engagement stats living in a different dict from the video metadata.
"""
import json

from app.scrapers.snapchat_scraper import SnapchatScraper, _num, _text, _unwrap


def _story(**overrides):
    """One spotlightStories entry, shaped like the live payload."""
    entry = {
        "story": {
            "storyId": {"value": "STORY123"},
            "thumbnailUrl": {"value": "https://cdn/thumb.256.jpg"},
            "snapList": [{
                "snapIndex": 0,
                "snapId": {"value": "STORY123"},
                "snapMediaType": 1,
                "snapUrls": {
                    "mediaUrl": "https://bolt-gcdn.sc-cdn.net/bo/abc.27.X",
                    "mediaPreviewUrl": {"value": "https://cdn/prev.256.X"},
                },
                "timestampInSec": {"value": 1785348032},
                "isSponsored": None,
            }],
        },
        "metadata": {
            "videoMetadata": {
                "name": "Going dinner",
                "description": "Another Spotlight Snap brought to you by Snapchat",
                "contentUrl": "https://bolt-gcdn.sc-cdn.net/bo/abc.27.X",
                "thumbnailUrl": "https://cdn/thumb.256.jpg",
                "durationMs": "9330",          # numeric string in the wild
                "width": 540,
                "height": 960,
                "viewCount": 24177,
                "shareCount": 0,
                "keywords": [],
                "embeddedTextCaption": "Going dinner",
                "creator": {
                    "$case": "personCreator",
                    "personCreator": {
                        "username": "constantinmir26",
                        "name": "Maya",
                        "url": "https://www.snapchat.com/@constantinmir26",
                        "followerCount": 0,
                    },
                },
            },
            "engagementStats": {
                "viewCount": 24177,
                "shareCount": 55,
                "commentCount": 210,
                "boostCount": 3021,
                "recommendCount": 108,
            },
            "llmTitle": "Maya's Fun Dance Routine | Cute Girls Dancing",
            "llmDescription": "Two young girls perform a synchronized dance routine.",
            "hashtags": [],
            "contextCards": [],
            "cuSignals": [
                {"signal": {"$case": "textMetadata", "textMetadata": {
                    "description": "Siblings enjoying dinner together.",
                    "keywords": ["dinner", "siblings", "twins", "dinner"],
                }}},
                {"signal": {"$case": "detectedLanguage", "detectedLanguage": {
                    "languageScores": [{"languageCode": "en", "score": 0.82}],
                }}},
                {"signal": {"$case": "detectedLanguage", "detectedLanguage": {
                    "languageScores": [{"languageCode": "hi", "score": 0.1}],
                }}},
            ],
            "oneLinkParams": {
                "deepLinkUrl": "https://www.snapchat.com/spotlight/STORY123",
            },
        },
    }
    entry["metadata"].update(overrides.pop("metadata", {}))
    entry["story"].update(overrides.pop("story", {}))
    return entry


def _scraper():
    return SnapchatScraper()


# --------------------------------------------------------------- coercion
def test_num_handles_strings_and_junk():
    assert _num("9330") == 9330
    assert _num(42) == 42
    assert _num(3.7) == 3
    assert _num(None) == 0
    assert _num("not a number") == 0
    assert _num(True) == 0  # booleans are not counts


def test_unwrap_and_text_scalar_boxes():
    assert _unwrap({"value": "x"}) == "x"
    assert _unwrap({"value": "x", "other": 1}) == {"value": "x", "other": 1}
    assert _text({"value": "  padded  "}) == "padded"
    assert _text(None) == ""


# --------------------------------------------------------------- parsing
def test_parse_story_extracts_full_metadata():
    item = _scraper()._parse_story(_story())
    assert item is not None
    assert item.source == "snapchat"
    assert item.source_id == "STORY123"
    assert item.author == "constantinmir26"
    assert item.author_name == "Maya"
    # llmTitle beats the creator caption.
    assert item.title == "Maya's Fun Dance Routine | Cute Girls Dancing"
    assert item.description.startswith("Two young girls")
    assert item.views == 24177
    # likes = boosts + recommends, since Snapchat has no like counter.
    assert item.likes == 3021 + 108
    assert item.comments == 210
    assert item.shares == 55
    assert item.duration_sec == 9          # 9330ms rounds to 9s
    assert (item.width, item.height) == (540, 960)
    assert item.is_vertical
    assert item.media_url.endswith(".27.X")
    assert item.published_at is not None
    assert item.published_at.tzinfo is not None


def test_parse_story_prefers_highest_scoring_language():
    item = _scraper()._parse_story(_story())
    assert item.language == "en"  # 0.82 beats 0.1


def test_parse_story_dedupes_keywords_in_order():
    item = _scraper()._parse_story(_story())
    assert item.keywords == ["dinner", "siblings", "twins"]


def test_parse_story_drops_placeholder_title_and_description():
    entry = _story()
    entry["metadata"]["llmTitle"] = ""
    entry["metadata"]["llmDescription"] = ""
    entry["metadata"]["videoMetadata"]["name"] = "Spotlight Snap"
    item = _scraper()._parse_story(entry)
    # Falls through the placeholder to the real caption.
    assert item.title == "Going dinner"
    # The boilerplate description is discarded rather than republished.
    assert "brought to you by Snapchat" not in item.description


def test_parse_story_skips_sponsored():
    entry = _story()
    entry["story"]["snapList"][0]["isSponsored"] = True
    assert _scraper()._parse_story(entry) is None


def test_parse_story_skips_non_video_snaps():
    entry = _story()
    entry["story"]["snapList"][0]["snapMediaType"] = 0
    assert _scraper()._parse_story(entry) is None


def test_parse_story_requires_a_media_url():
    entry = _story()
    entry["metadata"]["videoMetadata"]["contentUrl"] = ""
    entry["story"]["snapList"][0]["snapUrls"]["mediaUrl"] = ""
    assert _scraper()._parse_story(entry) is None


def test_parse_story_falls_back_to_context_card_creator():
    entry = _story()
    entry["metadata"]["videoMetadata"]["creator"] = {}
    entry["metadata"]["contextCards"] = [
        {"title": "Display Name", "subtitle": "handle_from_card"}
    ]
    item = _scraper()._parse_story(entry)
    assert item.author == "handle_from_card"
    assert item.author_name == "Display Name"


def test_parse_story_tolerates_garbage():
    scraper = _scraper()
    assert scraper._parse_story({}) is None
    assert scraper._parse_story({"story": None, "metadata": None}) is None
    assert scraper._parse_story("not a dict") is None


def test_stories_from_navigates_next_data():
    payload = {"props": {"pageProps": {"spotlightFeed": {
        "spotlightStories": [_story(), {}, "junk"]
    }}}}
    # Non-dict entries are filtered out before parsing.
    assert len(_scraper()._stories_from(payload)) == 2
    assert _scraper()._stories_from({}) == []
    assert _scraper()._stories_from(None) == []


def test_parse_next_data_extracts_embedded_json():
    payload = {"props": {"pageProps": {"ok": True}}}
    html = (
        '<html><head></head><body>'
        f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'
        '</body></html>'
    )
    assert SnapchatScraper._parse_next_data(html) == payload
    assert SnapchatScraper._parse_next_data("<html>no data</html>") is None
    bad = '<script id="__NEXT_DATA__" type="application/json">{nope}</script>'
    assert SnapchatScraper._parse_next_data(bad) is None


# --------------------------------------------------------------- handles
def test_normalize_handle_accepts_every_form():
    scraper = _scraper()
    assert scraper._normalize_handle("snoopdogg") == "snoopdogg"
    assert scraper._normalize_handle("@snoopdogg") == "snoopdogg"
    assert scraper._normalize_handle("https://www.snapchat.com/add/snoopdogg") == "snoopdogg"
    assert scraper._normalize_handle("https://www.snapchat.com/@snoopdogg") == "snoopdogg"
    assert scraper._normalize_handle("") is None
    assert scraper._normalize_handle("has spaces") is None


def test_profile_url_shape():
    assert _scraper()._profile_url("bob") == "https://www.snapchat.com/@bob"


# --------------------------------------------------- crawl budget / known ids
def _page(story_ids):
    """A __NEXT_DATA__ payload carrying the given story ids."""
    return {"props": {"pageProps": {"spotlightFeed": {"spotlightStories": [
        _story(story={"storyId": {"value": sid}},
               metadata={"videoMetadata": {
                   **_story()["metadata"]["videoMetadata"],
                   "contentUrl": f"https://cdn/{sid}.mp4",
               }})
        for sid in story_ids
    ]}}}}


def _graph_scraper(pages, calls):
    """A scraper whose _fetch_next_data serves a canned page graph."""
    s = _scraper()

    def fake_fetch(url, **kwargs):
        calls.append(url)
        for key, payload in pages.items():
            if url.endswith(key):
                return payload
        return _page([])

    s._fetch_next_data = fake_fetch
    return s


def test_known_stories_are_walked_but_do_not_fill_the_quota():
    """Seed page is entirely known: the crawl must push on, not return empty.

    This is the yield-decay bug -- a stateless crawl re-collected the same seed
    page every run, filled max_results with stories already in the database and
    reported them as skipped.
    """
    pages = {
        "/spotlight": _page(["OLD1", "OLD2"]),
        "/spotlight/OLD1": _page(["NEW1", "NEW2"]),
        "/spotlight/OLD2": _page(["NEW3"]),
    }
    calls: list[str] = []
    s = _graph_scraper(pages, calls)

    items = s.scrape_spotlight(max_results=3, known_ids={"OLD1", "OLD2"}, max_requests=10)
    got = {i.source_id for i in items}

    assert got == {"NEW1", "NEW2", "NEW3"}, got
    # It had to walk *through* the known stories to reach the new ones.
    assert any(c.endswith("/spotlight/OLD1") for c in calls)


def test_without_known_ids_seed_page_still_counts():
    """Regression guard: default behaviour is unchanged when nothing is known."""
    pages = {"/spotlight": _page(["A", "B"])}
    calls: list[str] = []
    s = _graph_scraper(pages, calls)

    items = s.scrape_spotlight(max_results=2, max_requests=10)
    assert {i.source_id for i in items} == {"A", "B"}
    # Quota filled by the seed page alone -- no cursor requests needed.
    assert calls == ["https://www.snapchat.com/spotlight"]


def test_request_budget_is_honoured():
    pages = {
        "/spotlight": _page(["OLD1", "OLD2", "OLD3"]),
        "/spotlight/OLD1": _page(["OLD4"]),
        "/spotlight/OLD2": _page(["OLD5"]),
        "/spotlight/OLD3": _page(["OLD6"]),
    }
    calls: list[str] = []
    s = _graph_scraper(pages, calls)

    # Everything is known, so the crawl can never fill the quota: only the
    # request budget stops it.
    known = {f"OLD{n}" for n in range(1, 7)}
    items = s.scrape_spotlight(max_results=50, known_ids=known, max_requests=3)

    assert items == []
    assert len(calls) == 3
