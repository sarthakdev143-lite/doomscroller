"""Regression tests for the five quality complaints (Nov 2026), one block per root cause.

1. B-roll that made no sense: a quota prompt, a second looser search, unjudged fallbacks, and cutaways
   allowed over punchlines. 2. Memes and sounds placed mechanically on every beat and rhythm point.
3. Full-screen solid colour washes (a white flash box, a yellow card fill, a black dim). 4. Silent memes.
5. The same GIF in every video: no memory across jobs, and one file counted twice under two ids.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from app.clipper import ai, captions, edit, memes, reactions, render, sfx
from app.clipper.edit import Beat, Cutaway, EditPlan, MemeOverlay, Reaction, PUNCH, STAT, CARD
from app.clipper.media import MediaInfo
from app.clipper.moments import Moment
from app.clipper.signals import Signals
from app.clipper.transcribe import Word
from app.config import settings

INFO = MediaInfo(duration=120.0, width=1280, height=720, fps=30.0, has_audio=True)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "content_dir", str(tmp_path / "content"))
    monkeypatch.setattr(settings, "clip_memes", True)
    monkeypatch.setattr(settings, "giphy_api_key", "g")
    monkeypatch.setattr(settings, "tenor_api_key", "")


def _signals(seconds=120):
    return Signals(duration=float(seconds), motion=np.zeros(seconds), loudness=np.full(seconds, -40.0),
                   brightness=np.zeros(seconds), centroid=np.full(seconds, 0.5),
                   centroid_weight=np.ones(seconds), cuts=[])


def _words(n=60, start=0.0, step=0.5):
    return [Word(f"w{i}", start + i * step, start + i * step + 0.4, 0.99) for i in range(n)]


# ---------- 3. no full-frame solid colour, anywhere ----------

def test_beats_never_put_a_fill_or_flash_in_the_filter_graph():
    plan_edit = EditPlan(words=_words(), segments=[(0.0, 30.0)], speed=1.0,
                         beats=[Beat(5.0, "", PUNCH), Beat(10.0, "40%", STAT), Beat(16.0, "BIG TURN", CARD)])
    plan = render.plan_clip(Moment(0, 30), [], _signals(), INFO, has_graphics=True, edit=plan_edit)
    graph = render.build_filtergraph(plan, INFO, 0.0)
    assert "drawbox" not in graph and "color=white" not in graph and "t=fill" not in graph
    assert not hasattr(plan, "flash_expr")
    # The shake is still there: a punch is a framing change, a shake and a hit, not a wash.
    assert "sin(t*75)" in graph


def test_stat_and_card_graphics_draw_text_only_never_a_full_frame_shape():
    beats = [(4.0, "40°C", "stat", "RECORD", 1.6), (8.0, "BIG TURN", "card", "", 0.9)]
    ass = captions.build_ass([], "", 20.0, "Font", None, beats)
    events = [line for line in ass.splitlines() if line.startswith("Dialogue")]
    assert events and not any("\\p1" in e for e in events), "no drawing commands at all"
    assert not any("m 0 0 l 1080 0" in e for e in events)
    card = next(e for e in events if "BIG TURN" in e)
    assert "&H00E5FF&" in card and "&H111111&" not in card  # yellow text with its outline, not dark text on yellow


def test_the_only_lavfi_colour_source_is_the_ten_pixel_progress_bar():
    import inspect
    src = inspect.getsource(render.render_moment)
    assert src.count("color=c=") == 1 and "s={WIDTH}x10" in src


def test_the_watermark_is_small_text_above_the_brand_inside_the_safe_band():
    ass = captions.build_ass([], "", 10.0, "Font", None, (), brand="@me", watermark="made with clipper")
    line = next(l for l in ass.splitlines() if "made with clipper" in l)
    assert ",Watermark," in line and "\\pos(540,123)" in line  # brand at 175, watermark 52px above
    style = next(l for l in ass.splitlines() if l.startswith("Style: Watermark"))
    assert style.split(",")[2] == "36"


# ---------- 1. B-roll only where the speaker names a concrete thing, and never unjudged ----------

@pytest.mark.parametrize("subject,query,sentence,ok", [
    ("flooded street", "flooded street", "The flooded street outside was knee deep.", True),
    ("basketball hoop", "basketball hoop close", "He hit the basketball hoop twice.", True),
    ("game", "chess game", "Bro, you've literally ruined the game.", False),          # generic noun
    ("water", "water slick", "these are super slick, with a bit of water", True),       # concrete word; judge decides
    ("wet ball", "duck pond", "if you make the ball wet you win", False),              # query drops the subject
    ("rolls royce", "rolls royce", "The audio is playing through the car speakers.", False),  # not in the sentence
    ("", "rain", "It rained all day.", False),                                          # no subject at all
])
def test_cutaway_must_name_a_concrete_thing_the_speaker_said(subject, query, sentence, ok):
    assert edit.grounded_subject(subject, query, sentence) is ok


def test_cutaways_never_cover_a_beat_a_reaction_or_the_payoff():
    plan = EditPlan(words=_words(80), segments=[(0.0, 40.0)], speed=1.0,
                    beats=[Beat(10.0, "", PUNCH)], reactions=[Reaction(20.0, "laugh")], payoff_at=32.0)
    shots = [Cutaway(9.5, 2.5, "a"), Cutaway(19.5, 2.5, "b"), Cutaway(26.0, 2.5, "c"), Cutaway(31.0, 2.5, "d"),
             Cutaway(4.0, 2.5, "e")]
    kept = edit._spaced_cutaways(shots, plan)
    assert [c.query for c in kept] == ["e", "c"]


def test_broll_is_at_most_a_third_of_the_clip_and_spaced_out():
    plan = EditPlan(words=_words(80), segments=[(0.0, 40.0)], speed=1.0)
    shots = [Cutaway(float(t), 3.0, f"q{t}") for t in range(3, 36, 2)]
    kept = edit._spaced_cutaways(shots, plan)
    assert sum(c.seconds for c in kept) <= 40 * 0.3
    times = [c.at for c in kept]
    assert all(b - a >= 3.0 + 4.0 - 1e-6 for a, b in zip(times, times[1:]))


def test_a_rejected_shot_never_blocks_the_next_concrete_thing(monkeypatch):
    """Zurich: 'big airport' then 'chocolate' 4s later. The airport shots were all rejected, and the planned
    6s gap had already dropped the chocolate, so the clip got no B-roll at all. Spacing now applies among
    shots that actually got footage."""
    from app.clipper import broll
    monkeypatch.setattr(settings, "pixabay_api_key", "k")
    monkeypatch.setattr(settings, "clip_broll", True)
    monkeypatch.setattr(ai, "available", lambda: True)
    plan = EditPlan(words=_words(80), segments=[(0.0, 40.0)], speed=1.0)
    plan.cutaways = edit._spaced_cutaways([Cutaway(5.5, 2.0, "busy airport terminal", subject="airport"),
                                           Cutaway(9.5, 2.0, "swiss chocolate shop", subject="chocolate"),
                                           Cutaway(13.0, 2.0, "chocolate bars", subject="chocolates")], plan,
                                          gap=edit._PLAN_CUTAWAY_GAP_SEC, share=edit._PLAN_CUTAWAY_SHARE)
    assert len(plan.cutaways) == 3
    searched = []

    def fetch(query, seconds, exclude=(), choose=None):
        searched.append(query)
        return None if "airport" in query else broll.StockClip(query, f"/x/{query}.mp4", 10.0, offset=0.4)

    monkeypatch.setattr(broll, "fetch", fetch)
    edit.attach_stock(plan)
    # The airport was rejected (and its subject-only retry too), the chocolate shop got footage, and the
    # third is too close to it to be used.
    assert searched == ["busy airport terminal", "airport", "swiss chocolate shop"]
    assert [c.query for c in plan.cutaways] == ["swiss chocolate shop"]


def test_the_edit_prompt_no_longer_demands_broll_and_says_when_in_doubt_leave_it_out():
    prompt = edit._edit_prompt([], "x")
    assert "do not hold back" not in prompt and "for every 5 seconds" not in prompt
    assert "When in doubt, leave it out" in prompt and '"subject"' in prompt
    assert '"reactions"' in prompt and "at least one every 5 seconds" not in prompt


def test_no_ai_means_no_stock_search_at_all(monkeypatch):
    from app.clipper import broll
    monkeypatch.setattr(settings, "pixabay_api_key", "k")
    monkeypatch.setattr(settings, "clip_broll", True)
    monkeypatch.setattr(ai, "available", lambda: False)
    monkeypatch.setattr(broll, "search", lambda q: pytest.fail("searched without a judge"))
    plan = EditPlan(words=_words(), segments=[(0.0, 30.0)], cutaways=[Cutaway(8.0, 2.5, "rain")])
    assert edit.attach_stock(plan) and plan.cutaways == []


# ---------- 2. memes only on marked reactions; sounds on meaning, not rhythm ----------

def test_reactions_are_parsed_by_meaning_and_land_as_the_punchline_ends():
    from app.clipper.moments import build_sentences
    words = [Word(t, i * 0.4, i * 0.4 + 0.3) for i, t in enumerate("I tried it. Bro, you ruined the game.".split())]
    sentences = build_sentences(words)
    reply = {"reactions": [{"sentence": 1, "phrase": "you ruined the game", "mood": "Fail"},
                           {"sentence": 1, "phrase": "you ruined the game", "mood": "sarcasm"},
                           {"sentence": 0, "phrase": "not here", "mood": "laugh"}]}
    found = edit._reply_reactions(reply, sentences, words, set(range(len(words))))
    assert [(round(r.at, 2), r.mood) for r in found] == [(round(words[-1].end - 0.05, 2), "fail")]


def test_punch_beats_alone_never_get_a_meme(monkeypatch):
    monkeypatch.setattr(memes, "fetch", lambda *a, **k: pytest.fail("no reaction, no lookup"))
    plan = EditPlan(words=_words(), segments=[(0.0, 30.0)], beats=[Beat(5.0, "", PUNCH), Beat(12.0, "", PUNCH)])
    assert edit.attach_memes(plan) == [] and plan.meme_overlays == []


def test_sounds_follow_meaning_not_the_rhythm_of_the_edit():
    plan = EditPlan(words=_words(), segments=[(0.0, 30.0)], speed=1.0, beats=[Beat(10.0, "", PUNCH)])
    points = render.rhythm_points(plan)
    assert len(points) >= 10, "the framing still changes every 1-2.4s"
    cues = render.sound_cues(plan, points)
    assert [(c.at, c.sound) for c in cues] == [(0.0, sfx.IMPACT), (10.0, sfx.IMPACT)]


def test_sounds_never_stack_and_a_meme_sound_wins(tmp_path):
    sting = tmp_path / "bruh.mp3"
    sting.write_bytes(b"x")
    clip = memes.MemeClip("giphy-1", str(tmp_path / "m.mp4"), "T", memes.GIPHY)
    from app.clipper.broll import StockClip
    plan = EditPlan(words=_words(), segments=[(0.0, 30.0)], speed=1.0,
                    beats=[Beat(10.0, "", PUNCH), Beat(20.0, "", PUNCH)],
                    cutaways=[Cutaway(20.1, 2.0, "x", StockClip("s", "/x.mp4", 9.0))],
                    meme_overlays=[MemeOverlay(10.1, 1.5, clip, mood="fail", sound=str(sting))])
    cues = render.sound_cues(plan)
    near_ten = [c for c in cues if 9.5 < c.at < 10.6]
    assert len(near_ten) == 1 and near_ten[0].sound == str(sting) and near_ten[0].gain < 1.0
    near_twenty = [c for c in cues if 19.5 < c.at < 20.6]
    assert len(near_twenty) == 1 and near_twenty[0].sound == sfx.IMPACT
    times = sorted(c.at for c in cues)
    assert all(b - a >= 0.45 for a, b in zip(times, times[1:]))


def test_with_effects_off_a_meme_still_has_its_sound():
    clip = memes.MemeClip("giphy-1", "/m.mp4", "T", memes.GIPHY)
    plan = EditPlan(words=_words(), segments=[(0.0, 30.0)], speed=1.0, beats=[Beat(5.0, "", PUNCH)],
                    meme_overlays=[MemeOverlay(12.0, 1.5, clip, sound=sfx.IMPACT)])
    assert [(c.at, c.sound) for c in render.sound_cues(plan, effects=False)] == [(12.0, sfx.IMPACT)]
    rplan = render.plan_clip(Moment(0, 30), [], _signals(), INFO, has_graphics=False, edit=plan)
    assert rplan.has_sfx and rplan.memes


# ---------- 4. every meme carries sound ----------

def _one_reaction_plan(at=8.0, mood="laugh", seconds=30.0):
    return EditPlan(words=_words(), segments=[(0.0, seconds)], speed=1.0, reactions=[Reaction(at, mood)])


def test_a_silent_gif_gets_a_pack_sting_that_fits_its_mood(tmp_path, monkeypatch):
    pack = tmp_path / "sfx" / "trending"
    pack.mkdir(parents=True)
    for name in ("haha-laugh-funny-moment.mp3", "bruh.mp3", "vine-boom-sound.mp3"):
        (pack / name).write_bytes(b"x")
    monkeypatch.setattr(settings, "clip_sfx_pack_dir", str(tmp_path / "sfx"))
    monkeypatch.setattr(sfx, "sting", lambda path, max_sec=0.0: np.ones(100))
    gif = tmp_path / "g.mp4"
    gif.write_bytes(b"gif")
    monkeypatch.setattr(memes, "fetch", lambda q, s, *, exclude=(), language="": memes.MemeClip("giphy-9", str(gif), "LOL", memes.GIPHY))
    from app.clipper import media
    monkeypatch.setattr(media, "probe", lambda p: MediaInfo(2.0, 480, 270, 25.0, False))
    plan = _one_reaction_plan(mood="laugh")
    edit.attach_memes(plan, language="en")
    assert Path(plan.meme_overlays[0].sound).name == "haha-laugh-funny-moment.mp3"
    plan2 = _one_reaction_plan(mood="fail")
    edit.attach_memes(plan2, language="en")
    assert Path(plan2.meme_overlays[0].sound).name == "bruh.mp3"


def test_with_no_pack_a_meme_falls_back_to_a_synthesised_hit_never_silence(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "clip_sfx_pack_dir", str(tmp_path / "empty"))
    monkeypatch.setattr(memes, "fetch", lambda q, s, *, exclude=(), language="": memes.MemeClip("giphy-9", "/nope.mp4", "T", memes.GIPHY))
    plan = _one_reaction_plan()
    edit.attach_memes(plan)
    assert plan.meme_overlays and plan.meme_overlays[0].sound == sfx.IMPACT


def test_a_pack_sound_that_will_not_decode_becomes_a_hit_never_silence(tmp_path, monkeypatch):
    pack = tmp_path / "sfx" / "trending"
    pack.mkdir(parents=True)
    (pack / "bruh.mp3").write_bytes(b"not audio")
    monkeypatch.setattr(settings, "clip_sfx_pack_dir", str(tmp_path / "sfx"))
    monkeypatch.setattr(memes, "fetch", lambda q, s, *, exclude=(), language="": memes.MemeClip("giphy-9", "/nope.mp4", "T", memes.GIPHY))
    plan = _one_reaction_plan(mood="fail")
    edit.attach_memes(plan)
    assert plan.meme_overlays[0].sound == sfx.IMPACT
    # And if a file cue still fails at mix time, the track gets a hit there instead of nothing.
    import wave
    out = tmp_path / "t.wav"
    sfx.build_track(str(out), 4.0, [sfx.Cue(1.0, str(pack / "bruh.mp3"), gain=0.5)])
    with wave.open(str(out)) as handle:
        samples = np.frombuffer(handle.readframes(handle.getnframes()), np.int16)
    assert np.abs(samples[int(sfx.RATE * 1.0):int(sfx.RATE * 1.4)]).max() > 1000


def test_a_clip_without_audio_gets_no_memes(monkeypatch):
    monkeypatch.setattr(memes, "fetch", lambda *a, **k: pytest.fail("a silent clip cannot carry a meme's sound"))
    plan = _one_reaction_plan()
    assert edit.attach_memes(plan, has_audio=False) == [] and plan.meme_overlays == []
    rplan = render.plan_clip(Moment(0, 30), [], _signals(), MediaInfo(120.0, 1280, 720, 30.0, False),
                             has_graphics=False, edit=EditPlan(words=[], segments=[(0.0, 30.0)], meme_overlays=[
                                 MemeOverlay(5.0, 1.5, memes.MemeClip("m", "/m.mp4"), sound=sfx.IMPACT)]))
    assert rplan.memes == ()


def test_a_meme_sound_is_mixed_under_the_voice_and_cut_with_its_picture(tmp_path, monkeypatch):
    import wave
    sting = tmp_path / "s.wav"
    with wave.open(str(sting), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sfx.RATE)
        handle.writeframes((np.ones(sfx.RATE * 4) * 20000).astype(np.int16).tobytes())
    out = tmp_path / "t.wav"
    sfx.build_track(str(out), 10.0, [sfx.Cue(1.0, str(sting), gain=sfx.MEME_STING_GAIN, max_sec=2.1)])
    with wave.open(str(out)) as handle:
        samples = np.frombuffer(handle.readframes(handle.getnframes()), np.int16).astype(float) / 32767
    loud = np.flatnonzero(np.abs(samples) > 0.01)
    assert abs(np.abs(samples).max() - 0.9 * sfx.MEME_STING_GAIN) < 0.02
    assert (loud[-1] - loud[0]) / sfx.RATE <= 2.15


def test_a_punchline_on_the_last_line_plays_its_meme_over_the_held_ending_with_the_ending_sting(tmp_path, monkeypatch):
    monkeypatch.setattr(memes, "fetch", lambda q, s, *, exclude=(), language="": memes.MemeClip("giphy-1", "/m.mp4", "T", memes.GIPHY))
    plan = _one_reaction_plan(at=29.8, seconds=30.0)
    plan.tail_room = 2.0
    edit.attach_memes(plan, ending_sound=str(tmp_path / "bruh.mp3"))
    assert len(plan.meme_overlays) == 1 and plan.meme_overlays[0].sound == str(tmp_path / "bruh.mp3")
    # Without a held ending it starts a little early instead, as long as that is within 1.5s.
    plan2 = _one_reaction_plan(at=29.8, seconds=30.0)
    edit.attach_memes(plan2)
    when = plan2.to_output(plan2.meme_overlays[0].at)
    assert when == pytest.approx(28.5, abs=0.05) and plan2.meme_overlays[0].sound != str(tmp_path / "bruh.mp3")


def test_the_ending_sting_plays_once_when_a_meme_shares_it(tmp_path, monkeypatch):
    sting = tmp_path / "bruh.wav"
    import wave
    with wave.open(str(sting), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sfx.RATE)
        handle.writeframes((np.ones(sfx.RATE) * 9000).astype(np.int16).tobytes())
    clip = memes.MemeClip("giphy-1", str(tmp_path / "m.mp4"), "T", memes.GIPHY)
    plan_edit = EditPlan(words=_words(20, start=1.0), segments=[(1.0, 11.0)], speed=1.0,
                         meme_overlays=[MemeOverlay(10.9, 1.5, clip, sound=str(sting))])
    built = []
    monkeypatch.setattr(render, "build_track", lambda path, dur, cues, **kw: built.extend(cues) or Path(path).write_bytes(b"W"))
    monkeypatch.setattr(render, "run_ffmpeg", lambda args, cwd=None: Path(args[-1]).write_bytes(b"0" * 4096))
    monkeypatch.setattr(render, "probe", lambda p: MediaInfo(13.0, 1080, 1920, 30.0, True))
    monkeypatch.setattr(render, "_bright_sharp_opening_offset", lambda *a, **k: 0.0)
    src = tmp_path / "src.mp4"
    src.write_bytes(b"s")
    render.render_moment(str(src), MediaInfo(60.0, 1080, 1920, 30.0, True), _signals(60), Moment(1.0, 11.0),
                         [], str(tmp_path / "o.mp4"), edit=plan_edit, sting=str(sting), captions=False)
    same = [c for c in built if Path(c.sound) == sting]
    assert len(same) == 1 and same[0].gain == 1.0


# ---------- 5. no GIF twice in a clip, nor in consecutive videos ----------

def _library(tmp_path, monkeypatch, count=6):
    """A fake reaction library: every search returns the same ranked results, like the 24h cache did."""
    files = {}
    for i in range(count):
        path = tmp_path / f"giphy-r{i}.mp4"
        path.write_bytes(f"gif-{i}".encode())
        files[f"giphy-r{i}"] = path
    ranked = [memes.Candidate(mid, f"https://media/{mid}.mp4", f"Reaction {mid}", "", memes.GIPHY) for mid in files]
    monkeypatch.setattr(memes, "search", lambda q: list(ranked))
    monkeypatch.setattr(memes, "_local_copy", lambda c, s: files[c.meme_id])
    return files


def _job(history: Path, moods=("laugh", "shock")) -> list[str]:
    """One clip job: history loaded at the start, each placed meme recorded, as job.clip_source does."""
    session = memes.MemeSession(recent=memes.load_history(history))
    plan = EditPlan(words=_words(100), segments=[(0.0, 45.0)], speed=1.0,
                    reactions=[Reaction(5.0 + 20 * i, m) for i, m in enumerate(moods)])
    edit.attach_memes(plan, session)
    for overlay in plan.meme_overlays:
        memes.record_history(memes.identity_keys(overlay.clip), history)
    return [o.clip.meme_id for o in plan.meme_overlays]


def test_consecutive_jobs_never_reuse_a_meme(tmp_path, monkeypatch):
    _library(tmp_path, monkeypatch, count=6)
    history = tmp_path / "meme_history.json"
    first, second, third = _job(history), _job(history), _job(history)
    assert len(first) == 2 and len(second) == 2 and len(third) == 2
    assert not set(first) & set(second) and not set(second) & set(third) and not set(first) & set(third)


def test_a_pool_too_small_means_fewer_memes_never_a_repeat(tmp_path, monkeypatch):
    _library(tmp_path, monkeypatch, count=3)
    history = tmp_path / "meme_history.json"
    first, second = _job(history), _job(history)
    assert len(first) == 2 and len(second) == 1 and not set(first) & set(second)
    assert _job(history) == []


def test_one_file_under_two_names_counts_once(tmp_path, monkeypatch):
    """The Nov 25-26 clips showed one GIF twice: once as giphy-X, once as local-giphy-X."""
    a = tmp_path / "giphy-X.mp4"
    b = tmp_path / "copy_of_it.mp4"
    a.write_bytes(b"same-bytes")
    b.write_bytes(b"same-bytes")
    assert memes.canonical_id("local-giphy-X") == "giphy-X" and memes.canonical_id("local-mine") == "local-mine"
    assert memes.content_key(a) == memes.content_key(b)
    ranked = [memes.Candidate("giphy-X", "https://m/X.mp4", "", "", memes.GIPHY),
              memes.Candidate("giphy-Y", "https://m/Y.mp4", "", "", memes.GIPHY)]
    files = {"giphy-X": b, "giphy-Y": tmp_path / "giphy-Y.mp4"}
    files["giphy-Y"].write_bytes(b"other")
    monkeypatch.setattr(memes, "search", lambda q: ranked)
    monkeypatch.setattr(memes, "_local_copy", lambda c, s: files[c.meme_id])
    got = memes.fetch("laughing reaction", 1.5, exclude={memes.content_key(a)})
    assert got.meme_id == "giphy-Y"
    assert memes.fetch("laughing reaction", 1.5, exclude={"local-giphy-X", "giphy-Y"}) is None or \
        memes.fetch("laughing reaction", 1.5, exclude={"giphy-X", "giphy-Y"}) is None


def test_history_is_bounded_and_survives_a_corrupt_file(tmp_path):
    history = tmp_path / "h.json"
    history.write_text("{not json", encoding="utf-8")
    assert memes.load_history(history) == set()
    for i in range(memes._HISTORY_KEEP + 20):
        memes.record_history([f"giphy-{i}"], history)
    data = json.loads(history.read_text(encoding="utf-8"))
    assert len(data["memes"]) == memes._HISTORY_KEEP
    recent = memes.load_history(history)
    assert len(recent) == memes.HISTORY_AVOID and f"giphy-{memes._HISTORY_KEEP + 19}" in recent


def test_history_lives_outside_the_folder_cleanup_ages(tmp_path):
    assert memes.history_path().parent == settings.content_path
    assert memes.history_path().name == "meme_history.json"


def test_searches_rotate_by_mood_across_clips(monkeypatch):
    asked = []
    monkeypatch.setattr(memes, "fetch", lambda q, s, *, exclude=(), language="": asked.append(q) or None)
    for placed in range(3):
        session = memes.MemeSession()
        session.placed = [f"x{i}" for i in range(placed)]
        edit.attach_memes(_one_reaction_plan(mood="shock"), session)
    firsts = [asked[i * len(reactions.QUERIES["shock"])] for i in range(3)]
    assert len(set(firsts)) == 3 and all(q in reactions.QUERIES["shock"] for q in firsts)


def test_hindi_clean_vlog_edits_get_memes_only_where_the_ai_marks_a_reaction(monkeypatch):
    words = [Word(t, 10 + i * 0.5, 10.4 + i * 0.5) for i, t in enumerate("भाई ने क्या किया. पूरा गेम खराब कर दिया.".split())]
    plan = EditPlan(words=[], segments=[(10.0, 40.0)], speed=1.0)
    monkeypatch.setattr(ai, "available", lambda: True)
    prompts = []
    monkeypatch.setattr(ai, "ask_json", lambda p, **k: prompts.append(p) or {"reactions": [
        {"sentence": 1, "phrase": "गेम खराब कर दिया", "mood": "fail"}]})
    edit.mark_reactions(plan, Moment(10.0, 40.0), words, title="t")
    assert [(r.mood, round(r.at, 2)) for r in plan.reactions] == [("fail", round(words[-1].end - 0.05, 2))]
    assert "most clips have zero or one" in prompts[0]
    monkeypatch.setattr(ai, "available", lambda: False)
    quiet = EditPlan(words=[], segments=[(10.0, 40.0)], speed=1.0)
    edit.mark_reactions(quiet, Moment(10.0, 40.0), words)
    assert quiet.reactions == []


def test_an_english_pack_without_a_laugh_finds_one_in_trending_but_never_a_hindi_only_sound(tmp_path, monkeypatch):
    root = tmp_path / "sfx"
    for folder, names in {"en": ["bruh.mp3"], "trending": ["haha-laugh-funny-moment.mp3", "aayein-confused.mp3"],
                          "hi": ["aayein-confused.mp3"]}.items():
        (root / folder).mkdir(parents=True)
        for name in names:
            (root / folder / name).write_bytes(b"x")
    monkeypatch.setattr(settings, "clip_sfx_pack_dir", str(root))
    assert sfx.mood_sting("laugh", "seed", language="en").name == "haha-laugh-funny-moment.mp3"
    assert sfx.mood_sting("fail", "seed", language="en").name == "bruh.mp3"
    assert sfx.mood_sting("awkward", "seed", language="en") is None  # aayein is Hindi-only
    assert sfx.mood_sting("awkward", "seed", language="hi").name == "aayein-confused.mp3"
