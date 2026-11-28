"""A speech clip starts and ends on a sentence boundary, on every path that sets its edges.

Regression for NASA's "Shedding Light on Black Holes" cut through the SaaS seam (Whisper on Groq): the clip ended
"...and as scientists learn even more about the properties". Whisper wrote "circle. and as scientists", the lowercase
"and" vetoed the period, the 58-word run hit the 40-word cap at "properties", and ``_fit_range`` grew a 12s pick to
the 15s minimum with that cap-made "sentence". ``_NASA`` is that run's real word timings (working-copy seconds).
"""
import numpy as np
import pytest

from app.clipper import ai, edit, moments
from app.clipper.moments import Moment, build_sentences
from app.clipper.signals import Signals
from app.clipper.timeline import edit_segments
from app.clipper.transcribe import Word

_NASA = [
    ('Myth', 64.897, 65.097), ('3', 65.097, 65.757), ('If', 65.757, 66.557), ('you', 66.557, 66.677),
    ('get', 66.677, 66.877), ('within', 66.877, 67.117), ('a', 67.117, 67.317), ('few', 67.317, 67.437),
    ('thousand', 67.437, 67.877), ('miles', 67.877, 68.237), ('of', 68.237, 68.417), ('a', 68.417, 68.537),
    ('black', 68.537, 68.657), ('hole', 68.657, 69.177), ('its', 68.917, 69.697), ('supergravity', 69.697, 70.677),
    ('will', 70.677, 70.957), ('pull', 70.957, 71.237), ('you', 71.237, 71.457), ('into', 71.457, 71.717),
    ('its', 71.717, 71.917), ('center.', 71.917, 72.637), ('It', 73.37, 73.557), ('turns', 73.557, 73.817),
    ('out,', 73.817, 74.237), ('you', 74.5, 74.717), ('can', 74.717, 74.857), ('get', 74.857, 74.997),
    ('surprisingly', 74.997, 75.557), ('close', 75.557, 76.037), ('to', 76.037, 76.277), ('a', 76.277, 76.357),
    ('black', 76.357, 76.577), ('hole.', 76.577, 77.177), ('If', 77.977, 78.217), ('you', 78.217, 78.337),
    ('approached', 78.337, 78.797), ('a', 78.797, 78.997), ('black', 78.997, 79.157), ('hole', 79.157, 79.537),
    ('with', 79.537, 79.777), ('a', 79.777, 79.897), ('mass', 79.897, 80.217), ('equal', 80.217, 80.597),
    ('to', 80.597, 80.877), ('our', 80.877, 81.077), ('suns,', 81.077, 81.597), ('for', 81.597, 81.717),
    ('example,', 81.717, 82.397), ('you', 81.917, 83.017), ('could', 83.017, 83.157), ('get', 83.157, 83.397),
    ('as', 83.397, 83.637), ('close', 83.637, 84.037), ('as', 84.037, 84.437), ('tens', 84.437, 84.837),
    ('of', 84.837, 85.057), ('miles.', 85.057, 85.617),
    ('So', 86.417, 86.517), ('imagine', 86.517, 87.217), ('if', 87.217, 87.497), ('we', 87.497, 87.617),
    ('replaced', 87.617, 88.077), ('our', 88.077, 88.337), ('sun', 88.337, 88.637), ('with', 88.637, 88.957),
    ('a', 88.957, 89.057), ('black', 89.057, 89.277), ('hole', 89.277, 89.597), ('of', 89.597, 89.817),
    ('the', 89.817, 89.917), ('same', 89.917, 90.197), ('mass.', 90.197, 90.877), ('All', 91.69, 92.037),
    ('of', 92.037, 92.157), ('the', 92.157, 92.257), ('planets', 92.257, 92.677), ('would', 92.677, 92.957),
    ('continue', 92.957, 93.517), ('to', 93.517, 93.737), ('revolve', 93.737, 94.277), ('around', 94.277, 94.617),
    ('it', 94.617, 94.857), ('at', 94.857, 95.317), ('exactly', 95.317, 95.897), ('the', 95.897, 96.137),
    ('same', 96.137, 96.477), ('speed', 95.917, 96.877), ('and', 96.877, 97.357), ('distance', 97.357, 97.837),
    ('as', 97.837, 98.197), ('they', 98.197, 98.337), ('do', 98.337, 98.537), ('now.', 98.537, 99.037),
    ('The', 99.937, 100.197), ('image', 100.197, 100.517), ('from', 100.517, 100.717), ('the', 100.717, 100.837),
    ('Event', 100.837, 101.097), ('Horizon', 101.097, 101.477), ('Telescope', 101.477, 102.177),
    ('confirmed', 102.177, 102.717), ('what', 102.717, 103.177), ('Albert', 103.177, 103.557),
    ("Einstein's", 103.557, 104.477), ('general', 104.377, 105.037), ('theory', 105.037, 105.397),
    ('of', 105.397, 105.637), ('relativity', 105.637, 106.157), ('predicted', 106.157, 106.777),
    ('over', 106.777, 107.137), ('100', 107.137, 107.637), ('years', 107.637, 108.097), ('ago,', 108.097, 108.637),
    ('that', 109.21, 109.477), ('a', 109.477, 109.597), ('black', 109.597, 109.797), ("hole's", 109.797, 110.317),
    ('form', 109.917, 110.757), ('is', 110.757, 111.457), ('that', 111.457, 111.737), ('of', 111.737, 111.977),
    ('a', 111.977, 112.097), ('perfect', 112.097, 112.457), ('circle.', 112.457, 113.237), ('and', 113.897, 114.077),
    ('as', 114.077, 114.277), ('scientists', 114.277, 115.057), ('learn', 115.057, 115.517), ('even', 115.517, 115.797),
    ('more', 115.797, 116.197), ('about', 116.197, 116.477), ('the', 116.477, 116.617),
    ('properties', 116.617, 117.117), ('of', 117.117, 117.317), ('this', 117.317, 117.477),
    ('gigantic', 117.477, 118.237), ('cosmic', 118.237, 118.837), ('mystery', 118.837, 119.377), ('we', 119.377, 119.637),
    ('call', 119.637, 119.937), ('a', 119.937, 120.097), ('black', 120.097, 120.337), ('hole', 120.337, 120.577),
    ("they'll", 120.577, 121.577), ('be', 121.577, 121.757), ('able', 121.757, 122.077), ('to', 122.077, 122.257),
    ('puncture', 122.257, 122.757), ('even', 122.757, 123.237), ('more', 123.237, 123.597), ('myths', 123.597, 123.977),
]
_CIRCLE = next(i for i, (text, _, _) in enumerate(_NASA) if text == "circle.")
_PROPERTIES = next(i for i, (text, _, _) in enumerate(_NASA) if text == "properties")
# What clip_to_directory asked for with max_clip_seconds=45, and the owner's queue (min(clip_max_sec, 32)).
_LIMITS = [(15, 45), (15, 30)]


