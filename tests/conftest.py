"""Pytest configuration — uses an in-memory SQLite DB per test session."""
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Ignore the owner's .env: it turns real uploads and the growth loop on.
os.environ["DOOMSCROLLER_ENV_FILE"] = ""

# Hide the owner's channels.json: multi-channel tests bring their own file, and the
# rest assume legacy single-channel mode (no channels.json). Without this, adding a
# real channels.json to the repo leaks its channels and feeds into every growth test.
_NO_CHANNELS = os.path.join(tempfile.gettempdir(), "doomscroller-test-no-channels.json")
try:
    os.unlink(_NO_CHANNELS)
except OSError:
    pass
os.environ["CHANNELS_FILE"] = _NO_CHANNELS

# And the owner's logs: a test that configures logging would otherwise point every later test's
# output (deliberate failures, fake secrets, missing keys) at the real logs/doomscroller.log.
os.environ["DOOMSCROLLER_LOG_DIR"] = os.path.join(tempfile.gettempdir(), "doomscroller-test-logs")

# And above all the owner's videos. Tests wipe the database's rows, so a cleanup run against the real
# content/ folder sees every finished clip as an orphan and deletes it. That is how real clips vanished.
os.environ["CONTENT_DIR"] = os.path.join(tempfile.gettempdir(), "doomscroller-test-content")

# Force in-memory SQLite + mock mode for tests
os.environ["USE_MOCK"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

# Tests must never spend real calls, whatever keys the developer's .env holds.
os.environ["PIXABAY_API_KEY"] = ""
os.environ["PEXELS_API_KEY"] = ""
os.environ["DEEPGRAM_API_KEY"] = ""
os.environ["OPENCODE_BIN"] = "definitely-not-installed"
os.environ["CLAUDE_BIN"] = "definitely-not-installed"
# Nor use the owner's YouTube login.
os.environ["COOKIES_FILE"] = ""
# Most clipper tests cover the cropped layout. Framed ones ask for it themselves.
os.environ["CLIP_LAYOUT"] = "fill"

import pytest  # noqa: E402  (the environment above must be set before anything imports app settings)


@pytest.fixture(autouse=True)
def _no_live_feed_reads(monkeypatch):
    """Supply's trending pick reads YouTube's feeds. Tests that need it stub it; none may reach the real one."""
    from app.growth import watch

    monkeypatch.setattr(watch, "recent_uploads", lambda feed_url, *, client=None: [])
