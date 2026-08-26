"""Tests for metadata generation (fallback path; LLM requires z-ai CLI)."""
from app.metadata import _fallback_metadata, detect_language, _extract_json


def test_detect_language_english():
    assert detect_language("This is clearly English text.") == "en"


def test_detect_language_empty():
    assert detect_language("") == "en"


def test_extract_json_direct():
    assert _extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_embedded():
    assert _extract_json('Here is the result: {"title": "X", "tags": ["a"]}') == {
        "title": "X", "tags": ["a"]
    }


def test_extract_json_invalid():
    assert _extract_json("not json at all") is None


def test_fallback_metadata_basic():
    md = _fallback_metadata(
        source_title="My cool video",
        source_desc="A description here",
        author="@creator",
        language="en",
    )
    assert "#shorts" in md.title.lower()
    assert "shorts" in md.tags
    assert md.detected_language == "en"
    assert md.source == "fallback"
