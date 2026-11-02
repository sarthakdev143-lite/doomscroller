"""The AI choosing stock shots from thumbnails, and everything that can go wrong with that."""


def test_tests_cannot_reach_real_services():
    from app.config import Settings
    fresh = Settings()
    assert fresh.pixabay_api_key == "" and fresh.pexels_api_key == "" and fresh.deepgram_api_key == ""
    assert fresh.opencode_bin == "definitely-not-installed"
