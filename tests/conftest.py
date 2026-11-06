"""Pytest configuration — uses an in-memory SQLite DB per test session."""
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

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
