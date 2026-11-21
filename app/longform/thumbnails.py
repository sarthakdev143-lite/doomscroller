"""Vision-AI hero frame picking + OpenCode prompt + ChatGPT/Gemini AI thumbnails + 3 Pillow variants + custom upload."""
from __future__ import annotations

import base64
import io
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional, Sequence

import httpx
from PIL import Image, ImageDraw, ImageEnhance, ImageOps

from app.clipper import ai
from app.clipper.media import run_ffmpeg
from app.config import settings
from app.growth.youtube import scrub_secrets
from app.transformers.fonts import resolve_font
from app.winproc import reclaim_memory

log = logging.getLogger(__name__)

THUMB_W, THUMB_H = 1280, 720
_MAX_THUMB_BYTES = 1_900_000
_MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def _load_pil_font(size: int, sample: str = ""):
    from PIL import ImageFont

    font_path = resolve_font(settings.clip_font, sample)
    if font_path and os.path.isfile(font_path):
        try:
            return ImageFont.truetype(font_path, size)
        except OSError:
            pass
    return ImageFont.load_default()


def _face_bonus(gray) -> float:
    """Detect frontal faces lazily with OpenCV Haar cascade; 0.0 when unavailable."""
    try:
        import cv2

        cascade_path = os.path.join(getattr(cv2.data, "haarcascades", ""), "haarcascade_frontalface_default.xml")
        if not cascade_path or not os.path.isfile(cascade_path):
            return 0.0
        clf = cv2.CascadeClassifier(cascade_path)
        faces = clf.detectMultiScale(gray, scaleFactor=1.15, minNeighbors=4, minSize=(48, 48))
        return float(len(faces)) if faces is not None else 0.0
    except Exception:
        return 0.0


def extract_candidate_frames(
    video_path: str,
    timestamps: Sequence[float],
    out_dir: Path,
    *,
    max_candidates: int = 8,
) -> list[dict]:
    """Extract sharp candidate JPEGs at ``timestamps`` using ffmpeg + lazy OpenCV/NumPy scoring."""
    import cv2
    import numpy as np

    out_dir.mkdir(parents=True, exist_ok=True)
    clean_stamps = [max(0.1, float(t)) for t in list(timestamps)[:max_candidates]]
    if not clean_stamps:
        clean_stamps = [2.0, 8.0, 16.0]

    candidates: list[dict] = []
    for idx, stamp in enumerate(clean_stamps):
        frame_path = out_dir / f"cand_{idx:02d}.jpg"
        try:
            run_ffmpeg(["-ss", f"{stamp:.2f}", "-i", os.path.abspath(video_path), "-frames:v", "1", "-q:v", "2", str(frame_path)])
        except Exception as exc:
            log.debug("longform.thumbnails: frame extract failed at %.2fs: %s", stamp, exc)
            continue
        if not frame_path.is_file() or frame_path.stat().st_size == 0:
            continue
        img = cv2.imread(str(frame_path))
        if img is None:
            continue
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        luma = float(np.mean(gray))
        # Penalize near-black or washed-out frames.
        exposure = 1.0 if 35.0 <= luma <= 220.0 else 0.35
        faces = _face_bonus(gray)
        score = (sharpness * exposure) + (faces * 500.0)
        candidates.append({
            "index": len(candidates),
            "timestamp": round(stamp, 2),
            "path": str(frame_path),
            "sharpness": round(sharpness, 2),
            "faces": int(faces),
            "score": round(score, 2),
        })
    return candidates


