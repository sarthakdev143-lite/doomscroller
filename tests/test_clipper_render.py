"""End-to-end clipper renders through the real ffmpeg binary.

Filter graphs only fail at ffmpeg's parser, so these exist for the same reason as
test_render_integration.py. Skipped automatically when ffmpeg is unavailable.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from app.clipper import ai, job, moments, timeline
from app.clipper.broll import StockClip
from app.clipper.edit import CARD, PUNCH, STAT, Beat, Cutaway, EditPlan
from app.clipper.ingest import SourceVideo
from app.clipper.media import MediaError, probe, read_ffmpeg
from app.clipper.moments import Moment
from app.clipper.render import render_moment
from app.clipper.signals import analyse
from app.clipper.transcribe import Transcript, Word
from app.config import settings
from app.db import init_db

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
        reason="ffmpeg/ffprobe not on PATH",
    ),
]


def setup_module(module):
    init_db()


def _make(path, size="1280x720", seconds=40, audio=True):
    args = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=25:duration={seconds}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=300:duration={seconds}",
                 "-filter_complex", "[1:a]volume='if(between(t,20,30),1.0,0.05)':eval=frame[a]",
                 "-map", "0:v", "-map", "[a]", "-c:a", "aac"]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(args, check=True, capture_output=True)
    return str(path)


@pytest.fixture(scope="module")
def landscape(tmp_path_factory):
    return _make(tmp_path_factory.mktemp("clip") / "landscape.mp4")


def _speech_words(start=5.0, repeat=4):
    text = ("Why's this hard? It's 100% about the {first} line, isn't it? "
            "Here's what \"nobody\" tells you: 50% off isn't a hook. "
            "Say the payoff first, then explain why it's true. ") * repeat
    words, now = [], start
    for index, token in enumerate(text.split()):
        words.append(Word(token, now, now + 0.28, 0.98))
        # Every so often a long pause that must get cut out.
        now += 0.28 + (0.8 if index % 7 == 6 else 0.04)
    return words


def _assert_valid(path, expected_seconds, *, audio=True):
    info = probe(path)
    assert (info.width, info.height) == (1080, 1920)
    assert info.has_audio is audio
    assert abs(info.duration - expected_seconds) < 0.5


def test_tightened_speech_clip_with_awkward_text(landscape, tmp_path):
    info = probe(landscape)
    signals = analyse(landscape, info)
    words = _speech_words(repeat=2)
    moment = Moment(words[0].start, words[-1].end, hook="It's 100% about {this}", kind="speech")
    out = tmp_path / "speech.mp4"
    plan = render_moment(landscape, info, signals, moment, words, str(out))
    assert len(plan.segments) > 3
    assert plan.duration < moment.duration
    _assert_valid(str(out), plan.duration)


def test_no_speech_clip_from_silent_source(tmp_path):
    source = _make(tmp_path / "silent.mp4", audio=False)
    info = probe(source)
    signals = analyse(source, info)
    out = tmp_path / "out.mp4"
    plan = render_moment(source, info, signals, Moment(5, 25, kind="visual"), [], str(out))
    _assert_valid(str(out), plan.duration, audio=False)


@pytest.mark.parametrize("size", ["720x1280", "720x720", "480x1280"])
def test_every_source_shape_fills_the_canvas(tmp_path, size):
    source = _make(tmp_path / "shape.mp4", size=size, seconds=20)
    info = probe(source)
    out = tmp_path / "out.mp4"
    plan = render_moment(source, info, analyse(source, info), Moment(2, 15, hook="Look", kind="visual"), [], str(out))
    _assert_valid(str(out), plan.duration)


def test_blur_fill_renders(landscape, tmp_path):
    info = probe(landscape)
    signals = analyse(landscape, info)
    signals.centroid[:] = [0.05 + 0.9 * (i % 2) for i in range(len(signals.centroid))]
    out = tmp_path / "wide.mp4"
    plan = render_moment(landscape, info, signals, Moment(2, 15, kind="visual"), [], str(out))
    assert plan.use_blur_fill
    _assert_valid(str(out), plan.duration)


def test_chosen_window_contains_the_loud_stretch(landscape):
    picked = moments.rank_windows(analyse(landscape, probe(landscape)), 1)
    assert picked[0].start <= 20 and picked[0].end >= 30


def test_full_run_with_a_transcript(landscape, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "content_dir", str(tmp_path))
    monkeypatch.setattr(ai, "ask", lambda *a, **k: None)
    monkeypatch.setattr(job, "transcribe", lambda path, info: Transcript(words=_speech_words(start=1.0, repeat=2), language="en", duration=40.0))
    result = job.clip_source(SourceVideo(path=landscape, title="Lesson", source="local", source_id="e2e-speech", url="x"), count=1)
    assert result["ok"] and result["kind"] == "speech"
    _assert_valid(result["clips"][0]["path"], result["clips"][0]["duration"])


def test_full_run_with_no_key_falls_back_to_visual(landscape, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "content_dir", str(tmp_path))
    monkeypatch.setattr(settings, "deepgram_api_key", "")
    monkeypatch.setattr(ai, "ask", lambda *a, **k: None)
    result = job.clip_source(SourceVideo(path=landscape, title="Film", source="local", source_id="e2e-visual", url="x"), count=2)
    assert result["ok"] and result["kind"] == "visual"
    assert len(result["clips"]) >= 1
    assert any("DEEPGRAM" in w for w in result["warnings"])
    _assert_valid(result["clips"][0]["path"], result["clips"][0]["duration"])


def _still_frames(path, *, every=1):
    raw = read_ffmpeg(["-i", str(path), "-map", "0:v:0", "-vf", f"select='not(mod(n,{every}))',format=gray",
                       "-vsync", "0", "-f", "rawvideo", "-"])
    return np.frombuffer(raw, np.uint8).reshape(-1, 1920, 1080)


def test_zoom_stays_centred_and_pixels_stay_square(tmp_path):
    """A white dot at exact centre must not drift during the snap or a punch."""
    source = tmp_path / "dot.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "color=c=black:s=1080x1920:r=30:d=14", "-f", "lavfi", "-i", "sine=frequency=300:duration=14",
         "-vf", "drawbox=x=520:y=940:w=40:h=40:color=white:t=fill", "-c:v", "libx264", "-preset", "ultrafast",
         "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)],
        check=True, capture_output=True,
    )
    info = probe(str(source))
    words = [Word("word", 1.0 + i * 0.6, 1.5 + i * 0.6, 0.99) for i in range(5)]
    words += [Word("word", 5.0 + i * 0.6, 5.5 + i * 0.6, 0.99) for i in range(5)]
    words += [Word("word", 9.0 + i * 0.6, 9.5 + i * 0.6, 0.99) for i in range(5)]
    out = tmp_path / "out.mp4"
    plan = render_moment(str(source), info, analyse(str(source), info), Moment(1.0, 12.0, kind="speech"),
                         [], str(out))
    speech_out = tmp_path / "speech.mp4"
    speech_plan = render_moment(str(source), info, analyse(str(source), info), Moment(1.0, 12.0, kind="speech"),
                                words, str(speech_out))
    assert len(speech_plan.segments) >= 3 and plan.duration > 0
    for path in (out, speech_out):
        for frame in _still_frames(path, every=3)[:60]:
            # Only the band around the dot: captions and the progress bar are elsewhere.
            ys, xs = np.nonzero(frame[700:1150] > 128)
            assert abs(xs.mean() - 539.5) <= 2
            assert abs(ys.mean() + 700 - 959.5) <= 2
        sar = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=sample_aspect_ratio", "-of", "csv=p=0", str(path)],
                             capture_output=True, text=True).stdout.strip()
        assert sar in ("1:1", "N/A", "")


def test_long_hook_and_caption_stay_inside_the_frame(tmp_path):
    source = tmp_path / "black.mp4"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "color=c=black:s=1080x1920:r=30:d=8", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", str(source)], check=True, capture_output=True)
    info = probe(str(source))
    words = [Word("Internationalization", 0.5, 2.0, 0.99), Word("Telecommunications", 2.0, 4.0, 0.99)]
    out = tmp_path / "out.mp4"
    render_moment(str(source), info, analyse(str(source), info),
                  Moment(0.0, 6.0, hook="Most people completely misunderstand how compound interest works", kind="visual"),
                  words, str(out))
    for frame in _still_frames(out, every=15):
        frame = frame[20:]
        assert frame[:, :2].max() < 40 and frame[:, -2:].max() < 40


def test_render_past_the_end_of_the_picture_fails_fast(tmp_path):
    source = tmp_path / "short_picture.mp4"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "testsrc2=size=640x360:rate=25:duration=10", "-f", "lavfi", "-i", "sine=frequency=300:duration=40",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)],
                   check=True, capture_output=True)
    info = probe(str(source))
    assert info.duration == pytest.approx(10.0, abs=0.2), "the picture sets the length, not the audio"
    out = tmp_path / "out.mp4"
    with pytest.raises(MediaError):
        render_moment(str(source), info, analyse(str(source), info), Moment(20.0, 35.0, kind="visual"), [], str(out))
    assert not out.exists()


def test_failed_rerender_leaves_the_existing_clip_alone(tmp_path):
    source = _make(tmp_path / "src.mp4", seconds=12)
    info = probe(source)
    signals = analyse(source, info)
    out = tmp_path / "clip.mp4"
    render_moment(source, info, signals, Moment(1, 8, kind="visual"), [], str(out))
    before = out.read_bytes()
    with pytest.raises(MediaError):
        render_moment(source, info, signals, Moment(40, 50, kind="visual"), [], str(out))
    assert out.read_bytes() == before
    assert not (tmp_path / "clip.mp4.tmp.mp4").exists()


def test_stray_keypress_on_stdin_does_not_end_a_render(tmp_path):
    """Run from a child with a stdin pipe holding q, as a piped shell loop would."""
    source = _make(tmp_path / "src.mp4", seconds=20, audio=False)
    out = tmp_path / "copy.mp4"
    code = ("import sys;from app.clipper.media import run_ffmpeg;"
            f"run_ffmpeg(['-i',{source!r},'-c:v','libx264','-preset','ultrafast',{str(out)!r}])")
    subprocess.run([sys.executable, "-c", code], input=b"q\nq\nq\n", check=True, capture_output=True,
                   env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)})
    assert probe(str(out)).duration == pytest.approx(20.0, abs=0.2)


def test_non_square_pixels_are_not_squeezed(tmp_path):
    source = tmp_path / "anamorphic.mp4"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "color=c=black:s=1440x1080:r=30:d=8",
                    # 300x400 stored pixels at SAR 4:3 is a 400x400 square on display.
                    "-vf", "drawbox=x=570:y=340:w=300:h=400:color=white:t=fill,setsar=4/3",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(source)],
                   check=True, capture_output=True)
    info = probe(str(source))
    assert info.width == 1920
    out = tmp_path / "out.mp4"
    render_moment(str(source), info, analyse(str(source), info), Moment(1, 7, kind="speech"), [], str(out))
    frame = _still_frames(out, every=60)[-1][20:]
    ys, xs = np.nonzero(frame > 128)
    assert (xs.max() - xs.min()) / (ys.max() - ys.min()) == pytest.approx(1.0, abs=0.05)


def test_crop_switches_on_the_exact_frame_of_a_cut(tmp_path):
    """The subject jumps sides at a hard cut, and must never be out of frame."""
    source = tmp_path / "two_shots.mp4"
    box = "drawbox=x='{x}+40*sin(t*6)':y=300:w=120:h=120:color=white:t=fill"
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "color=c=gray:s=1280x720:r=30:d=8", "-f", "lavfi", "-i", "color=c=black:s=1280x720:r=30:d=8",
         "-filter_complex",
         "[0:v]geq=lum='128+64*sin((X+80*T)/40)':cb=128:cr=128,drawbox=x=120:y=300:w=120:h=120:color=white:t=fill[a];"
         "[1:v]geq=lum='40+30*sin((Y+90*T)/30)':cb=128:cr=128,drawbox=x=1040:y=300:w=120:h=120:color=white:t=fill[b];"
         "[a][b]concat=n=2:v=1[v]",
         "-map", "[v]", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(source)],
        check=True, capture_output=True,
    )
    info = probe(str(source))
    signals = analyse(str(source), info)
    signals.centroid[:8] = 0.14
    signals.centroid[8:] = 0.86
    signals.centroid_weight[:] = 1
    assert any(7 <= c <= 9 for c in signals.cuts)
    out = tmp_path / "out.mp4"
    render_moment(str(source), info, signals, Moment(5.0, 11.0, kind="speech"), [], str(out))
    visible = [(frame[20:] > 235).sum() for frame in _still_frames(out)]
    assert sum(1 for pixels in visible if pixels < 2000) == 0


def _flash_and_beep_source(path, seconds=45):
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"color=c=black:s=1280x720:r=25:d={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=1000:sample_rate=48000:duration={seconds}",
         "-filter_complex",
         "[0:v]drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:enable='lt(mod(t,2),0.2)'[v];"
         "[1:a]volume='if(lt(mod(t,2),0.2),1,0)':eval=frame[a]",
         "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast",
         "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)],
        check=True, capture_output=True,
    )


def test_full_edit_renders_with_graphics_speed_and_sound_in_sync(tmp_path):
    source = tmp_path / "flash.mp4"
    _flash_and_beep_source(source)
    info = probe(str(source))
    words, now = [], 4.05
    while now < 36:
        words.append(Word("it's" if len(words) % 9 else "word,", now, now + 0.5, 0.99))
        now += 0.6
        if len(words) % 5 == 0:
            now += 1.1
    segments = timeline.edit_segments(words, list(range(len(words))), 0.12)
    edit = EditPlan(words=words, segments=segments, speed=1.12, beats=[
        Beat(words[10].start, "40°C", STAT, "RECORD"), Beat(words[20].start, "", PUNCH),
        Beat(words[30].start, "BIG TURN", CARD),
    ])
    out = tmp_path / "out.mp4"
    plan = render_moment(str(source), info, analyse(str(source), info), Moment(4.0, 36.0, hook="Heat kills", kind="speech"),
                         words, str(out), edit=edit)
    assert plan.speed == 1.12 and plan.has_sfx
    _assert_valid(str(out), plan.duration)


def test_broll_shows_at_the_right_time_under_the_captions(tmp_path):
    """A black talk with a white stock clip: white must appear only during the cutaway."""
    source = tmp_path / "talk.mp4"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "color=c=black:s=1280x720:r=30:d=20", "-f", "lavfi", "-i", "sine=frequency=300:duration=20",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)],
                   check=True, capture_output=True)
    stock = tmp_path / "stock.mp4"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "color=c=white:s=720x1280:r=25:d=6", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", str(stock)], check=True, capture_output=True)
    info = probe(str(source))
    words = [Word("word", 1.0 + i * 0.4, 1.35 + i * 0.4, 0.99) for i in range(40)]
    clip = StockClip("1", str(stock), 6.0, offset=0.4)
    edit = EditPlan(words=words, segments=timeline.edit_segments(words, list(range(40)), 0.12), speed=1.0,
                    cutaways=[Cutaway(words[10].start, 2.0, "rain", clip)])
    out = tmp_path / "out.mp4"
    plan = render_moment(str(source), info, analyse(str(source), info), Moment(1.0, 17.0, kind="speech"),
                         words, str(out), edit=edit)
    _assert_valid(str(out), plan.duration)
    frames = _still_frames(out)
    top_band = frames[:, 200:600, 400:680].mean(axis=(1, 2))
    white = np.flatnonzero(top_band > 200)
    assert len(white) == pytest.approx(60, abs=3), "two seconds at 30 fps"
    assert white[0] == pytest.approx(edit.to_output(words[10].start) * 30, abs=2)
    assert frames[white[30], 1200:1280, 200:880].min() < 60, "captions must stay on top of the footage"


def _yellow_graphic_pixels(frame_rgb):
    r, g, b = frame_rgb[..., 0].astype(int), frame_rgb[..., 1].astype(int), frame_rgb[..., 2].astype(int)
    return int(((r > 200) & (g > 170) & (b < 90))[500:950].sum())


def test_plain_number_graphic_is_really_on_screen(tmp_path):
    source = tmp_path / "gray.mp4"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "color=c=gray:s=540x960:r=30:d=14", "-f", "lavfi", "-i", "sine=frequency=300:duration=14",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)],
                   check=True, capture_output=True)
    info = probe(str(source))
    words = [Word("word", 1.0 + i * 0.4, 1.35 + i * 0.4, 0.99) for i in range(28)]
    edit = EditPlan(words=words, segments=timeline.edit_segments(words, list(range(28)), 0.12), speed=1.0,
                    beats=[Beat(words[10].start, "100%", STAT, "RECORD")])
    out = tmp_path / "out.mp4"
    render_moment(str(source), info, analyse(str(source), info), Moment(1.0, 12.5, kind="speech"), words, str(out), edit=edit)
    when = edit.to_output(words[10].start) + 0.8
    raw = read_ffmpeg(["-ss", f"{when:.3f}", "-i", str(out), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
    assert _yellow_graphic_pixels(np.frombuffer(raw, np.uint8).reshape(1920, 1080, 3)) > 3000


def test_bad_cached_stock_clip_is_replaced_by_a_playable_one(tmp_path, monkeypatch):
    from app.clipper import broll
    good = tmp_path / "good.mp4"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "color=c=white:s=720x1280:r=25:d=6", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", str(good)], check=True, capture_output=True)
    monkeypatch.setattr(settings, "content_dir", str(tmp_path / "content"))
    monkeypatch.setattr(settings, "pexels_api_key", "key")
    files = [{"file_type": "video/mp4", "width": 720, "height": 1280, "link": "https://cdn/x.mp4"}]
    monkeypatch.setattr(broll, "search", lambda query: broll._pexels_candidates([
        {"id": 1, "duration": 6, "video_files": files}, {"id": 2, "duration": 6, "video_files": files}]))

    def deliver(link, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(good.read_bytes() if path.stem == "2" else b"<html>sorry</html>")

    monkeypatch.setattr(broll, "_download", deliver)
    bad = settings.content_path / "broll" / "1.mp4"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"<html>sorry</html>")
    clip = broll.fetch("rain", 3.0)
    assert clip.video_id == "2" and not bad.exists()


def test_quiet_recording_keeps_its_voice_above_the_effects(tmp_path):
    source = tmp_path / "quiet.mp4"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "color=c=gray:s=540x960:r=30:d=14", "-f", "lavfi", "-i", "sine=frequency=1000:duration=14",
                    "-filter_complex", "[1:a]volume=-38dB[a]", "-map", "0:v", "-map", "[a]",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)],
                   check=True, capture_output=True)
    info = probe(str(source))
    words = [Word("word", 1.0 + i * 0.4, 1.35 + i * 0.4, 0.99) for i in range(28)]
    edit = EditPlan(words=words, segments=timeline.edit_segments(words, list(range(28)), 0.12), speed=1.12,
                    beats=[Beat(words[10].start, "", PUNCH)])
    out = tmp_path / "out.mp4"
    render_moment(str(source), info, analyse(str(source), info), Moment(1.0, 12.5, kind="speech"), words, str(out), edit=edit)
    when = edit.to_output(words[10].start)
    audio = np.frombuffer(read_ffmpeg(["-ss", f"{when:.3f}", "-t", "0.25", "-i", str(out), "-ac", "1", "-ar", "48000",
                                       "-f", "f32le", "-"]), np.float32)
    spectrum = np.abs(np.fft.rfft(audio))
    freqs = np.fft.rfftfreq(len(audio), 1 / 48000)
    impact = np.sqrt((spectrum[(freqs > 30) & (freqs < 200)] ** 2).sum())
    voice = np.sqrt((spectrum[(freqs > 800) & (freqs < 1200)] ** 2).sum())
    assert voice > impact


def test_punch_leaves_the_picture_undimmed_and_free_of_text(tmp_path):
    source = tmp_path / "gray.mp4"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "color=c=gray:s=540x960:r=30:d=14", "-f", "lavfi", "-i", "sine=frequency=300:duration=14",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)],
                   check=True, capture_output=True)
    info = probe(str(source))
    words = [Word("word", 1.0 + i * 0.4, 1.35 + i * 0.4, 0.99) for i in range(28)]
    edit = EditPlan(words=words, segments=timeline.edit_segments(words, list(range(28)), 0.12), speed=1.12,
                    beats=[Beat(words[12].start, "", PUNCH)])
    out = tmp_path / "out.mp4"
    render_moment(str(source), info, analyse(str(source), info), Moment(1.0, 12.5, kind="speech"), words, str(out), edit=edit)
    when = edit.to_output(words[12].start)

    def frame_at(seconds):
        raw = read_ffmpeg(["-ss", f"{seconds:.3f}", "-i", str(out), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
        return np.frombuffer(raw, np.uint8).reshape(1920, 1080, 3)

    # The middle of the frame, where the big word and its dimming used to be.
    during, before = frame_at(when + 0.5), frame_at(when - 0.5)
    assert _yellow_graphic_pixels(during) == 0
    assert abs(float(during[500:950].mean()) - float(before[500:950].mean())) < 3
    assert frame_at(when + 0.02)[500:950].mean() > before[500:950].mean() + 20, "the flash is still there"


def test_landscape_stock_clip_is_cropped_to_fill_the_frame(tmp_path):
    """Pixabay's footage is mostly landscape, so it must fill the canvas with no bars."""
    source = tmp_path / "talk.mp4"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "color=c=black:s=1280x720:r=30:d=14", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", str(source)], check=True, capture_output=True)
    stock = tmp_path / "wide.mp4"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "color=c=white:s=1920x1080:r=25:d=6", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", str(stock)], check=True, capture_output=True)
    info = probe(str(source))
    words = [Word("word", 1.0 + i * 0.4, 1.35 + i * 0.4, 0.99) for i in range(25)]
    clip = StockClip("pixabay-1", str(stock), 6.0, offset=0.4, provider="Pixabay")
    edit = EditPlan(words=words, segments=timeline.edit_segments(words, list(range(25)), 0.12), speed=1.0,
                    cutaways=[Cutaway(words[8].start, 2.0, "rain", clip)])
    out = tmp_path / "out.mp4"
    render_moment(str(source), info, analyse(str(source), info), Moment(1.0, 11.0, kind="speech"), words, str(out), edit=edit)
    index = int((edit.to_output(words[8].start) + 1.0) * 30)
    frame = _still_frames(out)[index]
    assert frame[40:200, :].mean() > 200 and frame[1700:1900, :].mean() > 200, "no bars at top or bottom"
    assert frame[900:1000, :10].mean() > 200 and frame[900:1000, -10:].mean() > 200, "no bars at the sides"


