"""Fast tests for the clipper: everything except real rendering."""
import json
import os
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from app import scheduler
from app.clipper import ai, captions, ingest, job, media, moments, render, timeline, transcribe
from app.clipper.media import Interrupted, MediaError, MediaInfo
from app.clipper.moments import Moment, build_sentences
from app.clipper.signals import Signals, _find_cuts
from app.clipper.transcribe import Transcript, Word
from app.config import settings
from app.db import init_db, session_scope
from app.models import ClipJob, Video, VideoStatus


def setup_module(module):
    init_db()


@pytest.fixture(autouse=True)
def isolated_content(tmp_path, monkeypatch):
    """Keep test clips and transcripts out of the real content directory."""
    monkeypatch.setattr(settings, "content_dir", str(tmp_path / "content"))


def _words(text, start=0.0, gap=0.05, length=0.3):
    out, now = [], start
    for token in text.split():
        out.append(Word(token, now, now + length, 0.99))
        now += length + gap
    return out


def _signals(seconds=120, loud_at=None, cuts=()):
    loudness = np.full(seconds, -40.0)
    motion = np.full(seconds, 0.01)
    if loud_at:
        loudness[loud_at[0]:loud_at[1]] = -10.0
        motion[loud_at[0]:loud_at[1]] = 0.2
    return Signals(
        duration=float(seconds), motion=motion, loudness=loudness,
        brightness=np.full(seconds, 0.5), centroid=np.full(seconds, 0.5),
        centroid_weight=np.ones(seconds), cuts=list(cuts),
    )


INFO = MediaInfo(duration=120.0, width=1280, height=720, fps=30.0, has_audio=True)


# ---------- Timeline ----------
def test_pauses_are_removed_and_short_gaps_kept():
    words = [Word("a", 10.0, 10.5), Word("b", 10.6, 11.0), Word("c", 14.0, 14.5)]
    segments = timeline.keep_segments(10.0, 14.5, words, max_gap=0.35)
    assert len(segments) == 2
    assert segments[0][1] < 12 and segments[1][0] > 13


def test_no_words_keeps_the_whole_span():
    assert timeline.keep_segments(5.0, 10.0, [], 0.35) == [(5.0, 10.0)]


def test_segments_sit_on_the_frame_grid():
    for a, b in timeline.keep_segments(1.013, 4.777, _words("one two three", start=1.1), 0.35):
        assert abs(a * timeline.FPS - round(a * timeline.FPS)) < 1e-6
        assert abs(b * timeline.FPS - round(b * timeline.FPS)) < 1e-6


def test_output_time_maps_across_a_removed_gap():
    segments = [(10.0, 12.0), (20.0, 23.0)]
    assert timeline.output_time(segments, 11.0) == 1.0
    assert timeline.output_time(segments, 15.0) == 2.0  # inside the gap: snaps forward
    assert timeline.output_time(segments, 21.0) == 3.0
    assert timeline.output_time(segments, 99.0) is None


# ---------- Picking moments ----------
def test_sentences_split_on_punctuation_and_long_gaps():
    words = _words("One two. Three four?") + [Word("Later", 50.0, 50.3)]
    assert [s.text for s in build_sentences(words)] == ["One two.", "Three four?", "Later"]


def _long_transcript(sentence_count=30):
    words = []
    now = 10.0
    for index in range(sentence_count):
        batch = _words(f"Sentence number {index} says something rather useful right here.", start=now)
        words += batch
        now = batch[-1].end + 0.2
    return words


def test_ai_sentence_numbers_become_clean_boundaries(monkeypatch):
    words = _long_transcript()
    reply = {"clips": [{"first": 2, "last": 8, "title": "Title #shorts", "hook": "A hook", "score": 90, "why": "good"}]}
    monkeypatch.setattr(ai, "ask_json", lambda *a, **k: reply)
    picked = moments.pick_speech(words, _signals(300), count=1)
    sentences = build_sentences(words)
    assert sentences[2].start - 0.1 <= picked[0].start <= sentences[2].start
    assert picked[0].title == "Title"
    assert any(s.end <= picked[0].end <= s.end + 0.1 for s in sentences)


@pytest.mark.parametrize("bad", [
    {"clips": [{"first": 999, "last": 1000}]},
    {"clips": [{"first": "x", "last": 2}]},
    {"clips": "not a list"},
    {"clips": 5},
    {"clips": [{"first": 1e999, "last": 2}]},
    {},
    None,
])
def test_bad_ai_replies_fall_back_to_rules(monkeypatch, bad):
    monkeypatch.setattr(ai, "ask_json", lambda *a, **k: bad)
    picked = moments.pick_speech(_long_transcript(), _signals(300), count=2)
    assert picked and all(m.kind == "speech" for m in picked)
    assert all(settings.clip_min_sec <= m.duration for m in picked)


def test_ai_description_and_tags_reach_the_moment(monkeypatch):
    reply = {"clips": [{"first": 2, "last": 8, "title": "T", "hook": "H", "score": 90,
                        "description": "What this is about. #viral", "tags": ["#heat", "climate", ""]}]}
    monkeypatch.setattr(ai, "ask_json", lambda *a, **k: reply)
    moment = moments.pick_speech(_long_transcript(), _signals(300), count=1)[0]
    assert moment.description == "What this is about."
    assert moment.tags == ("heat", "climate")


@pytest.mark.parametrize("tags", ["heat", 5, None, {"a": 1}])
def test_bad_tags_are_ignored(tags):
    assert moments.clean_tags(tags) == ()


def test_overlong_ai_range_shrinks_to_the_limit(monkeypatch):
    words = _long_transcript(60)
    monkeypatch.setattr(ai, "ask_json", lambda *a, **k: {"clips": [{"first": 0, "last": 59, "score": 50}]})
    picked = moments.pick_speech(words, _signals(600), count=1)
    segments = timeline.keep_segments(picked[0].start, picked[0].end, words, settings.clip_silence_gap_sec)
    assert timeline.total_duration(segments) <= settings.clip_max_sec


def test_overlapping_moments_are_deduped():
    kept = moments._dedupe([Moment(0, 30, score=9), Moment(5, 35, score=8), Moment(40, 70, score=7)], 5)
    assert [m.start for m in kept] == [0, 40]


def test_clips_may_touch_but_not_share_footage():
    assert len(moments._dedupe([Moment(0, 30, score=2), Moment(30, 60, score=1)], 5)) == 2
    assert len(moments._dedupe([Moment(0, 30, score=2), Moment(29, 60, score=1)], 5)) == 1


def test_rule_based_clips_never_repeat_footage(monkeypatch):
    monkeypatch.setattr(ai, "ask_json", lambda *a, **k: None)
    picked = moments.pick_speech(_long_transcript(60), _signals(600), count=8)
    assert len(picked) >= 4
    for index, a in enumerate(picked):
        assert not any(moments.clashes(a, b) for b in picked[index + 1:])


def test_abbreviations_do_not_split_sentences():
    words = _words('We met Dr. Smith at the U.S. embassy on Tuesday and it went well. '
                   'So did I. Did he? She said "stop." J. Smith agreed.')
    assert [s.text for s in build_sentences(words)] == [
        "We met Dr. Smith at the U.S. embassy on Tuesday and it went well.",
        "So did I.", "Did he?", 'She said "stop."', "J. Smith agreed.",
    ]


def _gapped_transcript(gap=40.0):
    """Two passages separated by a long wordless stretch."""
    first = _long_transcript(12)
    second = _long_transcript(12)
    shift = first[-1].end + gap - second[0].start
    return first + [Word(w.text, w.start + shift, w.end + shift, w.confidence) for w in second]


def test_ai_range_cannot_cross_a_long_silence(monkeypatch):
    words = _gapped_transcript()
    sentences = build_sentences(words)
    break_at = next(i for i in range(len(sentences) - 1) if sentences[i + 1].start - sentences[i].end > 30)
    reply = {"clips": [{"first": break_at - 6, "last": break_at + 3, "score": 90}]}
    monkeypatch.setattr(ai, "ask_json", lambda *a, **k: reply)
    picked = moments.pick_speech(words, _signals(300), count=1)
    assert picked[0].end <= sentences[break_at].end + 0.2


def test_growing_a_short_range_stops_at_a_long_silence():
    words = _gapped_transcript()
    sentences = build_sentences(words)
    break_at = next(i for i in range(len(sentences) - 1) if sentences[i + 1].start - sentences[i].end > 30)
    assert moments._fit_range(break_at, break_at, sentences, words, 300.0) is None


def test_prompt_marks_long_silences():
    sentences = build_sentences(_gapped_transcript())
    assert "seconds without speech" in moments._speech_prompt(sentences, 2, "x")


def test_non_finite_ai_scores_do_not_corrupt_ranking(monkeypatch):
    words = _long_transcript(40)
    monkeypatch.setattr(ai, "ask_json", lambda *a, **k: {"clips": [
        {"first": 0, "last": 5, "score": 10}, {"first": 10, "last": 15, "score": "NaN"},
        {"first": 20, "last": 25, "score": 95},
    ]})
    picked = moments.pick_speech(words, _signals(300), count=1)
    assert picked[0].score == 95


def test_tiny_island_does_not_keep_a_long_pause():
    words = _words("word " * 40, start=0.0) + [Word("Yes.", 44.4, 44.5)]
    segments = timeline.keep_segments(0.0, 44.6, words, 0.35)
    assert timeline.total_duration(segments) < 16
    first_word_alone = [Word("So", 0.0, 0.05)] + _words("word " * 30, start=40.0)
    assert timeline.total_duration(timeline.keep_segments(0.0, 51.0, first_word_alone, 0.35)) < 13


def test_no_speech_ranking_follows_activity_not_position():
    picked = moments.rank_windows(_signals(300, loud_at=(150, 180)), 1)
    assert 140 <= picked[0].start <= 160


def test_no_speech_windows_do_not_overlap():
    picked = moments.rank_windows(_signals(300, loud_at=(150, 180)), 5)
    assert len(picked) == 5
    assert len(moments._dedupe(picked, 5)) == 5


def test_short_video_becomes_one_clip():
    picked = moments.rank_windows(_signals(10), 3)
    assert len(picked) == 1 and picked[0].end == 10.0


@pytest.mark.parametrize("duration,cuts", [(10.5, [2.25]), (2.5, [2.25]), (16.5, [2.5]), (15.7, [])])
def test_short_fractional_sources_stay_whole(duration, cuts):
    signals = _signals(int(np.ceil(duration)), cuts=cuts)
    signals.duration = duration
    picked = moments.rank_windows(signals, 3)
    assert [(m.start, m.end) for m in picked] == [(0.0, duration)]


@pytest.mark.parametrize("visual_sec,expected", [(58, 58), (90, 59)])
def test_visual_clips_stay_inside_the_length_limits(monkeypatch, visual_sec, expected):
    monkeypatch.setattr(settings, "clip_visual_sec", visual_sec)
    picked = moments.rank_windows(_signals(300, cuts=[100.0, 150.0]), 3)
    assert picked
    assert all(settings.clip_min_sec <= m.duration <= settings.clip_max_sec for m in picked)


def test_vision_reply_with_bad_candidate_number_is_ignored(monkeypatch):
    monkeypatch.setattr(ai, "available", lambda: True)
    monkeypatch.setattr(moments, "_ai_rank_visual", lambda *a, **k: {"clips": [
        {"candidate": 99, "title": "x"}, {"candidate": -1, "title": "y"}, {"candidate": 1, "title": "Good", "hook": "Look", "score": 70},
    ]})
    picked = moments.pick_visual("x.mp4", INFO, _signals(120, loud_at=(50, 80)), count=2, title="Film")
    assert picked[0].title == "Good"
    assert all(m.title != "x" and m.title != "y" for m in picked)


def test_visual_path_titles_itself_without_ai(monkeypatch):
    monkeypatch.setattr(ai, "available", lambda: False)
    picked = moments.pick_visual("x.mp4", INFO, _signals(120), count=2, title="Film")
    assert picked[0].title == "Film - part 1"


# ---------- Signals ----------
def test_hard_cut_is_detected_but_steady_motion_is_not():
    diffs = np.full(100, 0.02)
    diffs[50] = 0.5
    assert _find_cuts(diffs) == [12.5]
    assert _find_cuts(np.full(100, 0.2)) == []


def test_dark_seconds_lose_interest():
    signals = _signals(60, loud_at=(10, 20))
    signals.brightness[10:20] = 0.0
    assert signals.interest()[15] < signals.interest()[30]


def test_centre_falls_back_to_middle_without_motion():
    signals = _signals(30)
    signals.centroid_weight[:] = 0
    assert signals.centre_between(0, 10) == 0.5


# ---------- Captions ----------
def test_scraped_text_cannot_inject_ass_tags_or_break_lines():
    ass = captions.build_ass([Word("{\\pos(0,0)}it's", 0.0, 0.5), Word("line\\Nbreak", 0.5, 1.0)], "Bad {tag}", 5.0, "Font")
    events = [line for line in ass.splitlines() if line.startswith("Dialogue")]
    assert not any("pos(0,0)" in event for event in events)
    assert not any("LINE\\NBREAK" in event for event in events)
    assert "IT'S" in ass


