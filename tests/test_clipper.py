"""Fast tests for the clipper: everything except real rendering."""
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
import numpy as np
import pytest
from app.clipper import ai, ingest, media, moments, timeline, transcribe
from app.clipper.media import Interrupted, MediaError, MediaInfo
from app.clipper.moments import Moment, build_sentences
from app.clipper.signals import Signals, _find_cuts
from app.clipper.transcribe import Transcript, Word
from app.config import settings


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
