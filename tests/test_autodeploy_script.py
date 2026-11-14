"""Safety invariants and sandboxed end-to-end runs of scripts/autodeploy.sh and install_services.sh."""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "autodeploy.sh"
INSTALL = ROOT / "scripts" / "install_services.sh"


def _text(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def _code_lines(path: Path) -> list[str]:
    return [s for line in _text(path).splitlines() if (s := line.strip()) and not s.startswith("#")]


@pytest.mark.parametrize("path", [SCRIPT, INSTALL], ids=lambda p: p.name)
def test_scripts_are_lf_ascii_strict_bash(path: Path) -> None:
    raw = path.read_bytes()
    assert b"\r" not in raw and raw.isascii() and raw.startswith(b"#!/bin/bash\n")
    assert "set -euo pipefail" in _text(path)


def test_autodeploy_invariants() -> None:
    code = "\n".join(_code_lines(SCRIPT))
    assert "git merge --ff-only" in code
    for forbidden in ("reset", "rebase", "stash", "checkout", "push", "clean", "commit", "revert"):
        assert not re.search(rf"\bgit\b[^\n]*\b{forbidden}\b", code), f"forbidden: git {forbidden}"
    assert '"$branch" != "master"' in code
    assert "git diff --quiet" in code and "git diff --cached --quiet" in code
    assert re.search(r'git diff --quiet "\$local_head" "\$remote_head" -- requirements\.txt', code)
    assert 'git show "$sha:requirements.txt"' in code
    assert "venv/bin/pip" in code and "install --quiet" in code and "PIP_TIMEOUT" in code and "timeout" in code
    assert "/health" in code and "HEALTH_TIMEOUT:-60" in code and "autodeploy_bad_sha" in code
    assert "from app.growth import notify" in code
    main = code[code.index("main() {") :]
    assert (
        main.index('"$bad_sha" = "$remote_head"')
        < main.index("merge-base --is-ancestor")
        < main.index("install_requirements")
        < main.index("git merge --ff-only")
        < main.index("systemctl --user restart")
        < main.index("wait_healthy")
    )
    assert _code_lines(SCRIPT)[-1] == 'main "$@"'


def test_deploy_unit_and_shellcheck() -> None:
    text = _text(INSTALL)
    svc = text[text.index('doomscroller-deploy.service" <<UNIT') :].split("\nUNIT\n", 1)[0]
    assert "Type=oneshot" in svc and "After=network-online.target" in svc
    assert re.search(r"^TimeoutStartSec=\d+min$", svc, re.MULTILINE)
    tmr = text[text.index('doomscroller-deploy.timer" <<UNIT') :].split("\nUNIT\n", 1)[0]
    assert "OnUnitActiveSec=1min" in tmr and "OnUnitInactiveSec=1min" in tmr
    shellcheck = shutil.which("shellcheck")
    if shellcheck:
        res = subprocess.run([shellcheck, str(SCRIPT), str(INSTALL)], stdin=subprocess.DEVNULL, capture_output=True, text=True)
        assert res.returncode == 0, res.stdout + res.stderr


_RUNNABLE = sys.platform != "win32" and bool(shutil.which("bash")) and bool(shutil.which("git"))
needs_bash = pytest.mark.skipif(not _RUNNABLE, reason="needs bash + git on a POSIX host")

_STUBS = {
    "venv/bin/pip": '#!/bin/bash\nexec pip "$@"\n',
    "venv/bin/python": '#!/bin/bash\necho "python $*" >>"$FAKE_LOG"\n'
    'if [ "${1:-}" = "-m" ] && [ "${2:-}" = "pip" ]; then shift 2; exec pip "$@"; fi\nexit 0\n',
    "bin/pip": '#!/bin/bash\necho "pip $*" >>"$FAKE_LOG"\n'
    'for a in "$@"; do case "$a" in */autodeploy-requirements.*) cp "$a" "$FAKE_LOG.req" ;; esac; done\n'
    'exit "${FAKE_PIP_EXIT:-0}"\n',
    "bin/systemctl": '#!/bin/bash\necho "systemctl $*" >>"$FAKE_LOG"\n',
    "bin/curl": '#!/bin/bash\necho "curl $*" >>"$FAKE_LOG"\nexit "${FAKE_CURL_EXIT:-0}"\n',
}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, stdin=subprocess.DEVNULL, check=True, capture_output=True, text=True).stdout.strip()


