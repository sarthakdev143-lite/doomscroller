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
from sqlalchemy import select

from app import scheduler
from app.clipper import ai, captions, edit, ingest, job, media, moments, render, timeline, transcribe
from app.clipper.media import Interrupted, MediaError, MediaInfo
from app.clipper.moments import Moment, build_sentences
from app.clipper.signals import Signals, _find_cuts
from app.clipper.transcribe import Transcript, Word
from app.config import settings
from app.db import init_db, session_scope
from app.models import ClipJob, GrowthEvent, Video, VideoStatus


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
    # Arabic has no installed font, so the overlay is skipped; Latin draws. (Devanagari now renders.)
    assert "Dialogue: 2" not in captions.build_ass([], "مرحبا بالعالم", 5.0, "Font")
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
    _edit_reply(monkeypatch, {"keep": [0, 2, 3, 4, 6, 7, 8, 10], "beats": []})
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
        if "/8" in Path(path).as_posix():
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

    def fussy(src, info, signals, moment, words, out_path, edit=None, creator="", music=None, captions=True, source_captioned=False):
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


@pytest.mark.parametrize("text", ["४०", "مرحبا", "!!!", "🔥"])
def test_text_the_font_cannot_draw_is_refused(text):
    assert not captions.beat_is_drawable(text)
    assert not captions.build_ass([], "", 20.0, "Font", None, [(4.0, text, "card", "", 0.9)]).count("Dialogue")


@pytest.mark.parametrize("text", ["مرحبا", "!!!", "४०"])
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
    assert plan.duration == pytest.approx((4.0 + render._END_HOLD) / 1.12, abs=0.05)  # plus the closing beat
    assert "setpts=PTS/1.12" in graph and "atempo=1.12" in graph
    assert "[sfxst]amix" in graph and "drawbox=x=0:y=0" in graph and "sin(t*75)" in graph


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


def test_a_touched_file_is_not_transcribed_again(tmp_path, monkeypatch):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    monkeypatch.setattr(settings, "deepgram_api_key", "key")
    monkeypatch.setattr(transcribe, "run_ffmpeg", lambda args: Path(args[-1]).write_bytes(b"a"))
    calls = []
    monkeypatch.setattr(transcribe, "_request", lambda path: calls.append(path) or PAYLOAD)
    info = MediaInfo(60.0, 1280, 720, 30.0, True)
    transcribe.transcribe(str(video), info)
    os.utime(video, (1000, 1000))
    assert transcribe.transcribe(str(video), info).words
    assert len(calls) == 1


def test_a_refetched_link_keeps_its_paid_transcript(tmp_path, monkeypatch):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    done = {"id": "abc", "title": "Talk", "extractor_key": "Youtube", "requested_downloads": [{"filepath": str(video)}]}
    _fake_ydl(monkeypatch, {"id": "abc"}, done)
    monkeypatch.setattr(settings, "deepgram_api_key", "key")
    monkeypatch.setattr(transcribe, "run_ffmpeg", lambda args: Path(args[-1]).write_bytes(b"a"))
    calls = []
    monkeypatch.setattr(transcribe, "_request", lambda path: calls.append(path) or PAYLOAD)
    info = MediaInfo(60.0, 1280, 720, 30.0, True)
    for _ in range(3):
        os.utime(video, (1000, 1000))  # the second fetch must have something to move
        transcribe.transcribe(ingest.fetch("https://youtube.com/watch?v=abc").path, info)
    assert len(calls) == 1


def test_a_file_replaced_by_one_of_the_same_size_is_transcribed_again(tmp_path, monkeypatch):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"one")
    monkeypatch.setattr(settings, "deepgram_api_key", "key")
    monkeypatch.setattr(transcribe, "run_ffmpeg", lambda args: Path(args[-1]).write_bytes(b"a"))
    calls = []
    monkeypatch.setattr(transcribe, "_request", lambda path: calls.append(path) or PAYLOAD)
    info = MediaInfo(60.0, 1280, 720, 30.0, True)
    transcribe.transcribe(str(video), info)
    video.write_bytes(b"two")
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


@pytest.mark.skipif(
    os.name == "nt" or getattr(os, "geteuid", lambda: -1)() == 0,
    reason="Windows chmod and POSIX root ignore directory write bits",
)
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
    monkeypatch.setattr(job, "pick_visual", lambda *a, **k: list(moment_list or [Moment(10, 40, title="Clip one", ai_title=True, kind="visual")]))

    def fake_render(src, info, signals, moment, words, out_path, edit=None, creator="", music=None, captions=True, source_captioned=False):
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

    def spy(src, info, signals, moment, words, out_path, edit=None, creator="", music=None, captions=True, source_captioned=False):
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

    def flaky(src, info, signals, moment, words, out_path, edit=None, creator="", music=None, captions=True, source_captioned=False):
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

    def flaky(src, info, signals, moment, words, out_path, edit=None, creator="", music=None, captions=True, source_captioned=False):
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


@pytest.mark.parametrize("transcript, language", [
    (_speech(language="hi"), "hi"),
    (_speech(language="EN"), "en"),
    (_speech(language="hi", per_minute=10), ""),  # a guess from lyrics or noise is no fact about the video
    (_speech(language="hi", confidence=0.2), ""),
    (Transcript(language="hi"), ""),
])
def test_spoken_language_is_only_reported_for_real_speech(transcript, language):
    assert transcript.spoken_language == language


def test_a_non_english_video_is_recorded_as_such_and_held_back_from_auto_queue(tmp_path, monkeypatch):
    _stub_pipeline(monkeypatch)
    monkeypatch.setattr(job, "transcribe", lambda path, info: _speech(language="hi"))
    result = job.clip_source(_source(tmp_path, "hindi"), auto_queue=True)
    assert result["clips"]
    with session_scope() as db:
        video = db.get(Video, result["clips"][0]["id"])
        assert video.language == "hi" and video.status == VideoStatus.PENDING
        assert video.meta["growth_flagged"] == "language: hi"


# ---------- Cutting more from a source already used ----------
def test_dedupe_steers_around_used_footage():
    kept = moments._dedupe([Moment(0, 30, score=9), Moment(40, 70, score=8), Moment(80, 110, score=7)], 2,
                           used=[Moment(10, 20)])
    assert [m.start for m in kept] == [40, 80]


def test_rules_give_the_next_best_speech_moments_when_the_best_are_used(monkeypatch):
    monkeypatch.setattr(ai, "ask_json", lambda *a, **k: None)
    words, signals = _long_transcript(60), _signals(600)
    first = moments.pick_speech(words, signals, count=2)
    second = moments.pick_speech(words, signals, count=2, used=first)
    assert len(second) == 2
    assert not any(moments.clashes(a, b) for a in first for b in second)


def test_the_ai_is_told_what_is_used_and_asked_for_extra(monkeypatch):
    words, signals = _long_transcript(60), _signals(600)
    prompts = []
    sentences = build_sentences(words)
    used = Moment(sentences[4].start, sentences[9].end)

    def reply(prompt, **kwargs):
        prompts.append(prompt)
        return None

    monkeypatch.setattr(ai, "ask_json", reply)
    moments.pick_speech(words, signals, count=2, used=[used])
    assert "already" in prompts[0] and "4-9" in prompts[0]
    assert "up to 6 clips" in prompts[0]  # (2 wanted + 1 used) * 2


def test_the_prompt_is_unchanged_when_nothing_is_used():
    sentences = build_sentences(_long_transcript(10))
    assert "already" not in moments._speech_prompt(sentences, 2, "x")


def test_ai_picks_that_repeat_used_footage_lose_to_its_other_picks(monkeypatch):
    words, signals = _long_transcript(60), _signals(600)
    sentences = build_sentences(words)
    used = [Moment(sentences[2].start, sentences[8].end)]
    reply = {"clips": [{"first": 2, "last": 8, "title": "Again", "score": 99},
                       {"first": 20, "last": 26, "title": "Fresh", "score": 50},
                       {"first": 30, "last": 36, "title": "Also fresh", "score": 40},
                       {"first": 40, "last": 46, "title": "Over the cap", "score": 30}]}
    monkeypatch.setattr(ai, "ask_json", lambda *a, **k: reply)
    picked = moments.pick_speech(words, signals, count=2, used=used)
    assert [m.title for m in picked] == ["Fresh", "Also fresh"]


