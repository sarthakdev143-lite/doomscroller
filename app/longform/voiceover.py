"""Sarvam AI bulbul:v3 voiceover synthesis, documentary storytelling scripts & live-motion bridges."""
from __future__ import annotations

import base64
import logging
import re
import tempfile
import time
import wave
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import httpx

from app.clipper import ai
from app.clipper.media import probe, run_ffmpeg
from app.clipper.render import _filter_path_arg
from app.config import settings
from app.growth import notify
from app.growth.youtube import scrub_secrets
from app.transformers.fonts import resolve_font

if TYPE_CHECKING:
    from .planner import CompilationPlan

log = logging.getLogger(__name__)

_SARVAM_TTS_URL = "https://api.sarvam.ai/text-to-speech"
_SARVAM_MODEL = "bulbul:v3"
_ALIMITER = "alimiter=limit=0.84:attack=2:release=40:level=0:latency=1"
_VOICE_CFG = {
    "hi": {"lang": "hi-IN", "speaker": "rohan", "pace": 1.06, "temperature": 0.7},
    "en": {"lang": "en-IN", "speaker": "shubh", "pace": 1.06, "temperature": 0.65},
}
_QUOTA_ALERT_COOLDOWN_SEC = 30 * 60
_last_quota_alert_at = 0.0

_CREATOR_SPEECH_MAP = (
    (re.compile(r"@?souravjvlogs\b", re.IGNORECASE), "Sourav Joshi"),
    (re.compile(r"@?mrbeast\b", re.IGNORECASE), "Mr. Beast"),
    (re.compile(r"@?ksi\b", re.IGNORECASE), "KSI"),
    (re.compile(r"@?fukrainsaan\b", re.IGNORECASE), "Fukra Insaan"),
    (re.compile(r"@?triggeredinsaan\b", re.IGNORECASE), "Triggered Insaan"),
    (re.compile(r"@?flyingbeast320\b", re.IGNORECASE), "Flying Beast"),
    (re.compile(r"@?elvishyadavvlogs\b", re.IGNORECASE), "Elvish Yadav"),
    (re.compile(r"@?mrindianhacker\b", re.IGNORECASE), "Mr. Indian Hacker"),
    (re.compile(r"@?crazyxyz\b", re.IGNORECASE), "Crazy XYZ"),
)
_CTRL_OR_EMOJI_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\U00010000-\U0010ffff]+")


class SarvamTTSQuotaError(RuntimeError):
    """Raised when Sarvam AI TTS rejects requests due to exhausted credits, quota, or key failure."""


def _scrub(text: str) -> str:
    clean = scrub_secrets(str(text or ""))
    key = (settings.sarvam_api_key or "").strip()
    if key:
        clean = clean.replace(key, "[REDACTED]")
    return clean


def _notify_quota_once(detail: str) -> None:
    global _last_quota_alert_at
    now = time.monotonic()
    if _last_quota_alert_at and (now - _last_quota_alert_at) < _QUOTA_ALERT_COOLDOWN_SEC:
        return
    _last_quota_alert_at = now
    try:
        notify.send(_scrub(detail), title="Sarvam AI TTS Alert")
    except Exception as exc:
        log.warning("longform.voiceover: quota alert send failed: %s", type(exc).__name__)


def _clean_spoken(text: str, max_chars: int = 360) -> str:
    clean = str(text or "").replace("#Shorts", "").replace("#shorts", "").replace("#", " ")
    clean = _CTRL_OR_EMOJI_RE.sub(" ", clean)
    for pattern, spoken_name in _CREATOR_SPEECH_MAP:
        clean = pattern.sub(spoken_name, clean)
    clean = re.sub(r"@([A-Za-z0-9._-]+)", r"\1", clean)
    clean = " ".join(clean.split()).strip()
    if len(clean) <= max_chars:
        return clean
    clipped = clean[:max_chars].rsplit(" ", 1)[0].strip()
    return clipped or clean[:max_chars]


