"""CI workflow, Dependabot, and requirements.txt dependency-hygiene tests."""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_yaml(path: Path) -> dict | None:
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return None
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded

_IMPORT_TO_DIST: dict[str, str] = {
    "PIL": "pillow",
    "apscheduler": "apscheduler",
    "cv2": "opencv-python-headless",
    "dotenv": "python-dotenv",
    "fastapi": "fastapi",
    "google": "google-auth",
    "google_auth_oauthlib": "google-auth-oauthlib",
    "googleapiclient": "google-api-python-client",
    "httplib2": "httplib2",
    "httpx": "httpx",
    "langdetect": "langdetect",
    "multipart": "python-multipart",
    "numpy": "numpy",
    "pydantic": "pydantic",
    "pydantic_settings": "pydantic-settings",
    "pytest": "pytest",
    "sqlalchemy": "sqlalchemy",
    "uvicorn": "uvicorn",
    "yt_dlp": "yt-dlp",
}

_REMOVED_DISTS: frozenset[str] = frozenset(
    {"streamlit", "playwright", "moviepy", "loguru", "celery", "redis"}
)


def _parsed_requirements() -> dict[str, str]:
    req_path = PROJECT_ROOT / "requirements.txt"
    assert req_path.is_file(), "requirements.txt must exist"
    pins: dict[str, str] = {}
    for raw_line in req_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        assert "==" in line, f"Expected exact == pin in requirements.txt, got: {line!r}"
        name_part, version = line.split("==", 1)
        dist = re.sub(r"\[.*\]$", "", name_part).strip().lower()
        assert dist, f"Empty package name in line: {line!r}"
        assert version.strip(), f"Empty version in line: {line!r}"
        pins[dist] = version.strip()
    return pins


def test_ci_workflow_targets_next_and_master_with_os_gating_and_lint():
    workflows_dir = PROJECT_ROOT / ".github" / "workflows"
    ci_path = workflows_dir / "ci.yml"
    assert ci_path.is_file()

    for wf in workflows_dir.glob("*.y*ml"):
        text = wf.read_text(encoding="utf-8")
        assert "secrets." not in text, f"Workflow {wf.name} must not reference secrets"

    data = _load_yaml(ci_path)
    assert data is not None, "PyYAML is expected in dev/test environment"
    assert data.get("permissions") == {"contents": "read"}

    triggers = data.get("on") or data.get(True)
    assert isinstance(triggers, dict)
    for event_name in ("push", "pull_request"):
        branches = triggers[event_name]["branches"]
        assert "next" in branches and "master" in branches

    concurrency = data.get("concurrency") or {}
    assert "github.ref" in str(concurrency.get("group", ""))
    cancel = concurrency.get("cancel-in-progress")
    assert cancel is True or "pull_request" in str(cancel)

    jobs = data["jobs"]
    first_job_name, first_job = next(iter(jobs.items()))
    assert first_job_name == "test"
    matrix = first_job["strategy"]["matrix"]
    assert set(matrix["os"]) >= {"ubuntu-latest", "windows-latest"}
    assert "3.11" in [str(v) for v in matrix["python-version"]]

    exclude_expr = str(matrix.get("exclude", ""))
    assert "windows-latest" in exclude_expr and "master" in exclude_expr, (
        "test matrix must exclude windows-latest on next PRs while keeping it on master / workflow_dispatch"
    )
    assert any(
        isinstance(step, dict) and (step.get("with") or {}).get("cache") == "pip"
        for step in first_job.get("steps", [])
    )

    assert "lint" in jobs, "ci.yml must include a lint job"
    lint_job = jobs["lint"]
    assert lint_job["runs-on"] == "ubuntu-latest"
    lint_steps_text = " ".join(str(s) for s in lint_job.get("steps", []))
    assert "ruff" in lint_steps_text


def test_dependabot_config_for_pip_and_github_actions_targets_next():
    dep_path = PROJECT_ROOT / ".github" / "dependabot.yml"
    assert dep_path.is_file(), ".github/dependabot.yml must exist"
    data = _load_yaml(dep_path)
    assert data is not None, "PyYAML is expected in dev/test environment"
    assert data.get("version") == 2

    updates = {entry["package-ecosystem"]: entry for entry in data.get("updates", [])}
    assert {"pip", "github-actions"}.issubset(updates.keys())
    for ecosystem in ("pip", "github-actions"):
        entry = updates[ecosystem]
        assert entry.get("directory") == "/"
        assert entry.get("target-branch") == "next"
        assert (entry.get("schedule") or {}).get("interval") == "weekly"


def test_requirements_txt_pins_and_top_level_imports():
    pins = _parsed_requirements()
    assert not (_REMOVED_DISTS & set(pins.keys())), (
        f"Removed dependencies must stay out of requirements.txt: {sorted(_REMOVED_DISTS & set(pins.keys()))}"
    )
    # numpy 2.5+ requires Python >=3.12 and fails on Python 3.11 CI runners.
    assert not pins["numpy"].startswith("2.5."), (
        f"numpy=={pins['numpy']} requires Python >=3.12; pin a Python 3.11-compatible release"
    )

    stdlib = set(sys.stdlib_module_names)
    local_roots = {"app", "tasks", "scripts", "tests"}
    missing: dict[str, list[str]] = {}
    for folder in ("app", "tasks", "scripts", "tests"):
        for py_file in sorted((PROJECT_ROOT / folder).rglob("*.py")):
            tree = ast.parse(
                py_file.read_text(encoding="utf-8").lstrip("\ufeff"),
                filename=str(py_file),
            )
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Import, ast.ImportFrom)) or node.col_offset != 0:
                    continue
                names = (
                    [alias.name for alias in node.names]
                    if isinstance(node, ast.Import)
                    else ([node.module] if node.level == 0 and node.module else [])
                )
                for mod in names:
                    root = mod.split(".")[0]
                    if root in stdlib or root in local_roots:
                        continue
                    dist = _IMPORT_TO_DIST.get(root, root.lower().replace("_", "-"))
                    if dist not in pins:
                        missing.setdefault(dist, []).append(str(py_file.relative_to(PROJECT_ROOT)))

    assert not missing, f"Top-level imports missing from requirements.txt: {missing}"