def _sentence_edges(words) -> tuple[set[int], set[int]]:
    """Where sentences start and end by the punctuation alone. "myths" ends the source's speech (125.05s long)."""
    ends = {i for i, w in enumerate(words) if w.text.endswith((".", "?", "!"))} | {len(words) - 1}
    return {0} | {i + 1 for i in ends if i + 1 < len(words)}, ends


def _nasa(*, period_after_circle: bool = True) -> list[Word]:
    words = [Word(text, start, end, 1.0) for text, start, end in _NASA]
    if not period_after_circle:  # one real 58-word sentence: the word cap has to split it
        words[_CIRCLE] = Word("circle", words[_CIRCLE].start, words[_CIRCLE].end, 1.0)
    return words


def _signals(duration: float = 125.05) -> Signals:
    n = int(duration) + 1
    return Signals(duration=duration, motion=np.full(n, 0.01), loudness=np.full(n, -30.0),
                   brightness=np.full(n, 0.5), centroid=np.full(n, 0.5), centroid_weight=np.ones(n), cuts=[])


def _stub_ai(monkeypatch, first, last, outcome=None):
    def ask_json(prompt, **kwargs):
        if prompt.startswith("You choose the moments"):
            return {"clips": [{"first": first, "last": last, "title": "Swap the Sun for a Black Hole",
                               "hook": "Swap the Sun for a black hole", "score": 85}]}
        if prompt.startswith("Each clip below"):
            return {"clips": [{"clip": 0, "last": last if outcome is None else outcome}]}
        return None

    monkeypatch.setattr(ai, "ask_json", ask_json)