def test_rank_windows_skips_used_footage():
    signals = _signals(300, loud_at=(150, 180))
    best = moments.rank_windows(signals, 1)[0]
    picked = moments.rank_windows(signals, 3, used=[best])
    assert len(picked) == 3
    assert not any(moments.clashes(m, best) for m in picked)


def test_visual_path_gives_the_next_best_without_ai(monkeypatch):
    monkeypatch.setattr(ai, "available", lambda: False)
    signals = _signals(300, loud_at=(150, 180))
    first = moments.pick_visual("x.mp4", INFO, signals, count=2, title="Film")
    second = moments.pick_visual("x.mp4", INFO, signals, count=2, title="Film", used=first)
    assert [m.title for m in second] == ["Film - part 3", "Film - part 4"]  # a second "part 1" would repeat a title
    assert not any(moments.clashes(a, b) for a in first for b in second)


def test_the_vision_prompt_names_the_used_ranges(monkeypatch):
    sent = []
    monkeypatch.setattr(ai, "available", lambda: True)
    monkeypatch.setattr(ai, "ask_json", lambda prompt, **kwargs: sent.append(prompt))
    monkeypatch.setattr(moments, "_frame_strip", lambda *a: None)
    moments.pick_visual("x.mp4", INFO, _signals(300), count=1, title="Film", used=[Moment(10, 40)])
    assert "10-40s" in sent[0]


def test_visual_path_with_nothing_left_asks_nobody(monkeypatch):
    monkeypatch.setattr(ai, "available", lambda: True)
    monkeypatch.setattr(moments, "_ai_rank_visual", lambda *a, **k: pytest.fail("nothing to rate"))
    assert moments.pick_visual("x.mp4", INFO, _signals(10), count=2, used=[Moment(0, 10)]) == []


def _real_visual_picks(monkeypatch):
    _stub_pipeline(monkeypatch)
    monkeypatch.setattr(job, "analyse", lambda path, info: _signals(300, loud_at=(150, 180)))
    monkeypatch.setattr(job, "pick_visual", moments.pick_visual)
    monkeypatch.setattr(ai, "available", lambda: False)


def _set_status(result, status):
    with session_scope() as db:
        for clip in result["clips"]:
            db.get(Video, clip["id"]).status = status


def _starts(result):
    return sorted(clip["start"] for clip in result["clips"])


@pytest.mark.parametrize("status", [VideoStatus.PUBLISHED, VideoStatus.TRANSFORMED, VideoStatus.REJECTED])
def test_mining_a_used_source_makes_new_clips(tmp_path, monkeypatch, status):
    _real_visual_picks(monkeypatch)
    first = job.clip_source(_source(tmp_path, f"mine{status.value}"), count=2)
    _set_status(first, status)
    second = job.clip_source(_source(tmp_path, f"mine{status.value}"), count=2, mine=True)
    assert second["ok"] and len(second["clips"]) == 2
    assert set(_starts(first)).isdisjoint(_starts(second))
    assert not any("left as it is" in w for w in second["warnings"])


def test_mining_speech_without_ai_makes_new_clips(tmp_path, monkeypatch):
    _stub_pipeline(monkeypatch)
    words = _long_transcript(60)
    monkeypatch.setattr(job, "transcribe", lambda path, info: Transcript(words=words, language="en", duration=words[-1].end))
    monkeypatch.setattr(job, "probe", lambda path: MediaInfo(duration=600.0, width=1280, height=720, fps=30.0, has_audio=True))
    monkeypatch.setattr(job, "analyse", lambda path, info: _signals(600))
    monkeypatch.setattr(job, "pick_speech", moments.pick_speech)
    monkeypatch.setattr(job, "build_edit", lambda *a, **k: None)
    monkeypatch.setattr(ai, "ask_json", lambda *a, **k: None)
    first = job.clip_source(_source(tmp_path, "minespeech"), count=2)
    _set_status(first, VideoStatus.PUBLISHED)
    second = job.clip_source(_source(tmp_path, "minespeech"), count=2, mine=True)
    assert second["kind"] == "speech" and len(second["clips"]) == 2
    assert set(_starts(first)).isdisjoint(_starts(second))


def test_an_unreviewed_clip_still_renders_again_rather_than_being_passed_over(tmp_path, monkeypatch):
    _real_visual_picks(monkeypatch)
    first = job.clip_source(_source(tmp_path, "pending"), count=2)
    second = job.clip_source(_source(tmp_path, "pending"), count=2)
    assert _starts(first) == _starts(second)


def test_a_stocked_clip_whose_file_is_gone_can_be_picked_again(tmp_path, monkeypatch):
    _real_visual_picks(monkeypatch)
    first = job.clip_source(_source(tmp_path, "lost"), count=2, auto_queue=True)
    for clip in first["clips"]:
        Path(clip["path"]).unlink()
    second = job.clip_source(_source(tmp_path, "lost"), count=2, auto_queue=True)
    assert _starts(first) == _starts(second)
    assert all(Path(clip["path"]).exists() for clip in second["clips"])


def test_a_spent_speech_source_finishes_without_cutting_silent_footage(tmp_path, monkeypatch):
    _stub_pipeline(monkeypatch)
    words = _long_transcript(60)
    monkeypatch.setattr(job, "transcribe", lambda path, info: Transcript(words=words, language="en", duration=words[-1].end))
    monkeypatch.setattr(job, "pick_speech", lambda *a, **k: [Moment(1, 30, kind="speech", title="Only")])
    monkeypatch.setattr(job, "build_edit", lambda *a, **k: None)
    first = job.clip_source(_source(tmp_path, "spent"), count=1)
    _set_status(first, VideoStatus.PUBLISHED)
    monkeypatch.setattr(job, "pick_speech", lambda *a, **k: [])
    monkeypatch.setattr(job, "pick_visual", lambda *a, **k: pytest.fail("this source's speech has all been used"))
    second = job.clip_source(_source(tmp_path, "spent"), count=1, mine=True)
    assert second["ok"] and second["clips"] == []
    assert any("already been used" in w for w in second["warnings"])


def test_a_source_cut_visually_can_still_be_cut_visually_again(tmp_path, monkeypatch):
    _real_visual_picks(monkeypatch)
    first = job.clip_source(_source(tmp_path, "visualagain"), count=1)
    _set_status(first, VideoStatus.PUBLISHED)
    words = _long_transcript(60)
    monkeypatch.setattr(job, "transcribe", lambda path, info: Transcript(words=words, language="en", duration=words[-1].end))
    monkeypatch.setattr(job, "pick_speech", lambda *a, **k: [])
    second = job.clip_source(_source(tmp_path, "visualagain"), count=1, mine=True)
    assert second["kind"] == "visual" and len(second["clips"]) == 1
    assert any("too little continuous speech" in w for w in second["warnings"])


def test_a_visual_source_with_nothing_left_is_not_a_failure(tmp_path, monkeypatch):
    _stub_pipeline(monkeypatch)
    monkeypatch.setattr(job, "analyse", lambda path, info: _signals(10))
    monkeypatch.setattr(job, "pick_visual", moments.pick_visual)
    monkeypatch.setattr(ai, "available", lambda: False)
    first = job.clip_source(_source(tmp_path, "shortone"), count=1)
    _set_status(first, VideoStatus.PUBLISHED)
    second = job.clip_source(_source(tmp_path, "shortone"), count=1, mine=True)
    assert second["ok"] and second["clips"] == []


# ---------- a job's count across restarts ----------
def _queue_setup(tmp_path, monkeypatch, source_id):
    _clear_jobs()
    _real_visual_picks(monkeypatch)
    # These tests are about stopping and resuming, not about which clips get held for review.
    monkeypatch.setattr(job, "_untitled_reason", lambda src, moment: None)
    src = _source(tmp_path, source_id)
    monkeypatch.setattr(job, "fetch", lambda link: src)
    return src


def _stop_render_on(monkeypatch, *stopped_calls):
    calls = []
    real = job.render_moment

    def render_or_stop(*args, **kwargs):
        calls.append(args[3])
        if len(calls) in stopped_calls:
            raise Interrupted("ffmpeg was stopped")
        return real(*args, **kwargs)

    monkeypatch.setattr(job, "render_moment", render_or_stop)
    return calls


def _rows_of(src):
    with session_scope() as db:
        rows = db.execute(select(Video).where(Video.source_id.startswith(f"{src.source_id}_"))).scalars().all()
        return [(r.id, r.status) for r in rows]


