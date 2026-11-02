"""The AI choosing stock shots from thumbnails, and everything that can go wrong with that."""
import pytest
from app.clipper import ai, broll
from app.clipper.broll import Candidate
from app.config import settings


@pytest.fixture(autouse=True)
def _library(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "content_dir", str(tmp_path / "content"))
    monkeypatch.setattr(settings, "pixabay_api_key", "key")
    monkeypatch.setattr(settings, "pexels_api_key", "")
    monkeypatch.setattr(settings, "clip_broll", True)
    monkeypatch.setattr(ai, "available", lambda: True)
    monkeypatch.setattr(broll, "_playable", lambda path, seconds: True)

    def deliver(link, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"v" * 2000)

    monkeypatch.setattr(broll, "_download", deliver)


def _candidate(number, duration=12.0):
    return Candidate(f"pixabay-{number}", f"https://cdn/{number}.mp4", duration, "Bea", "", broll.PIXABAY,
                     f"https://cdn/{number}.jpg")


def _search_returns(monkeypatch, **results):
    searches = []

    def search(query):
        searches.append(query)
        return results.get(query.replace(" ", "_"), [])

    monkeypatch.setattr(broll, "search", search)
    return searches


# ---- what the libraries' replies turn into

def _hit(hit_id=1, **flags):
    return {"id": hit_id, "duration": 12, "user": "Bea", "pageURL": "https://p", **flags, "videos": {
        "large": {"url": "https://cdn/l.mp4", "height": 2160, "size": 500_000_000, "thumbnail": "https://cdn/l.jpg"},
        "medium": {"url": "https://cdn/m.mp4", "height": 1440, "size": 400_000_000, "thumbnail": "https://cdn/m.jpg"},
        "small": {"url": "https://cdn/s.mp4", "height": 1080, "size": 300_000_000, "thumbnail": "https://cdn/s.jpg"},
        "tiny": {"url": "https://cdn/t.mp4", "height": 720, "size": 50_000_000, "thumbnail": "https://cdn/t.jpg"},
    }}


def test_pixabay_uses_the_tiny_file_when_every_larger_one_is_huge():
    assert broll.best_rendition(_hit())["height"] == 720


def test_pixabay_shots_carry_the_smallest_thumbnail():
    assert broll._pixabay_candidates([_hit()])[0].thumbnail == "https://cdn/t.jpg"


def test_pixabay_falls_back_to_the_chosen_files_thumbnail():
    hit = _hit()
    del hit["videos"]["tiny"]
    hit["videos"]["small"]["size"] = 1
    assert broll._pixabay_candidates([hit])[0].thumbnail == "https://cdn/s.jpg"


@pytest.mark.parametrize("flag", ["isLowQuality", "isAiGenerated"])
def test_flagged_pixabay_shots_are_left_out(flag):
    assert broll._pixabay_candidates([_hit(**{flag: True})]) == []
    assert len(broll._pixabay_candidates([_hit(**{flag: False})])) == 1


def test_pexels_shots_carry_their_picture():
    video = {"id": 5, "duration": 9, "image": "https://images/5.jpg", "video_files": [
        {"file_type": "video/mp4", "width": 1080, "height": 1920, "link": "https://cdn/5.mp4"}]}
    assert broll._pexels_candidates([video])[0].thumbnail == "https://images/5.jpg"


# ---- fetching with a choice

def test_fetch_tries_shots_in_the_order_chosen(monkeypatch):
    _search_returns(monkeypatch, rain=[_candidate(1), _candidate(2), _candidate(3)])
    seen = []

    def choose(found):
        seen.extend(c.video_id for c in found)
        return [found[2], found[0]]

    assert broll.fetch("rain", 2.0, choose=choose).video_id == "pixabay-3"
    assert seen == ["pixabay-1", "pixabay-2", "pixabay-3"]


def test_choice_only_sees_shots_it_could_use(monkeypatch):
    _search_returns(monkeypatch, rain=[_candidate(1), _candidate(2, duration=1.0), _candidate(3)])
    seen = []

    def choose(found):
        seen.extend(c.video_id for c in found)
        return found

    broll.fetch("rain", 2.0, exclude={"pixabay-1"}, choose=choose)
    assert seen == ["pixabay-3"]


def test_no_results_means_no_ai_call(monkeypatch):
    _search_returns(monkeypatch)
    calls = []
    assert broll.fetch("rain", 2.0, choose=lambda found: calls.append(found) or found) is None and not calls


def test_rejecting_all_means_no_clip_and_no_download(monkeypatch):
    _search_returns(monkeypatch, rain=[_candidate(1)])
    downloads = []
    monkeypatch.setattr(broll, "_download", lambda link, path: downloads.append(link))
    assert broll.fetch("rain", 2.0, choose=lambda found: []) is None and not downloads


def test_next_choice_is_tried_when_the_best_will_not_download(monkeypatch):
    _search_returns(monkeypatch, rain=[_candidate(1), _candidate(2), _candidate(3)])

    def deliver(link, path):
        if "1.mp4" in link:
            raise broll.StockError("download returned 500")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"v" * 2000)

    monkeypatch.setattr(broll, "_download", deliver)
    assert broll.fetch("rain", 2.0, choose=lambda found: [found[0], found[2]]).video_id == "pixabay-3"


def test_tests_cannot_reach_real_services():
    from app.config import Settings
    fresh = Settings()
    assert fresh.pixabay_api_key == "" and fresh.pexels_api_key == "" and fresh.deepgram_api_key == ""
    assert fresh.opencode_bin == "definitely-not-installed"