def test_every_word_gets_highlighted_once():
    words = _words("one two three four five six")
    ass = captions.build_ass(words, "", 5.0, "Font")
    assert sum(line.startswith("Dialogue: 1") for line in ass.splitlines()) == 6


def test_caption_events_never_overlap():
    words = [Word("End.", 0.0, 0.3), Word("Next", 0.4, 0.8), Word("one.", 0.9, 1.2), Word("Go", 1.3, 1.5)]
    rows = [line.split(",") for line in captions.build_ass(words, "", 5.0, "Font").splitlines() if line.startswith("Dialogue: 1")]
    for current, following in zip(rows, rows[1:]):
        assert captions._seconds(current[2]) <= captions._seconds(following[1])


def test_hook_skips_text_the_font_cannot_draw():
    assert "Dialogue: 2" not in captions.build_ass([], "हिंदी हुक", 5.0, "Font")
    assert "Dialogue: 2" in captions.build_ass([], "English hook", 5.0, "Font")


def test_font_family_is_read_from_the_file():
    path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    if not os.path.exists(path):
        pytest.skip("DejaVu not installed")
    assert captions.font_family(path) == "DejaVu Sans"


def test_unreadable_font_falls_back_to_its_file_name(tmp_path):
    bad = tmp_path / "MyFont.ttf"
    bad.write_bytes(b"nope")
    assert captions.font_family(str(bad)) == "MyFont"


def test_long_text_shrinks_to_fit_and_stays_shrunk_after_the_highlight():
    path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    if not os.path.exists(path):
        pytest.skip("DejaVu not installed")
    hook = "Most people completely misunderstand how compound interest works"
    ass = captions.build_ass([Word("A", 0.0, 0.3), Word("Internationalization", 0.3, 1.0)], hook, 5.0, "Font", path)
    hook_line = next(line for line in ass.splitlines() if line.startswith("Dialogue: 2"))
    assert int(hook_line.split("\\fs")[1].split("\\")[0]) < 72
    first_caption = next(line for line in ass.splitlines() if line.startswith("Dialogue: 1"))
    size = first_caption.split("\\fs")[1].split("}")[0]
    assert int(size) < 92
    assert f"{{\\r\\fs{size}}}" in first_caption


def test_missing_font_file_keeps_the_default_size():
    assert captions._fitted_size(["ANY TEXT"], 92, 24, 1.12, "/nope/font.ttf") == 92


# ---------- Render planning ----------
def test_landscape_speech_crops_to_vertical():
    plan = render.plan_clip(Moment(10, 40), _words("a b c", start=11), _signals(), INFO, has_graphics=True)
    assert plan.crop_width == 404 and not plan.use_blur_fill


def test_wide_action_keeps_the_whole_picture():
    signals = _signals()
    signals.centroid[10:40] = np.linspace(0.05, 0.95, 30)
    plan = render.plan_clip(Moment(10, 40, kind="visual"), [], signals, INFO, has_graphics=False)
    assert plan.use_blur_fill


def test_crop_follows_the_subject_per_shot():
    signals = _signals(cuts=[25.0])
    signals.centroid[10:25] = 0.1
    signals.centroid[25:40] = 0.9
    pieces = render._crop_pieces(Moment(10, 40), [(10.0, 40.0)], signals, INFO, 404)
    assert len(pieces) == 2
    assert pieces[0][1] < pieces[1][1]
    assert all(0 <= x <= 1280 - 404 for _, x in pieces)


def test_vertical_source_is_not_cropped():
    vertical = MediaInfo(120.0, 720, 1280, 30.0, True)
    plan = render.plan_clip(Moment(10, 40), [], _signals(), vertical, has_graphics=False)
    assert plan.crop_width == 720
    assert "crop=720" not in render._framing(plan, vertical)


def test_audio_chain_is_absent_when_the_source_is_silent():
    silent = MediaInfo(120.0, 1280, 720, 30.0, False)
    plan = render.plan_clip(Moment(10, 40), [], _signals(), silent, has_graphics=False)
    graph = render.build_filtergraph(plan, silent, 9.0)
    assert "[0:a]" not in graph and "aout" not in graph


def test_graph_has_no_paths_in_it():
    plan = render.plan_clip(Moment(10, 40), [], _signals(), INFO, has_graphics=True)
    assert "ass=clip.ass:fontsdir=fonts" in render.build_filtergraph(plan, INFO, 9.0)


def test_words_at_the_edges_stay_out_of_the_clip():
    words = [Word("tango", 14.10, 14.41), Word("why", 14.41, 14.80), Word("end", 20.0, 20.42), Word("next", 20.42, 20.9)]
    segments = timeline.keep_segments(14.41, 20.42, words, 0.35)
    shifted = render._shift_words(words, segments, 14.41, 20.42)
    assert [w.text for w in shifted] == ["why", "end"]


def test_zoom_and_crop_boundaries_sit_between_frames():
    assert "between(t,0.983,2.983)" in render._zoom_expression([(1.0, 3.0)])
    assert "gte(t,0.983)" in render._piecewise([(0.0, 10.0), (1.0, 20.0)])
    assert "lt(t,0.983)" in render._piecewise([(0.0, 10.0), (1.0, 20.0)])


def test_crop_uses_the_refined_cut_time():
    signals = _signals(cuts=[25.0])
    pieces = render._crop_pieces(Moment(10, 40), [(10.0, 40.0)], signals, INFO, 404, {25.0: 25.133})
    signals.centroid[25:40] = 0.9
    pieces = render._crop_pieces(Moment(10, 40), [(10.0, 40.0)], signals, INFO, 404, {25.0: 25.133})
    assert abs(pieces[1][0] - 15.133) < 1e-6


def test_crop_prefers_the_face_over_the_motion():
    signals = _signals(cuts=[25.0])
    signals.centroid[:] = 0.5
    pieces = render._crop_pieces(Moment(10, 40), [(10.0, 40.0)], signals, INFO, 404, None, {25.0: 0.9})
    assert pieces[0][1] != pieces[1][1]
    assert pieces[1][1] == 876.0  # right edge limit: 1280 - 404
    assert pieces[0][1] == 438.0  # no face in the first shot, so the motion decides


def test_a_found_face_keeps_a_wide_shot_cropped_rather_than_blurred():
    signals = _signals()
    signals.centroid[10:40] = np.linspace(0.05, 0.95, 30)
    assert render.plan_clip(Moment(10, 40, kind="visual"), [], signals, INFO, has_graphics=False).use_blur_fill
    plan = render.plan_clip(Moment(10, 40, kind="visual"), [], signals, INFO, has_graphics=False, faces={10: 0.3})
    assert not plan.use_blur_fill


def test_no_face_in_a_blank_frame():
    from app.clipper import faces
    blank = np.zeros((360, 640), dtype=np.uint8)
    assert faces.speaker_in_pair(blank, blank) is None


def test_unreadable_frames_mean_no_face(monkeypatch):
    from app.clipper import faces

    def broken(*a, **k):
        raise MediaError("no frame")

    monkeypatch.setattr(faces, "read_ffmpeg", broken)
    assert faces.face_centre("x.mp4", 0.0, 5.0, INFO) is None


def test_two_people_are_never_averaged_into_the_gap_between_them(monkeypatch):
    from app.clipper import faces
    picks = iter([(0.2, 5.0), (0.8, 9.0), (0.2, 4.0)])
    monkeypatch.setattr(faces, "_frames_at", lambda *a: (None, None))
    monkeypatch.setattr(faces, "speaker_in_pair", lambda a, b: next(picks))
    assert faces.face_centre("x.mp4", 0.0, 5.0, INFO) == 0.8


def test_the_face_that_moves_is_the_speaker(monkeypatch):
    from app.clipper import faces

    class Boxes:
        def detectMultiScale(self, *a, **k):
            return [(100, 100, 100, 100), (900, 100, 100, 100)]

    monkeypatch.setattr(faces, "_detector", lambda: Boxes())
    first = np.zeros((720, 1280), dtype=np.uint8)
    second = first.copy()
    second[100:230, 900:1000] = 200  # the right-hand person moved
    assert faces.speaker_in_pair(first, second)[0] == pytest.approx(950 / 1280)


def test_a_moving_blur_cannot_beat_a_real_size_difference_at_rest(monkeypatch):
    from app.clipper import faces

    class Boxes:
        def detectMultiScale(self, *a, **k):
            return [(100, 100, 36, 36), (900, 100, 256, 256)]

    monkeypatch.setattr(faces, "_detector", lambda: Boxes())
    still = np.zeros((720, 1280), dtype=np.uint8)
    assert faces.speaker_in_pair(still, still)[0] == pytest.approx(1028 / 1280)


def test_stutters_and_filler_are_marked_for_removal():
    from app.clipper.edit import droppable_indices
    words = _words("so we we don't um know, you know, what it means. You know that it matters.")
    drop = {words[i].text for i in droppable_indices(words)}
    assert "um" in drop and "we" in drop and "know," in drop
    kept = [w.text for i, w in enumerate(words) if i not in droppable_indices(words)]
    assert kept.count("we") == 1
    assert "that" in kept and kept[kept.index("that") - 1] == "know", "'you know that' carries meaning"


def _after_tidying(text):
    from app.clipper.edit import droppable_indices
    words = _words(text)
    drop = droppable_indices(words)
    return " ".join(w.text for i, w in enumerate(words) if i not in drop)


@pytest.mark.parametrize("text", [
    "We need to talk about money. Money is the whole problem.",
    "Fear leads to anger. Anger leads to hate.",
    "It's its own problem.",
    "It was a 50 50 chance.",
    "We flew to Bora Bora today.",
    "Never, never, never give up.",
    "She had had enough of it.",
    "I knew that that was it.",
    "Go! Go! Go!",
    "As you know, the sun is hot.",
    "If you know, you know.",
    "Do you know, John?",
    "Look, I mean it.",
    "Honestly, you know the answer.",
    "That is what I mean, right?",
    "First, kind of blue is my favourite album.",
    "Kind of, yes.",
    "He rushed to the ER. Nobody came.",
    "The bolt is 5 mm wide.",
    "Did you do it? Mhm.",
])
def test_real_words_survive_the_tidy(text):
    assert _after_tidying(text) == text


@pytest.mark.parametrize("text,expected", [
    ("So we we left today.", "So we left today."),
    ("I I think so.", "I think so."),
    ("we were, you know, tired.", "we were, tired."),
    ("it was, I mean, huge.", "it was, huge."),
    ("You know, it matters.", "it matters."),
    ("Um, so we left.", "so we left."),
    ("So um we left.", "So we left."),
    ("Well, mm, maybe not.", "Well, maybe not."),
])
def test_stutters_and_filler_do_go(text, expected):
    assert _after_tidying(text) == expected


def test_cutting_the_next_sentence_does_not_lose_a_word_from_this_one(monkeypatch):
    from app.clipper.edit import build_edit
    text = "We need to talk about money. Money is the whole problem here today. " + " ".join(
        f"Sentence {i} has plenty of useful words about the thing we care about." for i in range(8))
    words = _words(text, start=10.0)
    _edit_reply(monkeypatch, {"keep": [0, 2, 3, 4, 5, 6, 7, 8, 9], "beats": []})
    kept = [w.text for w in build_edit(Moment(10.0, words[-1].end), words).words]
    assert "money." in kept


def test_a_chant_is_not_edited_down_to_nothing(monkeypatch):
    from app.clipper.edit import build_edit
    words = _words("go " * 60, start=10.0)
    _edit_reply(monkeypatch, None)
    edit = build_edit(Moment(10.0, words[-1].end), words)
    assert timeline.total_duration(edit.segments) >= 12


def test_short_tightened_clip_keeps_the_ais_graphics_and_broll(monkeypatch):
    from app.clipper.edit import build_edit
    _edit_reply(monkeypatch, {"keep": [0], "beats": [
        {"sentence": 5, "phrase": "useful words", "text": "USEFUL", "style": "card"}],
        "broll": [{"sentence": 8, "phrase": "useful words", "query": "rain", "seconds": 2.5}]})
    words = _talk()
    edit = build_edit(Moment(10.0, words[-1].end), words)
    assert len(edit.words) == len(words)
    assert [b.text for b in edit.beats] == ["USEFUL"] and [c.query for c in edit.cutaways] == ["rain"]


def test_padding_stays_inside_the_moment():
    words = [Word("why", 14.41, 14.80), Word("end", 20.0, 20.42)]
    segments = timeline.edit_segments(words, [0, 1], 0.12, start=14.41, end=20.42)
    assert segments[0][0] >= 14.40 and segments[-1][1] <= 20.44


def test_removed_word_always_cuts_even_with_no_pause_around_it():
    words = [Word("one", 1.0, 1.3), Word("um", 1.3, 1.5), Word("two", 1.5, 1.8)]
    segments = timeline.edit_segments(words, [0, 2], 0.12)
    assert len(segments) == 2
    assert segments[0][1] <= 1.3 + 1 / 30 and segments[1][0] >= 1.5 - 1 / 30


def test_nearby_kept_words_stay_joined():
    words = [Word("one", 1.0, 1.3), Word("two", 1.35, 1.7), Word("three", 3.0, 3.3)]
    assert len(timeline.edit_segments(words, [0, 1, 2], 0.12)) == 2