def test_a_stopped_job_finishes_its_count_rather_than_starting_over(tmp_path, monkeypatch):
    src = _queue_setup(tmp_path, monkeypatch, "stopped3")
    calls = _stop_render_on(monkeypatch, 3)
    job_id = job.enqueue("https://example.com/v", count=3, auto_queue=True)
    job.clip_queue_job()
    assert len(_rows_of(src)) == 2
    job.clip_queue_job()
    assert len(_rows_of(src)) == 3 and len(calls) == 4
    with session_scope() as db:
        row = db.get(ClipJob, job_id)
        assert row.status == "done"
        # The result answers for the whole job, not for its last attempt.
        assert {c["id"] for c in row.result["clips"]} == {video_id for video_id, _ in _rows_of(src)}
        assert row.result["queued"] is True


def test_a_job_stopped_repeatedly_still_makes_only_its_count(tmp_path, monkeypatch):
    src = _queue_setup(tmp_path, monkeypatch, "stoppedtwice")
    _stop_render_on(monkeypatch, 2, 4)
    job.enqueue("https://example.com/v", count=3, auto_queue=True)
    job.clip_queue_job()
    job.clip_queue_job()
    job.clip_queue_job()
    assert len(_rows_of(src)) == 3


def test_a_job_requeued_after_its_last_clip_does_no_more_work(tmp_path, monkeypatch):
    src = _queue_setup(tmp_path, monkeypatch, "crashfinish")
    job_id = job.enqueue("https://example.com/v", count=2, auto_queue=True)
    job.clip_queue_job()
    with session_scope() as db:  # as if the process died before the outcome was saved
        row = db.get(ClipJob, job_id)
        row.status, row.result = "running", {}
    monkeypatch.setattr(job, "render_moment", lambda *a, **k: pytest.fail("rendered again"))
    monkeypatch.setattr(job, "transcribe", lambda *a: pytest.fail("transcribed again"))
    job.clip_queue_job()
    with session_scope() as db:
        row = db.get(ClipJob, job_id)
        assert row.status == "done" and len(row.result["clips"]) == 2
    assert len(_rows_of(src)) == 2


@pytest.mark.parametrize("auto_queue", [True, False])
def test_pasting_a_link_again_makes_nothing_new(tmp_path, monkeypatch, auto_queue):
    src = _queue_setup(tmp_path, monkeypatch, f"repaste{auto_queue}")
    job.enqueue("https://example.com/v", count=3, auto_queue=auto_queue)
    job.clip_queue_job()
    made = {video_id for video_id, _ in _rows_of(src)}
    if not auto_queue:
        with session_scope() as db:
            for video_id in made:
                db.get(Video, video_id).status = VideoStatus.APPROVED
    job.enqueue("https://example.com/v", count=3, auto_queue=auto_queue)
    job.clip_queue_job()
    assert {video_id for video_id, _ in _rows_of(src)} == made


def test_a_stopped_mining_job_counts_its_own_clips_and_still_avoids_them(tmp_path, monkeypatch):
    src = _queue_setup(tmp_path, monkeypatch, "minestop")
    first = job.clip_source(src, count=2, auto_queue=True)
    _stop_render_on(monkeypatch, 2)
    with session_scope() as db:
        db.add(ClipJob(source="https://example.com/v", count=2, auto_queue=True, mined=True))
    job.clip_queue_job()
    assert len(_rows_of(src)) == 3
    job.clip_queue_job()
    assert len(_rows_of(src)) == 4
    with session_scope() as db:
        rows = db.execute(select(Video).where(Video.source_id.startswith(f"{src.source_id}_"))).scalars().all()
        spans = [Moment(r.meta["clip_start"], r.meta["clip_end"]) for r in rows]
        mined = db.execute(select(ClipJob).where(ClipJob.mined.is_(True))).scalar_one()
        assert len(mined.result["clips"]) == 2
    assert not any(moments.clashes(a, b) for i, a in enumerate(spans) for b in spans[i + 1:])
    assert {c["id"] for c in first["clips"]}.isdisjoint(c["id"] for c in mined.result["clips"])


def _speech_pipeline(monkeypatch):
    _stub_pipeline(monkeypatch)
    words = _long_transcript(60)
    monkeypatch.setattr(job, "transcribe", lambda path, info: Transcript(words=words, language="en", duration=words[-1].end))
    monkeypatch.setattr(job, "probe", lambda path: MediaInfo(duration=600.0, width=1280, height=720, fps=30.0, has_audio=True))
    monkeypatch.setattr(job, "analyse", lambda path, info: _signals(600))
    monkeypatch.setattr(job, "pick_speech", moments.pick_speech)
    monkeypatch.setattr(job, "build_edit", lambda *a, **k: None)
    return words


def test_mining_steers_around_clips_awaiting_review(tmp_path, monkeypatch):
    words = _speech_pipeline(monkeypatch)
    sentences = build_sentences(words)
    monkeypatch.setattr(ai, "ask_json", lambda *a, **k: None)
    first = job.clip_source(_source(tmp_path, "minepending"), count=3)
    _set_status({"clips": first["clips"][:1]}, VideoStatus.PUBLISHED)  # the rest stay PENDING, as a flagged clip does
    pending = [Moment(c["start"], c["end"]) for c in first["clips"][1:]]

    def inside(moment):
        return [s.index for s in sentences if moments.clashes(Moment(s.start, s.end), moment)]

    prompts = []

    def reply(prompt, **kwargs):
        prompts.append(prompt)
        # Near repeats of the clips under review, ranked ahead of what is actually free.
        near = [{"first": inside(m)[0] + 1, "last": inside(m)[-1] + 1, "title": "Repeat", "score": 99} for m in pending]
        free = [{"first": 40 + 6 * n, "last": 45 + 6 * n, "title": f"New {n}", "score": 50} for n in range(3)]
        return {"clips": near + free}

    monkeypatch.setattr(ai, "ask_json", reply)
    second = job.clip_source(_source(tmp_path, "minepending"), count=2, mine=True)
    assert second["ok"] and len(second["clips"]) == 2
    assert not any("repeats clip" in w for w in second["warnings"])
    assert all(f"{inside(m)[0]}-{inside(m)[-1]}" in prompts[0] for m in pending)
    assert not any(moments.clashes(Moment(c["start"], c["end"]), m) for c in second["clips"] for m in pending)


# ---------- the prompt and the filter agree at clip edges ----------
@pytest.mark.parametrize("gap", [0.0, 0.05, 0.1, 0.15, 0.2])
def test_the_sentence_after_a_used_clip_really_is_free(monkeypatch, gap):
    words = []
    now = 10.0
    for index in range(30):
        batch = _words(f"Sentence number {index} says something rather useful right here.", start=now)
        words += batch
        now = batch[-1].end + gap
    sentences = build_sentences(words)
    reply = {"clips": [{"first": 0, "last": 8, "title": "One", "score": 50}]}
    used = moments._moments_from_reply(reply, sentences, words, 600.0)[0]
    assert "0-8" in moments._used_sentences(sentences, [used])
    monkeypatch.setattr(ai, "ask_json", lambda *a, **k: {"clips": [{"first": 9, "last": 17, "title": "Two", "score": 50}]})
    picked = moments.pick_speech(words, _signals(600), count=1, used=[used])
    assert picked and picked[0].title == "Two"


def test_clips_sharing_real_footage_still_clash():
    assert moments.clashes(Moment(0, 30), Moment(29, 60))
    assert not moments.clashes(Moment(0, 30), Moment(30, 60))


# ---------- What a failure means for the source ----------
def _failing_ydl(monkeypatch, message):
    class Refused:
        def __init__(self, options):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False
        def extract_info(self, url, download):
            raise RuntimeError(message)
    import yt_dlp
    monkeypatch.setattr(yt_dlp, "YoutubeDL", Refused)


def test_a_download_failure_keeps_its_real_cause(monkeypatch):
    _failing_ydl(monkeypatch, "HTTP Error 503: Service Unavailable")
    with pytest.raises(ingest.IngestError, match="download failed: HTTP Error 503") as caught:
        ingest.fetch("https://youtube.com/watch?v=x")
    assert caught.value.permanent is False


@pytest.mark.parametrize("message", [
    "ERROR: [youtube] x: Private video. Sign in if you've been granted access to this video. Use --cookies-from-browser",
    "ERROR: [youtube] x: This video has been removed by the uploader",
    "ERROR: [youtube] x: Video unavailable",
    "ERROR: [youtube] x: This video is no longer available because the YouTube account associated with it has been terminated.",
    "ERROR: [youtube] x: Premieres in 3 hours",
    "ERROR: [youtube] x: This live event will begin in a few moments.",
])
def test_a_source_that_is_gone_is_final(monkeypatch, message):
    _failing_ydl(monkeypatch, message)
    with pytest.raises(ingest.IngestError) as caught:
        ingest.fetch("https://youtube.com/watch?v=x")
    assert caught.value.permanent is True
    assert "COOKIES_FROM_BROWSER" not in str(caught.value)