def test_edit_works_without_any_audio(tmp_path):
    source = _make(tmp_path / "silent.mp4", audio=False)
    info = probe(source)
    words = [Word("word", 2.0 + i * 0.5, 2.4 + i * 0.5, 0.99) for i in range(30)]
    edit = EditPlan(words=words, segments=timeline.edit_segments(words, list(range(30)), 0.12), speed=1.12)
    out = tmp_path / "out.mp4"
    plan = render_moment(source, info, analyse(source, info), Moment(2.0, 17.0, kind="speech"), words, str(out), edit=edit)
    assert not plan.has_sfx
    _assert_valid(str(out), plan.duration, audio=False)


def test_audio_stays_locked_to_video_after_jump_cuts(tmp_path):
    """A white flash and a beep at the same instant must still coincide afterwards."""
    source = tmp_path / "flash.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "color=c=black:s=1280x720:r=25:d=45",
         "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000:duration=45",
         "-filter_complex",
         "[0:v]drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:enable='lt(mod(t,2),0.2)'[v];"
         "[1:a]volume='if(lt(mod(t,2),0.2),1,0)':eval=frame[a]",
         "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast",
         "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)],
        check=True, capture_output=True,
    )
    info = probe(str(source))
    words, now = [], 4.05
    while now < 36:
        words.append(Word("word", now, now + 0.5, 0.99))
        now += 0.6
        if len(words) % 5 == 0:
            now += 1.1  # a pause long enough to be cut out
    out = tmp_path / "out.mp4"
    plan = render_moment(str(source), info, analyse(str(source), info),
                         Moment(4.0, 36.0, kind="speech"), words, str(out))
    assert len(plan.segments) > 4

    video = np.frombuffer(read_ffmpeg(["-i", str(out), "-map", "0:v:0", "-vf", "scale=32:18,format=gray",
                                       "-f", "rawvideo", "-"]), np.uint8).reshape(-1, 18, 32).mean((1, 2))
    audio = np.frombuffer(read_ffmpeg(["-i", str(out), "-map", "0:a:0", "-ac", "1", "-ar", "8000",
                                       "-f", "s16le", "-"]), np.int16).astype(float)
    loud = np.sqrt(np.convolve(audio ** 2, np.ones(80) / 80, mode="same"))

    def onsets(values, threshold, rate):
        above = values > threshold
        return (np.flatnonzero(above[1:] & ~above[:-1]) + 1) / rate

    flashes, beeps = onsets(video, 128, 30), onsets(loud, loud.max() * 0.25, 8000)
    offsets = [beeps[np.argmin(np.abs(beeps - flash))] - flash for flash in flashes]
    close = [abs(offset) for offset in offsets if abs(offset) < 0.5]
    assert len(close) >= 5
    assert max(close) < 0.06