def _edit_reply(monkeypatch, reply):
    monkeypatch.setattr(ai, "ask_json", lambda *a, **k: reply)


def _talk():
    text = " ".join(f"Sentence {i} has 40 useful words about the thing we care about today." for i in range(12))
    return _words(text, start=10.0)


def test_edit_keeps_the_chosen_sentences_and_speeds_up(monkeypatch):
    from app.clipper.edit import build_edit
    _edit_reply(monkeypatch, {"keep": [0, 2, 4, 6, 8, 10], "beats": []})
    words = _talk()
    edit = build_edit(Moment(10.0, words[-1].end), words)
    assert len(edit.words) < len(words)
    assert edit.speed == settings.clip_speed
    assert edit.duration < timeline.total_duration(edit.segments)
    assert "Sentence 1 has" not in " ".join(w.text for w in edit.words)


def test_edit_that_guts_the_clip_is_refused(monkeypatch):
    from app.clipper.edit import build_edit
    _edit_reply(monkeypatch, {"keep": [0], "beats": []})
    words = _talk()
    edit = build_edit(Moment(10.0, words[-1].end), words)
    assert len(edit.words) == len(words)


@pytest.mark.parametrize("reply", [None, {}, {"keep": "all"}, {"keep": [999]}, {"keep": [1e999]}, {"beats": 5}])
def test_bad_edit_replies_still_give_a_usable_plan(monkeypatch, reply):
    from app.clipper.edit import build_edit
    _edit_reply(monkeypatch, reply)
    words = _talk()
    edit = build_edit(Moment(10.0, words[-1].end), words)
    assert edit.words and edit.segments and edit.duration > 0


def test_graphics_land_on_exact_phrases_and_bad_ones_are_dropped(monkeypatch):
    from app.clipper.edit import STAT, build_edit
    _edit_reply(monkeypatch, {"keep": list(range(12)), "beats": [
        {"sentence": 3, "phrase": "40 useful words", "text": "40", "label": "words", "style": "stat"},
        {"sentence": 6, "phrase": "not in this sentence", "text": "NOPE", "style": "punch"},
        {"sentence": 7, "phrase": "the thing", "text": "x" * 99, "style": "card"},
        {"sentence": 8, "phrase": "the thing", "text": "THING", "style": "explode"},
        {"sentence": "x", "phrase": "the thing", "text": "THING", "style": "punch"},
        "not a dict",
    ]})
    words = _talk()
    edit = build_edit(Moment(10.0, words[-1].end), words)
    assert [(b.text, b.style, b.label) for b in edit.beats] == [("40", STAT, "WORDS")]
    spoken = next(w for w in edit.words if w.start == edit.beats[0].at)
    assert spoken.text == "40"


def test_a_stat_without_a_number_becomes_a_punch(monkeypatch):
    from app.clipper.edit import PUNCH, build_edit
    _edit_reply(monkeypatch, {"keep": list(range(12)), "beats": [
        {"sentence": 3, "phrase": "useful words", "text": "USEFUL", "style": "stat"}]})
    words = _talk()
    beat = build_edit(Moment(10.0, words[-1].end), words).beats[0]
    assert beat.style == PUNCH and beat.text == ""


@pytest.mark.parametrize("text", ["", "BIG WORD", "x" * 99, "🔥"])
def test_a_punch_has_no_text_whatever_the_ai_wrote(monkeypatch, text):
    from app.clipper.edit import PUNCH, build_edit
    _edit_reply(monkeypatch, {"keep": list(range(12)), "beats": [
        {"sentence": 3, "phrase": "useful words", "text": text, "style": "punch"}]})
    words = _talk()
    edit = build_edit(Moment(10.0, words[-1].end), words)
    assert [(b.text, b.style) for b in edit.beats] == [("", PUNCH)]


def test_big_word_text_is_gone_for_good(monkeypatch):
    from app.clipper.edit import _edit_prompt, build_edit
    words = _talk()
    _edit_reply(monkeypatch, {"keep": list(range(12)), "beats": [
        {"sentence": 3, "phrase": "useful words", "text": "BIG", "style": "slam"}]})
    assert "BIG" not in [b.text for b in build_edit(Moment(10.0, words[-1].end), words).beats]
    assert "slam" not in _edit_prompt([], "x")
    assert "Dialogue" not in captions.build_ass([], "", 20.0, "Font", None, [(4.0, "BIG", "slam", "", 1.2)])


@pytest.mark.parametrize("text", ["", "BIG"])
def test_a_punch_draws_nothing_at_all(text):
    ass = captions.build_ass([], "", 20.0, "Font", None, [(4.0, text, "punch", "", 1.2)])
    assert "Dialogue" not in ass, "no text and no dimming"


def test_a_punch_still_flashes_shakes_thuds_and_changes_the_framing():
    from app.clipper.edit import Beat, EditPlan, PUNCH
    edit = EditPlan(words=_words("a b c d", start=11.0), segments=[(11.0, 19.0)], beats=[Beat(15.0, "", PUNCH)])
    shake, flash = render._edit_effects(edit)
    assert "between(t,4.000" in shake and "between(t,3.983" in flash
    assert 4.0 in render.rhythm_points(edit)
    assert any(c.at == 4.0 and c.sound is render.IMPACT for c in render.sound_cues(edit, [0.0]))


def test_graphics_keep_clear_of_the_hook_and_each_other(monkeypatch):
    from app.clipper.edit import build_edit
    _edit_reply(monkeypatch, {"keep": list(range(12)), "beats": [
        {"sentence": s, "phrase": "useful words", "text": "CARD" if s % 2 else "", "style": "card" if s % 2 else "punch"}
        for s in range(12)
    ]})
    words = _talk()
    edit = build_edit(Moment(10.0, words[-1].end), words)
    times = [edit.to_output(b.at) for b in edit.beats]
    assert times and times[0] >= 3.0
    assert all(b - a >= 2.5 for a, b in zip(times, times[1:]))
    assert sum(1 for b in edit.beats if b.style == "card") <= 1


def test_without_ai_numbers_still_get_graphics(monkeypatch):
    from app.clipper.edit import build_edit
    _edit_reply(monkeypatch, None)
    words = _talk()
    assert build_edit(Moment(10.0, words[-1].end), words).beats


def _video(video_id=1, duration=10, files=None, name="Ana"):
    return {"id": video_id, "duration": duration, "url": f"https://pexels.com/video/{video_id}", "user": {"name": name},
            "video_files": files if files is not None else [
                {"file_type": "video/mp4", "width": 1080, "height": 1920, "link": "https://cdn/1080.mp4"},
                {"file_type": "video/mp4", "width": 360, "height": 640, "link": "https://cdn/360.mp4"},
            ]}


def test_stock_file_choice_wants_portrait_mp4_near_full_hd():
    from app.clipper import broll
    assert broll.best_file(_video())["link"] == "https://cdn/1080.mp4"
    landscape = _video(files=[{"file_type": "video/mp4", "width": 1920, "height": 1080, "link": "x"}])
    assert broll.best_file(landscape) is None
    hls = _video(files=[{"file_type": "application/x-mpegURL", "width": 1080, "height": 1920, "link": "x"}])
    assert broll.best_file(hls) is None
    tiny = _video(files=[{"file_type": "video/mp4", "width": 360, "height": 640, "link": "x"}])
    assert broll.best_file(tiny) is None
    assert broll.best_file({"video_files": ["junk", {"file_type": "video/mp4"}]}) is None


def _stock(monkeypatch, videos):
    from app.clipper import broll
    monkeypatch.setattr(settings, "pexels_api_key", "key")
    monkeypatch.setattr(broll, "search", lambda query: broll._pexels_candidates(videos))
    monkeypatch.setattr(broll, "_playable", lambda path, seconds: True)
    downloads = []

    def fake_download(link, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"v" * 2000)
        downloads.append(link)

    monkeypatch.setattr(broll, "_download", fake_download)
    return downloads


def test_stock_clip_is_long_enough_and_not_a_repeat(monkeypatch):
    from app.clipper import broll
    _stock(monkeypatch, [_video(1, duration=1), _video(2, duration=10), _video(3, duration=10)])
    clip = broll.fetch("rain", 3.0, exclude={"2"})
    assert clip.video_id == "3" and clip.author == "Ana" and os.path.exists(clip.path)


def test_stock_clip_is_only_downloaded_once(monkeypatch):
    from app.clipper import broll
    downloads = _stock(monkeypatch, [_video(7)])
    broll.fetch("rain", 3.0)
    broll.fetch("rain", 3.0)
    assert len(downloads) == 1


def test_bad_cached_clip_is_thrown_away_and_replaced(monkeypatch):
    from app.clipper import broll
    downloads = _stock(monkeypatch, [_video(11)])
    bad = settings.content_path / "broll" / "11.mp4"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"<html>")
    os.utime(bad, (1000, 1000))
    verdicts = iter([False, True])
    monkeypatch.setattr(broll, "_playable", lambda path, seconds: next(verdicts))
    assert broll.fetch("rain", 3.0).video_id == "11"
    assert len(downloads) == 1


def test_bad_cached_file_does_not_renew_its_own_expiry(monkeypatch):
    from app.clipper import broll
    _stock(monkeypatch, [_video(12, duration=10)])
    bad = settings.content_path / "broll" / "12.mp4"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"<html>")
    monkeypatch.setattr(broll, "_playable", lambda path, seconds: False)
    with pytest.raises(broll.DownloadsFailed):
        broll.fetch("rain", 3.0)
    assert not bad.exists()


def test_download_that_will_not_play_moves_on_to_the_next(monkeypatch):
    from app.clipper import broll
    _stock(monkeypatch, [_video(13), _video(14)])
    verdicts = iter([False, True])
    monkeypatch.setattr(broll, "_playable", lambda path, seconds: next(verdicts))
    assert broll.fetch("rain", 3.0).video_id == "14"
    assert not (settings.content_path / "broll" / "13.mp4").exists()


def test_dead_cdn_gives_up_quickly(monkeypatch):
    from app.clipper import broll
    _stock(monkeypatch, [_video(i) for i in range(20, 35)])
    attempts = []

    def dead(link, path):
        attempts.append(link)
        raise broll.StockError("503")

    monkeypatch.setattr(broll, "_download", dead)
    with pytest.raises(broll.DownloadsFailed):
        broll.fetch("rain", 3.0)
    assert len(attempts) == 2


def test_odd_values_in_a_stock_reply_only_cost_that_result(monkeypatch):
    from app.clipper import broll
    assert broll.best_file({"video_files": 5}) is None
    assert broll.best_file({"video_files": [{"file_type": "video/mp4", "width": "1080", "height": "1920", "link": "x"}]}) is None


def test_failed_download_tries_the_next_result(monkeypatch):
    from app.clipper import broll
    downloads = _stock(monkeypatch, [_video(8), _video(9)])
    good = broll._download

    def flaky(link, path):
        if "/8" in str(path):
            raise broll.StockError("dropped")
        good(link, path)

    monkeypatch.setattr(broll, "_download", flaky)
    assert broll.fetch("rain", 3.0).video_id == "9"
    assert downloads


@pytest.mark.parametrize("videos", [[], ["junk"], [{"id": 1, "duration": "x"}], [_video(1, files=[])], [_video(0)]])
def test_unusable_results_mean_no_clip(monkeypatch, videos):
    from app.clipper import broll
    _stock(monkeypatch, videos)
    assert broll.fetch("rain", 3.0) is None


class _Reply:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


@pytest.mark.parametrize("reply,error", [
    (_Reply(429), "RateLimited"), (_Reply(401), "StockError"), (_Reply(200), "StockError"), (_Reply(200, []), "StockError"),
])
def test_search_failures_are_typed(monkeypatch, reply, error):
    from app.clipper import broll
    monkeypatch.setattr(settings, "pexels_api_key", "key")
    monkeypatch.setattr(broll.httpx, "get", lambda *a, **k: reply)
    with pytest.raises(getattr(broll, error)):
        broll.search("rain")


@pytest.mark.parametrize("status", [401, 403])
def test_refused_key_is_reported_as_such(monkeypatch, status):
    from app.clipper import broll
    monkeypatch.setattr(settings, "pexels_api_key", "key")
    monkeypatch.setattr(broll.httpx, "get", lambda *a, **k: _Reply(status))
    with pytest.raises(broll.BadKey):
        broll.search("rain")


def test_pasted_curly_quote_in_the_key_gets_a_clear_message(monkeypatch):
    from app.clipper import broll
    monkeypatch.setattr(settings, "pexels_api_key", "key")

    def encode_error(*a, **k):
        "\u201ckey".encode("ascii")

    monkeypatch.setattr(broll.httpx, "get", encode_error)
    with pytest.raises(broll.BadKey, match="paste it again"):
        broll.search("rain")


def test_search_asks_for_portrait_with_the_key(monkeypatch):
    from app.clipper import broll
    seen = {}

    def fake(url, params, headers, timeout):
        seen.update(url=url, params=params, headers=headers)
        return _Reply(200, {"videos": [_video()]})

    monkeypatch.setattr(settings, "pexels_api_key", "secret")
    monkeypatch.setattr(broll.httpx, "get", fake)
    assert broll.search("flooded street")[0].video_id == "1"
    assert seen["params"]["orientation"] == "portrait" and seen["headers"]["Authorization"] == "secret"


