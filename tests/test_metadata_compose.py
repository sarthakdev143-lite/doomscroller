"""Tests for the metadata finishing pass and provider priority."""
import pytest

from app import metadata
from app.metadata import (
    _TAGS_MAX_COUNT,
    _TITLE_MAX,
    build_tags,
    compose,
    generate_metadata,
)


# -------------------------------------------------------------------- title
def test_title_gets_shorts_hashtag_once():
    md = compose(title="A clip", description="", keywords=[])
    assert md.title == "A clip #Shorts"
    # An existing hashtag is not duplicated.
    again = compose(title="A clip #shorts", description="", keywords=[])
    assert again.title.lower().count("#shorts") == 1


def test_long_title_is_cut_at_a_separator():
    long_tail = "x" * 120
    md = compose(title=f"The Real Headline | {long_tail}", description="", keywords=[])
    assert md.title == "The Real Headline #Shorts"


def test_very_long_title_is_cut_on_a_word_boundary():
    md = compose(title=" ".join(["word"] * 60), description="", keywords=[])
    assert len(md.title) <= _TITLE_MAX
    assert md.title.endswith("#Shorts")
    assert "wor#" not in md.title  # never cut mid-word


def test_empty_title_falls_back_to_description_then_placeholder():
    from_desc = compose(title="", description="A thing happened", keywords=[])
    assert from_desc.title.startswith("A thing happened")
    empty = compose(title="", description="", keywords=[])
    assert empty.title == "Watch this #Shorts"


# --------------------------------------------------------------------- tags
def test_tags_are_normalised_and_deduped():
    tags = build_tags(["Keanu Reeves", "keanu reeves", "Stand-Up Comedy!", ""])
    assert "keanu reeves" in tags
    assert "standup comedy" in tags
    assert tags.count("keanu reeves") == 1


def test_single_character_tags_are_dropped():
    # A one-character YouTube tag matches nothing useful.
    assert build_tags(["a", "b", "dancing"]) == ["dancing", "shorts", "viral", "trending", "reels"]


def test_tags_drop_noise_words():
    assert "snapchat" not in build_tags(["snapchat", "dancing"])
    assert "dancing" in build_tags(["snapchat", "dancing"])


def test_tags_respect_count_and_length_caps():
    tags = build_tags([f"keyword number {i}" for i in range(50)])
    assert len(tags) <= _TAGS_MAX_COUNT
    assert all(len(t) <= 30 for t in tags)
    # Well inside YouTube's 500-char total.
    assert sum(len(t) + 2 for t in tags) < 500


def test_author_becomes_a_tag():
    assert "creator1" in build_tags(["dance"], author="@creator1")


# -------------------------------------------------------------- description
def test_description_carries_credit_and_link():
    md = compose(
        title="T", description="Body text", keywords=["dance"],
        author="@bob", author_name="Bob Smith",
        source_url="https://snap/x", platform="snapchat",
    )
    assert "Body text" in md.description
    assert "Credit: Bob Smith (@bob)" in md.description
    assert "on Snapchat" in md.description
    assert "https://snap/x" in md.description
    # The handle is not double-prefixed.
    assert "@@" not in md.description


def test_description_has_hashtags_and_shorts_tag():
    md = compose(title="T", description="Body", keywords=["dance", "pets"])
    assert "#Shorts" in md.description
    assert "#dance" in md.description


def test_description_omits_credit_without_an_author():
    md = compose(title="T", description="Body", keywords=[])
    assert "Credit:" not in md.description


def test_multiword_keywords_are_not_hashtagged():
    md = compose(title="T", description="B", keywords=["street interview"])
    assert "#street interview" not in md.description


# ---------------------------------------------------------------- providers
def test_platform_copy_wins_without_calling_the_llm(monkeypatch):
    def fail(*_a, **_k):
        raise AssertionError("the LLM must not be called when platform copy exists")

    monkeypatch.setattr(metadata, "_call_llm", fail)
    md = generate_metadata(
        source_title="raw name",
        platform_title="Snapchat SEO Title",
        platform_description="Snapchat wrote this.",
        keywords=["dance"], author="bob", platform="snapchat", language="hi",
    )
    assert md.source == "platform"
    assert md.title.startswith("Snapchat SEO Title")
    assert md.detected_language == "hi"  # the source language is preserved


def test_falls_back_when_llm_returns_nothing(monkeypatch):
    monkeypatch.setattr(metadata, "_call_llm", lambda *_a, **_k: None)
    md = generate_metadata(source_title="Plain title", source_desc="Desc", author="bob")
    assert md.source == "fallback"
    assert "Plain title" in md.title


def test_uses_llm_output_when_available(monkeypatch):
    monkeypatch.setattr(
        metadata, "_call_llm",
        lambda *_a, **_k: '{"title":"LLM Title","description":"LLM desc","tags":["skateboarding","fails"]}',
    )
    monkeypatch.setattr(metadata.settings, "use_llm_metadata", True)
    md = generate_metadata(source_title="raw", source_desc="raw desc", author="bob")
    assert md.source == "llm"
    assert md.title.startswith("LLM Title")
    assert "skateboarding" in md.tags


def test_llm_json_is_extracted_from_chatty_output(monkeypatch):
    monkeypatch.setattr(
        metadata, "_call_llm",
        lambda *_a, **_k: 'Sure! Here you go:\n{"title":"T","description":"D","tags":[]}\nHope that helps',
    )
    monkeypatch.setattr(metadata.settings, "use_llm_metadata", True)
    assert generate_metadata(source_title="x").source == "llm"


@pytest.mark.parametrize("bad", ["", "not json", "{broken", None])
def test_extract_json_rejects_junk(bad):
    assert metadata._extract_json(bad or "") is None


def test_description_is_capped():
    md = compose(title="T", description="x" * 9000, keywords=[])
    assert len(md.description) <= 5000