def _default_script(plan: "CompilationPlan") -> dict:
    segs = list(plan.segments)
    n = len(segs)
    hi = str(plan.language or "en").strip().lower().startswith("hi")
    who_clean = ", ".join(_clean_spoken(t, max_chars=40) for t in plan.targets if str(t).strip())
    if hi:
        who_hi = who_clean or "सौरव जोशी"
        hook = f"भाई, {who_hi} के इन {n} मोमेंट्स में जो आखिरी मोड़ आया है ना, किसी ने नहीं सोचा था!"
        bridges = []
        for idx, s in enumerate(segs, start=1):
            rank = (n - idx + 1) if plan.format_kind != "vs" else idx
            who_seg = _clean_spoken(s.creator, max_chars=36) or who_hi
            if plan.format_kind == "vs":
                bridges.append(f"राउंड {idx} में {who_seg} का सबसे खतरनाक चैलेंज शुरू होता है, ध्यान से देखिए।")
            elif rank == 1:
                bridges.append("और अब आता है आज की कहानी का सबसे पागल और बड़ा क्लाइमेक्स, अंत तक जरूर देखना!")
            else:
                bridges.append(f"चैप्टर {idx} में {who_seg} के सामने एक बिल्कुल अनोखा मोड़ आता है, आगे क्या हुआ खुद देखो।")
        outro = "आपको आज की कहानी का कौन सा पल सबसे ज्यादा मजेदार या खतरनाक लगा? कमेंट में जरूर बताइए और सब्सक्राइब कीजिए!"
    else:
        who_en = who_clean or "these creators"
        hook = f"Everything escalates fast in {who_en}'s wildest challenge run yet — wait until you see how the finale ends."
        bridges = []
        for idx, s in enumerate(segs, start=1):
            rank = (n - idx + 1) if plan.format_kind != "vs" else idx
            who_seg = _clean_spoken(s.creator, max_chars=36) or who_en
            if plan.format_kind == "vs":
                bridges.append(f"Round {idx} shifts the pressure straight onto {who_seg} — watch how this play unfolds.")
            elif rank == 1:
                bridges.append("And now the final payoff everyone waited for — here is how it ends at number one.")
            else:
                bridges.append(f"Next up at number {rank}, {who_seg} walks into a moment nobody saw coming.")
        outro = "Which moment shocked you the most? Drop your verdict in the comments and subscribe for the next story."
    return {"hook": hook, "intro": hook, "bridges": bridges, "outro": outro}


def generate_commentary_script(plan: "CompilationPlan") -> dict:
    """Generate cohesive documentary commentary lines tailored for Sarvam AI bulbul:v3."""
    fallback = _default_script(plan)
    segs = list(plan.segments)
    if not segs or not ai.available():
        return fallback

    n = len(segs)
    hi = str(plan.language or "en").strip().lower().startswith("hi")
    lang_note = (
        "natural conversational Hindi written STRICTLY in Devanagari script "
        "(transliterate English loanwords and creator names into Devanagari too, e.g. ग्लास ब्रिज, रोलर कोस्टर, सौरव जोशी, मिस्टर बीस्ट; NEVER use Roman Hinglish)"
        if hi else
        "crisp conversational YouTube documentary English (high energy, natural contractions)"
    )
    rows = []
    for idx, s in enumerate(segs, start=1):
        label = f"Round {idx}" if plan.format_kind == "vs" else f"Chapter {idx} (#{n - idx + 1})"
        who = _clean_spoken(s.creator, max_chars=40)
        excerpt = " ".join(str(getattr(s, "transcript_excerpt", "") or "").split())[:180]
        rows.append(
            f"{idx - 1}. {label} | {who} | topic={s.moment_title} | dur={s.duration:.0f}s"
            + (f" | context={excerpt}" if excerpt else "")
        )
    prompt = (
        f"Write gripping, story-driven narrator commentary lines in {lang_note} for a cohesive {n}-act YouTube 16:9 video "
        f"titled {plan.title!r}.\n"
        f"Acts in exact playback order:\n" + "\n".join(rows) + "\n\n"
        "Strict rules:\n"
        "- NO hashtags (#), NO emojis, NO robotic filler.\n"
        "- Do NOT just repeat the topic title word-for-word! Build curiosity, stakes, and narrative flow from one act to the next.\n"
        "- hook (12-18 words): plays OVER fast opening teaser footage — teases the climax payoff WITHOUT spoiling who wins.\n"
        "- bridges (one per act, 14-24 words each): sets up what is about to happen and why the viewer should care.\n"
        "- outro (14-20 words): short punchy reflection + debate question for comments + subscribe CTA.\n"
        f'Reply with JSON only: {{"hook": "...", "bridges": ["..." ({n} items matching 0..{n - 1})], "outro": "..."}}'
    )
    reply = ai.ask_json(prompt)
    if not isinstance(reply, dict):
        return fallback
    hook = _clean_spoken(reply.get("hook") or reply.get("intro") or "", max_chars=320) or fallback["hook"]
    outro = _clean_spoken(reply.get("outro") or "", max_chars=340) or fallback["outro"]
    raw_bridges = reply.get("bridges")
    bridges: list[str] = []
    for i, fb in enumerate(fallback["bridges"]):
        cand = raw_bridges[i] if isinstance(raw_bridges, list) and i < len(raw_bridges) else ""
        bridges.append(_clean_spoken(cand, max_chars=320) or fb)
    return {"hook": hook, "intro": hook, "bridges": bridges, "outro": outro}


