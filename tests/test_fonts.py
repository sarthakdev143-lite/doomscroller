"""Script-aware font resolution, so Hindi (Devanagari) text renders instead of being dropped."""
import pytest

from app.transformers import fonts
from app.transformers.ffmpeg_transformer import is_renderable

HINDI = "मुझे यह बहुत पसंद है"
ENGLISH = "This phone folds in half"


def test_dominant_script_detects_devanagari():
    assert fonts.dominant_script(HINDI) == "devanagari"
    assert fonts.dominant_script(ENGLISH) == "default"
    assert fonts.dominant_script("सबसे बड़ा धमाका") == "devanagari"  # a Hindi title
    assert fonts.dominant_script("") == "default"


def test_resolve_font_picks_the_devanagari_font_for_hindi(monkeypatch):
    monkeypatch.setattr(fonts, "devanagari_font", lambda: "/fonts/NotoSansDevanagari-Bold.ttf")
    monkeypatch.setattr(fonts, "_discovered_font", lambda: "/fonts/DejaVuSans-Bold.ttf")
    assert fonts.resolve_font("", HINDI) == "/fonts/NotoSansDevanagari-Bold.ttf"
    assert fonts.resolve_font("", ENGLISH) == "/fonts/DejaVuSans-Bold.ttf"
    # No text given keeps the old behaviour.
    assert fonts.resolve_font("") == "/fonts/DejaVuSans-Bold.ttf"


def test_resolve_font_falls_back_to_latin_when_no_devanagari_font(monkeypatch):
    monkeypatch.setattr(fonts, "devanagari_font", lambda: None)
    monkeypatch.setattr(fonts, "_discovered_font", lambda: "/fonts/DejaVuSans-Bold.ttf")
    assert fonts.resolve_font("", HINDI) == "/fonts/DejaVuSans-Bold.ttf"


def test_is_renderable_follows_the_devanagari_font(monkeypatch):
    assert is_renderable(ENGLISH) is True
    assert is_renderable("مرحبا بالعالم") is False  # Arabic, no font -> still skipped
    monkeypatch.setattr(fonts, "devanagari_font", lambda: "/fonts/NotoSansDevanagari-Bold.ttf")
    assert is_renderable(HINDI) is True              # with a Devanagari font, Hindi renders
    monkeypatch.setattr(fonts, "devanagari_font", lambda: None)
    assert is_renderable(HINDI) is False             # without one, it is dropped as before
