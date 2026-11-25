"""Multi-provider long-form TTS (ElevenLabs eleven_v4, Cartesia sonic-3, Sarvam bulbul:v3),
cost-saving SHA-256 disk cache, documentary script generator, and live-motion narrator bridges.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import shutil
import tempfile
import time
import wave
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import httpx

from app.clipper import ai
from app.clipper.media import probe, run_ffmpeg
from app.config import settings
from app.growth import notify
from app.growth.youtube import scrub_secrets
from app.transformers.fonts import resolve_font

if TYPE_CHECKING:
    from .planner import CompilationPlan

log = logging.getLogger(__name__)

_SARVAM_TTS_URL = "https://api.sarvam.ai/text-to-speech"
_SARVAM_MODEL = "bulbul:v3"
_ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=mp3_44100_128"
_CARTESIA_TTS_URL = "https://api.cartesia.ai/tts/bytes"

_ALIMITER = "alimiter=limit=0.84:attack=2:release=40:level=0:latency=1"
_VOICE_CFG = {
    "hi": {"lang": "hi-IN", "speaker": "rohan", "pace": 1.06, "temperature": 0.7},
    "en": {"lang": "en-IN", "speaker": "shubh", "pace": 1.06, "temperature": 0.65},
}
_QUOTA_ALERT_COOLDOWN_SEC = 30 * 60
_last_quota_alert_at = 0.0

_CREATOR_SPEECH_MAP = (
    (re.compile(r"@?souravjvlogs\b", re.IGNORECASE), "Sourav Joshi"),
    (re.compile(r"@?lakhneetvlogs\b", re.IGNORECASE), "Lakhneet"),
    (re.compile(r"@?mrbeast\b", re.IGNORECASE), "Mr. Beast"),
    (re.compile(r"@?ksi\b", re.IGNORECASE), "KSI"),
    (re.compile(r"@rug\b", re.IGNORECASE), "Faze Rug"),
    (re.compile(r"\bFaZe\b"), "Faze"),
    (re.compile(r"@?staywild-?", re.IGNORECASE), "Stay Wild"),
    (re.compile(r"@?fukrainsaan\b", re.IGNORECASE), "Fukra Insaan"),
    (re.compile(r"@?triggeredinsaan\b", re.IGNORECASE), "Triggered Insaan"),
    (re.compile(r"@?flyingbeast320\b", re.IGNORECASE), "Flying Beast"),
    (re.compile(r"@?elvishyadavvlogs\b", re.IGNORECASE), "Elvish Yadav"),
    (re.compile(r"@?mrindianhacker\b", re.IGNORECASE), "Mr. Indian Hacker"),
    (re.compile(r"@?crazyxyz\b", re.IGNORECASE), "Crazy XYZ"),
    (re.compile(r"@?dimplemalhanvlogs\b", re.IGNORECASE), "Dimple Malhan"),
    (re.compile(r"@?sonehavlogs\b", re.IGNORECASE), "Soneha"),
    (re.compile(r"@?anantrastogi\b", re.IGNORECASE), "Anant Rastogi"),
    (re.compile(r"@?sahiljoshivlogs7376\b", re.IGNORECASE), "Sahil Joshi"),
    (re.compile(r"@?albadivlogs\b", re.IGNORECASE), "Albadi"),
)
_CTRL_OR_EMOJI_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\U00010000-\U0010ffff]+")


class SarvamTTSQuotaError(RuntimeError):
    """Raised when configured TTS providers reject requests due to exhausted credits, quota, or missing/invalid keys."""


TTSQuotaError = SarvamTTSQuotaError


class _ProviderQuotaError(RuntimeError):
    """Internal per-provider quota/auth failure allowing failover before raising SarvamTTSQuotaError."""


def _provider_key(provider: str) -> str:
    if provider == "elevenlabs":
        return settings.resolved_elevenlabs_api_key
    if provider == "cartesia":
        return (settings.cartesia_api_key or "").strip()
    if provider == "sarvam":
        return (settings.sarvam_api_key or "").strip()
    return ""


def has_any_tts_key() -> bool:
    """Return True when at least one high-quality neural TTS key is configured."""
    return any(bool(_provider_key(p)) for p in ("elevenlabs", "cartesia", "sarvam"))


def tts_status() -> dict:
    """Return non-secret TTS failover order, model names, disk-cache hits count, and auto-supply guard."""
    cache_dir = settings.content_path / "tts_cache"
    cache_files = len(list(cache_dir.glob("*.wav"))) if cache_dir.is_dir() else 0
    return {
        "en_chain": _provider_chain("en"),
        "hi_chain": _provider_chain("hi"),
        "elevenlabs_model": (settings.elevenlabs_model_id or "eleven_v4").strip(),
        "cartesia_model": (settings.cartesia_model_id or "sonic-3").strip(),
        "sarvam_model": _SARVAM_MODEL,
        "cache_files": cache_files,
        "auto_supply": bool(settings.longform_auto_supply),
    }


def _scrub(text: str) -> str:
    clean = scrub_secrets(str(text or ""))
    for secret in (
        settings.sarvam_api_key,
        settings.cartesia_api_key,
        settings.elevenlabs_api_key,
        settings.eleven_labs_api_key,
    ):
        val = (secret or "").strip()
        if val:
            clean = clean.replace(val, "[REDACTED]")
    return clean


def _notify_quota_once(detail: str, *, title: str = "Sarvam AI TTS Alert") -> None:
    global _last_quota_alert_at
    now = time.monotonic()
    if _last_quota_alert_at and (now - _last_quota_alert_at) < _QUOTA_ALERT_COOLDOWN_SEC:
        return
    _last_quota_alert_at = now
    try:
        notify.send(_scrub(detail), title=title)
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
        hook = f"भाई, {who_hi} के इन {n} मोमेंट्स का आखिरी मोड़ किसी ने नहीं सोचा था!"
        bridges = []
        for idx, s in enumerate(segs, start=1):
            rank = (n - idx + 1) if plan.format_kind != "vs" else idx
            who_seg = _clean_spoken(s.creator, max_chars=36) or who_hi
            if plan.format_kind == "vs":
                bridges.append(f"राउंड {idx} में {who_seg} का सबसे बड़ा चैलेंज शुरू होता है।")
            elif rank == 1:
                bridges.append("और अब आज की कहानी का सबसे पागल क्लाइमेक्स, अंत तक जरूर देखना!")
            else:
                bridges.append(f"चैप्टर {idx} में {who_seg} के सामने एक अनोखा मोड़ आता है।")
        outro = "कौन सा पल सबसे मजेदार लगा? कमेंट में बताओ और सब्सक्राइब करो!"
    else:
        who_en = who_clean or "these creators"
        hook = f"Everything escalates fast in {who_en}'s wildest run yet — wait for the ending."
        bridges = []
        for idx, s in enumerate(segs, start=1):
            rank = (n - idx + 1) if plan.format_kind != "vs" else idx
            who_seg = _clean_spoken(s.creator, max_chars=36) or who_en
            if plan.format_kind == "vs":
                bridges.append(f"Round {idx} puts the pressure straight on {who_seg}.")
            elif rank == 1:
                bridges.append("Now the final payoff everyone waited for at number one.")
            else:
                bridges.append(f"Next at number {rank}, {who_seg} hits an unexpected turn.")
        outro = "Which moment topped it all? Drop your pick below and subscribe."
    return {"hook": hook, "intro": hook, "bridges": bridges, "outro": outro}


def generate_commentary_script(plan: "CompilationPlan") -> dict:
    """Generate cohesive, concise documentary commentary lines tailored for natural neural TTS."""
    fallback = _default_script(plan)
    segs = list(plan.segments)
    if not segs or not ai.available():
        return fallback

    n = len(segs)
    hi = str(plan.language or "en").strip().lower().startswith("hi")
    lang_note = (
        "everyday natural spoken YouTube Hinglish written STRICTLY in clean Devanagari script "
        "(e.g. 'पब्लिक के बीच इतना शर्माना', 'देखो आगे क्या सीन बनता है', 'भाई ये ट्विस्ट क्रेज़ी है'; "
        "transliterate English creator/loan words cleanly into Devanagari like ग्लास ब्रिज, सौरव जोशी, लखनीत, मिस्टर बीस्ट; "
        "NEVER misspell Lakhneet as लक्षनीत — always write लखनीत; "
        "STRICTLY FORBIDDEN stiff newsroom Sanskritized Hindi like 'सार्वजनिक शर्मिंदगी', 'जंगली दहशत', 'आइए देखते हैं', or 'प्रस्तुत')"
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
        f"Write gripping, concise narrator commentary lines in {lang_note} for a cohesive {n}-act YouTube 16:9 video "
        f"titled {plan.title!r}.\n"
        f"Acts in exact playback order:\n" + "\n".join(rows) + "\n\n"
        "Strict rules:\n"
        "- NO hashtags (#), NO emojis, NO robotic filler. Keep lines tight to save voiceover duration.\n"
        "- Do NOT just repeat the topic title word-for-word! Build curiosity, stakes, and narrative flow from one act to the next.\n"
        "- hook (8-12 words): punchy ~4s opener over fast teaser footage — teases the climax payoff WITHOUT spoiling who wins.\n"
        "- bridges (one per act, 8-12 words each): punchy 3.5-4.5s setup of what happens next and why the viewer should care.\n"
        "- outro (10-14 words): crisp closing question for comments + subscribe CTA.\n"
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


def _provider_chain(lang_code: str) -> list[str]:
    base = ["sarvam", "cartesia", "elevenlabs"] if lang_code == "hi" else ["elevenlabs", "cartesia", "sarvam"]
    pref = str(settings.longform_tts_provider or "auto").strip().lower()
    if pref in ("elevenlabs", "cartesia", "sarvam"):
        order = [pref] + [p for p in base if p != pref]
    else:
        order = base
    return [p for p in order if _provider_key(p)]


def _provider_spec(provider: str, lang_code: str) -> tuple[str, str]:
    if provider == "elevenlabs":
        model = (settings.elevenlabs_model_id or "eleven_v4").strip()
        voice = (
            (settings.elevenlabs_voice_hi if lang_code == "hi" else settings.elevenlabs_voice_en)
            or "TX3LPaxmHKxFdv7VOQHJ"
        ).strip()
        return model, voice
    if provider == "cartesia":
        model = (settings.cartesia_model_id or "sonic-3").strip()
        voice = (
            (settings.cartesia_voice_hi if lang_code == "hi" else settings.cartesia_voice_en)
            or ("4877b818-c7fe-4c89-b1cf-eadf8e23da72" if lang_code == "hi" else "47c38ca4-5f35-497b-b1a3-415245fb35e1")
        ).strip()
        return model, voice
    cfg = _VOICE_CFG["hi" if lang_code == "hi" else "en"]
    return _SARVAM_MODEL, f"{cfg['lang']}:{cfg['speaker']}"


def _cache_file(provider: str, lang_code: str, clean: str) -> Path:
    model_id, voice_id = _provider_spec(provider, lang_code)
    digest = hashlib.sha256(f"{provider}|{model_id}|{voice_id}|{lang_code}|{clean}".encode("utf-8")).hexdigest()
    return settings.content_path / "tts_cache" / f"{digest}.wav"


def _wav_duration(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as wf:
            rate_hz = wf.getframerate()
            return (wf.getnframes() / float(rate_hz)) if rate_hz > 0 else 0.0
    except Exception:
        return 0.0


def _synth_elevenlabs(clean: str, lang_code: str, api_key: str) -> Optional[tuple[bytes, str]]:
    model_id, voice_id = _provider_spec("elevenlabs", lang_code)
    try:
        resp = httpx.post(
            _ELEVENLABS_TTS_URL.format(voice_id=voice_id),
            headers={"xi-api-key": api_key, "Content-Type": "application/json", "Accept": "audio/mpeg"},
            json={
                "text": clean,
                "model_id": model_id,
                "voice_settings": {"stability": 0.45, "similarity_boost": 0.78, "style": 0.15, "use_speaker_boost": True},
            },
            timeout=35.0,
        )
    except Exception as exc:
        log.warning("longform.voiceover: ElevenLabs request error: %s", _scrub(str(exc)))
        return None
    if resp.status_code != 200:
        excerpt = _scrub(resp.text[:240])
        if _is_quota_or_auth_failure(resp.status_code, resp.text):
            raise _ProviderQuotaError(f"ElevenLabs HTTP {resp.status_code}: {excerpt}")
        log.warning("longform.voiceover: ElevenLabs HTTP %d: %s", resp.status_code, excerpt)
        return None
    raw = getattr(resp, "content", b"") or b""
    return (raw, ".mp3") if len(raw) >= 200 else None


def _synth_cartesia(clean: str, lang_code: str, api_key: str) -> Optional[tuple[bytes, str]]:
    model_id, voice_id = _provider_spec("cartesia", lang_code)
    try:
        resp = httpx.post(
            _CARTESIA_TTS_URL,
            headers={
                "X-API-Key": api_key,
                "Cartesia-Version": (settings.cartesia_version or "2026-08-14").strip(),
                "Content-Type": "application/json",
            },
            json={
                "model_id": model_id,
                "transcript": clean,
                "voice": {"mode": "id", "id": voice_id},
                "output_format": {"container": "wav", "encoding": "pcm_s16le", "sample_rate": 44100},
                "language": "hi" if lang_code == "hi" else "en",
            },
            timeout=35.0,
        )
    except Exception as exc:
        log.warning("longform.voiceover: Cartesia request error: %s", _scrub(str(exc)))
        return None
    if resp.status_code != 200:
        excerpt = _scrub(resp.text[:240])
        if _is_quota_or_auth_failure(resp.status_code, resp.text):
            raise _ProviderQuotaError(f"Cartesia HTTP {resp.status_code}: {excerpt}")
        log.warning("longform.voiceover: Cartesia HTTP %d: %s", resp.status_code, excerpt)
        return None
    raw = getattr(resp, "content", b"") or b""
    return (raw, ".wav") if len(raw) >= 200 else None


def _synth_sarvam(clean: str, lang_code: str, api_key: str) -> Optional[tuple[bytes, str]]:
    cfg = _VOICE_CFG["hi" if lang_code == "hi" else "en"]
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
            raise _ProviderQuotaError(f"Sarvam AI TTS credits exhausted or key rejected (HTTP {resp.status_code}): {excerpt}")
        log.warning("longform.voiceover: Sarvam TTS HTTP %d: %s", resp.status_code, excerpt)
        return None
    try:
        audios = (resp.json() or {}).get("audios") or []
        raw_bytes = base64.b64decode(audios[0]) if audios else b""
    except Exception as exc:
        log.warning("longform.voiceover: malformed Sarvam TTS payload: %s", type(exc).__name__)
        return None
    return (raw_bytes, ".wav") if len(raw_bytes) >= 200 else None


_SYNTH_FN = {
    "elevenlabs": _synth_elevenlabs,
    "cartesia": _synth_cartesia,
    "sarvam": _synth_sarvam,
}


def synthesize_wav(
    text: str,
    out_wav: Path,
    *,
    language: str = "en",
) -> Optional[float]:
    """Synthesize normalized 48kHz stereo WAV with multi-provider failover and SHA-256 disk caching.

    Raises SarvamTTSQuotaError (TTSQuotaError) when no TTS key is set or every configured provider
    rejects the request due to credits/quota/auth.
    """
    clean = _clean_spoken(text, max_chars=360)
    if not clean:
        return None

    code = str(language or "en").strip().lower().split("-")[0]
    chain = _provider_chain(code)
    if not chain:
        msg = (
            "No TTS API key is configured (SARVAM_API_KEY, ELEVENLABS_API_KEY, or CARTESIA_API_KEY) — "
            "refusing silent or robotic TTS fallback."
        )
        _notify_quota_once(msg, title="Sarvam AI TTS Alert")
        raise SarvamTTSQuotaError(msg)

    out_wav = Path(out_wav)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    # Skip disk caching only when a test inherits the shared session-wide temp content_dir.
    cache_on = not (os.environ.get("PYTEST_CURRENT_TEST") and "doomscroller-test-content" in str(settings.content_path))
    if cache_on:
        for cached_prov in chain:
            cfile = _cache_file(cached_prov, code, clean)
            if cfile.is_file():
                cdur = _wav_duration(cfile)
                if cdur >= 0.4:
                    shutil.copyfile(cfile, out_wav)
                    return cdur

    quota_errors: list[str] = []

    for prov in chain:
        cfile = _cache_file(prov, code, clean)
        try:
            res = _SYNTH_FN[prov](clean, code, _provider_key(prov))
        except _ProviderQuotaError as exc:
            err_clean = _scrub(str(exc))
            log.warning("longform.voiceover: %s quota/auth failover: %s", prov, err_clean)
            quota_errors.append(err_clean)
            continue
        if not res:
            continue

        raw_bytes, ext = res
        with tempfile.TemporaryDirectory(prefix="longform-tts-", ignore_cleanup_errors=True) as td:
            tmp_dir = Path(td)
            raw_in = tmp_dir / f"{prov}_raw{ext}"
            raw_in.write_bytes(raw_bytes)
            try:
                run_ffmpeg(
                    [
                        "-i", str(raw_in),
                        "-af",
                        "silenceremove=start_periods=1:start_silence=0.04:start_threshold=-45dB,"
                        "areverse,silenceremove=start_periods=1:start_silence=0.05:start_threshold=-45dB,areverse,"
                        f"loudnorm=I=-14:TP=-1.5:LRA=9,aresample=48000,{_ALIMITER},aformat=channel_layouts=stereo",
                        "-ar", "48000", "-ac", "2",
                        str(out_wav),
                    ],
                    cwd=str(tmp_dir),
                )
            except Exception as exc:
                log.warning("longform.voiceover: WAV normalization failed (%s): %s", prov, exc)
                continue

        dur = _wav_duration(out_wav)
        if dur >= 0.4:
            if cache_on:
                try:
                    cfile.parent.mkdir(parents=True, exist_ok=True)
                    tmp_cache = cfile.with_suffix(".wav.tmp")
                    shutil.copyfile(out_wav, tmp_cache)
                    tmp_cache.replace(cfile)
                except Exception:
                    pass
            return dur

    if quota_errors:
        joined = "; ".join(quota_errors)
        alert_title = "Sarvam AI TTS Alert" if chain == ["sarvam"] else "TTS Voiceover Quota Alert"
        msg = _scrub(f"All configured TTS providers exhausted/rejected credits ({joined}).")
        _notify_quota_once(msg, title=alert_title)
        raise SarvamTTSQuotaError(msg)
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
    from app.clipper.render import _filter_path_arg
    from .render import FPS, HEIGHT, WIDTH, _common_encode_args, _find_bright_offset, _probe_duration, _write_bridge_ass

    dur = max(1.0, float(duration or 1.0) + 0.35)
    need_src = max(1.0, dur / 1.18)
    src_info = probe(str(video_source))
    span = max(0.0, float(preview_end or 0.0) - float(preview_start or 0.0))
    if span >= 18.0:
        broll_start = float(preview_start) + span * 0.45
        broll_end = max(broll_start + need_src + 1.0, float(preview_end) - 2.0)
    else:
        broll_start = float(preview_start)
        broll_end = float(preview_end or src_info.duration or preview_start + 8.0)
    seek = _find_bright_offset(str(video_source), broll_start, broll_end, need_src)

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
            f"[0:a]atrim=0:{dur:.3f},asetpts=PTS-STARTPTS,volume=0.06,lowpass=f=900,"
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