class Box:
    def __init__(self, tmp: Path) -> None:
        self.tmp, self.origin, self.dev, self.box, self.log = tmp, tmp / "origin.git", tmp / "dev", tmp / "box", tmp / "calls.log"
        _git(tmp, "init", "--bare", "-q", str(self.origin))
        _git(tmp, "init", "-q", str(self.dev))
        _git(self.dev, "checkout", "-q", "-b", "master")
        (self.dev / "scripts").mkdir()
        shutil.copyfile(SCRIPT, self.dev / "scripts" / "autodeploy.sh")
        (self.dev / ".gitignore").write_text("venv/\nlogs/\n", encoding="utf-8")
        (self.dev / "requirements.txt").write_text("fastapi==1.0\n", encoding="utf-8")
        (self.dev / "app.py").write_text("v = 1\n", encoding="utf-8")
        _git(self.dev, "add", "-A")
        _git(self.dev, "commit", "-q", "-m", "init")
        _git(self.dev, "remote", "add", "origin", str(self.origin))
        _git(self.dev, "push", "-q", "origin", "master")
        _git(tmp, "clone", "-q", "-b", "master", str(self.origin), str(self.box))
        for rel, body in _STUBS.items():
            p = (self.box if rel.startswith("venv/") else tmp) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(body.encode("utf-8"))
            p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def ship(self, filename: str, content: str) -> str:
        (self.dev / filename).write_text(content, encoding="utf-8")
        _git(self.dev, "commit", "-qam", f"change {filename}")
        _git(self.dev, "push", "-q", "origin", "master")
        return _git(self.dev, "rev-parse", "HEAD")

    def head(self) -> str:
        return _git(self.box, "rev-parse", "HEAD")

    def calls(self) -> str:
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""

    def run(self, **env: str) -> subprocess.CompletedProcess[str]:
        self.log.unlink(missing_ok=True)
        full = {
            **os.environ,
            "PATH": f"{self.tmp / 'bin'}{os.pathsep}{os.environ['PATH']}",
            "FAKE_LOG": str(self.log),
            "AUTODEPLOY_HEALTH_TIMEOUT": "2",
            "AUTODEPLOY_HEALTH_INTERVAL": "1",
            **env,
        }
        return subprocess.run(
            ["bash", str(self.box / "scripts" / "autodeploy.sh")],
            cwd=self.tmp, env=full, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60,
        )

    def assert_clean_on_master(self) -> None:
        assert _git(self.box, "rev-parse", "--abbrev-ref", "HEAD") == "master"
        assert _git(self.box, "status", "--porcelain") == ""

    @property
    def bad_file(self) -> Path:
        return self.box / "logs" / "autodeploy_bad_sha"


@pytest.fixture
def box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Box:
    for k, v in {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(tmp_path),
    }.items():
        monkeypatch.setenv(k, v)
    return Box(tmp_path)


@needs_bash
def test_up_to_date_and_code_deploy(box: Box) -> None:
    assert box.run().returncode == 0 and box.calls() == ""
    new = box.ship("app.py", "v = 2\n")
    assert box.run().returncode == 0 and box.head() == new
    assert "systemctl --user restart doomscroller.service" in box.calls()
    assert "http://127.0.0.1:8000/health" in box.calls() and "pip " not in box.calls()
    box.assert_clean_on_master()


@needs_bash
def test_requirements_change_and_pip_failure_circuit_breaker(box: Box) -> None:
    old = box.head()
    bad = box.ship("requirements.txt", "doesnotexist==9\n")
    fail = box.run(FAKE_PIP_EXIT="1")
    assert fail.returncode != 0 and box.head() == old and "systemctl" not in box.calls()
    assert "ALERT" in fail.stderr and box.bad_file.read_text(encoding="utf-8").split()[0] == bad
    box.assert_clean_on_master()
    assert box.run(FAKE_PIP_EXIT="1").returncode == 0 and box.calls() == "" and box.head() == old

    good = box.ship("requirements.txt", "fastapi==2.0\n")
    assert box.run().returncode == 0 and box.head() == good and not box.bad_file.exists()
    calls = box.calls().splitlines()
    assert next(i for i, c in enumerate(calls) if c.startswith("pip install")) < next(
        i for i, c in enumerate(calls) if c.startswith("systemctl")
    )
    assert (box.tmp / "calls.log.req").read_text(encoding="utf-8") == "fastapi==2.0\n"
    assert not list((box.box / "logs").glob("autodeploy-requirements.*"))


@needs_bash
def test_failed_health_and_guards(box: Box) -> None:
    new = box.ship("app.py", "v = broken\n")
    res = box.run(FAKE_CURL_EXIT="7")
    assert res.returncode != 0 and "ALERT" in res.stderr and "NOT healthy" in res.stderr
    assert box.head() == new and box.bad_file.read_text(encoding="utf-8").split()[0] == new
    assert "python -c" in box.calls()
    box.assert_clean_on_master()
    assert box.run(FAKE_CURL_EXIT="7").returncode == 0 and box.calls() == ""

    box.ship("app.py", "v = 3\n")
    (box.box / "app.py").write_text("dirty\n", encoding="utf-8")
    assert box.run().returncode == 0 and box.head() == new and box.calls() == ""
    _git(box.box, "checkout", "-q", "--", "app.py")
    _git(box.box, "checkout", "-q", "-b", "feature")
    assert box.run().returncode == 0 and box.head() == new
    _git(box.box, "checkout", "-q", "master")
    (box.box / "local.txt").write_text("x\n", encoding="utf-8")
    _git(box.box, "add", "local.txt")
    _git(box.box, "commit", "-qm", "diverged")
    diverged = box.head()
    out = box.run()
    assert out.returncode == 1 and "not a fast-forward" in out.stdout and box.head() == diverged
