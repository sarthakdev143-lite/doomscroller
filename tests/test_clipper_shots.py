"""The AI choosing stock shots from thumbnails, and everything that can go wrong with that."""
import http.server
from io import BytesIO
import os
import threading

import numpy as np
from PIL import Image
import pytest

from app.clipper import ai, broll, shots
from app.clipper.broll import Candidate
from app.clipper.edit import Cutaway, EditPlan, attach_stock
from app.clipper.transcribe import Word
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


def _image(width=1280, height=720, colour=(10, 200, 30)):
    return np.full((height, width, 3), colour, np.uint8)


def _stub_thumbnails(monkeypatch, failing=()):
    asked = []

    def thumbnail(client, link):
        asked.append(link)
        return None if link in failing else _image()

    monkeypatch.setattr(shots, "_thumbnail", thumbnail)
    return asked


def _plan(*cutaways):
    words = [Word(f"w{i}", 10 + i * 0.5, 10.4 + i * 0.5, 0.99) for i in range(40)]
    return EditPlan(words=words, segments=[(10.0, 30.0)], speed=1.0, cutaways=[Cutaway(at, 2.0, q) for at, q in cutaways])


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


# ---- the contact sheet

@pytest.mark.parametrize("width,height,expected", [
    (1280, 720, (720, 405)), (720, 1280, (1280, 720)), (1080, 1920, (1920, 1080)), (500, 2000, (888, 500)),
    (1, 1, (1, 1)),
])
def test_thumbnail_is_cropped_like_the_render(width, height, expected):
    assert shots._as_rendered(_image(width, height)).shape[:2] == expected


