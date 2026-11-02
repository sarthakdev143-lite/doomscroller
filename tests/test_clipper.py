"""Fast tests for the clipper: everything except real rendering."""
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
import pytest
from app.clipper import ai, media
from app.clipper.media import Interrupted, MediaError
from app.config import settings


@pytest.fixture(autouse=True)
def isolated_content(tmp_path, monkeypatch):
    """Keep test clips and transcripts out of the real content directory."""
    monkeypatch.setattr(settings, "content_dir", str(tmp_path / "content"))


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
