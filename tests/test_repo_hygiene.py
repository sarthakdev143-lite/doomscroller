"""Wave 11 repository hygiene, CI workflow, contributor guide, and secret-leak audit tests."""
from __future__ import annotations

import ast
import json
import py_compile
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

SECRET_REGEXES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("google_api_key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("google_oauth_secret", re.compile(r"GOCSPX-[0-9A-Za-z_-]{20,}")),
    ("github_pat", re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[0-9A-Za-z]{30,}|github_pat_[0-9A-Za-z_]{50,}")),
    ("sk_api_key", re.compile(r"\bsk-(?:proj-|ant-)?[0-9A-Za-z_-]{24,}")),
    ("slack_token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{20,}")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private_key_pem", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----")),
    ("telegram_bot_token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
)

SENSITIVE_ASSIGN_NAMES: frozenset[str] = frozenset(
    {
        "review_token",
        "deepgram_api_key",
        "sarvam_api_key",
        "pixabay_api_key",
        "pexels_api_key",
        "telegram_bot_token",
        "client_secret",
        "refresh_token",
        "access_token",
    }
)


def scan_text_for_secrets(text: str) -> list[str]:
    """Return names of any credential regexes matched in ``text``."""
    return [name for name, rx in SECRET_REGEXES if rx.search(text)]