def _pixabay_hit(hit_id=7, large_size=50_000_000, duration=12):
    return {"id": hit_id, "pageURL": f"https://pixabay.com/videos/x-{hit_id}/", "duration": duration, "user": "Bea",
            "videos": {
                "large": {"url": "https://cdn.pixabay.com/large.mp4", "width": 3840, "height": 2160, "size": large_size},
                "medium": {"url": "https://cdn.pixabay.com/medium.mp4", "width": 1920, "height": 1080, "size": 9_000_000},
                "small": {"url": "https://cdn.pixabay.com/small.mp4", "width": 1280, "height": 720, "size": 4_000_000},
                "tiny": {"url": "https://cdn.pixabay.com/tiny.mp4", "width": 960, "height": 540, "size": 1_000_000},
            }}


def test_pixabay_prefers_4k_unless_it_is_huge():
    from app.clipper import broll
    assert broll.best_rendition(_pixabay_hit())["height"] == 2160
    assert broll.best_rendition(_pixabay_hit(large_size=900_000_000))["height"] == 1080
    empty_large = _pixabay_hit()
    empty_large["videos"]["large"] = {"url": "", "width": 0, "height": 0, "size": 0}
    assert broll.best_rendition(empty_large)["height"] == 1080


def test_pixabay_skips_files_too_small_to_crop():
    from app.clipper import broll
    assert broll.best_rendition({"videos": {"tiny": {"url": "x", "height": 540, "size": 1}}}) is None
    assert broll.best_rendition({"videos": "junk"}) is None
    assert broll.best_rendition({}) is None


def test_pixabay_results_become_candidates_with_the_download_flag(monkeypatch):
    from app.clipper import broll
    found = broll._pixabay_candidates([_pixabay_hit(), "junk", {"id": 2}, {"videos": {}}])
    assert len(found) == 1
    assert found[0].video_id == "pixabay-7" and found[0].provider == "Pixabay" and found[0].author == "Bea"
    assert found[0].link == "https://cdn.pixabay.com/large.mp4?download=1"
    assert broll._with_download_flag("https://x/a.mp4?token=1") == "https://x/a.mp4?token=1&download=1"


def test_pixabay_search_sends_the_key_and_asks_for_real_footage(monkeypatch):
    from app.clipper import broll
    seen = {}

    def fake(url, params, headers, timeout):
        seen.update(url=url, params=params)
        return _Reply(200, {"hits": [_pixabay_hit()]})

    monkeypatch.setattr(settings, "pexels_api_key", "")
    monkeypatch.setattr(settings, "pixabay_api_key", "secret")
    monkeypatch.setattr(broll.httpx, "get", fake)
    assert broll.search("flooded street")[0].provider == "Pixabay"
    assert seen["url"].startswith("https://pixabay.com/api/videos")
    assert seen["params"]["key"] == "secret" and seen["params"]["video_type"] == "film"


def test_pexels_key_wins_when_both_are_set(monkeypatch):
    from app.clipper import broll
    monkeypatch.setattr(settings, "pixabay_api_key", "b")
    monkeypatch.setattr(settings, "pexels_api_key", "a")
    assert broll.provider() == "Pexels"
    monkeypatch.setattr(settings, "pexels_api_key", "")
    assert broll.provider() == "Pixabay"
    monkeypatch.setattr(settings, "pixabay_api_key", "")
    assert broll.provider() is None and not broll.available()


def test_searches_are_cached_for_a_day_as_pixabay_requires(monkeypatch):
    from app.clipper import broll
    calls = []

    def fake(url, params, headers, timeout):
        calls.append(params["q"])
        return _Reply(200, {"hits": [_pixabay_hit()]})

    monkeypatch.setattr(settings, "pexels_api_key", "")
    monkeypatch.setattr(settings, "pixabay_api_key", "secret")
    monkeypatch.setattr(broll.httpx, "get", fake)
    broll.search("rain")
    broll.search("RAIN")
    assert calls == ["rain"]
    broll.search("snow")
    assert calls == ["rain", "snow"]
    later = broll.time.time() + 25 * 3600
    monkeypatch.setattr(broll.time, "time", lambda: later)
    broll.search("rain")
    assert calls == ["rain", "snow", "rain"]


def test_failed_search_is_not_cached(monkeypatch):
    from app.clipper import broll
    replies = iter([_Reply(500), _Reply(200, {"hits": [_pixabay_hit()]})])
    monkeypatch.setattr(settings, "pexels_api_key", "")
    monkeypatch.setattr(settings, "pixabay_api_key", "secret")
    monkeypatch.setattr(broll.httpx, "get", lambda *a, **k: next(replies))
    with pytest.raises(broll.StockError):
        broll.search("rain")
    assert broll.search("rain")


@pytest.mark.parametrize("failure", [
    "reply", "connection",
])
def test_pixabay_key_never_reaches_an_error_message(monkeypatch, failure):
    from app.clipper import broll
    monkeypatch.setattr(settings, "pexels_api_key", "")
    monkeypatch.setattr(settings, "pixabay_api_key", "topsecret")

    def fake(*a, **k):
        if failure == "connection":
            raise broll.httpx.ConnectError("failed https://pixabay.com/api/videos/?key=topsecret")
        return _Reply(400)

    monkeypatch.setattr(broll.httpx, "get", fake)
    with pytest.raises(broll.StockError) as caught:
        broll.search("rain")
    assert "topsecret" not in str(caught.value)


def test_request_urls_with_the_key_are_kept_out_of_the_logs(monkeypatch):
    """Over a real connection, because httpx writes the whole URL, key included, at INFO."""
    import http.server
    import json
    import logging
    import threading

    from app.clipper import broll

    class Library(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            body = json.dumps({"hits": []}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    class Collect(logging.Handler):
        def __init__(self):
            super().__init__(logging.DEBUG)
            self.lines = []

        def emit(self, record):
            self.lines.append(record.getMessage())

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Library)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    collect = Collect()
    root, client = logging.getLogger(), logging.getLogger("httpx")
    level_root, level_client = root.level, client.level
    root.addHandler(collect)
    root.setLevel(logging.DEBUG)
    try:
        monkeypatch.setattr(settings, "pexels_api_key", "")
        monkeypatch.setattr(settings, "pixabay_api_key", "topsecret")
        monkeypatch.setattr(broll, "_PIXABAY_URL", f"http://127.0.0.1:{server.server_address[1]}/api/videos/")
        broll.search("rain")
        assert not [line for line in collect.lines if "topsecret" in line]

        # Without the guard the key really is written, so the check above means something.
        client.setLevel(logging.INFO)
        broll.search("snow")
        assert [line for line in collect.lines if "topsecret" in line]
    finally:
        root.removeHandler(collect)
        root.setLevel(level_root)
        client.setLevel(level_client)
        server.shutdown()
        server.server_close()


def test_wrong_pixabay_key_stops_the_job_with_a_clear_message(monkeypatch):
    from app.clipper import broll
    monkeypatch.setattr(settings, "pexels_api_key", "")
    monkeypatch.setattr(settings, "pixabay_api_key", "wrong")
    monkeypatch.setattr(broll.httpx, "get", lambda *a, **k: _Reply(400))
    with pytest.raises(broll.BadKey, match="Pixabay refused"):
        broll.search("rain")


def test_pixabay_footage_is_fetched_and_credited(monkeypatch):
    from app.clipper import broll
    monkeypatch.setattr(settings, "pexels_api_key", "")
    monkeypatch.setattr(settings, "pixabay_api_key", "secret")
    monkeypatch.setattr(broll.httpx, "get", lambda *a, **k: _Reply(200, {"hits": [_pixabay_hit()]}))
    monkeypatch.setattr(broll, "_playable", lambda path, seconds: True)
    links = []

    def deliver(link, path):
        links.append(link)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"v" * 2000)

    monkeypatch.setattr(broll, "_download", deliver)
    clip = broll.fetch("rain", 3.0)
    assert clip.provider == "Pixabay" and clip.path.endswith("pixabay-7.mp4")
    assert links == ["https://cdn.pixabay.com/large.mp4?download=1"]
    assert broll.credit_line([clip.author], clip.provider) == "Stock video from Pixabay: Bea."


def test_interrupted_download_leaves_nothing_in_the_cache(tmp_path, monkeypatch):
    from app.clipper import broll

    class Stream:
        status_code = 200
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False
        def iter_bytes(self):
            yield b"partial"
            raise broll.httpx.ReadError("dropped")

    monkeypatch.setattr(broll.httpx, "stream", lambda *a, **k: Stream())
    target = tmp_path / "broll" / "1.mp4"
    with pytest.raises(broll.StockError):
        broll._download("https://cdn/x.mp4", target)
    assert not target.exists() and not list(target.parent.iterdir())


def _broll_reply(*shots):
    return {"keep": list(range(12)), "beats": [], "broll": [
        {"sentence": s, "phrase": "useful words", "query": q, "seconds": 2.5} for s, q in shots]}


def test_ai_broll_lands_on_kept_words_inside_the_limits(monkeypatch):
    from app.clipper.edit import build_edit
    _edit_reply(monkeypatch, _broll_reply((2, "flooded street"), (2, "too close"), (5, "dry earth"), (11, "too late")))
    words = _talk()
    edit = build_edit(Moment(10.0, words[-1].end), words)
    assert edit.cutaways
    times = [edit.to_output(c.at) for c in edit.cutaways]
    assert times[0] >= 2.5
    for cutaway, when in zip(edit.cutaways, times):
        assert 1.5 <= cutaway.seconds <= 3.2
        assert when + cutaway.seconds <= edit.duration - 1.5
    assert sum(c.seconds for c in edit.cutaways) <= edit.duration * 0.45
    assert "too close" not in [c.query for c in edit.cutaways]


@pytest.mark.parametrize("item", [
    "junk", {"sentence": 99, "phrase": "useful words", "query": "x"}, {"sentence": 3, "phrase": "not here", "query": "x"},
    {"sentence": 3, "phrase": "useful words", "query": ""}, {"sentence": 3, "phrase": "useful words", "query": "x" * 99},
    {"sentence": 3, "phrase": "useful words", "query": "x", "seconds": "NaN"},
])
def test_bad_broll_items_are_dropped(monkeypatch, item):
    from app.clipper.edit import build_edit
    _edit_reply(monkeypatch, {"keep": list(range(12)), "beats": [], "broll": [item]})
    words = _talk()
    assert build_edit(Moment(10.0, words[-1].end), words).cutaways == []


def _planned(monkeypatch, *shots):
    from app.clipper.edit import build_edit
    _edit_reply(monkeypatch, _broll_reply(*shots))
    words = _talk()
    return build_edit(Moment(10.0, words[-1].end), words)


def test_no_key_means_no_broll_and_one_clear_warning(monkeypatch):
    from app.clipper.edit import attach_stock
    monkeypatch.setattr(settings, "pexels_api_key", "")
    monkeypatch.setattr(settings, "pixabay_api_key", "")
    edit = _planned(monkeypatch, (3, "rain"))
    assert edit.cutaways
    assert "PIXABAY_API_KEY" in attach_stock(edit)[0]
    assert edit.cutaways == []


def test_rate_limit_stops_searching(monkeypatch):
    from app.clipper import broll
    from app.clipper.edit import attach_stock
    monkeypatch.setattr(settings, "pexels_api_key", "key")
    edit = _planned(monkeypatch, (3, "rain"), (6, "sun"))
    assert len(edit.cutaways) == 2
    calls = []

    def limited(query, seconds, exclude=(), choose=None):
        calls.append(query)
        raise broll.RateLimited("Pexels rate limit reached")

    monkeypatch.setattr(broll, "fetch", limited)
    warnings = attach_stock(edit)
    assert len(calls) == 1 and edit.cutaways == [] and "rate limit" in warnings[0]


def test_blocked_search_stops_every_later_clip_in_the_job(monkeypatch):
    from app.clipper import broll
    from app.clipper.edit import attach_stock
    monkeypatch.setattr(settings, "pexels_api_key", "key")
    calls = []

    def limited(query, seconds, exclude=(), choose=None):
        calls.append(query)
        raise broll.RateLimited("Pexels rate limit reached")

    monkeypatch.setattr(broll, "fetch", limited)
    session = broll.StockSession()
    first = attach_stock(_planned(monkeypatch, (3, "rain"), (6, "sun")), session)
    second = attach_stock(_planned(monkeypatch, (3, "rain"), (6, "sun")), session)
    assert len(calls) == 1 and first and second == []


def test_dead_cdn_stops_after_two_cutaways(monkeypatch):
    from app.clipper import broll
    from app.clipper.edit import attach_stock
    monkeypatch.setattr(settings, "pexels_api_key", "key")
    calls = []

    def dead(query, seconds, exclude=(), choose=None):
        calls.append(query)
        raise broll.DownloadsFailed("2 stock downloads failed")

    monkeypatch.setattr(broll, "fetch", dead)
    plan = _planned(monkeypatch, (2, "a"), (5, "b"), (8, "c"))
    assert len(plan.cutaways) == 3
    warnings = attach_stock(plan, broll.StockSession())
    assert len(calls) == 2 and plan.cutaways == [] and len(set(warnings)) == 1