@pytest.mark.parametrize("message", [
    "Sign in to confirm you're not a bot. Use --cookies-from-browser",
    "Video unavailable. This content isn't available, try again later. The current session has been rate-limited",
    "Video unavailable. The uploader has not made this video available in your country",
    "HTTP Error 429: Too Many Requests",
])
def test_a_block_on_this_host_is_not_final(monkeypatch, message):
    _failing_ydl(monkeypatch, message)
    with pytest.raises(ingest.IngestError) as caught:
        ingest.fetch("https://youtube.com/watch?v=x")
    assert caught.value.permanent is False


@pytest.mark.parametrize("info", [{"id": "x", "is_live": True}, {"id": "x", "live_status": "is_upcoming"}])
def test_a_live_or_upcoming_video_is_final(monkeypatch, info):
    _fake_ydl(monkeypatch, info, None)
    with pytest.raises(ingest.IngestError) as caught:
        ingest.fetch("https://youtube.com/watch?v=x")
    assert caught.value.permanent is True


def test_clip_source_marks_a_final_failure(monkeypatch):
    def gone(source):
        raise ingest.IngestError("private", permanent=True)

    monkeypatch.setattr(job, "require_tools", lambda: None)
    monkeypatch.setattr(job, "fetch", gone)
    result = job.clip_source("https://example.com/v")
    assert result["ok"] is False and result["permanent"] is True


def test_clip_source_does_not_mark_a_failure_that_may_clear_up(monkeypatch):
    def down(source):
        raise ingest.IngestError("download failed: 503")

    monkeypatch.setattr(job, "require_tools", lambda: None)
    monkeypatch.setattr(job, "fetch", down)
    assert "permanent" not in job.clip_source("https://example.com/v")

    def missing():
        raise MediaError("ffmpeg not found on PATH")

    monkeypatch.setattr(job, "require_tools", missing)
    assert "permanent" not in job.clip_source("https://example.com/v")


def test_no_usable_moments_is_final(tmp_path, monkeypatch):
    _stub_pipeline(monkeypatch)
    monkeypatch.setattr(job, "pick_visual", lambda *a, **k: [])
    result = job.clip_source(_source(tmp_path, "empty"))
    assert result["ok"] is False and result["permanent"] is True


@pytest.mark.parametrize("source", ["~nosuchuser_zz/x.mp4", "a\x00b.mp4"])
def test_a_path_that_cannot_be_read_fails_on_its_job_row(monkeypatch, source):
    monkeypatch.setattr(job, "require_tools", lambda: None)
    job_id = job.enqueue(source)
    job.clip_queue_job()
    with session_scope() as db:
        row = db.get(ClipJob, job_id)
        assert row.status == "failed" and row.error
        assert "fingerprint" not in row.result


def test_queueing_a_local_file_records_which_bytes_it_was(tmp_path):
    clip = tmp_path / "a.mp4"
    clip.write_bytes(b"x")
    job_id = job.enqueue(str(clip))
    with session_scope() as db:
        assert db.get(ClipJob, job_id).result["fingerprint"] == ingest.fetch(str(clip)).source_id
    before = ingest.file_fingerprint(clip)
    clip.write_bytes(b"xx")
    assert ingest.file_fingerprint(clip) != before


# ---------- A transcription outage ----------
def _deepgram_down(monkeypatch, exc=None):
    _stub_pipeline(monkeypatch)
    monkeypatch.setattr(settings, "deepgram_api_key", "key")

    def broken(path, info):
        raise exc or transcribe.TranscribeError("Deepgram returned 402")

    monkeypatch.setattr(job, "transcribe", broken)


def _transcribe_events():
    with session_scope() as db:
        return db.execute(select(GrowthEvent).where(GrowthEvent.kind == "transcribe_failed")).scalars().all()


def _clear_events():
    with session_scope() as db:
        db.query(GrowthEvent).delete()


def test_a_deepgram_outage_fails_an_unattended_job_and_saves_nothing(tmp_path, monkeypatch):
    _clear_events()
    _deepgram_down(monkeypatch)
    monkeypatch.setattr(job, "render_moment", lambda *a, **k: pytest.fail("rendered without a transcript"))
    result = job.clip_source(_source(tmp_path, "outage"), auto_queue=True)
    assert result["ok"] is False and result["clips"] == [] and "402" in result["error"]
    assert "permanent" not in result
    with session_scope() as db:
        assert db.execute(select(Video).where(Video.source_id.startswith("outage"))).first() is None
    assert len(_transcribe_events()) == 1 and "402" in _transcribe_events()[0].summary


def test_an_outage_event_does_not_promise_a_retry_nobody_will_make(tmp_path, monkeypatch):
    _clear_events()
    _deepgram_down(monkeypatch)
    job.clip_source(_source(tmp_path, "by_hand"), auto_queue=True)
    summary = _transcribe_events()[0].summary
    assert "will retry" not in summary and "by hand" in summary


def test_a_long_outage_records_one_event_a_day(tmp_path, monkeypatch):
    _clear_events()
    _deepgram_down(monkeypatch)
    for name in ("first", "second"):
        job.clip_source(_source(tmp_path, name), auto_queue=True)
    assert len(_transcribe_events()) == 1


def test_a_deepgram_outage_only_warns_on_a_manual_run(tmp_path, monkeypatch):
    _clear_events()
    _deepgram_down(monkeypatch)
    result = job.clip_source(_source(tmp_path, "manual"), auto_queue=False)
    assert result["ok"] is True and result["clips"]
    assert any("402" in w for w in result["warnings"])
    assert _transcribe_events() == []


def test_a_failed_audio_extract_is_not_a_deepgram_outage(tmp_path, monkeypatch):
    _clear_events()
    _deepgram_down(monkeypatch, MediaError("could not read the audio"))
    result = job.clip_source(_source(tmp_path, "audio"), auto_queue=True)
    assert result["ok"] is True and result["clips"]
    assert _transcribe_events() == []


def test_no_key_stays_on_the_no_speech_path_when_unattended(tmp_path, monkeypatch):
    _clear_events()
    _stub_pipeline(monkeypatch)
    monkeypatch.setattr(settings, "deepgram_api_key", "")
    result = job.clip_source(_source(tmp_path, "nokey2"), auto_queue=True)
    assert result["ok"] is True and result["kind"] == "visual" and result["clips"]
    assert _transcribe_events() == []


# ---------- A clip titled only by a filename ----------
def _inbox_file(tmp_path):
    clip = tmp_path / "VID_20260101_final2.mp4"
    clip.write_bytes(b"x" * 2000)
    return str(clip)


def _saved(result):
    with session_scope() as db:
        video = db.get(Video, result["clips"][0]["id"])
        return video.status, dict(video.meta)


def test_a_filename_title_with_no_written_title_waits_for_review(tmp_path, monkeypatch):
    _stub_pipeline(monkeypatch, [Moment(10, 40, title="VID 20260101 final2 - part 1", kind="visual")])
    status, meta = _saved(job.clip_source(_inbox_file(tmp_path), auto_queue=True))
    assert status == VideoStatus.PENDING
    assert "filename" in meta["growth_flagged"]


def test_a_filename_source_with_an_ai_title_stays_unattended(tmp_path, monkeypatch):
    _stub_pipeline(monkeypatch, [Moment(10, 40, title="Dog catches frisbee", kind="visual", ai_title=True)])
    status, meta = _saved(job.clip_source(_inbox_file(tmp_path), auto_queue=True))
    assert status == VideoStatus.TRANSFORMED and "growth_flagged" not in meta


def test_a_visual_clip_no_ai_titled_waits_for_review_even_with_a_real_source_title(tmp_path, monkeypatch):
    # Rule-based picks took a film's end credits and titled them "<film> - part 2".
    _stub_pipeline(monkeypatch, [Moment(10, 40, title="Talk - part 1", kind="visual")])
    status, meta = _saved(job.clip_source(_source(tmp_path, "titled"), auto_queue=True))
    assert status == VideoStatus.PENDING and "no AI" in meta["growth_flagged"]