def _is_quota_or_auth_failure(status_code: int, body: str) -> bool:
    if status_code in (401, 402, 403, 429):
        return True
    low = (body or "").lower()
    return any(tok in low for tok in ("quota", "credit", "insufficient", "balance", "limit exceeded"))


def synthesize_wav(
    text: str,
    out_wav: Path,
    *,
    language: str = "en",
) -> Optional[float]:
    """Synthesize normalized 48kHz stereo WAV via Sarvam AI bulbul:v3; raises SarvamTTSQuotaError on credit exhaustion."""
    clean = _clean_spoken(text, max_chars=500)
    if not clean:
        return None
    api_key = (settings.sarvam_api_key or "").strip()
    if not api_key:
        msg = (
            "SARVAM_API_KEY is missing or empty — Sarvam AI (bulbul:v3) is required for "
            "long-form voiceover; refusing silent or robotic TTS fallback."
        )
        _notify_quota_once(msg)
        raise SarvamTTSQuotaError(msg)

    code = str(language or "en").strip().lower().split("-")[0]
    cfg = _VOICE_CFG["hi" if code == "hi" else "en"]
    payload = {
        "text": clean,
        "target_language_code": cfg["lang"],
        "speaker": cfg["speaker"],
        "model": _SARVAM_MODEL,
        "pace": cfg["pace"],
        "temperature": cfg["temperature"],
        "speech_sample_rate": 24000,
        "output_audio_codec": "wav",
    }
    try:
        resp = httpx.post(
            _SARVAM_TTS_URL,
            headers={"api-subscription-key": api_key, "Content-Type": "application/json"},
            json=payload,
            timeout=35.0,
        )
    except Exception as exc:
        log.warning("longform.voiceover: Sarvam TTS request error: %s", _scrub(str(exc)))
        return None

    if resp.status_code != 200:
        excerpt = _scrub(resp.text[:240])
        if _is_quota_or_auth_failure(resp.status_code, resp.text):
            msg = (
                f"Sarvam AI TTS credits exhausted or key rejected (HTTP {resp.status_code}). "
                "Please create a fresh Sarvam account/key and update SARVAM_API_KEY in .env — "
                "long-form builds are paused so we never ship robotic audio."
            )
            _notify_quota_once(msg)
            raise SarvamTTSQuotaError(f"{msg} Detail: {excerpt}")
        log.warning("longform.voiceover: Sarvam TTS HTTP %d: %s", resp.status_code, excerpt)
        return None

    try:
        audios = (resp.json() or {}).get("audios") or []
        raw_bytes = base64.b64decode(audios[0]) if audios else b""
    except Exception as exc:
        log.warning("longform.voiceover: malformed Sarvam TTS payload: %s", type(exc).__name__)
        return None
    if len(raw_bytes) < 200:
        return None

    out_wav = Path(out_wav)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="longform-tts-", ignore_cleanup_errors=True) as td:
        tmp_dir = Path(td)
        raw_wav = tmp_dir / "sarvam_raw.wav"
        raw_wav.write_bytes(raw_bytes)
        try:
            run_ffmpeg(
                [
                    "-i", str(raw_wav),
                    "-af",
                    "silenceremove=start_periods=1:start_silence=0.04:start_threshold=-45dB,"
                    "areverse,silenceremove=start_periods=1:start_silence=0.05:start_threshold=-45dB,areverse,"
                    f"loudnorm=I=-14:TP=-1.5:LRA=9,aresample=48000,{_ALIMITER},aformat=channel_layouts=stereo",
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
            log.warning("longform.voiceover: WAV normalization failed: %s", exc)
            return None


def render_narrator_bridge(
    wav_path: Path,
    video_source: Path,
    out_path: Path,
    tmp_dir: Path,
    *,
    badge_text: str,
    headline: str,
    creator_label: str = "",
    brand_name: str = "",
    duration: float,
    preview_start: float = 0.0,
    preview_end: float = 0.0,
    music_path: Optional[Path] = None,
) -> float:
    """Render a live-motion 1080p narrator bridge with clean upper-left typography & ducked stereo audio."""
    from .render import FPS, HEIGHT, WIDTH, _common_encode_args, _find_bright_offset, _probe_duration, _write_bridge_ass

    dur = max(1.0, float(duration or 1.0) + 0.35)
    need_src = max(1.0, dur / 1.18)
    src_info = probe(str(video_source))
    seek = _find_bright_offset(str(video_source), float(preview_start), float(preview_end or src_info.duration or preview_start + 8.0), need_src)

    ass_path = tmp_dir / f"{out_path.stem}.ass"
    sample = f"{badge_text} {headline} {creator_label} {brand_name}"
    font_ref = resolve_font(settings.clip_font, sample)
    fonts_dir = str(Path(font_ref).parent) if font_ref else str(tmp_dir)
    _write_bridge_ass(
        ass_path,
        badge_text=badge_text,
        headline=headline,
        brand=brand_name,
        hold_sec=dur,
        sample_text=sample,
    )
    ass_arg = f"ass={_filter_path_arg(str(ass_path))}:fontsdir={_filter_path_arg(fonts_dir)}"

    inputs = [
        "-ss", f"{max(0.0, seek):.3f}", "-i", str(video_source),
        "-i", str(wav_path),
    ]
    has_music = music_path is not None and Path(music_path).is_file()
    if has_music:
        inputs += ["-stream_loop", "-1", "-i", str(music_path)]

    fade_out_v = max(0.2, dur - 0.18)
    fade_out_a = max(0.2, dur - 0.20)
    filters = [
        f"[0:v]setpts=1.18*(PTS-STARTPTS),scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={WIDTH}:{HEIGHT},setsar=1,eq=brightness=-0.12:contrast=1.08:saturation=1.05,"
        f"tpad=stop_mode=clone:stop_duration=3,trim=0:{dur:.3f},fps={FPS},"
        f"fade=t=in:st=0:d=0.22,fade=t=out:st={fade_out_v:.3f}:d=0.18,{ass_arg}[v]",
        f"[1:a]adelay=120|120,loudnorm=I=-14:TP=-1.5:LRA=9,aresample=48000,"
        f"aformat=channel_layouts=stereo,apad=whole_dur={dur:.3f},atrim=0:{dur:.3f},asetpts=N/SR/TB[vo]",
    ]
    if has_music:
        filters.append(
            f"[2:a]volume=0.08,aresample=48000,aformat=channel_layouts=stereo,"
            f"afade=t=in:d=0.20,afade=t=out:st={fade_out_a:.3f}:d=0.20,"
            f"atrim=0:{dur:.3f},asetpts=N/SR/TB[bed]"
        )
        filters.append(f"[vo][bed]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,{_ALIMITER},aformat=channel_layouts=stereo,asetpts=N/SR/TB[a]")
    elif src_info.has_audio:
        filters.append(
            f"[0:a]atrim=0:{dur:.3f},asetpts=PTS-STARTPTS,volume=0.14,lowpass=f=2000,"
            f"aresample=48000,aformat=channel_layouts=stereo,apad=whole_dur={dur:.3f},atrim=0:{dur:.3f},"
            f"afade=t=in:d=0.20,afade=t=out:st={fade_out_a:.3f}:d=0.20,asetpts=N/SR/TB[bed]"
        )
        filters.append(f"[vo][bed]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,{_ALIMITER},aformat=channel_layouts=stereo,asetpts=N/SR/TB[a]")
    else:
        filters.append(f"[vo]{_ALIMITER},aformat=channel_layouts=stereo,asetpts=N/SR/TB[a]")

    run_ffmpeg(
        [
            *inputs,
            "-filter_complex", ";".join(filters),
            "-map", "[v]", "-map", "[a]",
            "-t", f"{dur:.3f}",
            *_common_encode_args(),
            str(out_path),
        ],
        cwd=str(tmp_dir),
    )
    return max(0.2, _probe_duration(out_path, dur))