@pytest.mark.parametrize("recipe, speed", [
    ({"pace": "tight", "sfx": "off", "broll": "off"}, 1.18),
    ({"pace": "relaxed", "sfx": "on", "broll": "on"}, 1.0),
])
def test_growth_recipe_reaches_the_render_and_is_recorded(landscape, tmp_path, monkeypatch, recipe, speed):
    from app.growth import recipes
    from app.db import session_scope
    from app.models import Video

    monkeypatch.setattr(settings, "content_dir", str(tmp_path))
    monkeypatch.setattr(ai, "ask", lambda *a, **k: None)
    monkeypatch.setattr(job, "transcribe", lambda path, info: Transcript(words=_speech_words(start=1.0, repeat=2), language="en", duration=40.0))
    monkeypatch.setattr(recipes, "choose", lambda: dict(recipe))
    seen = {}
    real_render = job.render_moment

    def spy(*args, **kwargs):
        seen["sfx"] = settings.clip_sfx
        plan = real_render(*args, **kwargs)
        seen["speed"] = plan.speed
        return plan

    monkeypatch.setattr(job, "render_moment", spy)
    before = settings.clip_speed
    source_id = f"recipe-{recipe['pace']}"
    result = job.clip_source(SourceVideo(path=landscape, title="Lesson", source="local", source_id=source_id, url="x"), count=1)
    assert result["ok"] and result["kind"] == "speech"
    assert seen == {"sfx": recipe["sfx"] == "on", "speed": speed}
    assert settings.clip_speed == before
    _assert_valid(result["clips"][0]["path"], result["clips"][0]["duration"])
    with session_scope() as db:
        recorded = db.get(Video, result["clips"][0]["id"]).meta["growth_recipe"]
    assert recorded["pace"] == recipe["pace"] and recorded["broll"] == "off"  # no stock key in tests