def test_a_file_source_says_its_title_is_a_filename(tmp_path):
    assert ingest.fetch(_inbox_file(tmp_path)).title_from_filename is True


def test_only_a_written_title_counts_as_written(monkeypatch):
    info = MediaInfo(duration=600.0, width=1280, height=720, fps=30.0, has_audio=True)
    monkeypatch.setattr(ai, "available", lambda: False)
    plain = moments.pick_visual("x.mp4", info, _signals(600), count=1, title="Talk")
    assert plain[0].ai_title is False
    monkeypatch.setattr(ai, "available", lambda: True)
    monkeypatch.setattr(moments, "_ai_rank_visual", lambda *a, **k: {"clips": [
        {"candidate": 1, "title": "A dog", "score": 90}, {"candidate": 2, "title": "", "score": 80}]})
    rated = moments.pick_visual("x.mp4", info, _signals(600), count=2, title="Talk")
    assert [m.ai_title for m in rated] == [True, False]
    words = _long_transcript()
    sentences = build_sentences(words)
    reply = {"clips": [{"first": 0, "last": 3, "title": "From the AI", "score": 90}]}
    assert moments._moments_from_reply(reply, sentences, words, 300.0)[0].ai_title is True
    assert moments._rule_based_speech(sentences, words, _signals(300), 1)[0].ai_title is False


# ---------- The line a quote title may use ----------
LINES = "Nobody told me this at all. I found out much later on. That is the whole story here."


def _plan(words):
    return edit.EditPlan(words=list(words), segments=[])


def test_the_opening_line_is_the_first_sentence_word_for_word():
    words = _words(LINES)
    moment = Moment(0, words[-1].end, kind="speech")
    assert edit.opening_line(moment, words, _plan(words)) == "Nobody told me this at all."


def test_the_opening_line_follows_what_the_edit_kept():
    words = _words(LINES)
    moment = Moment(0, words[-1].end, kind="speech")
    assert edit.opening_line(moment, words, _plan(words[6:])) == "I found out much later on."


def test_a_tidied_word_does_not_change_the_quoted_sentence():
    words = _words("Um, nobody told me this. I found out later on.")
    moment = Moment(0, words[-1].end, kind="speech")
    assert edit.opening_line(moment, words, _plan(words[1:])) == "Um, nobody told me this."


def test_a_moment_opening_mid_sentence_has_no_opening_line():
    words = _words(LINES)
    moment = Moment(words[3].start, words[-1].end, kind="speech")
    assert edit.opening_line(moment, words, _plan(words[3:])) == ""


def test_an_unfinished_sentence_has_no_opening_line():
    words = _words("nobody told me this at all", length=0.3)
    moment = Moment(0, words[-1].end, kind="speech")
    assert edit.opening_line(moment, words, _plan(words)) == ""


def test_no_edit_or_no_words_has_no_opening_line():
    words = _words(LINES)
    moment = Moment(0, words[-1].end, kind="speech")
    assert edit.opening_line(moment, words, None) == ""
    assert edit.opening_line(moment, words, _plan([])) == ""


def _talking_pipeline(monkeypatch, recipe=None, *, audio=True):
    """A speech clip through the real edit, up to a render that reports effects the way the real plan does."""
    _stub_pipeline(monkeypatch)
    words = _words(LINES)
    info = MediaInfo(duration=600.0, width=1280, height=720, fps=30.0, has_audio=audio)
    moment = Moment(0, words[-1].end, title="Talk", hook="Made up by the AI", ai_title=True, kind="speech")
    monkeypatch.setattr(job, "transcribe", lambda path, _: _SpeechTranscript(words))
    monkeypatch.setattr(job, "probe", lambda path: info)
    monkeypatch.setattr(job, "pick_speech", lambda *a, **k: [moment])
    monkeypatch.setattr(ai, "ask_json", lambda *a, **k: None)
    monkeypatch.setattr(job.recipes, "choose", lambda: dict(recipe or {}))

    def fake_render(src, info, signals, moment, words, out_path, edit=None, creator="", music=None, captions=True, source_captioned=False):
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_bytes(b"v" * 2000)
        return render.ClipPlan([(moment.start, moment.end)], moment.duration, True, 404, "0", False, "1", False,
                               has_sfx=bool(edit) and info.has_audio and settings.clip_sfx)

    monkeypatch.setattr(job, "render_moment", fake_render)


class _SpeechTranscript(Transcript):
    def __init__(self, words):
        super().__init__(words=words, language="en", duration=words[-1].end)

    @property
    def is_english_speech(self):
        return True


def test_a_speech_clip_stores_its_opening_line_and_keeps_the_hook_apart(tmp_path, monkeypatch):
    _talking_pipeline(monkeypatch)
    _, meta = _saved(job.clip_source(_source(tmp_path, "line")))
    assert meta["clip_opening_line"] == "Nobody told me this at all."
    assert meta["clip_hook"] == "Made up by the AI"


def test_a_picked_moment_keeps_its_first_sentence_as_the_opening_line():
    words = _long_transcript()
    sentences = build_sentences(words)
    reply = {"clips": [{"first": 2, "last": 6, "title": "T", "score": 90}]}
    moment = moments._moments_from_reply(reply, sentences, words, 300.0)[0]
    plan = edit.EditPlan(words=[w for w in words if moment.start <= w.start < moment.end], segments=[])
    assert edit.opening_line(moment, words, plan) == sentences[2].text


def test_a_visual_clip_has_no_opening_line(tmp_path, monkeypatch):
    _stub_pipeline(monkeypatch)
    _, meta = _saved(job.clip_source(_source(tmp_path, "quiet")))
    assert not meta.get("clip_opening_line")


# ---------- The recipe says what the clip got ----------
RECIPE = {"pace": "normal", "sfx": "on", "broll": "on"}


@pytest.mark.parametrize("switch, audio, recorded", [(True, True, "on"), (False, True, "off"), (True, False, "off")])
def test_sfx_is_recorded_as_rendered(tmp_path, monkeypatch, switch, audio, recorded):
    _talking_pipeline(monkeypatch, RECIPE, audio=audio)
    monkeypatch.setattr(settings, "clip_sfx", switch)
    _, meta = _saved(job.clip_source(_source(tmp_path, f"sfx{switch}{audio}")))
    assert meta["growth_recipe"]["sfx"] == recorded


def test_a_recipe_asking_for_no_effects_is_recorded_off(tmp_path, monkeypatch):
    _talking_pipeline(monkeypatch, {**RECIPE, "sfx": "off"})
    monkeypatch.setattr(settings, "clip_sfx", True)
    _, meta = _saved(job.clip_source(_source(tmp_path, "sfxoff")))
    assert meta["growth_recipe"]["sfx"] == "off"


def test_a_lost_file_rendered_again_records_its_new_recipe_and_line(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "clip_sfx", True)
    _talking_pipeline(monkeypatch, {"pace": "tight", "sfx": "on", "broll": "off"})
    source = _source(tmp_path, "lost")
    first = job.clip_source(source, auto_queue=True)
    video_id = first["clips"][0]["id"]
    with session_scope() as db:
        video = db.get(Video, video_id)
        assert video.status == VideoStatus.TRANSFORMED
        assert video.meta["growth_recipe"]["pace"] == "tight"
        video.meta = {**video.meta, "clip_opening_line": "stale"}
    os.unlink(first["clips"][0]["path"])
    monkeypatch.setattr(job.recipes, "choose", lambda: {"pace": "relaxed", "sfx": "off", "broll": "off"})
    job.clip_source(source, auto_queue=True)
    with session_scope() as db:
        meta = db.get(Video, video_id).meta
    assert meta["growth_recipe"] == {"pace": "relaxed", "sfx": "off", "broll": "off"}
    assert meta["clip_opening_line"] == "Nobody told me this at all."


def test_creator_credit_lasts_the_whole_clip_and_is_escaped():
    from app.clipper.captions import build_ass

    ass = build_ass([], "", 30.0, "Arial", creator="{\\an8}Some Creator")
    line = next(l for l in ass.splitlines() if ",Credit,," in l)
    assert line.startswith("Dialogue: 0,0:00:00.00,0:00:30.00,Credit")
    assert "Credit: (an8)Some Creator" in line
    assert ",Credit,," not in build_ass([], "", 30.0, "Arial")


def test_channel_branding_shows_only_when_named():
    from app.clipper.captions import build_ass

    assert ",Brand,," not in build_ass([], "", 30.0, "Arial")
    line = next(l for l in build_ass([], "", 30.0, "Arial", brand="@MyChannel").splitlines() if ",Brand,," in l)
    assert line.endswith("@MyChannel") and "0:00:30.00" in line