def scan_python_ast_for_hardcoded_secrets(source: str, *, filename: str = "<memory>") -> list[tuple[int, str]]:
    """Return ``(lineno, target_name)`` pairs where a credential variable gets a non-empty string literal."""
    tree = ast.parse(source, filename=filename)
    findings: list[tuple[int, str]] = []

    def _target_name(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id.lower()
        if isinstance(node, ast.Attribute):
            return node.attr.lower()
        return ""

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            val = node.value
            if isinstance(val, ast.Constant) and isinstance(val.value, str) and val.value.strip():
                for target in node.targets:
                    tname = _target_name(target)
                    if tname in SENSITIVE_ASSIGN_NAMES:
                        findings.append((node.lineno, tname))
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            val = node.value
            if isinstance(val, ast.Constant) and isinstance(val.value, str) and val.value.strip():
                tname = _target_name(node.target)
                if tname in SENSITIVE_ASSIGN_NAMES:
                    findings.append((node.lineno, tname))
    return findings


def test_gitignore_protects_secrets_databases_wal_and_runtime_state():
    gitignore_path = PROJECT_ROOT / ".gitignore"
    assert gitignore_path.is_file(), ".gitignore must exist at repo root"
    lines = {
        line.strip()
        for line in gitignore_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    required_entries = {
        ".env",
        ".env.local",
        "secrets/",
        "channels.json",
        "cookies*.txt",
        "doomscroller.db",
        "doomscroller.db-wal",
        "doomscroller.db-shm",
        "doomscroller.db.bak*",
        "content/",
        "logs/",
        "venv/",
        ".venv/",
    }
    missing = required_entries - lines
    assert not missing, f".gitignore is missing required rules: {sorted(missing)}"

    git_bin = shutil.which("git")
    if git_bin and (PROJECT_ROOT / ".git").exists():
        must_ignore = [
            ".env",
            ".env.local",
            "secrets/youtube_token.json",
            "secrets/client_secret.json",
            "channels.json",
            "cookies.txt",
            "cookies_youtube.txt",
            "doomscroller.db",
            "doomscroller.db-wal",
            "doomscroller.db-shm",
            "doomscroller.db.bak.1",
            "logs/doomscroller.log",
            "content/inbox/sample.mp4",
        ]
        for rel in must_ignore:
            proc = subprocess.run(
                [git_bin, "check-ignore", "-q", rel],
                cwd=str(PROJECT_ROOT),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
            )
            assert proc.returncode == 0, f"git check-ignore failed to ignore sensitive/runtime path: {rel}"

        for tracked_template in (".env.example", "channels.example.json"):
            proc = subprocess.run(
                [git_bin, "check-ignore", "-q", tracked_template],
                cwd=str(PROJECT_ROOT),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
            )
            assert proc.returncode != 0, f"Template {tracked_template} must remain trackable (not gitignored)"


def test_github_actions_ci_workflow_structure_and_commands():
    ci_path = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
    assert ci_path.is_file(), ".github/workflows/ci.yml must exist"
    raw = ci_path.read_text(encoding="utf-8")

    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        yaml = None

    if yaml is not None:
        parsed = yaml.safe_load(raw)
        assert isinstance(parsed, dict)
        triggers = parsed.get("on") or parsed.get(True)
        assert isinstance(triggers, dict)
        assert "push" in triggers and "pull_request" in triggers
        perms = parsed.get("permissions")
        assert isinstance(perms, dict) and perms.get("contents") == "read"

        jobs = parsed.get("jobs")
        assert isinstance(jobs, dict) and jobs
        job = next(iter(jobs.values()))
        matrix = job["strategy"]["matrix"]
        assert set(matrix["os"]) >= {"ubuntu-latest", "windows-latest"}
        py_versions = [str(v) for v in matrix["python-version"]]
        assert "3.11" in py_versions

    assert "ubuntu-latest" in raw
    assert "windows-latest" in raw
    assert "3.11" in raw
    assert "node-version" in raw and "20" in raw
    assert "ffmpeg" in raw
    assert "requirements.txt" in raw
    assert "pytest" in raw
    assert "node" in raw and "--check" in raw
    assert "app.review_ui" in raw and "PAGE" in raw
    assert "python scripts/final_audit.py --offline" in raw
    assert 'pytest -q -m "not slow"' in raw


def test_contributing_guide_covers_quickstart_architecture_invariants_tdd_and_cli():
    contrib_path = PROJECT_ROOT / "CONTRIBUTING.md"
    assert contrib_path.is_file(), "CONTRIBUTING.md must exist at repo root"
    text = contrib_path.read_text(encoding="utf-8")
    assert len(text.splitlines()) >= 40

    # Core sections & hard architectural invariants mandated for contributors
    required_snippets = [
        "requirements.txt",
        "scripts/serve_slim.py",
        "numpy",
        "cv2",
        "shots.py",
        "_guard",
        "winproc",
        "background_subprocess_kwargs",
        "DEVNULL",
        "RED",
        "GREEN",
        'pytest -q -m "not slow"',
        "scripts/doctor.py",
        "scripts/final_audit.py",
        "scripts/channels.py",
        "scripts/growth.py",
        "scripts/clip.py",
        "scripts/run.py",
    ]
    for snippet in required_snippets:
        assert snippet in text, f"CONTRIBUTING.md is missing required contributor guidance: {snippet!r}"


def test_readme_and_claude_md_document_wave11_features():
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    claude_md = (PROJECT_ROOT / "CLAUDE.md").read_text(encoding="utf-8")

    wave11_doc_tokens = [
        "doctor.py",
        "stop.ps1",
        "export",
        "import",
        "clone",
        "/api/logs",
        "download=1",
        "approve-clean",
        "CONTRIBUTING.md",
    ]
    for token in wave11_doc_tokens:
        assert token in readme, f"README.md does not document Wave 11 item: {token!r}"
        assert token in claude_md, f"CLAUDE.md does not document Wave 11 item: {token!r}"


def test_final_audit_checks_wave11_surface_and_supports_json(tmp_path, capsys):
    audit_src = (PROJECT_ROOT / "scripts" / "final_audit.py").read_text(encoding="utf-8")
    assert "D:\\Sarthak" not in audit_src

    from scripts import final_audit

    assert hasattr(final_audit, "check_surface"), "final_audit.py must expose check_surface(root)"

    # 1. Incomplete tree must flag missing Wave 11 surface items
    empty_checks = final_audit.check_surface(tmp_path)
    expected_keys = {
        "doctor_cli",
        "stop_ps1",
        "channels_portability",
        "review_logs_route",
        "review_save_mp4",
        "review_approve_clean",
        "ci_workflow",
        "contributing_guide",
    }
    assert expected_keys.issubset(empty_checks.keys())
    for key in expected_keys:
        assert empty_checks[key] is False, f"Expected {key}=False on empty root"

    # 2. Complete synthetic tree must satisfy every Wave 11 surface check
    (tmp_path / "scripts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "app" / "growth").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    (tmp_path / "scripts" / "doctor.py").write_text("def run_doctor(*, fix: bool = False):\n    return {}\n", encoding="utf-8")
    (tmp_path / "stop.ps1").write_text("scripts\\run.py --stop\n", encoding="utf-8")
    (tmp_path / "scripts" / "channels.py").write_text("SUBCOMMANDS = ('export', 'import', 'clone')\n", encoding="utf-8")
    (tmp_path / "app" / "growth" / "channels.py").write_text(
        "def export_channels_snapshot(db): ...\n"
        "def import_channels_snapshot(db, payload, *, dry_run=False): ...\n"
        "def clone_channel(db, source_slug, new_slug, *, name=''): ...\n",
        encoding="utf-8",
    )
    (tmp_path / "app" / "growth" / "approval.py").write_text(
        "def approve_clean(db, channel_slug='', min_score=None): ...\n",
        encoding="utf-8",
    )
    (tmp_path / "app" / "review.py").write_text(
        "@review_app.get('/api/logs')\ndef api_logs(request): _guard(request)\n"
        "@review_app.get('/api/media/{video_id}')\ndef api_media(request, download: str = ''):\n"
        "    _guard(request)\n    headers = {'Content-Disposition': 'attachment'}\n"
        "@review_app.post('/api/clips/approve-clean')\ndef api_approve_clean(request): _guard(request)\n",
        encoding="utf-8",
    )
    (tmp_path / "app" / "review_ui.py").write_text(
        "const u = '/api/logs'; const dl = '?download=1'; const ac = '/api/clips/approve-clean';\n",
        encoding="utf-8",
    )
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text("name: CI\n", encoding="utf-8")
    (tmp_path / "CONTRIBUTING.md").write_text("# Contributing\n", encoding="utf-8")

    full_checks = final_audit.check_surface(tmp_path)
    for key in expected_keys:
        assert full_checks[key] is True, f"Expected {key}=True on complete synthetic root"

    # 3. Live repo run_audit(live=False) and --json CLI output
    report = final_audit.run_audit(live=False)
    assert report["platforms_ok"] is True
    assert "storage" in report
    assert "surface_checks" in report
    assert report["surface_checks"]["ci_workflow"] is True
    assert report["surface_checks"]["contributing_guide"] is True
    assert report["surface_checks"]["review_routes_guarded"] is True

    capsys.readouterr()
    rc = final_audit.main(["--offline", "--json"])
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["platforms_ok"] is True
    assert "surface_checks" in payload
    assert rc in (0, 1)


def test_py_compile_syntax_across_app_tasks_and_scripts(tmp_path):
    py_files: list[Path] = []
    for folder in ("app", "tasks", "scripts"):
        py_files.extend(sorted((PROJECT_ROOT / folder).rglob("*.py")))

    assert len(py_files) >= 75, f"Expected >=75 Python files across app/tasks/scripts, found {len(py_files)}"
    for idx, src_file in enumerate(py_files):
        source = src_file.read_text(encoding="utf-8")
        ast.parse(source, filename=str(src_file))
        cfile = tmp_path / f"mod_{idx}_{src_file.stem}.pyc"
        py_compile.compile(str(src_file), cfile=str(cfile), doraise=True)


def test_no_hardcoded_api_tokens_or_secrets_in_tracked_source_files():
    # Self-test scanner helpers using dynamically assembled fake tokens (never literal secrets in source)
    fake_google = "AIza" + ("B" * 35)
    fake_gh = "ghp_" + ("C" * 36)
    fake_tg = "123456789" + ":" + ("D" * 35)
    assert "google_api_key" in scan_text_for_secrets(fake_google)
    assert "github_pat" in scan_text_for_secrets(fake_gh)
    assert "telegram_bot_token" in scan_text_for_secrets(fake_tg)
    assert scan_python_ast_for_hardcoded_secrets('REVIEW_TOKEN = "super-secret-value"\n') == [
        (1, "review_token")
    ]
    assert scan_python_ast_for_hardcoded_secrets('REVIEW_TOKEN = ""\n') == []

    # Collect tracked non-test files + repo source/config/doc files
    candidates: set[Path] = set()
    git_bin = shutil.which("git")
    if git_bin and (PROJECT_ROOT / ".git").exists():
        proc = subprocess.run(
            [git_bin, "ls-files"],
            cwd=str(PROJECT_ROOT),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=True,
        )
        for rel in proc.stdout.splitlines():
            rel = rel.strip()
            if rel and not rel.startswith("tests/"):
                p = PROJECT_ROOT / rel
                if p.is_file():
                    candidates.add(p)

    for folder in ("app", "tasks", "scripts", ".github"):
        base = PROJECT_ROOT / folder
        if base.is_dir():
            for p in base.rglob("*"):
                if p.is_file() and "__pycache__" not in p.parts and p.suffix in {
                    ".py",
                    ".ps1",
                    ".bat",
                    ".sh",
                    ".yml",
                    ".yaml",
                    ".md",
                    ".json",
                }:
                    candidates.add(p)
    for root_doc in (".env.example", "channels.example.json", "README.md", "CLAUDE.md", "CONTRIBUTING.md"):
        p = PROJECT_ROOT / root_doc
        if p.is_file():
            candidates.add(p)

    regex_leaks: list[tuple[str, list[str]]] = []
    ast_leaks: list[tuple[str, list[tuple[int, str]]]] = []
    for file_path in sorted(candidates):
        text = file_path.read_text(encoding="utf-8", errors="replace")
        matched = scan_text_for_secrets(text)
        if matched:
            regex_leaks.append((str(file_path.relative_to(PROJECT_ROOT)), matched))
        if file_path.suffix == ".py":
            hits = scan_python_ast_for_hardcoded_secrets(text, filename=str(file_path))
            if hits:
                ast_leaks.append((str(file_path.relative_to(PROJECT_ROOT)), hits))

    assert not regex_leaks, f"Hardcoded credential token pattern(s) detected: {regex_leaks}"
    assert not ast_leaks, f"Hardcoded credential literal assignment(s) detected: {ast_leaks}"

    # Verify .env.example leaves every credential blank
    env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    for env_key in (
        "REVIEW_TOKEN",
        "DEEPGRAM_API_KEY",
        "SARVAM_API_KEY",
        "PIXABAY_API_KEY",
        "PEXELS_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "YOUTUBE_CLIENT_SECRETS",
    ):
        m = re.search(rf"^{env_key}=(.*)$", env_example, flags=re.MULTILINE)
        assert m is not None, f"{env_key}= missing from .env.example"
        assert m.group(1).strip() == "", f"{env_key} must be empty in .env.example"