def pick_hero_frames(
    candidates: Sequence[dict],
    *,
    title: str,
    format_kind: str = "best_of",
) -> list[dict]:
    """Use Vision AI over a contact sheet to pick hero frames, falling back to sharpness+face rank."""
    items = list(candidates)
    if not items:
        return []
    ranked = sorted(items, key=lambda c: c["score"], reverse=True)
    if not ai.available() or len(items) == 1:
        return ranked

    with tempfile.TemporaryDirectory(prefix="longform-sheet-", ignore_cleanup_errors=True) as td:
        cols = min(4, len(items))
        rows = (len(items) + cols - 1) // cols
        cell_w, cell_h = 320, 180
        sheet = Image.new("RGB", (cols * cell_w, rows * cell_h), (18, 18, 22))
        draw = ImageDraw.Draw(sheet)
        font = _load_pil_font(22)
        for pos, cand in enumerate(items):
            try:
                tile = ImageOps.fit(Image.open(cand["path"]).convert("RGB"), (cell_w, cell_h))
                x = (pos % cols) * cell_w
                y = (pos // cols) * cell_h
                sheet.paste(tile, (x, y))
                draw.rectangle([x + 6, y + 6, x + 52, y + 36], fill=(0, 0, 0))
                draw.text((x + 12, y + 8), f"#{pos}", fill=(255, 229, 0), font=font)
            except Exception:
                continue
        sheet_path = Path(td) / "contact_sheet.jpg"
        sheet.save(sheet_path, format="JPEG", quality=85)
        prompt = (
            f"Pick the 2 most expressive, eye-catching frames from this contact sheet for a YouTube {format_kind} "
            f"thumbnail titled {title!r}. Prefer close-up reaction faces and high-energy action.\n"
            'Reply with JSON only: {"hero": 0, "alt": 1}'
        )
        reply = ai.ask_json(prompt, files=[str(sheet_path)], model=settings.opencode_vision_model or settings.opencode_model)
        if isinstance(reply, dict):
            chosen_indices: list[int] = []
            for key in ("hero", "alt"):
                try:
                    idx = int(reply.get(key))
                except (TypeError, ValueError):
                    continue
                if 0 <= idx < len(items) and idx not in chosen_indices:
                    chosen_indices.append(idx)
            if chosen_indices:
                ordered = [items[i] for i in chosen_indices] + [c for i, c in enumerate(items) if i not in chosen_indices]
                return ordered
    return ranked


def _save_under_2mb(img: Image.Image, dest: Path) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.stem + ".tmp.jpg")
    rgb = img.convert("RGB")
    if rgb.size != (THUMB_W, THUMB_H):
        rgb = ImageOps.fit(rgb, (THUMB_W, THUMB_H), method=Image.Resampling.LANCZOS)
    for quality in (92, 86, 80, 74):
        buf = io.BytesIO()
        rgb.save(buf, format="JPEG", quality=quality, optimize=True)
        if buf.tell() <= _MAX_THUMB_BYTES or quality == 74:
            tmp.write_bytes(buf.getvalue())
            os.replace(tmp, dest)
            return str(dest)
    return str(dest)


def _headline_words(title: str, max_words: int = 4) -> str:
    clean = " ".join(str(title or "").replace("#Shorts", "").split()).strip()
    words = [w.strip("|—-:,") for w in clean.split() if w.strip("|—-:,")]
    return " ".join(words[:max_words]).upper() or "BEST MOMENTS"