def test_creator_credit_is_really_on_screen(landscape, tmp_path):
    info = probe(landscape)
    signals = analyse(landscape, info)
    moment = Moment(5.0, 20.0, kind="visual")
    plain, credited = tmp_path / "plain.mp4", tmp_path / "credited.mp4"
    render_moment(landscape, info, signals, moment, [], str(plain))
    render_moment(landscape, info, signals, moment, [], str(credited), creator="Some Creator")

    def band(path):
        raw = read_ffmpeg(["-ss", "3", "-i", str(path), "-frames:v", "1", "-vf", "crop=1080:80:0:1440",
                           "-f", "rawvideo", "-pix_fmt", "gray", "-"])
        return np.frombuffer(raw, dtype=np.uint8).astype(int)

    assert np.abs(band(credited) - band(plain)).mean() > 2


def _striped_source(path, seconds=25):
    """Red left edge, blue right edge: both are on screen only if the width is uncropped."""
    run_args = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c=0x808080:s=1280x720:d={seconds}:r=30",
                "-vf", "drawbox=x=0:y=0:w=64:h=720:color=red:t=fill,drawbox=x=1216:y=0:w=64:h=720:color=blue:t=fill",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(run_args, check=True)


def _rgb_frame(path, when):
    raw = read_ffmpeg(["-ss", str(when), "-i", str(path), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
    return np.frombuffer(raw, np.uint8).reshape(1920, 1080, 3).astype(int)


def test_framed_clip_shows_the_whole_picture_over_a_dimmer_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "clip_layout", "framed")
    source = tmp_path / "striped.mp4"
    _striped_source(source)
    info = probe(str(source))
    out = tmp_path / "out.mp4"
    plan = render_moment(str(source), info, analyse(str(source), info), Moment(5.0, 20.0, kind="visual"), [], str(out))
    assert plan.picture_height == 608
    _assert_valid(str(out), plan.duration, audio=False)
    frame = _rgb_frame(out, 5)
    middle = frame[960]
    assert middle[10][0] > 150 and middle[10][2] < 100, "the left edge of the picture is cropped"
    assert middle[1070][2] > 150 and middle[1070][0] < 100, "the right edge of the picture is cropped"
    assert middle[540].mean() - frame[300, 540].mean() > 25, "the backdrop should be dimmer than the picture"
    assert abs(frame[300, 540].mean() - frame[1700, 540].mean()) < 5, "both bands should match"


def test_framed_clip_with_full_edit_and_branding_renders_in_sync(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "clip_layout", "framed")
    monkeypatch.setattr(settings, "brand_name", "@MyChannel")
    source = tmp_path / "flash.mp4"
    _flash_and_beep_source(source)
    info = probe(str(source))
    words, now = [], 4.05
    while now < 36:
        words.append(Word("word,", now, now + 0.5, 0.99))
        now += 0.6
    segments = timeline.edit_segments(words, list(range(len(words))), 0.12)
    edit = EditPlan(words=words, segments=segments, speed=1.12, beats=[
        Beat(words[10].start, "40°C", STAT, "RECORD"), Beat(words[20].start, "", PUNCH), Beat(words[30].start, "BIG TURN", CARD),
    ])
    out = tmp_path / "out.mp4"
    plan = render_moment(str(source), info, analyse(str(source), info), Moment(4.0, 36.0, hook="Heat kills", kind="speech"),
                         words, str(out), edit=edit, creator="Some Creator")
    assert plan.picture_height == 608 and plan.has_sfx
    _assert_valid(str(out), plan.duration)


def test_framed_speaker_focus_keeps_the_speaker_and_crops_the_far_side(tmp_path, monkeypatch):
    from app.clipper import render as render_module

    monkeypatch.setattr(settings, "clip_layout", "framed")
    monkeypatch.setattr(render_module, "speaker_position", lambda *a: (0.95, 0.5))
    source = tmp_path / "striped.mp4"
    _striped_source(source)
    info = probe(str(source))
    words = [Word("word,", 5.0 + i * 0.6, 5.5 + i * 0.6, 0.99) for i in range(20)]
    out = tmp_path / "out.mp4"
    plan = render_moment(str(source), info, analyse(str(source), info), Moment(5.0, 17.0, kind="speech"), words, str(out))
    assert plan.focus_x_expr
    middle = _rgb_frame(out, 3)[960]
    assert middle[1070][2] > 150, "the speaker's side should stay in the picture"
    assert not (middle[10][0] > 150 and middle[10][2] < 100), "the far side should be cropped away"



def test_music_bed_renders_under_the_voice(landscape, tmp_path):
    bed = tmp_path / "bed.mp3"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=220:d=4", "-c:a", "libmp3lame", str(bed)],
                   check=True)
    info = probe(landscape)
    out = tmp_path / "out.mp4"
    plan = render_moment(landscape, info, analyse(landscape, info), Moment(5.0, 20.0, kind="visual"), [], str(out),
                         music=str(bed))
    assert plan.music
    _assert_valid(str(out), plan.duration)  # a 4-second track loops to fill the clip
