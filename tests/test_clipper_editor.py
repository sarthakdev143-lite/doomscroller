"""The editor seam: the bundle kept beside each clip, the spec, and re-rendering from it."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from app.clipper import cc0_sfx, editor, metering, reactions, sfx, standalone
from app.clipper.broll import StockClip
from app.clipper.edit import PUNCH, STAT, Beat, Cutaway, EditPlan, MemeOverlay, Reaction
from app.clipper.media import MediaInfo, probe, read_ffmpeg
from app.clipper.memes import MemeClip
from app.clipper.moments import Moment
from app.clipper.render import ClipPlan
from app.clipper.transcribe import Word
from app.config import settings

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
slow = [pytest.mark.slow, pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not on PATH")]
INFO = MediaInfo(duration=600.0, width=1280, height=720, fps=25.0, has_audio=True)


# ---------------------------------------------------------------- a bundle without ffmpeg

def _edit(meme_path="", stock_path="", speed=1.0):
    words = [Word(f"w{i}", 100.0 + i * 0.5, 100.0 + i * 0.5 + 0.4) for i in range(40)]
    plan = EditPlan(words=[w for w in words if w.start < 109.5 or w.start >= 111.0],
                    segments=[(100.0, 109.5), (111.0, 119.9)], speed=speed)
    plan.beats = [Beat(103.0, "", PUNCH), Beat(113.0, "40%", STAT)]
    plan.reactions = [Reaction(105.0, reactions.LAUGH)]
    if meme_path:
        plan.meme_overlays = [MemeOverlay(105.0, 1.5, MemeClip("giphy-abc", meme_path, "lol", "Giphy"),
                                          mood=reactions.LAUGH, sound=sfx.IMPACT)]
    if stock_path:
        plan.cutaways = [Cutaway(114.0, 2.0, "red car", StockClip("pexels-9", stock_path, 12.0, "Ana", "https://p/9",
                                                                     1.5, "Pexels"), subject="car")]
    return words, plan


def _fake_ffmpeg(calls):
    def run(args, **kw):
        calls.append(list(args))
        Path(args[-1]).write_bytes(b"x")
    return run


def _bundle(monkeypatch, tmp_path, *, edit=True, logo=True, custom=(), speed=1.0, sting=False):
    calls = []
    monkeypatch.setattr(editor, "run_ffmpeg", _fake_ffmpeg(calls))
    meme = tmp_path / "in" / "lol.mp4"
    stock = tmp_path / "in" / "car.mp4"
    logo_file = tmp_path / "in" / "logo.png"
    for f in (meme, stock, logo_file):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"data")
    words, plan = _edit(str(meme), str(stock), speed)
    sting_file = tmp_path / "in" / "boom.wav"
    sting_file.write_bytes(b"RIFF")
    clip = tmp_path / "out" / "clip_01_100_120.mp4"
    clip.parent.mkdir(parents=True)
    clip.write_bytes(b"clip")
    rendered = ClipPlan(segments=[(100.0, 109.5), (111.0, 120.3)], duration=19.7, has_audio=True, crop_width=1280,
                        crop_x_expr="0", use_blur_fill=False, zoom_expr="1", has_graphics=True,
                        edit_segments=((100.0, 109.5), (111.0, 119.9)), moment_start=100.0, moment_end=119.9,
                        speed=speed, tail=0.9 if sting else 0.0)
    signals = type("S", (), {"seconds": 600, "cuts": [50.0, 104.0, 130.0],
                             **{n: np.arange(600, dtype=float) for n in ("motion", "loudness", "brightness",
                                                                         "centroid", "centroid_weight")}})()
    manifest = editor.write_bundle(
        clip, src_path=str(tmp_path / "src.mp4"), info=INFO, signals=signals,
        moment=Moment(100.0, 119.9, title="T", hook="The hook", kind="speech", sound="boom" if sting else ""),
        edit=plan if edit else None, plan=rendered, words=words, language="en", timed=True, burned_in=False,
        captions=True, track=None, sting=str(sting_file) if sting else None, brand="", watermark="made with x", logo=str(logo_file) if logo else "",
        options={"captions": True}, title="T", description="D", hashtags=["#shorts"], score=88.0,
        custom_memes=custom)
    return manifest, json.loads(manifest.read_text(encoding="utf-8")), calls


def test_the_bundle_is_written_beside_the_clip_in_excerpt_time(monkeypatch, tmp_path):
    path, doc, calls = _bundle(monkeypatch, tmp_path)
    assert path.name == "clip_01_100_120_manifest.json"
    assert doc["excerpt"] == "clip_01_100_120_source.mp4" and doc["excerpt_offset_s"] == 98
    # The excerpt starts on a whole second, two before the cut, so the 30fps grid and the per-second
    # signals line up; it is re-encoded (never stream-copied, which would start on a keyframe).
    assert calls[0][:2] == ["-ss", "98"] and "libx264" in calls[0]
    assert doc["edit"]["segments"] == [[2.0, 11.5], [13.0, pytest.approx(21.9)]]
    assert doc["signals"]["motion"][0] == 98.0 and doc["signals"]["cuts"] == [6.0]
    # The clip-timeline view: caption i indexes the edit's words; times are seconds into the clip.
    first = doc["captions"][0]
    assert first == {"i": 0, "text": "w0", "start_s": 0.0, "end_s": 0.4}
    assert [m["id"] for m in doc["memes"]] == ["giphy-abc"] and doc["memes"][0]["start_s"] == 5.0
    assert doc["cutaways"] == [{"stock_id": "pexels-9", "start_s": 12.5, "duration_s": 2.0, "query": "red car"}]
    assert [b["start_s"] for b in doc["beats"]] == [3.0, 11.5]
    # Every asset is copied into the clip's own folder and named relative to it.
    for rel in [doc["logo"], doc["edit"]["memes"][0]["clip"]["path"], doc["edit"]["cutaways"][0]["clip"]["path"]]:
        assert not Path(rel).is_absolute() and (path.parent / rel).is_file()
    assert doc["memes_available"][0]["id"] == "giphy-abc"
    assert "clip_ai_api_key" not in json.dumps(doc) and "clip_sfx" in doc["render_settings"]


def _stub_render(monkeypatch, record):
    from app.clipper import render as render_mod

    def fake(source, info, signals, moment, words, out_path, **kw):
        record.append({"source": source, "moment": moment, "words": words, "signals": signals, **kw})
        Path(out_path).write_bytes(b"new")
        cut = tuple(kw["edit"].segments) if kw.get("edit") else ((moment.start, moment.end),)
        return ClipPlan(segments=list(cut), duration=sum(e - s for s, e in cut), has_audio=True, crop_width=1,
                        crop_x_expr="0", use_blur_fill=False, zoom_expr="1", has_graphics=True, edit_segments=cut)

    monkeypatch.setattr(render_mod, "render_moment", fake)
    monkeypatch.setattr(editor, "probe", lambda p: INFO)
    monkeypatch.setattr(standalone, "make_thumbnail", lambda path, d: None)
    monkeypatch.setattr(cc0_sfx, "ensure_pack", lambda folder=None: Path("cc0"))


def _no_network(monkeypatch):
    import httpx

    def refuse(*a, **k):
        raise AssertionError("the editor must not touch the network")

    for name in ("get", "post", "stream", "request"):
        monkeypatch.setattr(httpx, name, refuse)
    monkeypatch.setattr(standalone.ai, "ask_json", refuse)


def test_rerender_applies_the_spec_offline_and_deterministically(monkeypatch, tmp_path):
    path, doc, _ = _bundle(monkeypatch, tmp_path)
    (path.parent / doc["excerpt"]).write_bytes(b"x")
    record = []
    _stub_render(monkeypatch, record)
    _no_network(monkeypatch)
    spec = {"captions": {"edits": [{"i": 2, "text": "ZEBRA"}]}, "hook": {"text": "New hook"},
            "logo": {"position": "top-left", "scale": 0.5}, "memes": [], "music": {"volume_db": -100},
            "watermark_text": None, "trim": {"start_s": 1.0, "end_s": 15.0}}
    with metering.metered():
        result = editor.rerender_clip(path, spec, tmp_path / "v2.mp4")
        again = editor.rerender_clip(path, spec, tmp_path / "v3.mp4")
    first, second = record
    assert first["edit"].segments == second["edit"].segments and first["moment"] == second["moment"]
    assert first["edit"].meme_overlays == [] and first["moment"].hook == "New hook"
    assert [w.text for w in first["edit"].words if w.text == "ZEBRA"] == ["ZEBRA"]
    assert first["logo_position"] == "top-left" and first["logo_scale"] == 0.25 and first["watermark"] == ""
    assert first["edit"].segments[0][0] == pytest.approx(3.0) and first["skip_dark_opening"] is False
    # Signals come from the manifest, not a new pass over the excerpt.
    assert list(first["signals"].cuts) == [6.0]
    assert set(result) >= {"path", "thumbnail_path", "duration_s", "start_s", "end_s", "title", "description",
                           "hashtags", "score", "language", "kind", "hook", "memes_used", "manifest_path",
                           "source_excerpt_path", "meter"}
    assert result["start_s"] == pytest.approx(101.0) and result["memes_used"] == [] and again["path"].endswith("v3.mp4")


def test_trimmed_items_are_dropped_not_snapped_to_the_first_frame(monkeypatch, tmp_path):
    path, doc, _ = _bundle(monkeypatch, tmp_path)
    (path.parent / doc["excerpt"]).write_bytes(b"x")
    record = []
    _stub_render(monkeypatch, record)
    editor.rerender_clip(path, {"trim": {"start_s": 6.0, "end_s": 18.0}}, tmp_path / "v2.mp4")
    edit = record[0]["edit"]
    start = edit.segments[0][0]
    assert all(w.start >= start for w in edit.words)
    assert [b.style for b in edit.beats] == [STAT] and edit.meme_overlays == [] and edit.reactions == []
    assert len(edit.cutaways) == 1


def test_memes_only_from_the_clip_or_the_callers_own(monkeypatch, tmp_path):
    custom = tmp_path / "mine.png"
    from PIL import Image
    Image.new("RGB", (64, 64), (255, 0, 0)).save(custom)
    path, doc, _ = _bundle(monkeypatch, tmp_path)
    (path.parent / doc["excerpt"]).write_bytes(b"x")
    record = []
    _stub_render(monkeypatch, record)
    monkeypatch.setattr(editor, "_playable", lambda p, s: True)
    with pytest.raises(standalone.ClipError) as caught:
        editor.rerender_clip(path, {"memes": [{"id": "giphy-other", "start_s": 1}]}, tmp_path / "v.mp4")
    assert caught.value.code == "invalid_spec"
    from app.clipper.memes import custom_candidates
    mine = custom_candidates([custom])[0].meme_id
    editor.rerender_clip(path, {"memes": [{"id": "giphy-abc", "start_s": 2.0, "sound": False},
                                          {"id": mine, "start_s": 3.0, "duration_s": 4.0}]},
                         tmp_path / "v.mp4", custom_memes=[custom])
    overlays = record[0]["edit"].meme_overlays
    assert [o.clip.meme_id for o in overlays] == ["giphy-abc", mine]
    # The first ends where the second begins (they share a corner); a silenced meme carries no sound.
    assert overlays[0].seconds == pytest.approx(1.0) and overlays[0].sound == "none"
    assert overlays[1].sound == sfx.IMPACT and overlays[1].clip.path == str(custom.resolve())


@pytest.mark.parametrize("spec, message", [
    ({"colour": 1}, "unknown field"),
    ({"captions": {"enabled": True, "font": "x"}}, "unknown field"),
    ({"memes": [{"id": "giphy-abc", "volume": 2}]}, "unknown field"),
    ({"captions": {"enabled": 1}}, "true or false"),
    ({"hook": {"text": "x" * 81}}, "longer than 80"),
    ({"watermark_text": "x" * 41}, "longer than 40"),
    ({"captions": {"edits": [{"i": 9999, "text": "x"}]}}, "not a caption word"),
    ({"captions": {"edits": [{"i": True, "text": "x"}]}}, "not a caption word"),
    ({"captions": {"edits": [{"i": 0, "text": "   "}]}}, "cannot be empty"),
    ({"trim": {"start_s": 5.0, "end_s": 6.0}}, "at least 3"),
    ({"trim": {"start_s": float("nan")}}, "must be a number"),
    ({"logo": {"position": "middle"}}, "logo.position"),
    ({"memes": [{"id": "giphy-abc", "start_s": 1.0}, {"id": "giphy-abc", "start_s": 1.2}]}, "half a second"),
    ({"memes": "giphy-abc"}, "must be a list"),
    ([], "must be an object"),
])
def test_the_spec_is_validated(monkeypatch, tmp_path, spec, message):
    path, doc, _ = _bundle(monkeypatch, tmp_path)
    with pytest.raises(editor.SpecError, match=message):
        editor.validate_spec(spec, doc, [m["id"] for m in doc["memes_available"]])


def test_the_spec_is_clamped_and_cleaned(monkeypatch, tmp_path):
    path, doc, _ = _bundle(monkeypatch, tmp_path)
    got = editor.validate_spec({"logo": {"scale": 9}, "music": {"volume_db": 0}, "trim": {"start_s": -5, "end_s": 999},
                                "memes": [{"id": "giphy-abc", "duration_s": 60, "start_s": 999}],
                                "hook": {"text": "a\nb c\x00d"}}, doc, ["giphy-abc"])
    assert got["logo"]["scale"] == 0.25 and got["music"]["volume_db"] == -6.0
    assert got["trim"] == {"start_s": 0.0, "end_s": doc["duration_s"]}
    assert got["memes"][0]["duration_s"] == 4.0 and got["memes"][0]["start_s"] == doc["duration_s"] - 0.5
    assert got["hook"]["text"] == "a b c d"


def test_ass_overrides_in_customer_text_are_neutralised():
    from app.clipper.captions import _safe
    assert "{" not in _safe("{\\fs300}BIG") and "\\N" not in _safe("one\\Ntwo")


def test_a_visual_clip_takes_no_memes_and_a_bad_asset_path_is_refused(monkeypatch, tmp_path):
    path, doc, _ = _bundle(monkeypatch, tmp_path, edit=False)
    assert doc["edit"] is None
    with pytest.raises(editor.SpecError, match="speech"):
        editor.validate_spec({"memes": [{"id": "x"}]}, doc, ["x"])
    doc["excerpt"] = "../../escape.mp4"
    path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(standalone.ClipError) as caught:
        editor.rerender_clip(path, {}, tmp_path / "v.mp4")
    assert caught.value.code == "invalid_manifest"


def test_no_logo_to_move_is_an_invalid_spec(monkeypatch, tmp_path):
    path, doc, _ = _bundle(monkeypatch, tmp_path, logo=False)
    (path.parent / doc["excerpt"]).write_bytes(b"x")
    _stub_render(monkeypatch, [])
    with pytest.raises(standalone.ClipError) as caught:
        editor.rerender_clip(path, {"logo": {"position": "top-left"}}, tmp_path / "v.mp4")
    assert caught.value.code == "invalid_spec"


def test_sped_up_edits_map_spec_times_through_the_speed(monkeypatch, tmp_path):
    path, doc, _ = _bundle(monkeypatch, tmp_path, speed=1.1)
    # Clip time is source time over the speed: the meme 5s of source in lands at 5/1.1 in the clip.
    assert doc["memes"][0]["start_s"] == pytest.approx(5.0 / 1.1, abs=0.002)
    record = []
    _stub_render(monkeypatch, record)
    editor.rerender_clip(path, {"memes": [{"id": "giphy-abc", "start_s": 3.0}], "trim": {"start_s": 1.0, "end_s": 12.0}},
                         tmp_path / "v.mp4")
    edit = record[0]["edit"]
    assert edit.segments[0][0] == pytest.approx(2.0 + 1.1, abs=0.02)  # 1s of clip is 1.1s of source
    # The meme is placed on the original timeline, so after the 1s trim it shows 2s into the new clip.
    assert edit.to_output(edit.meme_overlays[0].at) == pytest.approx(2.0, abs=0.04)


def test_a_trimmed_end_drops_the_ending_sting_and_a_full_one_keeps_it(monkeypatch, tmp_path):
    path, doc, _ = _bundle(monkeypatch, tmp_path, sting=True)
    assert doc["ending_sting"] and doc["moment"]["sound"] == "boom"
    record = []
    _stub_render(monkeypatch, record)
    editor.rerender_clip(path, {"trim": {"start_s": 2.0}}, tmp_path / "a.mp4")
    editor.rerender_clip(path, {"trim": {"start_s": 0.0, "end_s": 10.0}}, tmp_path / "b.mp4")
    assert record[0]["sting"] and record[0]["moment"].sound == "boom"
    assert record[1]["sting"] is None


def test_sarvam_reads_the_mp3_the_clipper_extracts(monkeypatch, tmp_path):
    from app.clipper import sarvam
    pcm = np.zeros(16000 * 20, dtype=np.float32)
    pcm[16000 * 2:16000 * 9] = 0.3 * np.sin(np.arange(16000 * 7) * 0.07)
    pcm[16000 * 11:16000 * 19] = 0.3 * np.sin(np.arange(16000 * 8) * 0.07)
    monkeypatch.setattr(sarvam, "read_ffmpeg", lambda args, **k: pcm.tobytes())
    mp3 = tmp_path / "audio.mp3"
    mp3.write_bytes(b"ID3")
    active = sarvam.speech_frames(str(mp3))
    assert active is not None and active[500] and not active[1000] and active[1500]


def test_the_callers_logo_replaces_the_clips_own_but_is_never_added_unasked(monkeypatch, tmp_path):
    from PIL import Image
    brand = tmp_path / "brand.png"
    Image.new("RGB", (40, 20), (255, 0, 255)).save(brand)
    path, doc, _ = _bundle(monkeypatch, tmp_path, logo=False)
    record = []
    _stub_render(monkeypatch, record)
    editor.rerender_clip(path, {}, tmp_path / "a.mp4", logo_path=str(brand))
    editor.rerender_clip(path, {"logo": {"enabled": True, "position": "bottom-right"}}, tmp_path / "b.mp4",
                         logo_path=str(brand))
    assert record[0]["logo"] == "" and record[1]["logo"] == str(brand) and record[1]["logo_position"] == "bottom-right"
    with_logo, _, _ = _bundle(monkeypatch, tmp_path / "second")
    editor.rerender_clip(with_logo, {}, tmp_path / "c.mp4", logo_path=str(brand))
    editor.rerender_clip(with_logo, {"logo": {"enabled": False}}, tmp_path / "d.mp4", logo_path=str(brand))
    assert record[2]["logo"] == str(brand) and record[3]["logo"] == ""


def test_clip_to_directory_names_each_clips_bundle(monkeypatch, tmp_path):
    from tests.test_clipper_saas import _seam
    calls = _seam(monkeypatch, tmp_path)
    result = standalone.clip_to_directory("x", tmp_path / "job", count=1)
    clip = result["clips"][0]
    assert clip["manifest_path"].endswith("_manifest.json") and Path(clip["manifest_path"]).is_file()
    assert clip["source_excerpt_path"].endswith("_source.mp4")
    assert calls["bundle"][0]["signals"] is not None and calls["bundle"][0]["timed"] is True


# ---------------------------------------------------------------- the CC0 pack

def test_the_synthesised_pack_is_cc0_and_every_mood_rotates_two_sounds(monkeypatch, tmp_path):
    summary = cc0_sfx.build_pack(tmp_path / "cc0", source="synth")
    assert summary["sounds"] == 10 and summary["moods"] == sorted(reactions.MOODS)
    doc = json.loads((tmp_path / "cc0" / "MANIFEST.json").read_text(encoding="utf-8"))
    assert all(s["license"] == "CC0-1.0" and s["source"] == "synthesised" for s in doc["sounds"])
    monkeypatch.setattr(settings, "clip_sfx_pack_dir", str(tmp_path / "cc0"))
    for mood in reactions.MOODS:
        picks = {sfx.mood_sting(mood, f"clip{i}", language="en").name for i in range(16)}
        assert len(picks) == 2, (mood, picks)
        assert all(sfx.sting(str(tmp_path / "cc0" / "trending" / p)) is not None for p in picks)


def test_the_seam_defaults_to_the_cc0_pack_and_the_owner_keeps_theirs(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "clip_cc0_sfx_dir", str(tmp_path / "cc0"))
    monkeypatch.setattr(settings, "clip_sfx_pack_dir", "")
    pack = standalone.resolve_sfx_pack(None, tmp_path / "cache")
    assert pack == tmp_path / "cc0" and (pack / "MANIFEST.json").is_file()
    stamp = (pack / "MANIFEST.json").stat().st_mtime_ns
    assert standalone.resolve_sfx_pack(None, tmp_path / "cache") == pack
    assert (pack / "MANIFEST.json").stat().st_mtime_ns == stamp  # built once
    assert standalone.resolve_sfx_pack(tmp_path / "theirs", tmp_path / "cache") == tmp_path / "theirs"
    assert sfx.pack_dir() == settings.content_path / "sfx"  # the owner pipeline is unchanged


def test_freesound_cc0_pack_keeps_only_cc0_and_names_files_for_their_mood(monkeypatch, tmp_path):
    from app.clipper import trending_sfx as fs
    asked = []

    def request(url, params):
        asked.append(params)
        n = len(asked)
        return {"results": [
            {"id": 900 + n, "name": "nope", "username": "u", "license": "Attribution", "url": "https://fs/nope",
             "previews": {"preview-hq-mp3": "https://cdn/nope.mp3"}},
            {"id": n, "name": f"good {n}", "username": "ana", "license": "Creative Commons 0",
             "url": f"https://freesound.org/s/{n}/", "previews": {"preview-hq-mp3": f"https://cdn/{n}.mp3"}}]}

    downloaded = []

    def download(link, path):
        assert "nope" not in link
        downloaded.append(link)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b"x")

    monkeypatch.setattr(fs, "_request", request)
    monkeypatch.setattr(fs, "_download", download)
    monkeypatch.setattr(sfx, "sting", lambda path, max_sec=0.0: np.ones(10))
    entries = cc0_sfx.from_freesound(tmp_path)
    assert all('license:"Creative Commons 0"' in p["filter"] for p in asked)
    assert len(entries) == 12 and all(e["license"] == "CC0-1.0" and e["source_url"].startswith("https://freesound.org/")
                                      for e in entries)
    for e in entries:
        word = cc0_sfx.FREESOUND_QUERIES[e["mood"]][1]
        assert Path(e["file"]).name.startswith(f"{word}-")


def test_fetch_sfx_cc0_refuses_myinstants(capsys):
    import scripts.fetch_sfx as fetch
    assert fetch.main(["--cc0", "--source", "myinstants", "--json"]) == 1
    assert "not CC0" in capsys.readouterr().out


# ---------------------------------------------------------------- real renders

def _run(*args):
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args], check=True, capture_output=True)


def _frame(path, at):
    raw = read_ffmpeg(["-ss", f"{at:.3f}", "-i", str(path), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
    return np.frombuffer(raw, dtype=np.uint8).reshape(1920, 1080, 3).astype(float)


def _tone_level(path, start, seconds, freq):
    raw = read_ffmpeg(["-ss", f"{start:.3f}", "-t", f"{seconds:.3f}", "-i", str(path), "-ac", "1", "-ar", "48000",
                       "-f", "f32le", "-"])
    x = np.frombuffer(raw, dtype=np.float32)
    spectrum = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    freqs = np.fft.rfftfreq(len(x), 1 / 48000)
    return float(spectrum[(freqs > freq - 15) & (freqs < freq + 15)].max() / (spectrum.max() + 1e-9))


@pytest.fixture(scope="module")
def made(tmp_path_factory):
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg/ffprobe not on PATH")
    from app.clipper.render import render_moment
    from app.clipper.signals import analyse

    root = tmp_path_factory.mktemp("editor")
    src, meme, stock, logo = root / "src.mp4", root / "meme.mp4", root / "stock.mp4", root / "logo.png"
    _run("-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25:duration=40", "-f", "lavfi",
         "-i", "sine=frequency=300:duration=40", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(src))
    _run("-f", "lavfi", "-i", "color=c=red:s=320x320:r=30:d=3", "-f", "lavfi", "-i", "sine=frequency=1000:duration=3",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(meme))
    _run("-f", "lavfi", "-i", "smptebars=size=1280x720:rate=25:duration=6", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         str(stock))
    _run("-f", "lavfi", "-i", "color=c=0xFF00FF:s=200x100", "-frames:v", "1", str(logo))
    words = [Word(f"word{i}", 5.0 + i * 0.5, 5.0 + i * 0.5 + 0.4) for i in range(30)]
    edit = EditPlan(words=[w for w in words if w.start < 12.0 or w.start >= 13.0],
                    segments=[(5.0, 12.0), (13.0, 19.9)], speed=1.0)
    edit.beats = [Beat(8.0, "", PUNCH), Beat(16.0, "100%", STAT)]
    edit.meme_overlays = [MemeOverlay(10.0, 1.5, MemeClip("giphy-red", str(meme), "red", "Giphy"),
                                      mood=reactions.LAUGH, sound=str(meme))]
    edit.cutaways = [Cutaway(14.5, 2.0, "bars", StockClip("pexels-1", str(stock), 6.0, "Ana", "", 0.5, "Pexels"),
                             subject="bars")]
    moment = Moment(5.0, 19.9, title="Title", hook="A hook", kind="speech")
    info = probe(str(src))
    signals = analyse(str(src), info)
    out = root / "out" / "clip_01_5_19.mp4"
    plan = render_moment(str(src), info, signals, moment, words, str(out), edit=edit, captions=True, language="en",
                         logo=str(logo), brand="")
    manifest = editor.write_bundle(out, src_path=str(src), info=info, signals=signals, moment=moment, edit=edit,
                                   plan=plan, words=words, language="en", timed=True, burned_in=False, captions=True,
                                   track=None, sting=None, brand="", watermark="", logo=str(logo),
                                   options={"captions": True}, title="Title", description="D",
                                   hashtags=["#shorts"], score=80.0)
    doc = json.loads(manifest.read_text(encoding="utf-8"))
    return {"root": root, "out": out, "plan": plan, "manifest": manifest, "doc": doc}


def _rerender(made, spec, name, monkeypatch):
    _no_network(monkeypatch)
    return editor.rerender_clip(made["manifest"], spec, made["root"] / "edits" / f"{name}.mp4",
                                sfx_pack_dir=made["root"] / "no_pack")


@pytest.fixture(scope="module")
def baseline(made):
    mp = pytest.MonkeyPatch()
    try:
        return _rerender(made, {}, "same", mp)
    finally:
        mp.undo()


@pytest.mark.slow
@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not on PATH")
def test_an_empty_spec_renders_the_same_clip(made, baseline):
    doc = made["doc"]
    assert abs(baseline["duration_s"] - made["plan"].duration) <= 1 / 30 + 0.01
    assert abs(probe(baseline["path"]).duration - probe(str(made["out"])).duration) < 0.1
    for at in (doc["captions"][3]["start_s"] + 0.1, doc["memes"][0]["start_s"] + 0.5,
               doc["cutaways"][0]["start_s"] + 0.5, 2.0):
        diff = np.abs(_frame(made["out"], at) - _frame(baseline["path"], at)).mean()
        assert diff < 4.0, (at, diff)


@pytest.mark.slow
@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not on PATH")
def test_trim_shortens_the_clip(made, monkeypatch):
    got = _rerender(made, {"trim": {"start_s": 2.0, "end_s": 9.0}}, "trim", monkeypatch)
    assert abs(got["duration_s"] - 7.0) < 0.6
    assert abs(probe(got["path"]).duration - got["duration_s"]) < 0.2


@pytest.mark.slow
@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not on PATH")
def test_a_caption_edit_is_visible_in_the_caption_band_only(made, baseline, monkeypatch):
    word = made["doc"]["captions"][3]
    got = _rerender(made, {"captions": {"edits": [{"i": word["i"], "text": "ZEBRAZEBRA"}]}}, "caption", monkeypatch)
    at = word["start_s"] + 0.15
    rows = np.abs(_frame(got["path"], at) - _frame(baseline["path"], at)).mean(axis=(1, 2))
    changed = np.nonzero(rows > 8)[0]
    assert len(changed) > 20  # the new word is drawn
    band = (changed.min(), changed.max())
    assert band[0] > 900, band  # below the picture, where captions sit; the picture itself is untouched
    assert rows[:900].mean() < 1.0


def _magenta_mask(frame):
    return (frame[..., 0] > 180) & (frame[..., 1] < 90) & (frame[..., 2] > 180)


def _magenta(frame, x0, y0, x1, y1):
    return float(_magenta_mask(frame[y0:y1, x0:x1]).mean())


@pytest.mark.slow
@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not on PATH")
def test_the_logo_moves_to_a_corner(made, baseline, monkeypatch):
    got = _rerender(made, {"logo": {"position": "top-left", "scale": 0.2}}, "logo", monkeypatch)
    before, after = _frame(baseline["path"], 3.0), _frame(got["path"], 3.0)
    corner = (40, 120, 40 + 216, 120 + 108)
    assert _magenta(after, *corner) > 0.8 and _magenta(before, *corner) < 0.05
    ys, xs = np.nonzero(_magenta_mask(before))
    assert len(xs) > 5000  # the original logo, centred above the picture
    assert _magenta(after, xs.min(), ys.min(), xs.max() + 1, ys.max() + 1) < 0.05  # and gone from there


@pytest.mark.slow
@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not on PATH")
def test_removing_the_meme_removes_its_picture_and_its_sound(made, baseline, monkeypatch):
    got = _rerender(made, {"memes": []}, "nomeme", monkeypatch)
    at = made["doc"]["memes"][0]["start_s"]

    def red(frame):
        band = frame[:640, 500:]  # the meme corner, above the picture
        return float(((band[..., 0] > 200) & (band[..., 1] < 60) & (band[..., 2] < 60)).mean())

    assert red(_frame(baseline["path"], at + 0.6)) > 0.01 and red(_frame(got["path"], at + 0.6)) < 0.001
    assert _tone_level(baseline["path"], at, 1.0, 1000) > 0.2
    assert _tone_level(got["path"], at, 1.0, 1000) < 0.05
    assert got["memes_used"] == [] and baseline["memes_used"]