def _edges(moment: Moment, words) -> tuple[int, int]:
    inside = [i for i, w in enumerate(words) if w.end > moment.start and w.start < moment.end]
    return inside[0], inside[-1]


def test_the_nasa_clip_ends_on_the_sentence_not_where_the_word_cap_split_it(monkeypatch):
    words = _nasa()
    # The log: the model's outcome for this clip was the next sentence, refused as longer than the clip itself.
    _stub_ai(monkeypatch, 3, 4, outcome=5)  # "So imagine ..." to "... as they do now."
    picked = moments.pick_speech(words, _signals(), count=1, min_score=75, language="en",
                                 min_sec=15, max_sec=45, target_sec=26)
    assert len(picked) == 1
    first, last = _edges(picked[0], words)
    assert (words[first].text, words[last].text) == ("So", "circle.")
    assert picked[0].end == pytest.approx(113.337, abs=0.001)  # was 117.117, the end of "properties"


def test_a_period_before_a_lowercase_word_ends_a_sentence_only_after_a_pause():
    texts = [s.text for s in build_sentences(_nasa())]
    assert texts[5].endswith("is that of a perfect circle.")
    assert texts[6].startswith("and as scientists")
    # An unlisted abbreviation read straight on still does not split.
    words = [Word("about", 0.0, 0.3), Word("approx.", 0.3, 0.8), Word("ten", 0.82, 1.1), Word("people.", 1.1, 1.6)]
    assert [s.text for s in build_sentences(words)] == ["about approx. ten people."]


def test_a_sentence_the_word_cap_splits_is_marked_and_kept_whole():
    sentences = build_sentences(_nasa(period_after_circle=False))
    assert [s.forced for s in sentences] == [False] * 5 + [True, False]
    assert sentences[5].last_word == _PROPERTIES
    assert moments.whole_range(5, 5, sentences) == (5, 6)
    assert moments.whole_range(6, 6, sentences) == (5, 6)
    assert moments.whole_range(4, 5, sentences) == (4, 6)


@pytest.mark.parametrize("period_after_circle", [True, False])
@pytest.mark.parametrize("min_sec,max_sec", _LIMITS)
def test_no_ai_pick_starts_or_ends_inside_a_sentence(monkeypatch, period_after_circle, min_sec, max_sec):
    words = _nasa(period_after_circle=period_after_circle)
    starts, ends = _sentence_edges(words)
    count = len(build_sentences(words))
    seen = 0
    for first in range(count):
        for last in range(first, count):
            for outcome in [None, *range(last + 1, count)]:
                _stub_ai(monkeypatch, first, last, outcome)
                for moment in moments.pick_speech(words, _signals(), count=1, language="en",
                                                  min_sec=min_sec, max_sec=max_sec, target_sec=26):
                    seen += 1
                    a, b = _edges(moment, words)
                    assert a in starts and b in ends, (first, last, outcome, words[a].text, words[b].text)
    assert seen


@pytest.mark.parametrize("min_sec,max_sec", _LIMITS)
def test_rule_based_picks_never_end_where_the_word_cap_split_a_sentence(monkeypatch, min_sec, max_sec):
    monkeypatch.setattr(ai, "ask_json", lambda *a, **k: None)
    words = _nasa(period_after_circle=False)
    picked = moments.pick_speech(words, _signals(), count=3, min_sec=min_sec, max_sec=max_sec)
    starts, ends = _sentence_edges(words)
    assert picked
    for moment in picked:
        a, b = _edges(moment, words)
        assert a in starts and b in ends, (words[a].text, words[b].text)