def _add_gradient(img: Image.Image) -> Image.Image:
    base = img.convert("RGBA")
    grad = Image.new("RGBA", (THUMB_W, THUMB_H), (0, 0, 0, 0))
    d = ImageDraw.Draw(grad)
    for y in range(THUMB_H // 2, THUMB_H):
        alpha = int(205 * ((y - THUMB_H / 2) / (THUMB_H / 2)) ** 1.35)
        d.line([(0, y), (THUMB_W, y)], fill=(8, 8, 12, alpha))
    return Image.alpha_composite(base, grad).convert("RGB")


def _draw_outlined_text(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, font, *, fill=(255, 255, 255), stroke=5) -> None:
    draw.text(xy, text, font=font, fill=fill, stroke_width=stroke, stroke_fill=(0, 0, 0))


def _fit_text_font(draw: ImageDraw.ImageDraw, text: str, max_width: int, *, start_size: int = 96, min_size: int = 44):
    size = start_size
    while size >= min_size:
        font = _load_pil_font(size, text)
        left, _, right, _ = draw.textbbox((0, 0), text, font=font, stroke_width=7)
        if right - left <= max_width or size == min_size:
            return font
        size -= 6
    return _load_pil_font(min_size, text)


def _draw_pill_badge(draw: ImageDraw.ImageDraw, x: int, y: int, text: str, font, *, fill, outline=None, text_fill=(255, 255, 255), right_align: bool = False) -> None:
    label = text.upper()
    l, t, r, b = draw.textbbox((0, 0), label, font=font)
    pw = (r - l) + 44
    ph = (b - t) + 26
    x0 = (x - pw) if right_align else x
    draw.rounded_rectangle([x0, y, x0 + pw, y + ph], radius=14, fill=fill, outline=outline, width=3 if outline else 0)
    draw.text((x0 + 22 - l, y + 13 - t), label, font=font, fill=text_fill)


def _render_variant_hero(hero_path: str, dest: Path, *, title: str, badge: str) -> str:
    img = ImageOps.fit(Image.open(hero_path).convert("RGB"), (THUMB_W, THUMB_H), method=Image.Resampling.LANCZOS)
    # Subtle 1.08x center punch zoom for tighter facial impact.
    crop_w, crop_h = int(THUMB_W / 1.08), int(THUMB_H / 1.08)
    left, top = (THUMB_W - crop_w) // 2, (THUMB_H - crop_h) // 2
    img = img.crop((left, top, left + crop_w, top + crop_h)).resize((THUMB_W, THUMB_H), Image.Resampling.LANCZOS)
    img = ImageEnhance.Contrast(ImageEnhance.Color(img).enhance(1.15)).enhance(1.12)
    img = _add_gradient(img)
    draw = ImageDraw.Draw(img)
    headline = _headline_words(title, max_words=3)
    font = _fit_text_font(draw, headline, THUMB_W - 112, start_size=96)
    _draw_outlined_text(draw, (56, THUMB_H - 165), headline, font, fill=(255, 232, 36), stroke=7)
    if badge:
        _draw_pill_badge(draw, 54, 44, badge, _load_pil_font(34, badge), fill=(220, 38, 38))
    return _save_under_2mb(img, dest)


def _render_variant_split(left_path: str, right_path: str, dest: Path, *, format_kind: str, creators: Sequence[str]) -> str:
    half_w = THUMB_W // 2
    left = ImageOps.fit(Image.open(left_path).convert("RGB"), (half_w, THUMB_H), method=Image.Resampling.LANCZOS)
    right = ImageOps.fit(Image.open(right_path).convert("RGB"), (half_w, THUMB_H), method=Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (THUMB_W, THUMB_H))
    canvas.paste(ImageEnhance.Color(left).enhance(1.15), (0, 0))
    canvas.paste(ImageEnhance.Color(right).enhance(1.15), (half_w, 0))
    canvas = _add_gradient(canvas)
    draw = ImageDraw.Draw(canvas)
    draw.rectangle([half_w - 5, 0, half_w + 5, THUMB_H], fill=(255, 255, 255))
    if format_kind == "vs":
        vfont = _load_pil_font(66, "VS")
        draw.rounded_rectangle([half_w - 72, THUMB_H // 2 - 52, half_w + 72, THUMB_H // 2 + 52], radius=22, fill=(235, 38, 38), outline=(255, 255, 255), width=4)
        _draw_outlined_text(draw, (half_w - 46, THUMB_H // 2 - 38), "VS", vfont, fill=(255, 255, 255), stroke=3)
    if len(creators) >= 2:
        cfont = _load_pil_font(38, " ".join(creators[:2]))
        _draw_outlined_text(draw, (38, THUMB_H - 92), str(creators[0])[:22], cfont, stroke=5)
        _draw_outlined_text(draw, (half_w + 38, THUMB_H - 92), str(creators[1])[:22], cfont, stroke=5)
    return _save_under_2mb(canvas, dest)


def _render_variant_clean(hero_path: str, dest: Path, *, badge: str) -> str:
    img = ImageOps.fit(Image.open(hero_path).convert("RGB"), (THUMB_W, THUMB_H), method=Image.Resampling.LANCZOS)
    img = ImageEnhance.Contrast(ImageEnhance.Color(img).enhance(1.22)).enhance(1.18)
    if badge:
        draw = ImageDraw.Draw(img)
        _draw_pill_badge(
            draw, THUMB_W - 42, 86, badge, _load_pil_font(34, badge),
            fill=(18, 18, 24), outline=(255, 229, 0), text_fill=(255, 229, 0), right_align=True,
        )
    return _save_under_2mb(img, dest)


def generate_thumbnail_prompt(
    hero_path: str,
    *,
    title: str,
    format_kind: str = "best_of",
    creator_labels: Sequence[str] = (),
) -> str:
    """Ask OpenCode Vision for a crisp ChatGPT/Gemini image-edit thumbnail prompt, with deterministic fallback."""
    who = ", ".join(str(c).strip() for c in creator_labels if str(c).strip())
    headline = _headline_words(title, max_words=3)
    fallback = (
        f"Edit this video frame into a high-CTR 16:9 YouTube thumbnail about {title!r}"
        f"{' featuring ' + who if who else ''}. Keep the real person's face natural, sharp, and expressive; "
        f"boost vibrant contrast, cinematic rim lighting, subtle dark vignette behind bold 3-word yellow-and-white text "
        f"'{headline}', clean composition with zero clutter or watermarks."
    )
    if not ai.available() or not os.path.isfile(hero_path):
        return fallback
    ask = (
        f"Look at this video frame for a 16:9 YouTube compilation titled {title!r} (format: {format_kind}, "
        f"creators: {who or 'creator'}). Write ONE concise image-editing prompt for ChatGPT / Gemini image generation "
        "that preserves the real creator likeness and turns this scene into a viral, clickable 16:9 YouTube thumbnail "
        "with crisp studio lighting, exaggerated contrast, and 2-3 huge readable words.\n"
        'Reply with JSON only: {"prompt": "..."}'
    )
    reply = ai.ask_json(ask, files=[hero_path], model=settings.opencode_vision_model or settings.opencode_model)
    if isinstance(reply, dict):
        p = " ".join(str(reply.get("prompt") or "").split()).strip()
        if len(p) >= 20:
            return p[:900]
    return fallback


def generate_ai_thumbnail(hero_path: str, prompt: str, dest: Path) -> Optional[tuple[str, str]]:
    """Generate a 1280x720 AI thumbnail via OpenAI gpt-image-1 or Gemini free-tier image model when keys exist."""
    openai_key = (settings.openai_api_key or "").strip()
    gemini_key = (settings.gemini_api_key or "").strip()
    if not os.path.isfile(hero_path) or (not openai_key and not gemini_key):
        return None
    raw_hero = Path(hero_path).read_bytes()

    if openai_key:
        try:
            resp = httpx.post(
                "https://api.openai.com/v1/images/edits",
                headers={"Authorization": f"Bearer {openai_key}"},
                data={"model": "gpt-image-1", "prompt": prompt[:2000], "size": "1536x1024", "quality": "medium"},
                files={"image": ("hero.jpg", raw_hero, "image/jpeg")},
                timeout=90.0,
            )
            if resp.status_code == 200:
                item = ((resp.json().get("data") or [{}])[0]) if isinstance(resp.json(), dict) else {}
                b64 = item.get("b64_json")
                if b64:
                    return ("ChatGPT AI", save_custom_thumbnail(dest, base64.b64decode(b64)))
                img_url = item.get("url")
                if img_url:
                    dl = httpx.get(img_url, timeout=45.0)
                    if dl.status_code == 200:
                        return ("ChatGPT AI", save_custom_thumbnail(dest, dl.content))
            log.warning("longform.thumbnails: OpenAI image edit HTTP %s: %s", resp.status_code, scrub_secrets(resp.text[:240]))
        except Exception as exc:
            log.warning("longform.thumbnails: OpenAI thumbnail failed: %s", scrub_secrets(exc))

    if gemini_key:
        try:
            url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-image:generateContent"
            payload = {
                "contents": [{
                    "parts": [
                        {"text": f"Create a wide 16:9 YouTube thumbnail: {prompt[:2000]}"},
                        {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(raw_hero).decode("ascii")}},
                    ]
                }],
                "generationConfig": {"responseModalities": ["IMAGE"]},
            }
            resp = httpx.post(
                url,
                headers={"x-goog-api-key": gemini_key, "Content-Type": "application/json"},
                json=payload,
                timeout=90.0,
            )
            if resp.status_code == 200:
                body = resp.json()
                parts = (((body.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
                for part in parts:
                    inline = part.get("inlineData") or part.get("inline_data")
                    if isinstance(inline, dict) and inline.get("data"):
                        return ("Gemini AI", save_custom_thumbnail(dest, base64.b64decode(inline["data"])))
            log.warning("longform.thumbnails: Gemini image HTTP %s: %s", resp.status_code, scrub_secrets(resp.text[:240]))
        except Exception as exc:
            log.warning("longform.thumbnails: Gemini thumbnail failed: %s", scrub_secrets(exc))
    return None


def generate_thumbnails_with_prompt(
    video_path: str,
    *,
    out_dir: Path,
    title: str,
    format_kind: str = "best_of",
    chapters: Sequence[tuple[float, str]] = (),
    timestamps: Sequence[float] = (),
    part_number: Optional[int] = None,
    creator_labels: Sequence[str] = (),
) -> tuple[list[dict], str]:
    """Generate Pillow + optional ChatGPT/Gemini AI thumbnails along with OpenCode's thumbnail prompt."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamps = list(timestamps) or [t for t, _ in chapters[1:7]] or [3.0, 12.0, 24.0]
    cand_dir = out_dir / "_candidates"
    candidates = extract_candidate_frames(video_path, stamps, cand_dir)
    if not candidates:
        raise RuntimeError(f"no candidate frames could be extracted from {video_path}")
    ordered = pick_hero_frames(candidates, title=title, format_kind=format_kind)
    hero = ordered[0]["path"]
    second = ordered[1]["path"] if len(ordered) > 1 else hero

    if format_kind == "best_of" and part_number:
        badge = f"PART {part_number}"
    elif format_kind == "vs":
        badge = "SHOWDOWN"
    else:
        badge = f"TOP {max(2, len(chapters) - 1)}" if len(chapters) > 2 else "BEST OF"

    with Image.open(hero) as raw_im:
        _save_under_2mb(raw_im.convert("RGB"), out_dir / "hero_raw.jpg")

    v1 = _render_variant_hero(hero, out_dir / "thumb_v1.jpg", title=title, badge=badge)
    v2 = _render_variant_split(
        hero, second, out_dir / "thumb_v2.jpg",
        format_kind=format_kind, creators=creator_labels,
    )
    v3 = _render_variant_clean(hero, out_dir / "thumb_v3.jpg", badge=badge)
    prompt = generate_thumbnail_prompt(hero, title=title, format_kind=format_kind, creator_labels=creator_labels)
    ai_res = generate_ai_thumbnail(hero, prompt, out_dir / "thumb_v4.jpg")

    variants = [
        {"id": "v1", "label": "Hero + Hook", "path": v1},
        {"id": "v2", "label": "VS Split" if format_kind == "vs" else "Reaction Diptych", "path": v2},
        {"id": "v3", "label": "Clean Color-Pop", "path": v3},
    ]
    if ai_res is not None:
        variants.append({"id": "v4", "label": ai_res[0], "path": ai_res[1]})

    # Clean up temporary candidate frames after rendering variants.
    for cand in candidates:
        try:
            os.remove(cand["path"])
        except OSError:
            pass
    try:
        cand_dir.rmdir()
    except OSError:
        pass
    reclaim_memory()
    return variants, prompt


def generate_thumbnails(
    video_path: str,
    *,
    out_dir: Path,
    title: str,
    format_kind: str = "best_of",
    chapters: Sequence[tuple[float, str]] = (),
    timestamps: Sequence[float] = (),
    part_number: Optional[int] = None,
    creator_labels: Sequence[str] = (),
) -> list[dict]:
    """Generate distinct 1280x720 JPEG variants (<2MB) picked around Vision-AI hero frames."""
    variants, _ = generate_thumbnails_with_prompt(
        video_path,
        out_dir=out_dir,
        title=title,
        format_kind=format_kind,
        chapters=chapters,
        timestamps=timestamps,
        part_number=part_number,
        creator_labels=creator_labels,
    )
    return variants


def save_custom_thumbnail(out_path: Path, raw_bytes: bytes) -> str:
    """Validate and center-crop/resize an owner-uploaded image to an exact 1280x720 JPEG (<2MB)."""
    if not raw_bytes:
        raise ValueError("uploaded thumbnail is empty")
    if len(raw_bytes) > _MAX_UPLOAD_BYTES:
        raise ValueError("thumbnail exceeds 10 MB upload limit")
    try:
        img = Image.open(io.BytesIO(raw_bytes))
        img.load()
    except Exception as exc:
        raise ValueError(f"invalid image file: {exc}") from exc
    if img.mode in ("RGBA", "LA", "P"):
        rgba = img.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (12, 12, 16, 255))
        img = Image.alpha_composite(bg, rgba).convert("RGB")
    else:
        img = img.convert("RGB")
    fitted = ImageOps.fit(img, (THUMB_W, THUMB_H), method=Image.Resampling.LANCZOS)
    return _save_under_2mb(fitted, Path(out_path))