def test_framed_text_sits_in_the_bands_around_a_wide_picture():
    from app.clipper.captions import framed_layout

    layout = framed_layout(608)  # 16:9, so the picture spans y=656 to y=1264
    assert 1264 < layout.credit_y < layout.caption_y <= 1440
    assert layout.hook_y < layout.brand_y < 656


def test_framed_text_stays_clear_of_the_shorts_buttons_on_a_tall_picture():
    from app.clipper.captions import framed_layout

    layout = framed_layout(1080)  # square
    assert layout.caption_y <= 1440 and layout.credit_y <= 1530 and layout.hook_y >= 200


@pytest.mark.parametrize("height", [452, 608, 720, 810, 1080, 1350])
def test_framed_captions_never_land_on_the_credit(height):
    from app.clipper.captions import framed_layout

    layout = framed_layout(height)
    assert abs(layout.caption_y - layout.credit_y) >= 130, "two caption lines would cover the credit"
    assert layout.caption_y <= 1440
    assert layout.hook_y < layout.brand_y


def test_visual_picks_leave_out_the_closing_credits():
    busy_end = np.zeros(720, dtype=float)
    busy_end[650:] = 1.0  # scrolling names: plenty of motion
    busy_end[100:130] = 0.4

    class Long:
        duration = 720.0
        cuts: list = []

        @staticmethod
        def interest():
            return busy_end

    picks = moments.rank_windows(Long, 2)
    assert all(m.end <= 720 * 0.85 + 1 for m in picks)
    assert picks[0].start < 130


def test_a_short_source_can_still_use_its_ending():
    busy_end = np.zeros(120, dtype=float)
    busy_end[85:] = 1.0

    class Short:
        duration = 120.0
        cuts: list = []

        @staticmethod
        def interest():
            return busy_end

    assert moments.rank_windows(Short, 1)[0].end > 110


def test_a_visual_clip_no_ai_looked_at_is_held_back():
    src = ingest.SourceVideo(path="x", title="Real title", source="local", source_id="s")
    assert "no AI" in job._untitled_reason(src, moments.Moment(1, 30, kind="visual"))
    assert job._untitled_reason(src, moments.Moment(1, 30, kind="visual", ai_title=True)) is None
    assert "no AI" in job._untitled_reason(src, moments.Moment(1, 30, kind="speech"))
    assert job._untitled_reason(src, moments.Moment(1, 30, kind="speech", ai_title=True)) is None


def test_backdrop_is_blurred_by_default_and_can_be_sharp(monkeypatch):
    monkeypatch.setattr(settings, "clip_layout", "framed")
    plan = render.plan_clip(Moment(10, 40, kind="visual"), [], _signals(), INFO, has_graphics=True)
    assert "boxblur=14:1" in render.build_filtergraph(plan, INFO, 0.0)
    monkeypatch.setattr(settings, "clip_backdrop_blur", 0)
    assert "boxblur" not in render.build_filtergraph(plan, INFO, 0.0)


def test_source_time_undoes_output_time():
    from app.clipper.timeline import output_time, source_time

    segments = [(10.0, 14.0), (20.0, 25.0)]
    for when in (10.5, 13.9, 20.0, 24.0):
        assert source_time(segments, output_time(segments, when)) == pytest.approx(when)


def test_found_speaker_moves_the_framed_picture_onto_them(monkeypatch):
    monkeypatch.setattr(settings, "clip_layout", "framed")
    focus = [(0.0, 0.8, 0.3), (5.0, 0.2, 0.35)]
    plan = render.plan_clip(Moment(10, 40), [], _signals(), INFO, has_graphics=True, focus=focus)
    assert plan.focus_x_expr.startswith("0.8*") and "0.2*" in plan.focus_x_expr
    assert plan.zoom_expr.startswith("1.35+")
    graph = render.build_filtergraph(plan, INFO, 0.0)
    assert "(1080*(" in graph and plan.focus_x_expr in graph


def test_speaker_focus_only_applies_to_the_framed_layout():
    plan = render.plan_clip(Moment(10, 40), [], _signals(), INFO, has_graphics=True, focus=[(0.0, 0.8, 0.3)])
    assert plan.focus_x_expr == "" and not plan.zoom_expr.startswith("1.35")


def test_speaker_changes_only_where_the_framing_changes(monkeypatch):
    from types import SimpleNamespace

    sequence = iter([(0.2, 0.3), (0.21, 0.31), (0.8, 0.3)])
    monkeypatch.setattr(render, "speaker_position", lambda *a: next(sequence))
    edit = SimpleNamespace()
    monkeypatch.setattr(render, "rhythm_points", lambda e: [0.0, 2.0, 4.0])
    signals = SimpleNamespace(cuts=[])
    pieces = render.speaker_pieces("x.mp4", INFO, signals, Moment(10, 16), [(10.0, 16.0)], edit, 1.0)
    # The second reading is jitter of the first, so the picture holds until the real change at 4s.
    assert pieces == [(0.0, 0.2, 0.3), (4.0, 0.8, 0.3)]


def test_framed_plan_keeps_the_whole_picture(monkeypatch):
    monkeypatch.setattr(settings, "clip_layout", "framed")
    plan = render.plan_clip(Moment(10, 40, kind="visual"), [], _signals(), INFO, has_graphics=True)
    assert plan.picture_height == 608 and plan.crop_width == 1280 and not plan.use_blur_fill
    graph = render.build_filtergraph(plan, INFO, 0.0)
    assert "[backdrop]" in graph and "crop=404" not in graph and "overlay=x=0:y=656" in graph


def test_tall_source_still_fills_the_canvas_when_framed(monkeypatch):
    monkeypatch.setattr(settings, "clip_layout", "framed")
    vertical = MediaInfo(120.0, 720, 1280, 30.0, True)
    assert render.plan_clip(Moment(10, 40), [], _signals(), vertical, has_graphics=False).picture_height == 0


def test_framed_cutaways_replace_the_picture_and_not_the_backdrop(monkeypatch):
    monkeypatch.setattr(settings, "clip_layout", "framed")
    plan = render.plan_clip(Moment(10, 40, kind="visual"), [], _signals(), INFO, has_graphics=True)
    plan.broll = ((2.0, 2.0, "/stock.mp4", 0.0),)
    graph = render.build_filtergraph(plan, INFO, 0.0)
    assert "scale=1080:608:force_original_aspect_ratio=increase,crop=1080:608" in graph
    assert graph.index("[broll0]overlay") < graph.index("[backdrop][framed]")


def test_picture_height_follows_the_layout_setting(monkeypatch):
    monkeypatch.setattr(settings, "clip_layout", "framed")
    assert render._picture_height(INFO) == 608
    monkeypatch.setattr(settings, "clip_layout", "fill")
    assert render._picture_height(INFO) == 0


def test_the_source_creator_reaches_the_render(tmp_path, monkeypatch):
    seen = {}
    source = tmp_path / "long.mp4"
    source.write_bytes(b"v" * 2000)
    monkeypatch.setattr(settings, "content_dir", str(tmp_path))
    monkeypatch.setattr(job, "require_tools", lambda: None)
    monkeypatch.setattr(job, "probe", lambda path: MediaInfo(100.0, 1280, 720, 30.0, True))
    monkeypatch.setattr(job, "transcribe", lambda path, info: None)
    monkeypatch.setattr(job, "analyse", lambda path, info: None)
    monkeypatch.setattr(job, "pick_visual", lambda *a, **k: [moments.Moment(10, 40, title="Clip", kind="visual")])

    def fake_render(src, info, signals, moment, words, out_path, edit=None, creator="", music=None, captions=True, source_captioned=False):
        seen["creator"] = creator
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_bytes(b"v" * 2000)
        return render.ClipPlan([(10.0, 40.0)], 30.0, True, 404, "0", False, "1", False)

    monkeypatch.setattr(job, "render_moment", fake_render)
    src = ingest.SourceVideo(path=str(source), title="Long", author="handle", author_name="Display Name",
                             source="local", source_id="credit-reaches-render")
    assert job.clip_source(src, count=1)["ok"]
    assert seen["creator"] == "Display Name"


