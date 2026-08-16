"""
Launcher for the Doomscroller dashboard.

Run with: streamlit run dashboard/_launch.py --server.port 8501

We separate the launcher from the actual UI so we can reliably put the
project root on sys.path and avoid the `dashboard/app.py` shadow
problem without having to mutate the UI source.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    os.chdir(PROJECT_ROOT)
except OSError:
    pass

from dashboard.app import main  # noqa: E402

main()