def _spoken(text: str, start: float = 0.0) -> list[Word]:
    out, now = [], start
    for token in text.split():
        out.append(Word(token, now, now + 0.3, 0.99))
        now += 0.35
    return out


def test_the_edit_pass_cannot_drop_half_of_a_sentence_the_cap_split(monkeypatch):
    long_sentence = "Then " + " ".join(f"w{i}" for i in range(1, 49)) + " done."
    words = _spoken(f"Short one. {long_sentence} Last one here.")
    sentences = build_sentences(words)
    assert [s.forced for s in sentences] == [False, True, False, False]
    # The model keeps the first 40 words of the long sentence and skips its last ten.
    monkeypatch.setattr(ai, "ask_json", lambda *a, **k: {"keep": [0, 1, 3]})
    plan = edit.build_edit(Moment(words[0].start, words[-1].end + 0.1), words)
    kept = [w.text for w in plan.words]
    assert kept[kept.index("w39"):kept.index("Last")] == ["w39", *(f"w{i}" for i in range(40, 49)), "done."]


def test_trimming_to_the_limit_ends_after_the_last_whole_sentence():
    words = _spoken("One two three four five. Six seven eight nine ten. Eleven twelve thirteen fourteen fifteen.")
    plan = edit.EditPlan(words=list(words), segments=edit_segments(words, range(len(words)), 0.12), speed=1.0)
    edit.trim_to(plan, 4.5)  # the limit falls inside "thirteen"
    assert plan.segments[-1][1] == pytest.approx(words[10].start, abs=1 / 30)  # right after "ten."
    assert plan.duration <= 4.5


def test_trimming_never_keeps_a_setup_without_most_of_the_clip():
    words = _spoken("Short setup. Then " + " ".join(f"w{i}" for i in range(1, 30)) + " payoff.")
    plan = edit.EditPlan(words=list(words), segments=edit_segments(words, range(len(words)), 0.12), speed=1.0)
    before = plan.duration
    edit.trim_to(plan, before - 1.0)  # cutting after "setup." would keep under a tenth of the clip
    assert plan.duration == pytest.approx(before - 1.0, abs=1 / 30)


def test_trimming_with_no_whole_sentence_that_fits_still_meets_the_limit():
    words = _spoken(" ".join(f"w{i}" for i in range(30)) + " end.")
    plan = edit.EditPlan(words=list(words), segments=edit_segments(words, range(len(words)), 0.12), speed=1.0)
    edit.trim_to(plan, 5.0)
    assert plan.duration <= 5.0 + 1 / 30


def test_the_clean_vlog_cap_stops_at_the_last_pause_that_fits(monkeypatch):
    monkeypatch.setattr(edit.timeline, "detect_silences", lambda *a, **k: [(18.0, 19.0), (27.0, 28.0)])
    plan = edit.build_clean_vlog_edit(Moment(10.0, 40.0, kind="speech"), "v.mp4", speed=1.0, max_sec=22.0)
    assert plan.segments[-1][1] <= 27.2  # the pause at 27s, not inside the speech that runs on from 28s
    assert edit.timeline.total_duration(plan.segments) <= 22.0


def test_sparse_or_missing_punctuation_keeps_the_word_cap_as_the_only_boundary():
    bare = _spoken(" ".join(f"w{i}" for i in range(120)))
    assert not any(s.forced for s in build_sentences(bare))
    sparse = list(bare)
    sparse[89] = Word("khatam.", sparse[89].start, sparse[89].end, 0.99)
    assert not moments.is_punctuated(sparse)
    assert not any(s.forced for s in build_sentences(sparse))


def test_a_hindi_transcript_ending_its_sentences_with_danda_keeps_the_cap_as_before():
    # Sarvam's shape: a danda about every nine words, a few "?", no long pauses.
    tokens = [("क्या?" if i < 10 and i % 2 else "कहा।" if i % 9 == 8
               else "भाई") for i in range(200)]
    words = _spoken(" ".join(tokens))
    assert sum(t.endswith("?") for t in tokens) * 40 >= len(tokens)  # dense enough to count as punctuated alone
    assert not moments.is_punctuated(words)
    assert not any(s.forced for s in build_sentences(words))