def test_crop_keeps_the_centre_of_the_picture():
    image = _image(1280, 720, (0, 0, 0))
    image[:, 600:680] = (255, 255, 255)
    kept = shots._as_rendered(image)
    assert kept[0, kept.shape[1] // 2].tolist() == [255, 255, 255]
    assert kept[0, 0].tolist() == [0, 0, 0]


@pytest.mark.parametrize("count,rows", [(1, 1), (4, 1), (5, 2), (8, 2)])
def test_sheet_has_a_numbered_tile_for_every_shot(tmp_path, count, rows):
    path = str(tmp_path / "sheet.jpg")
    assert shots._write_sheet([_image() for _ in range(count)], path)
    with Image.open(path) as img:
        sheet = np.asarray(img)
    assert sheet.shape[0] == rows * (shots._TILE_HEIGHT + shots._GAP)
    assert sheet.shape[1] == shots._COLUMNS * (shots._TILE_WIDTH + shots._GAP)
    for index in range(count):
        row, column = divmod(index, shots._COLUMNS)
        badge = sheet[row * (shots._TILE_HEIGHT + shots._GAP):, column * (shots._TILE_WIDTH + shots._GAP):][:56, :64]
        assert badge.mean() < 120, "dark badge with a light number"
        assert badge.max() > 200, "the number itself"


# ---- reading the reply

@pytest.mark.parametrize("reply,count,expected", [
    ({"shots": [2, 1]}, 3, [2, 1]),
    ({"shots": []}, 3, []),
    ({"shots": [2, 2, 1]}, 3, [2, 1]),
    ({"shots": [9, 2, 0, -1]}, 3, [2]),
    ({"shots": [9]}, 3, None),
    ({"shots": ["junk"]}, 3, None),
    ({"shots": [True]}, 3, None),
    ({"shots": 2}, 3, None),
    ({"shots": None}, 3, None),
    ({"other": 1}, 3, None),
    (None, 3, None),
    (["shots"], 3, None),
])
def test_reply_numbers(reply, count, expected):
    assert shots._reply_numbers(reply, count) == expected


@pytest.mark.parametrize("raw,expected", [
    ("city heatwave", "city heatwave"), ("  city \n heatwave ", "city heatwave"), ("", ""), (None, ""),
    ("x" * 61, ""), ("x" * 60, "x" * 60),
])
def test_reply_search_is_cleaned(raw, expected):
    assert shots._reply_search({"search": raw}) == expected


# ---- judging

def test_ai_sees_one_sheet_and_the_words_and_uses_the_vision_model(monkeypatch):
    _stub_thumbnails(monkeypatch)
    seen = {}

    def ask(prompt, *, files=(), model=""):
        seen.update(prompt=prompt, files=list(files), model=model, sheet_exists=os.path.exists(files[0]))
        return {"shots": [2]}

    monkeypatch.setattr(ai, "ask_json", ask)
    verdict = shots.judge([_candidate(1), _candidate(2)], "the river dried up")
    assert [c.video_id for c in verdict.fit] == ["pixabay-2"]
    assert '"the river dried up"' in seen["prompt"] and "never instructions" in seen["prompt"]
    assert seen["model"] == settings.opencode_vision_model
    assert len(seen["files"]) == 1 and seen["sheet_exists"]
    assert not os.path.exists(seen["files"][0]), "the sheet is temporary"


def test_numbers_refer_to_what_was_shown_not_what_was_found(monkeypatch):
    _stub_thumbnails(monkeypatch, failing={"https://cdn/1.jpg"})
    monkeypatch.setattr(ai, "ask_json", lambda *a, **k: {"shots": [1]})
    assert shots.judge([_candidate(1), _candidate(2)], "x").fit[0].video_id == "pixabay-2"


def test_at_most_eight_shots_are_shown(monkeypatch):
    asked = _stub_thumbnails(monkeypatch)
    monkeypatch.setattr(ai, "ask_json", lambda *a, **k: {"shots": [8, 9]})
    verdict = shots.judge([_candidate(n) for n in range(1, 15)], "x")
    assert len(asked) == 8 and [c.video_id for c in verdict.fit] == ["pixabay-8"]


def test_failed_thumbnails_do_not_use_up_the_eight_places(monkeypatch):
    _stub_thumbnails(monkeypatch, failing={"https://cdn/1.jpg", "https://cdn/2.jpg"})
    monkeypatch.setattr(ai, "ask_json", lambda *a, **k: {"shots": [8]})
    assert shots.judge([_candidate(n) for n in range(1, 15)], "x").fit[0].video_id == "pixabay-10"


def test_long_speech_is_cut_short_in_the_prompt():
    prompt = shots._prompt("word " * 500, 3)
    assert len(prompt) < 2000 and "word" in prompt


def test_rejecting_everything_can_suggest_a_search(monkeypatch):
    _stub_thumbnails(monkeypatch)
    monkeypatch.setattr(ai, "ask_json", lambda *a, **k: {"shots": [], "search": "hot sun"})
    verdict = shots.judge([_candidate(1)], "x")
    assert verdict.fit == [] and verdict.better_search == "hot sun"


def test_a_suggested_search_is_ignored_when_something_fits(monkeypatch):
    _stub_thumbnails(monkeypatch)
    monkeypatch.setattr(ai, "ask_json", lambda *a, **k: {"shots": [1], "search": "hot sun"})
    assert shots.judge([_candidate(1)], "x").better_search == ""


@pytest.mark.parametrize("reply", [None, {"shots": "junk"}, {"shots": [99]}])
def test_nonsense_means_could_not_judge(monkeypatch, reply):
    _stub_thumbnails(monkeypatch)
    monkeypatch.setattr(ai, "ask_json", lambda *a, **k: reply)
    assert shots.judge([_candidate(1)], "x") is None


def test_no_thumbnails_means_the_ai_is_not_asked(monkeypatch):
    _stub_thumbnails(monkeypatch, failing={"https://cdn/1.jpg"})
    calls = []
    monkeypatch.setattr(ai, "ask_json", lambda *a, **k: calls.append(1))
    with pytest.raises(shots.NothingToShow):
        shots.judge([_candidate(1)], "x")
    assert not calls


def test_shots_has_no_opencv_dependency():
    assert not hasattr(shots, "cv2")


def test_sheet_that_cannot_be_written_counts_as_failure(monkeypatch):
    _stub_thumbnails(monkeypatch)
    monkeypatch.setattr(shots, "_write_sheet", lambda images, path: False)
    assert shots.judge([_candidate(1)], "x") is None


def test_rejection_says_which_shots_were_seen(monkeypatch):
    _stub_thumbnails(monkeypatch, failing={"https://cdn/2.jpg"})
    monkeypatch.setattr(ai, "ask_json", lambda *a, **k: {"shots": []})
    assert shots.judge([_candidate(1), _candidate(2), _candidate(3)], "x").rejected == ("pixabay-1", "pixabay-3")


# ---- real connections for the thumbnails

@pytest.fixture
def thumbnails():
    buf = BytesIO()
    Image.fromarray(_image(640, 360), mode="RGB").save(buf, format="JPEG", quality=85)
    good = buf.getvalue()
    routes = {"/good.jpg": (200, good), "/gone.jpg": (404, b"no"), "/html.jpg": (200, b"<html>sorry</html>"),
              "/empty.jpg": (200, b""), "/huge.jpg": (200, b"x" * (shots._MAX_THUMBNAIL_BYTES + 1000))}

    class Files(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            status, body = routes[self.path]
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Files)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_thumbnail_download_cases(thumbnails):
    with shots.httpx.Client(timeout=shots._TIMEOUT) as client:
        assert shots._thumbnail(client, f"{thumbnails}/good.jpg").shape == (360, 640, 3)
        for name in ("gone", "html", "empty", "huge"):
            assert shots._thumbnail(client, f"{thumbnails}/{name}.jpg") is None, name
        assert shots._thumbnail(client, "") is None
        assert shots._thumbnail(client, "http://127.0.0.1:1/refused.jpg") is None
        assert shots._thumbnail(client, "not a url") is None


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


# ---- the whole decision, per cutaway and across a job

def test_ai_is_told_the_words_around_the_cutaway(monkeypatch):
    _search_returns(monkeypatch, rain=[_candidate(1)])
    told = []
    monkeypatch.setattr(shots, "judge", lambda found, spoken: told.append(spoken) or shots.Verdict(list(found)))
    attach_stock(_plan((15.0, "rain")))
    words = told[0].split()
    # Shot runs 15-17, plus a second either side, and words are half a second apart.
    assert words[0] == "w8" and words[-1] == "w15"


def test_words_after_a_cut_inside_the_shot_are_included(monkeypatch):
    _search_returns(monkeypatch, rain=[_candidate(1)])
    told = []
    monkeypatch.setattr(shots, "judge", lambda found, spoken: told.append(spoken) or shots.Verdict(list(found)))
    first = [Word(f"a{i}", 10 + i * 0.4, 10.3 + i * 0.4, 0.99) for i in range(15)]
    second = [Word(f"c{i}", 34 + i * 0.4, 34.3 + i * 0.4, 0.99) for i in range(15)]
    plan = EditPlan(words=first + second, segments=[(10.0, 16.0), (34.0, 40.0)], speed=1.12,
                    cutaways=[Cutaway(14.4, 3.0, "rain")])
    attach_stock(plan)
    spoken = told[0].split()
    # The shot plays over the last of the first sentence and well into the second.
    assert "a11" in spoken and "c0" in spoken and "c5" in spoken
    assert "a6" not in spoken and "c13" not in spoken


def test_a_shot_with_no_position_in_the_edit_skips_the_ai(monkeypatch):
    _search_returns(monkeypatch, rain=[_candidate(1)])
    calls = []
    monkeypatch.setattr(shots, "judge", lambda found, spoken: calls.append(spoken))
    plan = _plan((99.0, "rain"))
    attach_stock(plan)
    assert not calls and plan.cutaways[0].clip.video_id == "pixabay-1"


def test_chosen_shot_goes_on_the_cutaway(monkeypatch):
    _search_returns(monkeypatch, rain=[_candidate(1), _candidate(2)])
    monkeypatch.setattr(shots, "judge", lambda found, spoken: shots.Verdict([found[1]]))
    plan = _plan((15.0, "rain"))
    assert attach_stock(plan) == [] and plan.cutaways[0].clip.video_id == "pixabay-2"


def test_better_search_is_tried_once_and_recorded(monkeypatch):
    searches = _search_returns(monkeypatch, rain=[_candidate(1)], hot_sun=[_candidate(2)])

    def judge(found, spoken):
        if found[0].video_id == "pixabay-1":
            return shots.Verdict([], "hot sun")
        return shots.Verdict(list(found))

    monkeypatch.setattr(shots, "judge", judge)
    plan = _plan((15.0, "rain"))
    attach_stock(plan)
    assert searches == ["rain", "hot sun"]
    assert plan.cutaways[0].clip.video_id == "pixabay-2" and plan.cutaways[0].query == "hot sun"


def test_second_rejection_drops_the_cutaway_without_a_third_search(monkeypatch):
    searches = _search_returns(monkeypatch, rain=[_candidate(1)], hot_sun=[_candidate(2)], snow=[_candidate(3)])
    replies = iter(["hot sun", "snow"])
    monkeypatch.setattr(shots, "judge", lambda found, spoken: shots.Verdict([], next(replies)))
    plan = _plan((15.0, "rain"))
    assert attach_stock(plan) == [] and plan.cutaways == []
    assert searches == ["rain", "hot sun"]


def test_suggesting_the_same_search_again_is_not_retried(monkeypatch):
    searches = _search_returns(monkeypatch, rain=[_candidate(1)])
    monkeypatch.setattr(shots, "judge", lambda found, spoken: shots.Verdict([], "RAIN"))
    plan = _plan((15.0, "rain"))
    attach_stock(plan)
    assert searches == ["rain"] and plan.cutaways == []


def test_rejection_without_a_suggestion_drops_the_cutaway(monkeypatch):
    searches = _search_returns(monkeypatch, rain=[_candidate(1)])
    monkeypatch.setattr(shots, "judge", lambda found, spoken: shots.Verdict([]))
    plan = _plan((15.0, "rain"))
    assert attach_stock(plan) == [] and plan.cutaways == [] and searches == ["rain"]


def test_a_suggestion_from_one_cutaway_never_leaks_into_the_next(monkeypatch):
    searches = _search_returns(monkeypatch, rain=[_candidate(1)], sun=[_candidate(2)], hot=[_candidate(3)])
    replies = iter([shots.Verdict([], "hot"), shots.Verdict([]), shots.Verdict([])])
    monkeypatch.setattr(shots, "judge", lambda found, spoken: next(replies))
    attach_stock(_plan((12.0, "rain"), (18.0, "sun")))
    assert searches == ["rain", "hot", "sun"]


def test_better_search_that_is_rate_limited_stops_the_job_cleanly(monkeypatch):
    def search(query):
        if query == "hot":
            raise broll.RateLimited("Pixabay rate limit reached")
        return [_candidate(1)]

    monkeypatch.setattr(broll, "search", search)
    monkeypatch.setattr(shots, "judge", lambda found, spoken: shots.Verdict([], "hot"))
    session = broll.StockSession()
    plan = _plan((12.0, "rain"), (18.0, "sun"))
    warnings = attach_stock(plan, session)
    assert session.stopped and plan.cutaways == [] and "rate limit" in warnings[0]


def test_used_shot_is_not_offered_again_on_the_better_search(monkeypatch):
    _search_returns(monkeypatch, rain=[_candidate(1)], sun=[_candidate(2)], hot=[_candidate(1), _candidate(3)])
    offered = []

    def judge(found, spoken):
        offered.append([c.video_id for c in found])
        if found[0].video_id == "pixabay-2":
            return shots.Verdict([], "hot")
        return shots.Verdict(list(found))

    monkeypatch.setattr(shots, "judge", judge)
    plan = _plan((12.0, "rain"), (18.0, "sun"))
    attach_stock(plan)
    assert offered == [["pixabay-1"], ["pixabay-2"], ["pixabay-3"]]
    assert [c.clip.video_id for c in plan.cutaways] == ["pixabay-1", "pixabay-3"]


def test_when_the_ai_fails_no_unjudged_stock_is_used_and_it_is_not_asked_again(monkeypatch):
    _search_returns(monkeypatch, rain=[_candidate(1)], sun=[_candidate(2)])
    calls = []
    monkeypatch.setattr(shots, "judge", lambda found, spoken: calls.append(spoken))
    session = broll.StockSession()
    plan = _plan((12.0, "rain"), (18.0, "sun"))
    warnings = attach_stock(plan, session)
    assert plan.cutaways == []
    assert len(calls) == 1 and session.picking_failed
    assert len(warnings) == 1 and "left them out" in warnings[0]

    later = _plan((12.0, "rain"))
    assert attach_stock(later, session) == [] and later.cutaways == [] and len(calls) == 1


def test_without_ai_the_first_match_is_used_and_nothing_extra_is_downloaded(monkeypatch):
    _search_returns(monkeypatch, rain=[_candidate(1), _candidate(2)])
    monkeypatch.setattr(ai, "available", lambda: False)
    asked = _stub_thumbnails(monkeypatch)
    plan = _plan((15.0, "rain"))
    assert attach_stock(plan) == [] and plan.cutaways[0].clip.video_id == "pixabay-1" and not asked


def test_dead_thumbnails_cost_one_search_and_leave_the_ai_on(monkeypatch):
    _search_returns(monkeypatch, rain=[_candidate(1)], sun=[_candidate(2), _candidate(3)])
    _stub_thumbnails(monkeypatch, failing={"https://cdn/1.jpg"})
    asks = []

    def ask(prompt, *, files=(), model=""):
        asks.append(prompt)
        return {"shots": [2]}

    monkeypatch.setattr(ai, "ask_json", ask)
    session = broll.StockSession()
    plan = _plan((12.0, "rain"), (18.0, "sun"))
    assert attach_stock(plan, session) == []
    assert not session.picking_failed and len(asks) == 1
    assert [c.clip.video_id for c in plan.cutaways] == ["pixabay-1", "pixabay-3"]


def test_better_search_never_offers_a_shot_already_rejected(monkeypatch):
    searches = _search_returns(monkeypatch, rain=[_candidate(1), _candidate(2)], hot=[_candidate(1), _candidate(5)])
    _stub_thumbnails(monkeypatch)
    offered = []
    replies = iter([{"shots": [], "search": "hot"}, {"shots": [1]}])

    def ask(prompt, *, files=(), model=""):
        offered.append(prompt.split(" stock video shots")[0])
        return next(replies)

    monkeypatch.setattr(ai, "ask_json", ask)
    plan = _plan((15.0, "rain"))
    attach_stock(plan)
    assert searches == ["rain", "hot"] and offered[1].endswith("shows 1")
    assert plan.cutaways[0].clip.video_id == "pixabay-5"


def test_ai_failing_on_the_better_search_does_not_bring_back_a_rejected_shot(monkeypatch):
    _search_returns(monkeypatch, rain=[_candidate(1)], hot=[_candidate(1), _candidate(5)])
    _stub_thumbnails(monkeypatch)
    replies = iter([{"shots": [], "search": "hot"}, None])
    monkeypatch.setattr(ai, "ask_json", lambda *a, **k: next(replies))
    plan = _plan((15.0, "rain"))
    attach_stock(plan)
    assert plan.cutaways == []  # neither the rejected shot nor an unjudged one


def _failing_downloads(monkeypatch, bad):
    def deliver(link, path):
        if any(f"/{n}.mp4" in link for n in bad):
            raise broll.StockError("download returned 500")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"v" * 2000)

    monkeypatch.setattr(broll, "_download", deliver)


