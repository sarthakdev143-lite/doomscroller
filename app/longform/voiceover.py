"""Zero-cost neural voiceover commentary for 16:9 compilations (Edge-TTS primary + Windows SAPI fallback)."""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Sequence

from app.clipper import ai
from app.clipper.media import run_ffmpeg
from app.clipper.render import _filter_path_arg
from app.config import settings
from app.transformers.fonts import resolve_font
from app.winproc import background_subprocess_kwargs

if TYPE_CHECKING:
    from .planner import CompilationPlan

log = logging.getLogger(__name__)

_VOICES = {
    "en": "en-US-AndrewMultilingualNeural",
    "hi": "hi-IN-MadhurNeural",
}
_PS_SAPI_SCRIPT = (
    "Add-Type -AssemblyName System.Speech; "
    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
    "$s.Rate = 1; "
    "$s.SetOutputToWaveFile($env:DOOMSCROLLER_TTS_OUT); "
    "$s.Speak($env:DOOMSCROLLER_TTS_TEXT); "
    "$s.Dispose()"
)


def _voice_for(language: str) -> str:
    code = str(language or "en").strip().lower().split("-")[0]
    return _VOICES.get(code, _VOICES["en"])


def _clean_spoken(text: str, max_chars: int = 240) -> str:
    clean = " ".join(str(text or "").replace("#Shorts", "").replace("#shorts", "").split()).strip()
    clean = clean.replace("#", "").strip()
    if len(clean) <= max_chars:
        return clean
    return clean[:max_chars].rsplit(" ", 1)[0].strip() or clean[:max_chars]


def _default_script(plan: "CompilationPlan") -> dict:
    segs = list(plan.segments)
    n = len(segs)
    hi = str(plan.language or "en").strip().lower().startswith("hi")
    who = ", ".join(plan.targets) if plan.targets else "top creators"
    if hi:
        intro = f"Aaj dekhte hain {who} ke {n} sabse zabardast moments. Number one miss mat karna!"
        bridges = []
        for idx, s in enumerate(segs, start=1):
            rank = (n - idx + 1) if plan.format_kind != "vs" else idx
            if plan.format_kind == "vs":
                bridges.append(f"Round {idx}, {s.creator} ka yeh clip dekho.")
            elif rank == 1:
                bridges.append("Aur ab number one par aaj ka sabse bada moment!")
            else:
                bridges.append(f"Number {rank}, {s.creator} ki taraf se.")
        outro = "Kaunsa moment best laga, comment me zaroor batao aur channel ko subscribe kar lo!"
    else:
        intro = f"Here are the top {n} craziest moments from {who}. Wait until you see number one."
        bridges = []
        for idx, s in enumerate(segs, start=1):
            rank = (n - idx + 1) if plan.format_kind != "vs" else idx
            if plan.format_kind == "vs":
                bridges.append(f"Round {idx} goes to {s.creator}.")
            elif rank == 1:
                bridges.append("And taking the number one spot, watch this finale.")
            else:
                bridges.append(f"At number {rank}, {s.creator} brings this moment.")
        outro = "Which moment won for you? Drop your pick below and subscribe for the next compilation."
    return {"intro": intro, "bridges": bridges, "outro": outro}


def generate_commentary_script(plan: "CompilationPlan") -> dict:
    """Generate short transformative countdown commentary script for a 16:9 compilation."""
    fallback = _default_script(plan)
    segs = list(plan.segments)
    if not segs or not ai.available():
        return fallback

    n = len(segs)
    hi = str(plan.language or "en").strip().lower().startswith("hi")
    lang_note = "Hinglish (casual Hindi written cleanly in English/Latin script)" if hi else "natural conversational English"
    rows = []
    for idx, s in enumerate(segs, start=1):
        label = f"Round {idx}" if plan.format_kind == "vs" else f"#{n - idx + 1}"
        rows.append(f"{idx - 1}. {label} | {s.creator} | {s.moment_title}")
    prompt = (
        f"Write crisp energetic narrator lines in {lang_note} for a {n}-clip YouTube compilation titled {plan.title!r}.\n"
        f"Segments in playback order:\n" + "\n".join(rows) + "\n\n"
        "Rules: short punchy spoken sentences (no emojis, no hashtags), intro <= 18 words, each bridge <= 12 words, outro <= 16 words.\n"
        f'Reply with JSON only: {{"intro": "...", "bridges": ["..." ({n} items matching 0..{n - 1})], "outro": "..."}}'
    )
    reply = ai.ask_json(prompt)
    if not isinstance(reply, dict):
        return fallback
    intro = _clean_spoken(reply.get("intro") or "") or fallback["intro"]
    outro = _clean_spoken(reply.get("outro") or "") or fallback["outro"]
    raw_bridges = reply.get("bridges")
    bridges: list[str] = []
    for i, fb in enumerate(fallback["bridges"]):
        cand = raw_bridges[i] if isinstance(raw_bridges, list) and i < len(raw_bridges) else ""
        bridges.append(_clean_spoken(cand, max_chars=140) or fb)
    return {"intro": intro, "bridges": bridges, "outro": outro}