@pytest.mark.parametrize("error", [TypeError("bad"), UnicodeEncodeError("ascii", "x", 0, 1, "bad"), RuntimeError("bad")])
def test_any_stock_error_costs_only_the_broll(monkeypatch, error):
    from app.clipper import broll
    from app.clipper.edit import attach_stock
    monkeypatch.setattr(settings, "pexels_api_key", "key")

    def broken(query, seconds, exclude=(), choose=None):
        raise error

    monkeypatch.setattr(broll, "fetch", broken)
    plan = _planned(monkeypatch, (3, "rain"))
    assert attach_stock(plan) and plan.cutaways == []


def test_shutdown_still_gets_through_the_stock_code(monkeypatch):
    from app.clipper import broll
    from app.clipper.edit import attach_stock
    monkeypatch.setattr(settings, "pexels_api_key", "key")

    def stopped(query, seconds, exclude=(), choose=None):
        raise Interrupted("ffmpeg was stopped")

    monkeypatch.setattr(broll, "fetch", stopped)
    with pytest.raises(Interrupted):
        attach_stock(_planned(monkeypatch, (3, "rain")))


def test_one_failed_search_keeps_the_other_shots(monkeypatch):
    from app.clipper import broll
    from app.clipper.edit import attach_stock
    monkeypatch.setattr(settings, "pexels_api_key", "key")
    edit = _planned(monkeypatch, (3, "rain"), (6, "sun"))

    def mixed(query, seconds, exclude=(), choose=None):
        if query == "rain":
            raise broll.StockError("Pexels returned 500")
        return broll.StockClip("5", "/x/5.mp4", 10.0, "Ana", offset=0.4)

    monkeypatch.setattr(broll, "fetch", mixed)
    warnings = attach_stock(edit)
    assert [c.query for c in edit.cutaways] == ["sun"] and "500" in warnings[0]


def test_same_stock_clip_is_not_used_twice_in_a_clip(monkeypatch):
    from app.clipper import broll
    from app.clipper.edit import attach_stock
    monkeypatch.setattr(settings, "pexels_api_key", "key")
    edit = _planned(monkeypatch, (3, "rain"), (6, "sun"))
    seen = []

    def fetch(query, seconds, exclude=(), choose=None):
        seen.append(set(exclude))
        return broll.StockClip(query, f"/x/{query}.mp4", 10.0, offset=0.4)

    monkeypatch.setattr(broll, "fetch", fetch)
    attach_stock(edit)
    assert seen == [set(), {"rain"}]


def test_broll_goes_into_the_graph_under_the_graphics(monkeypatch):
    from app.clipper.edit import Cutaway, EditPlan
    from app.clipper.broll import StockClip
    clip = StockClip("1", "/x/1.mp4", 10.0, offset=0.4)
    edit = EditPlan(words=_words("a b c d", start=11.0), segments=[(11.0, 20.0)], speed=1.0,
                    cutaways=[Cutaway(14.0, 2.0, "rain", clip), Cutaway(17.0, 1.5, "sun", clip)])
    plan = render.plan_clip(Moment(10, 40), [], _signals(), INFO, has_graphics=True, edit=edit)
    graph = render.build_filtergraph(plan, INFO, 1.0)
    assert "[3:v]scale=1080:1920" in graph and "[4:v]scale=1080:1920" in graph
    assert graph.index("overlay=eof_action=pass") < graph.index("ass=clip.ass")
    assert "between(t,2.983,4.983)" in graph
    points = render.rhythm_points(edit)
    assert any(abs(p - 3.0) < 0.01 for p in points) and any(abs(p - 5.0) < 0.01 for p in points)
    whooshes = [c for c in render.sound_cues(edit, points) if c.sound == "whoosh"]
    assert len(whooshes) >= 4


def test_stock_inputs_shift_when_there_is_no_sound_track(monkeypatch):
    from app.clipper.edit import Cutaway, EditPlan
    from app.clipper.broll import StockClip
    edit = EditPlan(words=_words("a b", start=11.0), segments=[(11.0, 20.0)],
                    cutaways=[Cutaway(14.0, 2.0, "rain", StockClip("1", "/x/1.mp4", 10.0))])
    silent = MediaInfo(120.0, 1280, 720, 30.0, False)
    plan = render.plan_clip(Moment(10, 40), [], _signals(), silent, has_graphics=True, edit=edit)
    assert "[2:v]scale=1080:1920" in render.build_filtergraph(plan, silent, 1.0)


def test_credit_line_names_each_videographer_once():
    from app.clipper import broll
    assert broll.credit_line(["Ana", "Ben", "Ana", ""]) == "Stock video from Pexels: Ana, Ben."
    assert broll.credit_line([""]) == "Stock video from Pexels."


def test_stock_credit_reaches_the_upload_copy(tmp_path, monkeypatch):
    from app.clipper import broll
    from app.clipper.edit import Cutaway, EditPlan
    moment = Moment(10, 40, title="T", kind="speech", description="About heat.")
    _stub_pipeline(monkeypatch, [moment])
    speech = Transcript(words=_words("word " * 400, start=1.0), language="en", duration=120.0)
    monkeypatch.setattr(job, "transcribe", lambda path, info: speech)
    monkeypatch.setattr(job, "pick_speech", lambda *a, **k: [moment])
    clip = broll.StockClip("1", "/x/1.mp4", 10.0, "Ana")
    plan = EditPlan(words=speech.words[:50], segments=[(10.0, 40.0)], cutaways=[Cutaway(12.0, 2.0, "rain", clip)])
    monkeypatch.setattr(job, "build_edit", lambda *a, **k: plan)
    monkeypatch.setattr(job, "attach_stock", lambda edit, stock=None: [])
    _, description, _ = _draft(job.clip_source(_source(tmp_path, "credit"))["clips"][0]["id"])
    assert "Stock video from Pexels: Ana." in description


def _with_stock(monkeypatch, moment, author="Ana"):
    from app.clipper import broll
    from app.clipper.edit import Cutaway, EditPlan
    _stub_pipeline(monkeypatch, [moment])
    speech = Transcript(words=_words("word " * 400, start=1.0), language="en", duration=120.0)
    monkeypatch.setattr(job, "transcribe", lambda path, info: speech)
    monkeypatch.setattr(job, "pick_speech", lambda *a, **k: [moment])
    clip = broll.StockClip("1", "/x/1.mp4", 10.0, author)
    plan = EditPlan(words=speech.words[:50], segments=[(10.0, 40.0)], cutaways=[Cutaway(12.0, 2.0, "rain", clip)])
    monkeypatch.setattr(job, "build_edit", lambda *a, **k: plan)
    monkeypatch.setattr(job, "attach_stock", lambda edit, stock=None: [])
    return plan


def test_stock_credit_does_not_replace_the_source_description(tmp_path, monkeypatch):
    _with_stock(monkeypatch, Moment(10, 40, title="T", kind="speech"))
    source = _source(tmp_path, "keepdesc")
    source.description = "The source video's own description."
    _, description, _ = _draft(job.clip_source(source)["clips"][0]["id"])
    assert "The source video's own description." in description and "Stock video from Pexels: Ana." in description


def test_broll_that_will_not_render_costs_only_the_broll(tmp_path, monkeypatch):
    plan = _with_stock(monkeypatch, Moment(10, 40, title="T", kind="speech", description="About heat."))
    original = job.render_moment
    calls = []

    def fussy(src, info, signals, moment, words, out_path, edit=None):
        calls.append(bool(edit.cutaways))
        if edit.cutaways:
            raise job.MediaError("moov atom not found")
        return original(src, info, signals, moment, words, out_path, edit=edit)

    monkeypatch.setattr(job, "render_moment", fussy)
    result = job.clip_source(_source(tmp_path, "fallbackbroll"))
    assert result["ok"] and calls == [True, False] and plan.cutaways == []
    assert any("lost its B-roll" in w for w in result["warnings"])
    _, description, _ = _draft(result["clips"][0]["id"])
    assert "About heat." in description and "Pexels" not in description


def test_rerendering_a_lost_file_updates_its_credit_and_length(tmp_path, monkeypatch):
    moment = Moment(10, 40, title="T", kind="speech", description="About heat.")
    _with_stock(monkeypatch, moment, author="First Author")
    first = job.clip_source(_source(tmp_path, "newcredit"))["clips"][0]
    _approve(first["id"])
    os.unlink(first["path"])
    _with_stock(monkeypatch, moment, author="Second Author")
    job.clip_source(_source(tmp_path, "newcredit"))
    _, description, _ = _draft(first["id"])
    assert "Second Author" in description and "First Author" not in description
    with session_scope() as db:
        assert db.get(Video, first["id"]).status == VideoStatus.APPROVED


def test_framing_never_sits_still_for_long(monkeypatch):
    from app.clipper.edit import build_edit
    _edit_reply(monkeypatch, {"keep": list(range(12)), "beats": []})
    words = _talk()
    edit = build_edit(Moment(10.0, words[-1].end), words)
    points = render.rhythm_points(edit) + [edit.duration]
    gaps = [b - a for a, b in zip(points, points[1:])]
    assert max(gaps) <= 3.0
    assert min(gaps[:-1]) >= 0.5


def test_count_up_ends_on_the_real_value():
    assert captions._count_frames("40°C")[-1] == "40°C"
    assert captions._count_frames("40°C")[0] == "4°C"
    assert captions._count_frames("$1,200")[4] == "$600"
    assert captions._count_frames("2.5M")[-2].endswith("M")
    assert captions._count_frames("NONE") == ["NONE"]


@pytest.mark.parametrize("text", ["40", "100%", "$1,000", "2024", "1,000,000", "3.5", "40°C", "5K", "THOUSANDS DIED"])
def test_plain_numbers_are_really_drawn(text):
    ass = captions.build_ass([], "", 20.0, "Font", None, [(4.0, text, "stat" if text[0] in "$1234" else "card", "", 1.6)])
    assert any(line.startswith("Dialogue: 3") for line in ass.splitlines())


@pytest.mark.parametrize("text", ["४०", "भूमि", "!!!", "🔥"])
def test_text_the_font_cannot_draw_is_refused(text):
    assert not captions.beat_is_drawable(text)
    assert not captions.build_ass([], "", 20.0, "Font", None, [(4.0, text, "card", "", 0.9)]).count("Dialogue")


@pytest.mark.parametrize("text", ["भूमि", "!!!", "४०"])
def test_undrawable_graphic_gets_no_flash_or_thud(monkeypatch, text):
    from app.clipper.edit import build_edit
    _edit_reply(monkeypatch, {"keep": list(range(12)), "beats": [
        {"sentence": 3, "phrase": "useful words", "text": text, "style": "card"}]})
    # No digits in the talk, so the rule-based fallback has nothing to show either.
    words = _words(" ".join(f"Sentence {chr(97 + i)} has plenty of useful words about things." for i in range(12)), start=10.0)
    assert build_edit(Moment(10.0, words[-1].end), words).beats == []


@pytest.mark.parametrize("text", ["B2B", "COVID-19", "5G", "3D", "1ST", "1990S", "MP4", "24/7", "9/11", "1990", "2024 ELECTION"])
def test_things_that_are_not_quantities_do_not_count_up(text):
    assert captions._count_frames(text) == [text]


@pytest.mark.parametrize("text", ["$2M", "40°C", "40%", "5K", "10X", "3.5M", "2,000 PEOPLE", "$5 BILLION"])
def test_real_quantities_count_up(text):
    frames = captions._count_frames(text)
    assert len(frames) > 1 and frames[-1] == text


def test_no_ai_graphics_land_only_on_real_quantities(monkeypatch):
    from app.clipper.edit import _rule_beats
    words = [Word(t, i * 6.0, i * 6.0 + 0.3) for i, t in enumerate(["B2B", "40°C,", "COVID-19", "$2M.", "5k"])]
    assert [b.text for b in _rule_beats(words, range(5))] == ["40°C", "$2M", "5K"]


def test_graphics_are_written_under_captions_and_stay_escaped():
    beats = [(4.0, "THOUSANDS {DIED}", "card", "", 0.9), (8.0, "40°C", "stat", "RECORD", 1.6), (12.0, "BIG TURN", "card", "", 0.9)]
    ass = captions.build_ass(_words("a b c"), "", 20.0, "Font", None, beats)
    events = [line for line in ass.splitlines() if line.startswith("Dialogue")]
    assert any("THOUSANDS" in e and e.startswith("Dialogue: 3") for e in events)
    assert any("RECORD" in e for e in events)
    assert sum(1 for e in events if "\\p1" in e) == 3
    assert not any("{DIED}" in e for e in events)


def test_long_graphic_text_breaks_into_short_lines():
    assert captions._beat_lines("THOUSANDS DIED") == ["THOUSANDS", "DIED"]
    assert captions._beat_lines("A BIG WIN") == ["A BIG WIN"]


