"""Long descriptions must never push the creator credit or hashtags past
YouTube's 5000-char cap (issue #3, ported from PR #4)."""
from app.metadata import compose


def test_long_description_preserves_credit_and_hashtags():
    md = compose(
        title="T", description="x" * 9000, keywords=["dance"],
        author="@bob", author_name="Bob Smith",
        source_url="https://snap/x", platform="snapchat",
    )
    assert len(md.description) <= 5000
    assert "#Shorts" in md.description
    assert "Credit: Bob Smith (@bob)" in md.description
    assert "https://snap/x" in md.description


def test_long_description_preserves_credit_without_a_handle():
    md = compose(
        title="T", description="y" * 9000, keywords=[],
        platform="snapchat", source_url="https://snap/y",
        require_credit=True,
    )
    assert len(md.description) <= 5000
    assert "Originally posted on Snapchat" in md.description
    assert "https://snap/y" in md.description