def _synth_edge_tts(clean_text: str, mp3_path: Path, *, language: str, rate: str) -> bool:
    try:
        import asyncio
        import threading
        import edge_tts
    except ImportError:
        return False

    voice = _voice_for(language)

    async def _run() -> None:
        comm = edge_tts.Communicate(clean_text, voice=voice, rate=rate)
        await asyncio.wait_for(comm.save(str(mp3_path)), timeout=22.0)

    try:
        try:
            asyncio.get_running_loop()
            err: list[BaseException] = []

            def _runner() -> None:
                try:
                    asyncio.run(_run())
                except BaseException as exc:
                    err.append(exc)

            t = threading.Thread(target=_runner, daemon=True)
            t.start()
            t.join(timeout=25.0)
            if t.is_alive() or err:
                return False
        except RuntimeError:
            asyncio.run(_run())
        return mp3_path.is_file() and mp3_path.stat().st_size > 200
    except Exception as exc:
        log.debug("longform.voiceover: edge_tts skipped: %s", exc)
        return False


def _synth_windows_sapi(clean_text: str, raw_wav: Path) -> bool:
    if os.name != "nt":
        return False
    try:
        env = {**os.environ, "DOOMSCROLLER_TTS_TEXT": clean_text, "DOOMSCROLLER_TTS_OUT": str(raw_wav)}
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _PS_SAPI_SCRIPT],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=25,
            env=env,
            **background_subprocess_kwargs(low_priority=True),
        )
        return proc.returncode == 0 and raw_wav.is_file() and raw_wav.stat().st_size > 200
    except Exception as exc:
        log.debug("longform.voiceover: windows SAPI fallback failed: %s", exc)
        return False


def synthesize_wav(
    text: str,
    out_wav: Path,
    *,
    language: str = "en",
    rate: str = "+8%",
) -> Optional[float]:
    """Synthesize normalized 48kHz stereo WAV via Edge-TTS or Windows SAPI; None when unavailable."""
    clean = _clean_spoken(text)
    if not clean:
        return None
    out_wav = Path(out_wav)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="longform-tts-", ignore_cleanup_errors=True) as td:
        tmp_dir = Path(td)
        mp3 = tmp_dir / "tts.mp3"
        raw_wav = tmp_dir / "tts_raw.wav"
        src: Optional[Path] = None
        if _synth_edge_tts(clean, mp3, language=language, rate=rate):
            src = mp3
        elif _synth_windows_sapi(clean, raw_wav):
            src = raw_wav
        if src is None:
            return None
        try:
            run_ffmpeg(
                [
                    "-i", str(src),
                    "-af", "silenceremove=start_periods=1:start_silence=0.05:start_threshold=-45dB,aresample=48000,aformat=channel_layouts=stereo",
                    "-ar", "48000", "-ac", "2",
                    str(out_wav),
                ],
                cwd=str(tmp_dir),
            )
            with wave.open(str(out_wav), "rb") as wf:
                rate_hz = wf.getframerate()
                dur = (wf.getnframes() / float(rate_hz)) if rate_hz > 0 else 0.0
            return dur if dur >= 0.4 else None
        except Exception as exc:
            log.warning("longform.voiceover: WAV conversion failed: %s", exc)
            return None


def render_narrator_card(
    wav_path: Path,
    bg_source: Path,
    out_path: Path,
    tmp_dir: Path,
    *,
    badge_text: str,
    headline: str,
    brand_name: str,
    duration: float,
    seek_sec: float = 0.0,
) -> float:
    """Render a blurred 1920x1080 countdown/narrator card synced with a -14 LUFS voiceover."""
    from .render import FPS, HEIGHT, WIDTH, _common_encode_args, _probe_duration, _write_ass_overlay

    dur = max(0.8, float(duration or 0.8) + 0.15)
    still = tmp_dir / f"{out_path.stem}_bg.jpg"
    if str(bg_source).lower().endswith((".jpg", ".jpeg", ".png")):
        still = Path(bg_source)
    else:
        run_ffmpeg(
            ["-ss", f"{max(0.0, float(seek_sec)):.2f}", "-i", str(bg_source), "-frames:v", "1", "-q:v", "2", str(still)],
            cwd=str(tmp_dir),
        )
    ass_path = tmp_dir / f"{out_path.stem}.ass"
    sample = f"{badge_text} {headline} {brand_name}"
    font_ref = resolve_font(settings.clip_font, sample)
    fonts_dir = str(Path(font_ref).parent) if font_ref else str(tmp_dir)
    _write_ass_overlay(
        ass_path,
        badge_text=badge_text,
        lower_third=headline,
        brand=brand_name,
        start_sec=0.05,
        hold_sec=dur,
        sample_text=sample,
        fonts_dir=fonts_dir,
    )
    ass_arg = f"ass={_filter_path_arg(str(ass_path))}:fontsdir={_filter_path_arg(fonts_dir)}"
    run_ffmpeg(
        [
            "-loop", "1", "-t", f"{dur:.3f}", "-i", str(still),
            "-i", str(wav_path),
            "-filter_complex",
            f"[0:v]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,crop={WIDTH}:{HEIGHT},setsar=1,"
            f"gblur=sigma=20,eq=brightness=-0.16:saturation=0.9,fps={FPS},{ass_arg}[v];"
            f"[1:a]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,aformat=channel_layouts=stereo,"
            f"apad=whole_dur={dur:.3f},atrim=0:{dur:.3f},asetpts=N/SR/TB[a]",
            "-map", "[v]", "-map", "[a]",
            "-t", f"{dur:.3f}",
            *_common_encode_args(),
            str(out_path),
        ],
        cwd=str(tmp_dir),
    )
    return max(0.2, _probe_duration(out_path, dur))
