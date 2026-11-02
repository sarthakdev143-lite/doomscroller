"""The dashboard must at least load. Twice it crashed on start with nothing catching it:
a fragment writing to the sidebar, and code newer than the pinned Streamlit.

Runs in its own process because the dashboard rewires sys.modules on import, which
would leave later tests patching a different copy of the settings than the app uses.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECK = """
import json
from streamlit.testing.v1 import AppTest
from app.db import init_db
init_db()
page = AppTest.from_file("dashboard/app.py", default_timeout=60).run()
print("RESULT" + json.dumps({
    "exceptions": [str(e.value) for e in page.exception],
}))
"""


def test_dashboard_loads(tmp_path):
    env = {**os.environ, "USE_MOCK": "true", "DATABASE_URL": f"sqlite:///{tmp_path}/d.db",
           "CONTENT_DIR": str(tmp_path / "content"), "PYTHONPATH": str(ROOT)}
    proc = subprocess.run([sys.executable, "-c", CHECK], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT")), None)
    assert line, proc.stderr[-2000:]
    page = json.loads(line[len("RESULT"):])
    assert not page["exceptions"], page["exceptions"]