def test_sound_track_has_the_right_length_and_is_not_clipped(tmp_path):
    import wave
    from app.clipper import sfx
    path = tmp_path / "sfx.wav"
    sfx.build_track(str(path), 10.0, [sfx.Cue(0.0, sfx.IMPACT), sfx.Cue(3.0, sfx.WHOOSH), sfx.Cue(9.9, sfx.POP), sfx.Cue(99.0, sfx.POP)])
    with wave.open(str(path)) as handle:
        assert handle.getnframes() == 11 * sfx.RATE
        samples = np.frombuffer(handle.readframes(handle.getnframes()), np.int16)
    assert np.abs(samples).max() > 1000 and np.abs(samples).max() < 32767


def test_voice_is_levelled_before_the_effects_go_on():
    from app.clipper.edit import EditPlan
    edit = EditPlan(words=_words("a b", start=11.0), segments=[(11.0, 16.0)], speed=1.12)
    plan = render.plan_clip(Moment(10, 40), [], _signals(), INFO, has_graphics=True, edit=edit)
    graph = render.build_filtergraph(plan, INFO, 1.0)
    assert graph.index("loudnorm") < graph.index("amix") < graph.index("alimiter")
    assert "level=0" in graph


def test_edit_plan_changes_the_graph(monkeypatch):
    from app.clipper.edit import Beat, EditPlan
    edit = EditPlan(words=_words("a b c d", start=11.0), segments=[(11.0, 13.0), (14.0, 16.0)],
                    beats=[Beat(14.5, "", "punch")], speed=1.12)
    plan = render.plan_clip(Moment(10, 40), [], _signals(), INFO, has_graphics=True, edit=edit)
    graph = render.build_filtergraph(plan, INFO, 1.0)
    assert plan.duration == pytest.approx(4.0 / 1.12)
    assert "setpts=PTS/1.12" in graph and "atempo=1.12" in graph
    assert "[2:a]amix" in graph and "drawbox=x=0:y=0" in graph and "sin(t*75)" in graph


def test_crop_position_is_held_inside_each_frame():
    """A shake with no margin to move into made ffmpeg read outside the image and crash."""
    from app.clipper.edit import Beat, EditPlan
    edit = EditPlan(words=_words("a b c d", start=11.0), segments=[(11.0, 16.0)], beats=[Beat(14.0, "", "punch")], speed=1.12)
    plan = render.plan_clip(Moment(10, 40), [], _signals(), INFO, has_graphics=True, edit=edit)
    assert plan.shake_expr != "0"
    graph = render.build_filtergraph(plan, INFO, 1.0)
    assert "x='clip(" in graph and "y='clip(" in graph


def test_zoom_windows_cannot_double_up_or_drop_below_full_frame():
    expression = render._edit_zoom_expression([0.0, 2.0, 4.0], 6.0)
    assert "between" not in expression
    assert "max(1,min(1.5," in expression


def test_sfx_can_be_switched_off(monkeypatch):
    from app.clipper.edit import EditPlan
    monkeypatch.setattr(settings, "clip_sfx", False)
    edit = EditPlan(words=_words("a b", start=11.0), segments=[(11.0, 16.0)], speed=1.12)
    plan = render.plan_clip(Moment(10, 40), [], _signals(), INFO, has_graphics=True, edit=edit)
    assert "amix" not in render.build_filtergraph(plan, INFO, 1.0)


def test_centre_ignores_a_second_that_straddles_a_cut():
    signals = _signals(30)
    signals.centroid[:] = 0.1
    signals.centroid[10] = 0.9  # the second holding the cut at 10.5
    signals.centroid_weight[10] = 100
    assert signals.centre_between(10.5, 20) == pytest.approx(0.1)


# ---------- Media ----------
def _stub_run(monkeypatch, returncode=0, stdout="", stderr=""):
    def fake(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], returncode, stdout, stderr)
    monkeypatch.setattr(media.subprocess, "run", fake)


@pytest.mark.parametrize("code", [255, -15, -2, -1])
def test_a_stopped_ffmpeg_is_an_interruption_not_a_failure(monkeypatch, code):
    _stub_run(monkeypatch, returncode=code, stderr="h264 errors")
    with pytest.raises(Interrupted):
        media.run_ffmpeg(["-i", "x"])


@pytest.mark.parametrize("code", [1, -9, 234])
def test_a_real_ffmpeg_failure_stays_a_failure(monkeypatch, code):
    _stub_run(monkeypatch, returncode=code, stderr="bad")
    with pytest.raises(MediaError):
        media.run_ffmpeg(["-i", "x"])


def test_ffmpeg_timeouts_become_media_errors(monkeypatch):
    def slow(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 1)
    monkeypatch.setattr(media.subprocess, "run", slow)
    with pytest.raises(MediaError, match="timed out"):
        media.run_ffmpeg(["-i", "x"])
    with pytest.raises(MediaError, match="timed out"):
        media.read_ffmpeg(["-i", "x"])
    with pytest.raises(MediaError, match="timed out"):
        media.probe("x")


def test_ffmpeg_cannot_read_stdin(monkeypatch):
    seen = {}

    def fake(command, **kwargs):
        seen.update(command=command, **kwargs)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(media.subprocess, "run", fake)
    media.read_ffmpeg(["-i", "x"])
    assert seen["stdin"] == subprocess.DEVNULL and "-nostdin" in seen["command"]
    media.run_ffmpeg(["-i", "x"])
    assert seen["stdin"] == subprocess.DEVNULL and "-nostdin" in seen["command"]


def _probe_json(video=None, audio=True, duration="60.0", **extra):
    stream = {"codec_type": "video", "width": 1280, "height": 720, "avg_frame_rate": "30/1", **(video or {})}
    streams = [stream] + ([{"codec_type": "audio"}] if audio else [])
    return json.dumps({"format": {"duration": duration}, "streams": streams, **extra})


def test_cover_art_is_not_a_video(monkeypatch):
    _stub_run(monkeypatch, stdout=_probe_json(video={"disposition": {"attached_pic": 1}}))
    with pytest.raises(MediaError, match="no video stream"):
        media.probe("song.mp3")


def test_duration_is_the_pictures_not_the_longer_audio(monkeypatch):
    _stub_run(monkeypatch, stdout=_probe_json(video={"duration": "20.0"}))
    assert media.probe("x.mp4").duration == 20.0


def test_container_duration_is_used_when_the_stream_has_none(monkeypatch):
    _stub_run(monkeypatch, stdout=_probe_json())
    assert media.probe("x.mkv").duration == 60.0


def test_tiny_video_is_refused(monkeypatch):
    _stub_run(monkeypatch, stdout=_probe_json(video={"duration": "0.2"}))
    with pytest.raises(MediaError, match="too short"):
        media.probe("x.mp4")


@pytest.mark.parametrize("sar,expected", [("4:3", 1920), ("0:1", 1440), ("N/A", 1440), (None, 1440)])
def test_width_is_the_display_width(monkeypatch, sar, expected):
    _stub_run(monkeypatch, stdout=_probe_json(video={"width": 1440, "height": 1080, "sample_aspect_ratio": sar}))
    assert media.probe("x.mp4").width == expected


def test_rotated_non_square_pixels_are_swapped_too(monkeypatch):
    video = {"width": 1920, "height": 1080, "sample_aspect_ratio": "4:3", "side_data_list": [{"rotation": -90}]}
    _stub_run(monkeypatch, stdout=_probe_json(video=video))
    info = media.probe("x.mp4")
    assert (info.width, info.height) == (810, 1920)


# ---------- Transcript ----------
PAYLOAD = {"results": {"channels": [{"detected_language": "en", "alternatives": [{"words": [
    {"word": "hello", "punctuated_word": "Hello,", "start": 1.0, "end": 1.4, "confidence": 0.98},
    {"word": "there", "start": 1.5, "end": 1.9, "confidence": 0.9},
]}]}]}}


def test_deepgram_response_is_parsed():
    parsed = transcribe.parse_response(PAYLOAD, 60.0)
    assert [w.text for w in parsed.words] == ["Hello,", "there"]
    assert parsed.language == "en"


@pytest.mark.parametrize("payload", [{}, {"results": {}}, {"results": {"channels": []}}])
def test_empty_deepgram_response_is_harmless(payload):
    assert transcribe.parse_response(payload, 60.0).words == []


def _speech(language="en", per_minute=150, confidence=0.95):
    return Transcript(
        words=[Word("w", i * 0.4, i * 0.4 + 0.3, confidence) for i in range(per_minute)],
        language=language, duration=60.0,
    )


def test_english_speech_is_recognised():
    assert _speech().is_english_speech


@pytest.mark.parametrize("transcript", [
    _speech(language="hi"),
    _speech(per_minute=10),
    _speech(confidence=0.2),
    Transcript(),
])
def test_other_audio_takes_the_no_speech_path(transcript):
    assert not transcript.is_english_speech


def test_transcript_cache_avoids_a_second_paid_call(tmp_path, monkeypatch):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    monkeypatch.setattr(settings, "deepgram_api_key", "key")
    monkeypatch.setattr(transcribe, "run_ffmpeg", lambda args: Path(args[-1]).write_bytes(b"a"))
    calls = []
    monkeypatch.setattr(transcribe, "_request", lambda path: calls.append(path) or PAYLOAD)
    info = MediaInfo(60.0, 1280, 720, 30.0, True)
    first = transcribe.transcribe(str(video), info)
    second = transcribe.transcribe(str(video), info)
    assert len(calls) == 1
    assert first.words == second.words


def test_replaced_file_is_transcribed_again(tmp_path, monkeypatch):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    monkeypatch.setattr(settings, "deepgram_api_key", "key")
    monkeypatch.setattr(transcribe, "run_ffmpeg", lambda args: Path(args[-1]).write_bytes(b"a"))
    calls = []
    monkeypatch.setattr(transcribe, "_request", lambda path: calls.append(path) or PAYLOAD)
    info = MediaInfo(60.0, 1280, 720, 30.0, True)
    transcribe.transcribe(str(video), info)
    video.write_bytes(b"different and longer")
    transcribe.transcribe(str(video), info)
    assert len(calls) == 2


def test_old_cache_without_a_fingerprint_is_not_trusted(tmp_path, monkeypatch):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    Path(f"{video}.transcript.json").write_text(json.dumps({"language": "en", "words": []}))
    monkeypatch.setattr(settings, "deepgram_api_key", "key")
    monkeypatch.setattr(transcribe, "run_ffmpeg", lambda args: Path(args[-1]).write_bytes(b"a"))
    calls = []
    monkeypatch.setattr(transcribe, "_request", lambda path: calls.append(path) or PAYLOAD)
    transcribe.transcribe(str(video), MediaInfo(60.0, 1280, 720, 30.0, True))
    assert calls


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_read_only_folder_still_keeps_the_paid_transcript(tmp_path, monkeypatch):
    folder = tmp_path / "ro"
    folder.mkdir()
    video = folder / "v.mp4"
    video.write_bytes(b"x")
    folder.chmod(0o555)
    monkeypatch.setattr(settings, "deepgram_api_key", "key")
    monkeypatch.setattr(transcribe, "run_ffmpeg", lambda args: Path(args[-1]).write_bytes(b"a"))
    calls = []
    monkeypatch.setattr(transcribe, "_request", lambda path: calls.append(path) or PAYLOAD)
    info = MediaInfo(60.0, 1280, 720, 30.0, True)
    try:
        assert transcribe.transcribe(str(video), info).words
        transcribe.transcribe(str(video), info)
    finally:
        folder.chmod(0o755)
    assert len(calls) == 1


@pytest.mark.parametrize("payload", [
    {"results": {"channels": [{"alternatives": [{"words": [{"word": "x", "start": "oops"}]}]}]}},
    {"results": [1]},
    [],
])
def test_strange_deepgram_replies_become_transcribe_errors(tmp_path, monkeypatch, payload):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    monkeypatch.setattr(settings, "deepgram_api_key", "key")
    monkeypatch.setattr(transcribe, "run_ffmpeg", lambda args: Path(args[-1]).write_bytes(b"a"))
    monkeypatch.setattr(transcribe, "_request", lambda path: payload)
    with pytest.raises(transcribe.TranscribeError):
        transcribe.transcribe(str(video), MediaInfo(60.0, 1280, 720, 30.0, True))


def test_unreadable_deepgram_body_is_a_transcribe_error(tmp_path, monkeypatch):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x")

    class Reply:
        status_code = 200
        text = "<html>"

        def json(self):
            raise ValueError("not json")

    monkeypatch.setattr(transcribe.httpx, "post", lambda *a, **k: Reply())
    with pytest.raises(transcribe.TranscribeError):
        transcribe._request(str(audio))


def test_no_key_and_no_audio_cost_nothing(tmp_path, monkeypatch):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    monkeypatch.setattr(settings, "deepgram_api_key", "")
    monkeypatch.setattr(transcribe, "_request", lambda path: pytest.fail("should not call Deepgram"))
    assert transcribe.transcribe(str(video), MediaInfo(60.0, 1280, 720, 30.0, True)) is None
    monkeypatch.setattr(settings, "deepgram_api_key", "key")
    assert transcribe.transcribe(str(video), MediaInfo(60.0, 1280, 720, 30.0, False)) is None