def test_audio_with_no_words_says_why_there_are_no_captions(tmp_path, monkeypatch):
    source = tmp_path / "long.mp4"
    source.write_bytes(b"v" * 2000)
    monkeypatch.setattr(settings, "content_dir", str(tmp_path))
    monkeypatch.setattr(job, "require_tools", lambda: None)
    monkeypatch.setattr(job, "probe", lambda path: MediaInfo(100.0, 1280, 720, 30.0, True))
    monkeypatch.setattr(job, "transcribe", lambda path, info: transcribe.Transcript(words=[], language="es", duration=100.0))
    monkeypatch.setattr(job, "analyse", lambda path, info: None)
    monkeypatch.setattr(job, "pick_visual", lambda *a, **k: [])
    result = job.clip_source(ingest.SourceVideo(path=str(source), source="local", source_id="no-words"), count=1)
    assert any("no words" in warning for warning in result["warnings"])


def test_a_cookies_file_is_used_only_when_it_has_contents(tmp_path, monkeypatch):
    empty, full = tmp_path / "empty.txt", tmp_path / "cookies.txt"
    empty.write_text("")
    full.write_text("# Netscape HTTP Cookie File\n")
    monkeypatch.setattr(settings, "cookies_from_browser", "")
    monkeypatch.setattr(settings, "cookies_file", str(empty))
    assert "cookiefile" not in ingest.network_options()
    monkeypatch.setattr(settings, "cookies_file", str(full))
    assert ingest.network_options()["cookiefile"] == str(full)


def test_youtube_challenge_solver_is_set_up_when_a_runtime_exists(monkeypatch):
    monkeypatch.setattr(ingest.shutil, "which", lambda name: "/usr/bin/node" if name == "node" else None)
    options = ingest.network_options()
    assert options["js_runtimes"] == {"node": {"path": "/usr/bin/node"}}
    assert options["remote_components"] == {"ejs:github"}
    monkeypatch.setattr(ingest.shutil, "which", lambda name: None)
    assert "js_runtimes" not in ingest.network_options()


def test_downloads_prefer_the_original_audio_over_dubs(tmp_path):
    assert ingest._ydl_options(tmp_path)["format"].split("/")[0].endswith("+ba[format_note*=original]")



def _fake_cli(monkeypatch, *, opencode, claude):
    """opencode/claude: a reply string, None for a failure, or 'timeout'."""
    calls = []
    monkeypatch.setattr(ai, "opencode_path", lambda: "/bin/opencode")
    monkeypatch.setattr(ai, "claude_path", lambda: "/bin/claude")
    monkeypatch.setattr(ai, "_opencode_down_until", 0.0)

    def run(command, **kwargs):
        name = "claude" if command[0] == "/bin/claude" else "opencode"
        calls.append((name, command, kwargs))
        reply = claude if name == "claude" else opencode
        if reply == "timeout":
            raise subprocess.TimeoutExpired(command, 1)
        if reply is None:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="boom")
        if name == "claude":
            out = json.dumps({"type": "result", "is_error": False, "result": reply})
        else:
            out = json.dumps({"type": "text", "part": {"text": reply}})
        return subprocess.CompletedProcess(command, 0, stdout=out + "\n", stderr="")

    monkeypatch.setattr(ai.subprocess, "run", run)
    return calls


def test_opencode_answers_first_when_it_works(monkeypatch):
    calls = _fake_cli(monkeypatch, opencode="from opencode", claude="from claude")
    assert ai.ask("hi") == "from opencode"
    assert [c[0] for c in calls] == ["opencode"]


def test_claude_takes_over_when_opencode_times_out_and_stays_on_for_a_while(monkeypatch):
    calls = _fake_cli(monkeypatch, opencode="timeout", claude="from claude")
    assert ai.ask("first") == "from claude"
    assert ai.ask("second") == "from claude"
    # One timeout costs minutes, so the second call does not wait on OpenCode again.
    assert [c[0] for c in calls] == ["opencode", "claude", "claude"]


def test_claude_runs_with_no_tools_no_settings_and_no_session(monkeypatch):
    calls = _fake_cli(monkeypatch, opencode=None, claude="ok")
    ai.ask("hi")
    command, kwargs = calls[-1][1], calls[-1][2]
    tools = command[command.index("--tools") + 1]
    assert tools == "" and "--strict-mcp-config" in command and "--safe-mode" in command
    assert "--no-session-persistence" in command and "--system-prompt" in command
    assert kwargs["cwd"] != os.getcwd() and kwargs["env"]["PWD"] == kwargs["cwd"]


def test_images_go_to_claude_inside_the_message(monkeypatch, tmp_path):
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"\xff\xd8 jpeg bytes")
    calls = _fake_cli(monkeypatch, opencode=None, claude="seen")
    assert ai.ask("what is this?", files=[str(frame)]) == "seen"
    message = json.loads(calls[-1][2]["input"])
    image, text = message["message"]["content"]
    assert image["source"]["media_type"] == "image/jpeg" and image["source"]["data"]
    assert text == {"type": "text", "text": "what is this?"}
    assert str(frame) not in " ".join(calls[-1][1])  # never a path Claude would have to open


def test_a_claude_error_result_counts_as_no_answer(monkeypatch):
    _fake_cli(monkeypatch, opencode=None, claude="x")
    monkeypatch.setattr(ai.subprocess, "run", lambda command, **k: subprocess.CompletedProcess(
        command, 0, stdout=json.dumps({"type": "result", "is_error": True, "result": "rate limited"}) + "\n", stderr=""))
    assert ai.ask("hi") is None


def test_no_claude_fallback_when_switched_off(monkeypatch):
    monkeypatch.setattr(settings, "claude_fallback", False)
    assert ai.claude_path() is None


def test_music_is_picked_from_the_owners_folder_and_stays_the_same_per_clip(tmp_path, monkeypatch):
    from app.clipper import music

    monkeypatch.setattr(settings, "clip_music_dir", str(tmp_path))
    assert music.pick("clip-1") is None  # an empty folder means no music, not an error
    for name in ("a.mp3", "b.m4a", "notes.txt"):
        (tmp_path / name).write_bytes(b"x")
    assert {p.name for p in music.library()} == {"a.mp3", "b.m4a"}
    assert music.pick("clip-1") == music.pick("clip-1")
    monkeypatch.setattr(settings, "clip_music", False)
    assert music.pick("clip-1") is None


def test_music_credit_comes_from_a_note_beside_the_track(tmp_path):
    from app.clipper import music

    track = tmp_path / "song.mp3"
    track.write_bytes(b"x")
    assert music.credit(track) == ""
    (tmp_path / "song.txt").write_text("Music: Song by Someone (CC BY 4.0)\n")
    assert music.credit(track) == "Music: Song by Someone (CC BY 4.0)"


def test_music_is_ducked_under_the_voice_and_mixed_with_the_effects():
    plan = render.plan_clip(Moment(10, 40), [], _signals(), INFO, has_graphics=True)
    plan.music, plan.has_sfx = "/music/bed.mp3", True
    graph = render.build_filtergraph(plan, INFO, 0.0)
    assert "sidechaincompress" in graph and "[bed][vkey]" in graph
    assert "[vmix][sfxst][ducked]amix=inputs=3" in graph
    # Every layer is stereo so the music bed keeps its width, and the output is pinned stereo.
    assert graph.count("aformat=channel_layouts=stereo") >= 4 and graph.rstrip().endswith("[aout]")
    plan.music = ""
    assert "sidechaincompress" not in render.build_filtergraph(plan, INFO, 0.0)


def test_a_source_with_its_own_captions_is_marked_for_the_render(tmp_path, monkeypatch):
    from app.clipper import subtitles

    seen = {}
    monkeypatch.setattr(subtitles, "already_captioned", lambda path, info: True)
    _talking_pipeline(monkeypatch, {})
    real = job.render_moment

    def spy(*args, **kwargs):
        seen["captions"] = kwargs.get("source_captioned")
        return real(*args, **kwargs)

    monkeypatch.setattr(job, "render_moment", spy)
    result = job.clip_source(_source(tmp_path, "captioned"), auto_queue=True)
    assert seen["captions"] is True
    assert any("burns in its own captions" in w for w in result["warnings"])


def test_the_caption_check_reads_the_ai_reply(monkeypatch):
    from app.clipper import subtitles

    monkeypatch.setattr(subtitles.ai, "available", lambda: True)
    monkeypatch.setattr(subtitles, "run_ffmpeg", lambda args: open(args[-1], "wb").write(b"jpg"))
    for reply, expected in (({"captions": True}, True), ({"captions": False}, False), (None, False), ({"captions": "yes"}, False)):
        monkeypatch.setattr(subtitles.ai, "ask_json", lambda *a, r=reply, **k: r)
        assert subtitles.already_captioned("x.mp4", INFO) is expected


