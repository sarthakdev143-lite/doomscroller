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
    "tabs": [tab.label for tab in page.tabs],
    "inputs": [box.label for box in page.text_input],
}))
"""


def test_dashboard_loads_with_the_clip_tab(tmp_path):
    env = {**os.environ, "USE_MOCK": "true", "DATABASE_URL": f"sqlite:///{tmp_path}/d.db",
           "CONTENT_DIR": str(tmp_path / "content"), "PYTHONPATH": str(ROOT)}
    proc = subprocess.run([sys.executable, "-c", CHECK], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT")), None)
    assert line, proc.stderr[-2000:]
    page = json.loads(line[len("RESULT"):])
    assert not page["exceptions"], page["exceptions"]
    assert "✂️ Clip" in page["tabs"]
    assert "Video link or local file" in page["inputs"]


GROWTH_CHECK = """
import json
from streamlit.testing.v1 import AppTest
from app.db import init_db, session_scope
from app.models import GrowthEvent
init_db()
with session_scope() as db:
    db.add(GrowthEvent(kind="strike", summary="yt5 has a problem: copyright"))
    db.add(GrowthEvent(kind="posted", summary="Posted in window 00-04."))
page = AppTest.from_file("dashboard/app.py", default_timeout=60).run()
print("RESULT" + json.dumps({
    "exceptions": [str(e.value) for e in page.exception],
    "warnings": [w.value for w in page.warning],
    "markdown": [m.value for m in page.markdown],
    "captions": [c.value for c in page.caption],
    "tables": [t.value.to_dict("records") for t in page.dataframe],
}))
"""


def test_growth_tab_shows_problems_apart_from_routine_events_and_warns_of_an_empty_shelf(tmp_path):
    env = {**os.environ, "USE_MOCK": "true", "DATABASE_URL": f"sqlite:///{tmp_path}/d.db", "GROWTH_ENABLED": "true",
           "CONTENT_DIR": str(tmp_path / "content"), "PYTHONPATH": str(ROOT)}
    proc = subprocess.run([sys.executable, "-c", GROWTH_CHECK], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT")), None)
    assert line, proc.stderr[-2000:]
    page = json.loads(line[len("RESULT"):])
    assert not page["exceptions"], page["exceptions"]
    assert any("No finished clips in stock" in warning for warning in page["warnings"])
    assert "**Problems in the last 30 days**" in page["markdown"]
    assert any("Last successful collect: never" in caption for caption in page["captions"])
    details = [{row["What"]: row["Detail"] for row in table} for table in page["tables"] if table and "What" in table[0]]
    assert len(details) == 2
    problems, routine = details
    assert problems == {"strike": "yt5 has a problem: copyright"}
    assert "strike" not in routine and "posted" in routine