def test_one_bad_file_chosen_by_the_ai_costs_only_that_cutaway(monkeypatch):
    _search_returns(monkeypatch, a=[_candidate(1)], b=[_candidate(2)], c=[_candidate(3)])
    monkeypatch.setattr(shots, "judge", lambda found, spoken: shots.Verdict(list(found)))
    _failing_downloads(monkeypatch, {1, 2})
    session = broll.StockSession()
    plan = _plan((11.0, "a"), (15.0, "b"), (19.0, "c"))
    attach_stock(plan, session)
    assert not session.stopped and [c.clip.video_id for c in plan.cutaways] == ["pixabay-3"]
    assert session.failed_files == 0, "a good download clears the count"


def test_four_bad_files_in_a_row_still_mean_a_dead_cdn(monkeypatch):
    searches = _search_returns(monkeypatch, a=[_candidate(1)], b=[_candidate(2)], c=[_candidate(3)],
                               d=[_candidate(4)], e=[_candidate(5)])
    monkeypatch.setattr(shots, "judge", lambda found, spoken: shots.Verdict(list(found)))
    _failing_downloads(monkeypatch, {1, 2, 3, 4, 5})
    session = broll.StockSession()
    plan = _plan((11.0, "a"), (13.0, "b"), (15.0, "c"), (17.0, "d"), (19.0, "e"))
    attach_stock(plan, session)
    assert session.stopped and searches == ["a", "b", "c", "d"] and plan.cutaways == []


def test_a_crash_while_judging_costs_the_shot_but_not_the_clip(monkeypatch):
    _search_returns(monkeypatch, rain=[_candidate(1)])

    def boom(found, spoken):
        raise RuntimeError("sheet went wrong")

    monkeypatch.setattr(shots, "judge", boom)
    plan = _plan((15.0, "rain"))
    warnings = attach_stock(plan)
    assert plan.cutaways == [] and "sheet went wrong" in warnings[0]


def test_tests_cannot_reach_real_services():
    from app.config import Settings
    fresh = Settings()
    assert fresh.pixabay_api_key == "" and fresh.pexels_api_key == "" and fresh.deepgram_api_key == ""
    assert fresh.opencode_bin == "definitely-not-installed"