def test_without_ai_nothing_counts_as_captioned(monkeypatch):
    from app.clipper import subtitles

    monkeypatch.setattr(subtitles.ai, "available", lambda: False)
    assert subtitles.already_captioned("x.mp4", INFO) is False


def test_captions_off_drops_our_words_but_keeps_the_hook(tmp_path):
    words = [Word("hello", 1.0, 1.4), Word("there", 1.5, 2.0)]
    from app.clipper.captions import build_ass
    assert ",Caption,," in build_ass(words, "Hook", 5.0, "Arial")
    assert ",Caption,," not in build_ass([], "Hook", 5.0, "Arial") and ",Hook,," in build_ass([], "Hook", 5.0, "Arial")


def test_edit_never_drops_the_ending(monkeypatch):
    from app.clipper.edit import build_edit
    _edit_reply(monkeypatch, {"keep": [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10], "beats": []})
    words = _talk()
    edit = build_edit(Moment(10.0, words[-1].end), words)
    assert edit.words[-1] == words[-1]


def test_an_edit_that_cuts_half_the_story_is_refused(monkeypatch):
    from app.clipper.edit import build_edit
    _edit_reply(monkeypatch, {"keep": [0, 2, 4, 6, 8, 10], "beats": []})
    words = _talk()
    assert len(build_edit(Moment(10.0, words[-1].end), words).words) == len(words)


def test_payoff_pass_extends_a_clip_that_stops_on_its_setup(monkeypatch):
    replies = iter([
        {"clips": [{"first": 2, "last": 4, "title": "Title", "score": 90}]},
        {"clips": [{"clip": 0, "last": 7}]},
    ])
    monkeypatch.setattr(ai, "ask_json", lambda *a, **k: next(replies))
    words = _long_transcript()
    sentences = moments.build_sentences(words)
    picked = moments.pick_speech(words, _signals(300), count=1)
    assert picked[0].end >= sentences[7].end - 0.01


def test_payoff_pass_keeps_the_ending_and_drops_early_setup_when_too_long(monkeypatch):
    monkeypatch.setattr(settings, "clip_max_sec", 25)
    replies = iter([
        {"clips": [{"first": 0, "last": 3, "title": "Title", "score": 90}]},
        {"clips": [{"clip": 0, "last": 9}]},
    ])
    monkeypatch.setattr(ai, "ask_json", lambda *a, **k: next(replies))
    words = _long_transcript()
    sentences = moments.build_sentences(words)
    picked = moments.pick_speech(words, _signals(300), count=1)
    assert picked[0].end >= sentences[9].end - 0.01 and picked[0].start > sentences[0].start


@pytest.mark.parametrize("answer", [{"clips": [{"clip": 0, "last": 1}]}, {"clips": [{"clip": "x", "last": 1e999}]},
                                    {"clips": [{"clip": 0, "last": 999}]}])
def test_payoff_pass_ignores_answers_it_cannot_use(monkeypatch, answer):
    def pick(second):
        replies = iter([{"clips": [{"first": 2, "last": 4, "title": "Title", "score": 90}]}, second])
        monkeypatch.setattr(ai, "ask_json", lambda *a, **k: next(replies))
        return moments.pick_speech(_long_transcript(), _signals(300), count=1)

    assert [(m.start, m.end) for m in pick(answer)] == [(m.start, m.end) for m in pick(None)]


def test_source_captions_are_cropped_out_of_the_framed_picture(monkeypatch):
    monkeypatch.setattr(settings, "clip_layout", "framed")
    plan = render.plan_clip(Moment(10, 40), [], _signals(), INFO, has_graphics=True, crop_bottom=0.2)
    assert plan.picture_height == round(1080 * 720 * 0.8 / 1280 / 2) * 2
    assert "crop=iw:trunc(ih*0.8/2)*2:0:0" in render.build_filtergraph(plan, INFO, 0.0)


def test_the_last_segment_runs_on_past_the_final_word():
    held = render.with_end_hold([(1.0, 2.0), (3.0, 4.0)], 100.0)
    assert held[0] == (1.0, 2.0) and held[1][1] > 4.0
    assert render.with_end_hold([(3.0, 4.0)], 4.1)[0][1] <= 4.1  # never past the end of the source



# ---------- audio QA (earshot skill, opt-in) ----------
def test_audio_qa_is_off_without_the_flag_or_binary(monkeypatch):
    from app.clipper import audio_qa
    monkeypatch.setattr(audio_qa, "earshot_bin", lambda: "/bin/earshot")
    monkeypatch.setattr(settings, "earshot_qa", False)
    assert audio_qa.available() is False
    monkeypatch.setattr(settings, "earshot_qa", True)
    assert audio_qa.available() is True
    monkeypatch.setattr(audio_qa, "earshot_bin", lambda: None)
    assert audio_qa.available() is False


def test_audio_qa_summarise_trims_to_verdict_and_fixable_issues():
    from app.clipper import audio_qa
    raw = {"verdict": "needs work", "score": 0.846, "in_range": 19, "scored": 23,
           "actions": [{"metric": "music_frac", "weight": "critical", "action": "music is heard in only 5% of windows: add a bed"},
                       {"metric": "side_to_mid_db", "weight": "minor", "action": "the mix is mono"},
                       {"metric": "tempo", "weight": "info", "action": "ignore me"},
                       {"metric": "band_sub", "weight": "minor", "action": "thin lows"},
                       {"metric": "x", "weight": "minor", "action": "fourth, dropped"}]}
    out = audio_qa.summarise(raw)
    assert out["verdict"] == "needs work" and out["score"] == 0.846 and out["in_range"] == 19
    assert len(out["issues"]) == 3 and out["issues"][0]["metric"] == "music_frac"  # info dropped, capped at 3
    assert audio_qa.summarise({"no": "verdict"}) is None


def test_audio_qa_score_shells_out_and_parses(monkeypatch, tmp_path):
    from app.clipper import audio_qa
    clip = tmp_path / "c.mp4"; clip.write_bytes(b"x")
    monkeypatch.setattr(audio_qa, "earshot_bin", lambda: "/bin/earshot")
    import subprocess as sp
    payload = json.dumps({"verdict": "launch-grade", "score": 0.95, "in_range": 22, "scored": 23, "actions": []})
    monkeypatch.setattr(audio_qa.subprocess, "run",
                        lambda cmd, **k: sp.CompletedProcess(cmd, 0, stdout=payload, stderr=""))
    assert audio_qa.score(str(clip))["verdict"] == "launch-grade"
    monkeypatch.setattr(audio_qa.subprocess, "run",
                        lambda cmd, **k: sp.CompletedProcess(cmd, 1, stdout="", stderr="boom"))
    assert audio_qa.score(str(clip)) is None  # a non-zero exit is swallowed, never raised


def test_a_clip_stores_its_audio_score_only_when_qa_is_on(tmp_path, monkeypatch):
    from app.clipper import audio_qa
    from app.db import session_scope, init_db
    from app.models import Video
    init_db()
    with session_scope() as db:
        v = Video(source="local", source_id="qa-store", source_url="x", meta={"clip_rendered": True})
        db.add(v); db.flush(); vid = v.id
    monkeypatch.setattr(audio_qa, "available", lambda: True)
    monkeypatch.setattr(audio_qa, "score", lambda p: {"verdict": "close", "score": 0.8, "issues": []})
    job._store_audio_qa(vid, str(tmp_path / "c.mp4"))
    with session_scope() as db:
        assert db.get(Video, vid).meta["earshot"]["verdict"] == "close"
    monkeypatch.setattr(audio_qa, "available", lambda: False)
    monkeypatch.setattr(audio_qa, "score", lambda p: (_ for _ in ()).throw(AssertionError("must not be called")))
    job._store_audio_qa(vid, str(tmp_path / "c.mp4"))  # no-op when off


def test_review_card_shows_a_one_line_audio_verdict():
    from app.growth.approval import _audio_line
    assert _audio_line(None) == ""
    assert _audio_line({"verdict": "launch-grade", "score": 0.95, "issues": []}) == "launch-grade 95%"
    line = _audio_line({"verdict": "needs work", "score": 0.84,
                        "issues": [{"metric": "band_mid", "weight": "major", "action": "honky mids: cut 1-3 kHz"}]})
    assert line == "needs work 84%: honky mids"