# ---------- OpenCode ----------
def _fake_opencode(tmp_path, reply='{"answer": 1}', code=0):
    """A stand-in CLI that records how it was called."""
    record = tmp_path / "call.json"
    script = tmp_path / "opencode"
    script.write_text(f"""#!{sys.executable}
import json, os, sys
stdin = "" if "--file" in sys.argv else sys.stdin.read()
json.dump({{"argv": sys.argv[1:], "stdin": stdin, "cwd": os.getcwd(), "pwd": os.environ.get("PWD"),
            "no_project": os.environ.get("OPENCODE_DISABLE_PROJECT_CONFIG"),
            "config": os.environ.get("OPENCODE_CONFIG_CONTENT")}}, open({str(record)!r}, "w"))
print(json.dumps({{"type": "step_start"}}))
print(json.dumps({{"type": "text", "part": {{"text": {reply!r}}}}}))
sys.exit({code})
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script), record


def test_opencode_runs_locked_down(tmp_path, monkeypatch):
    binary, record = _fake_opencode(tmp_path)
    monkeypatch.setattr(settings, "opencode_bin", binary)
    monkeypatch.setenv("PWD", str(tmp_path))
    assert ai.ask_json("transcript text") == {"answer": 1}
    call = json.loads(record.read_text())
    config = json.loads(call["config"])
    assert config["permission"] == {"*": "deny"}
    assert "--standalone" in call["argv"]
    assert "--auto" not in call["argv"]
    assert call["stdin"] == "transcript text"
    assert "transcript text" not in call["argv"]
    assert Path(call["cwd"]).resolve() != Path.cwd().resolve()
    # OpenCode roots its session in $PWD, not the real working directory.
    assert Path(call["pwd"]).resolve() == Path(call["cwd"]).resolve()
    assert call["no_project"] == "1"


def test_opencode_attachments_are_passed_as_files(tmp_path, monkeypatch):
    binary, record = _fake_opencode(tmp_path)
    monkeypatch.setattr(settings, "opencode_bin", binary)
    ai.ask("look", files=["/x/a.jpg"], model="vision/model")
    argv = json.loads(record.read_text())["argv"]
    assert argv[argv.index("--file") + 1] == "/x/a.jpg"
    assert argv[argv.index("--model") + 1] == "vision/model"


def test_opencode_failure_returns_none(tmp_path, monkeypatch):
    binary, _ = _fake_opencode(tmp_path, code=3)
    monkeypatch.setattr(settings, "opencode_bin", binary)
    assert ai.ask("x") is None


def test_opencode_prose_reply_returns_none(tmp_path, monkeypatch):
    binary, _ = _fake_opencode(tmp_path, reply="sorry, I cannot")
    monkeypatch.setattr(settings, "opencode_bin", binary)
    assert ai.ask_json("x") is None


@pytest.mark.parametrize("stdout", [
    'null\n{"type":"text","part":{"text":"{\\"answer\\": 1}"}}\n',
    '{"type":"text","part":"oops"}\n{"type":"text","part":{"text":"{\\"answer\\": 1}"}}\n',
    '{"type":"text","part":{"text":5}}\n{"type":"text","part":{"text":"{\\"answer\\": 1}"}}\n',
])
def test_malformed_events_are_skipped(stdout):
    assert ai._text_from_events(stdout) == '{"answer": 1}'


def test_opencode_output_is_decoded_as_utf8_whatever_the_locale(tmp_path, monkeypatch):
    seen = {}

    def fake(command, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(command, 0, "", "")

    binary, _ = _fake_opencode(tmp_path)
    monkeypatch.setattr(settings, "opencode_bin", binary)
    monkeypatch.setattr(ai.subprocess, "run", fake)
    ai.ask("x")
    assert seen["encoding"] == "utf-8" and seen["errors"] == "replace"


def test_missing_opencode_is_not_an_error(monkeypatch):
    monkeypatch.setattr(settings, "opencode_bin", "definitely-not-installed")
    assert not ai.available()
    assert ai.ask("x") is None


# ---------- Ingest ----------
def test_links_and_paths_are_told_apart():
    assert ingest.is_url("https://youtu.be/x") and ingest.is_url("  HTTP://x.com/y")
    assert not ingest.is_url("/home/me/video.mp4")


def test_missing_file_is_reported(tmp_path):
    with pytest.raises(ingest.IngestError):
        ingest.fetch(str(tmp_path / "nope.mp4"))


def test_local_file_id_is_stable(tmp_path):
    video = tmp_path / "my_long-talk.mp4"
    video.write_bytes(b"x")
    assert ingest.fetch(str(video)).source_id == ingest.fetch(str(video)).source_id
    assert ingest.fetch(str(video)).title == "my long talk"


def _fake_ydl(monkeypatch, probe_info, downloaded=None):
    calls = []

    class FakeDL:
        def __init__(self, options):
            assert options["noplaylist"] is True
            assert options["extract_flat"] == "in_playlist"
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False
        def extract_info(self, url, download):
            calls.append(("extract", download))
            return probe_info
        def process_ie_result(self, info, download):
            calls.append(("download", download))
            return downloaded
    import yt_dlp
    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeDL)
    return calls


@pytest.mark.parametrize("info,message", [
    ({"entries": [{}, {}]}, "playlist"),
    ({"id": "x", "is_live": True}, "live"),
])
def test_playlists_and_live_streams_are_refused_before_any_download(monkeypatch, info, message):
    calls = _fake_ydl(monkeypatch, info)
    with pytest.raises(ingest.IngestError, match=message):
        ingest.fetch("https://youtube.com/x")
    assert calls == [("extract", False)]


def test_single_video_is_downloaded_and_its_age_refreshed(tmp_path, monkeypatch):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    os.utime(video, (1000, 1000))
    done = {"id": "abc", "title": "Talk", "extractor_key": "Youtube", "requested_downloads": [{"filepath": str(video)}]}
    calls = _fake_ydl(monkeypatch, {"id": "abc"}, done)
    source = ingest.fetch("https://youtube.com/watch?v=abc")
    assert calls == [("extract", False), ("download", True)]
    assert source.path == str(video) and source.source == "youtube"
    assert video.stat().st_mtime > 1000


def test_youtube_sign_in_wall_says_what_to_do(monkeypatch):
    class Refused:
        def __init__(self, options):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False
        def extract_info(self, url, download):
            raise RuntimeError("Sign in to confirm you're not a bot. Use --cookies-from-browser")
    import yt_dlp
    monkeypatch.setattr(yt_dlp, "YoutubeDL", Refused)
    with pytest.raises(ingest.IngestError, match="COOKIES_FROM_BROWSER"):
        ingest.fetch("https://youtube.com/watch?v=x")


# ---------- Job and queue ----------
def _source(tmp_path, source_id):
    path = tmp_path / f"{source_id}.mp4"
    path.write_bytes(b"x" * 2000)
    return ingest.SourceVideo(path=str(path), title="Talk", url="https://example.com/v", author="me",
                              source="local", source_id=source_id)


def _stub_pipeline(monkeypatch, moment_list=None):
    monkeypatch.setattr(job, "require_tools", lambda: None)
    monkeypatch.setattr(job, "probe", lambda path: INFO)
    monkeypatch.setattr(job, "transcribe", lambda path, info: None)
    monkeypatch.setattr(job, "analyse", lambda path, info: _signals())
    monkeypatch.setattr(job, "pick_visual", lambda *a, **k: list(moment_list or [Moment(10, 40, title="Clip one", kind="visual")]))

    def fake_render(src, info, signals, moment, words, out_path, edit=None):
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_bytes(b"v" * 2000)
        return render.ClipPlan([(moment.start, moment.end)], moment.duration, True, 404, "0", False, "1", False)

    monkeypatch.setattr(job, "render_moment", fake_render)


def test_clips_wait_for_review_by_default(tmp_path, monkeypatch):
    _stub_pipeline(monkeypatch)
    monkeypatch.setattr(settings, "clip_auto_queue", False)
    result = job.clip_source(_source(tmp_path, "review"))
    assert result["ok"] and result["kind"] == "visual"
    with session_scope() as db:
        video = db.get(Video, result["clips"][0]["id"])
        assert video.status == VideoStatus.PENDING
        assert video.meta["clip_rendered"] is True
        assert video.transformed_path == video.local_path


def test_auto_queue_skips_review(tmp_path, monkeypatch):
    _stub_pipeline(monkeypatch)
    result = job.clip_source(_source(tmp_path, "auto"), auto_queue=True)
    with session_scope() as db:
        video = db.get(Video, result["clips"][0]["id"])
        assert video.status == VideoStatus.TRANSFORMED
        assert video.transformed_at is not None


def test_approving_a_clip_does_not_render_it_again(tmp_path, monkeypatch):
    from tasks import pipeline
    _stub_pipeline(monkeypatch)
    result = job.clip_source(_source(tmp_path, "approve"))
    video_id = result["clips"][0]["id"]
    monkeypatch.setattr(pipeline, "get_transformer", lambda: pytest.fail("clip was re-rendered"))
    monkeypatch.setattr(settings, "use_mock", False)
    outcome = pipeline.transform_task(video_id)
    assert outcome["ok"] and outcome["action"] == "already rendered"
    with session_scope() as db:
        assert db.get(Video, video_id).status == VideoStatus.TRANSFORMED


def test_clip_title_reaches_the_metadata_draft(tmp_path, monkeypatch):
    from app.models import MetadataDraft
    from sqlalchemy import select
    _stub_pipeline(monkeypatch)
    result = job.clip_source(_source(tmp_path, "meta"))
    with session_scope() as db:
        draft = db.execute(select(MetadataDraft).where(MetadataDraft.video_id == result["clips"][0]["id"])).scalar_one()
        assert "Clip one" in draft.title


def _approve(video_id):
    with session_scope() as db:
        video = db.get(Video, video_id)
        video.status = VideoStatus.APPROVED
        video.curated_at = datetime.now(timezone.utc)


def test_scheduler_moves_an_approved_clip_to_ready_without_rendering(tmp_path, monkeypatch):
    from tasks import pipeline
    with session_scope() as db:
        db.query(Video).filter(Video.status == VideoStatus.APPROVED).delete()
    _stub_pipeline(monkeypatch)
    result = job.clip_source(_source(tmp_path, "sched"))
    video_id, path = result["clips"][0]["id"], result["clips"][0]["path"]
    _approve(video_id)
    monkeypatch.setattr(pipeline, "get_transformer", lambda: pytest.fail("clip was re-rendered"))
    monkeypatch.setattr(settings, "use_mock", False)
    scheduler.transform_job()
    with session_scope() as db:
        video = db.get(Video, video_id)
        assert video.status == VideoStatus.TRANSFORMED and video.transformed_path == path


def test_finished_clips_do_not_count_as_the_scrapers_backlog(tmp_path, monkeypatch):
    _stub_pipeline(monkeypatch)
    with session_scope() as db:
        before = scheduler._scraper_pending(db)
    job.clip_source(_source(tmp_path, "backlog"))
    with session_scope() as db:
        assert scheduler._scraper_pending(db) == before
        db.add(Video(source="mock", source_id="plain-pending", source_url="x", status=VideoStatus.PENDING))
        db.flush()
        assert scheduler._scraper_pending(db) == before + 1


def test_auto_approve_leaves_clips_for_review(tmp_path, monkeypatch):
    _stub_pipeline(monkeypatch)
    result = job.clip_source(_source(tmp_path, "autoapprove"))
    monkeypatch.setattr(settings, "auto_approve", True)
    monkeypatch.setattr(settings, "auto_approve_veto_hours", 0)
    outcome = scheduler.auto_approve_job()
    assert outcome["reasons"].get("clip awaits review", 0) >= 1
    with session_scope() as db:
        assert db.get(Video, result["clips"][0]["id"]).status == VideoStatus.PENDING


@pytest.mark.parametrize("status", [
    VideoStatus.REJECTED, VideoStatus.APPROVED, VideoStatus.TRANSFORMED,
    VideoStatus.PUBLISHING, VideoStatus.PUBLISHED,
])
@pytest.mark.parametrize("auto_queue", [True, False])
def test_rerun_never_undoes_a_decision_or_renders_again(tmp_path, monkeypatch, status, auto_queue):
    _stub_pipeline(monkeypatch)
    source_id = f"keep-{status.value}-{auto_queue}"
    first = job.clip_source(_source(tmp_path, source_id))
    video_id = first["clips"][0]["id"]
    with session_scope() as db:
        db.get(Video, video_id).status = status
    monkeypatch.setattr(job, "render_moment", lambda *a: pytest.fail("rendered again"))
    result = job.clip_source(_source(tmp_path, source_id), auto_queue=auto_queue)
    assert result["ok"]
    with session_scope() as db:
        assert db.get(Video, video_id).status == status


def test_rejected_and_published_clips_are_not_shown_as_new_output(tmp_path, monkeypatch):
    _stub_pipeline(monkeypatch)
    first = job.clip_source(_source(tmp_path, "shown"))
    with session_scope() as db:
        db.get(Video, first["clips"][0]["id"]).status = VideoStatus.REJECTED
    result = job.clip_source(_source(tmp_path, "shown"))
    assert result["ok"] and result["clips"] == []
    assert any("already rejected" in w for w in result["warnings"])


def test_failed_clip_renders_again(tmp_path, monkeypatch):
    _stub_pipeline(monkeypatch)
    first = job.clip_source(_source(tmp_path, "refail"))
    video_id = first["clips"][0]["id"]
    with session_scope() as db:
        db.get(Video, video_id).status = VideoStatus.FAILED
    assert job.clip_source(_source(tmp_path, "refail"))["clips"][0]["id"] == video_id
    with session_scope() as db:
        assert db.get(Video, video_id).status == VideoStatus.PENDING


def test_unreviewed_clip_is_promoted_when_review_is_skipped(tmp_path, monkeypatch):
    _stub_pipeline(monkeypatch)
    first = job.clip_source(_source(tmp_path, "promote"))
    result = job.clip_source(_source(tmp_path, "promote"), auto_queue=True)
    assert result["queued"] is True
    with session_scope() as db:
        video = db.get(Video, first["clips"][0]["id"])
        assert video.status == VideoStatus.TRANSFORMED and video.transformed_at is not None


def test_lost_file_renders_again_and_keeps_its_status(tmp_path, monkeypatch):
    _stub_pipeline(monkeypatch)
    first = job.clip_source(_source(tmp_path, "lost"))
    clip = first["clips"][0]
    _approve(clip["id"])
    os.unlink(clip["path"])
    job.clip_source(_source(tmp_path, "lost"))
    assert os.path.exists(clip["path"])
    with session_scope() as db:
        assert db.get(Video, clip["id"]).status == VideoStatus.APPROVED


def test_slightly_different_moment_does_not_make_a_second_clip(tmp_path, monkeypatch):
    _stub_pipeline(monkeypatch, [Moment(11.3, 40.0, title="Same idea", kind="visual")])
    job.clip_source(_source(tmp_path, "repeat"), auto_queue=True)
    _stub_pipeline(monkeypatch, [Moment(14.9, 40.0, title="Same idea again", kind="visual")])
    result = job.clip_source(_source(tmp_path, "repeat"), auto_queue=True)
    assert result["ok"] and result["clips"] == []
    assert any("repeats clip" in w for w in result["warnings"])


def test_too_little_speech_falls_back_to_visual_with_captions(tmp_path, monkeypatch):
    _stub_pipeline(monkeypatch)
    speech = Transcript(words=_words("short words only " * 40, start=1.0), language="en", duration=60.0)
    monkeypatch.setattr(job, "transcribe", lambda path, info: speech)
    monkeypatch.setattr(job, "pick_speech", lambda *a, **k: [])
    used = {}
    original = job.render_moment

    def spy(src, info, signals, moment, words, out_path, edit=None):
        used["words"] = len(words)
        return original(src, info, signals, moment, words, out_path, edit=edit)

    monkeypatch.setattr(job, "render_moment", spy)
    result = job.clip_source(_source(tmp_path, "fallback"))
    assert result["ok"] and result["kind"] == "visual"
    assert any("too little continuous speech" in w for w in result["warnings"])
    assert used["words"] > 0


def test_words_past_the_end_of_the_picture_are_dropped(tmp_path, monkeypatch):
    _stub_pipeline(monkeypatch)
    words = _words("word " * 100, start=1.0) + [Word("late", 500.0, 500.5)]
    monkeypatch.setattr(job, "transcribe", lambda path, info: Transcript(words=words, language="en", duration=120.0))
    seen = {}
    monkeypatch.setattr(job, "pick_speech", lambda w, *a, **k: seen.setdefault("words", w) and [Moment(1, 30, kind="speech")])
    job.clip_source(_source(tmp_path, "pastend"))
    assert all(w.end <= INFO.duration for w in seen["words"])


def test_unexpected_error_on_one_clip_keeps_the_others(tmp_path, monkeypatch):
    _stub_pipeline(monkeypatch, [Moment(0, 30, title="A", kind="visual"), Moment(40, 70, title="B", kind="visual"),
                                 Moment(80, 110, title="C", kind="visual")])
    original = job.render_moment

    def flaky(src, info, signals, moment, words, out_path, edit=None):
        if moment.title == "B":
            raise OSError("disk trouble")
        return original(src, info, signals, moment, words, out_path, edit=edit)

    monkeypatch.setattr(job, "render_moment", flaky)
    result = job.clip_source(_source(tmp_path, "oserror"))
    assert result["ok"] and [c["title"] for c in result["clips"]] == ["A", "C"]
    assert any("disk trouble" in w for w in result["warnings"])


def test_crash_after_some_clips_still_reports_them(tmp_path, monkeypatch):
    _stub_pipeline(monkeypatch)
    from tasks import pipeline
    real = job._existing_row
    calls = []

    def boom(src, moment):
        calls.append(moment)
        if len(calls) > 1:
            raise RuntimeError("late crash")
        return real(src, moment)

    monkeypatch.setattr(job, "pick_visual", lambda *a, **k: [Moment(0, 30, title="A", kind="visual"), Moment(40, 70, title="B", kind="visual")])
    monkeypatch.setattr(job, "_existing_row", boom)
    result = job.clip_source(_source(tmp_path, "latecrash"))
    assert result["ok"] and [c["title"] for c in result["clips"]] == ["A"]
    assert result["error"] == "late crash"


def test_interrupt_passes_through_clip_source(tmp_path, monkeypatch):
    _stub_pipeline(monkeypatch)

    def stopped(*a, **k):
        raise Interrupted("ffmpeg was stopped")

    monkeypatch.setattr(job, "render_moment", stopped)
    with pytest.raises(Interrupted):
        job.clip_source(_source(tmp_path, "stopped"))


def _draft(video_id):
    from app.models import MetadataDraft
    from sqlalchemy import select
    with session_scope() as db:
        draft = db.execute(select(MetadataDraft).where(MetadataDraft.video_id == video_id)).scalar_one()
        return draft.title, draft.description, list(draft.tags)


def test_upload_copy_has_a_real_description_tags_and_credit(tmp_path, monkeypatch):
    moment = Moment(10, 40, title="Heat Is a Silent Killer", kind="speech",
                    description="Why heat is deadlier than floods.", tags=("heat", "climate"))
    _stub_pipeline(monkeypatch, [moment])
    monkeypatch.setattr(job, "pick_visual", lambda *a, **k: [moment])
    source = _source(tmp_path, "copy")
    source.source, source.author = "generic", ""
    source.url = "https://example.com/talk.webm"
    title, description, tags = _draft(job.clip_source(source)["clips"][0]["id"])
    assert "Heat Is a Silent Killer" in title
    assert "Why heat is deadlier than floods." in description
    assert "Source\nhttps://example.com/talk.webm" in description
    assert "Generic" not in description
    assert tags[:2] == ["heat", "climate"]


def test_local_file_gets_no_credit_line(tmp_path, monkeypatch):
    _stub_pipeline(monkeypatch)
    source = _source(tmp_path, "mine")
    source.url, source.author = "", ""
    _, description, _ = _draft(job.clip_source(source)["clips"][0]["id"])
    assert "Source" not in description and "mine.mp4" not in description


def test_rerun_updates_instead_of_duplicating(tmp_path, monkeypatch):
    _stub_pipeline(monkeypatch)
    first = job.clip_source(_source(tmp_path, "rerun"))
    second = job.clip_source(_source(tmp_path, "rerun"))
    assert first["clips"][0]["id"] == second["clips"][0]["id"]


def test_published_clip_is_not_pulled_back_by_a_rerun(tmp_path, monkeypatch):
    _stub_pipeline(monkeypatch)
    first = job.clip_source(_source(tmp_path, "published"))
    with session_scope() as db:
        db.get(Video, first["clips"][0]["id"]).status = VideoStatus.PUBLISHED
    job.clip_source(_source(tmp_path, "published"))
    with session_scope() as db:
        assert db.get(Video, first["clips"][0]["id"]).status == VideoStatus.PUBLISHED


def test_one_bad_clip_does_not_lose_the_rest(tmp_path, monkeypatch):
    _stub_pipeline(monkeypatch, [Moment(10, 40, title="Bad", kind="visual"), Moment(50, 80, title="Good", kind="visual")])
    original = job.render_moment

    def flaky(src, info, signals, moment, words, out_path, edit=None):
        if moment.title == "Bad":
            raise job.MediaError("boom")
        return original(src, info, signals, moment, words, out_path, edit=edit)

    monkeypatch.setattr(job, "render_moment", flaky)
    result = job.clip_source(_source(tmp_path, "flaky"))
    assert [c["title"] for c in result["clips"]] == ["Good"]
    assert any("clip 1 failed" in w for w in result["warnings"])


def test_failed_transcription_still_makes_clips_and_warns(tmp_path, monkeypatch):
    _stub_pipeline(monkeypatch)

    def broken(path, info):
        raise transcribe.TranscribeError("Deepgram returned 401")

    monkeypatch.setattr(job, "transcribe", broken)
    result = job.clip_source(_source(tmp_path, "nokey"))
    assert result["ok"]
    assert any("401" in w for w in result["warnings"])


def test_missing_ffmpeg_is_a_clear_failure(monkeypatch):
    def missing():
        raise job.MediaError("ffmpeg not found on PATH")

    monkeypatch.setattr(job, "require_tools", missing)
    result = job.clip_source("/any/path.mp4")
    assert not result["ok"] and "ffmpeg" in result["error"]


def _clear_jobs():
    with session_scope() as db:
        db.query(ClipJob).delete()


def test_queue_records_success_and_failure(monkeypatch):
    _clear_jobs()
    monkeypatch.setattr(job, "clip_source", lambda source, **k: {"ok": source == "good", "error": None if source == "good" else "bad link"})
    good, bad = job.enqueue("good"), job.enqueue("bad")
    assert job.clip_queue_job() == {"processed": 2, "failed": 1}
    with session_scope() as db:
        assert db.get(ClipJob, good).status == "done"
        assert db.get(ClipJob, bad).status == "failed"
        assert db.get(ClipJob, bad).error == "bad link"


def test_queue_survives_a_crashing_job(monkeypatch):
    _clear_jobs()

    def explode(source, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(job, "clip_source", explode)
    crashed = job.enqueue("x")
    assert job.clip_queue_job()["failed"] == 1
    with session_scope() as db:
        assert "kaboom" in db.get(ClipJob, crashed).error


def test_queue_restarts_a_job_left_running_by_a_crash(monkeypatch):
    _clear_jobs()
    monkeypatch.setattr(job, "clip_source", lambda source, **k: {"ok": True})
    orphan = job.enqueue("x")
    with session_scope() as db:
        db.get(ClipJob, orphan).status = "running"
    assert job.clip_queue_job()["processed"] == 1
    with session_scope() as db:
        assert db.get(ClipJob, orphan).status == "done"


def test_interrupted_job_goes_back_in_the_queue(monkeypatch):
    _clear_jobs()

    def stopped(source, **k):
        raise Interrupted("ffmpeg was stopped")

    monkeypatch.setattr(job, "clip_source", stopped)
    stopped_id = job.enqueue("x")
    assert job.clip_queue_job()["processed"] == 0
    with session_scope() as db:
        row = db.get(ClipJob, stopped_id)
        assert row.status == "queued" and not row.result


def test_job_that_keeps_getting_stopped_eventually_fails(monkeypatch):
    _clear_jobs()

    def stopped(source, **k):
        raise Interrupted("ffmpeg was stopped")

    monkeypatch.setattr(job, "clip_source", stopped)
    stuck = job.enqueue("x")
    for _ in range(job._MAX_ATTEMPTS):
        job.clip_queue_job()
    with session_scope() as db:
        assert db.get(ClipJob, stuck).status == "queued"
    job.clip_queue_job()
    with session_scope() as db:
        row = db.get(ClipJob, stuck)
        assert row.status == "failed" and "stopped" in row.error


def test_locked_database_does_not_fail_on_a_progress_label(monkeypatch):
    from sqlalchemy.exc import OperationalError

    def locked():
        raise OperationalError("UPDATE", {}, Exception("database is locked"))

    monkeypatch.setattr(job, "session_scope", locked)
    job._set_stage("any", "rendering")


def test_queue_runs_one_at_a_time():
    assert job._queue_lock.acquire(blocking=False)
    try:
        assert job.clip_queue_job() == {"skipped": "already running"}
    finally:
        job._queue_lock.release()


# ---------- API ----------
def test_clip_endpoints(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    _clear_jobs()
    monkeypatch.setattr(job, "clip_source", lambda source, **k: {"ok": True, "clips": [], "source": source})
    client = TestClient(app)
    created = client.post("/clip", json={"source": "https://youtu.be/abc", "count": 2})
    assert created.status_code == 200
    job_id = created.json()["job_id"]
    fetched = client.get(f"/clip/jobs/{job_id}").json()
    assert fetched["status"] == "done"
    assert fetched["result"]["source"] == "https://youtu.be/abc"
    assert any(row["id"] == job_id for row in client.get("/clip/jobs").json())
    assert client.get("/clip/jobs/nope").status_code == 404
    assert client.post("/clip", json={"source": "   "}).status_code == 400
    assert client.post("/clip", json={"source": "x", "count": 99}).status_code == 422
