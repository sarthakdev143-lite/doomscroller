"""Tests for Cloudflare quick-tunnel discovery and Windows daemon spawning."""
from __future__ import annotations

from pathlib import Path
import subprocess

import pytest


def test_find_binary_resolves_windows_exe_and_path_fallback(tmp_path, monkeypatch):
    from scripts import tunnel

    monkeypatch.delenv("CLOUDFLARED_BIN", raising=False)
    monkeypatch.setattr(tunnel.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(tunnel.shutil, "which", lambda name: None)

    assert tunnel.find_binary() is None

    # PATH fallback when ~/tools has nothing
    on_path = tmp_path / "bin" / "cloudflared.exe"
    on_path.parent.mkdir(parents=True)
    on_path.write_bytes(b"exe")
    monkeypatch.setattr(tunnel.shutil, "which", lambda name: str(on_path))
    assert tunnel.find_binary() == str(on_path)

    # ~/tools/cloudflared.exe wins over PATH on Windows
    tools_exe = tmp_path / "tools" / "cloudflared.exe"
    tools_exe.parent.mkdir(parents=True)
    tools_exe.write_bytes(b"exe")
    monkeypatch.setattr(tunnel, "_is_windows", lambda: True)
    assert tunnel.find_binary() == str(tools_exe)

    # Explicit CLOUDFLARED_BIN wins over everything when the file exists
    custom = tmp_path / "custom-cf.exe"
    custom.write_bytes(b"exe")
    monkeypatch.setenv("CLOUDFLARED_BIN", str(custom))
    assert tunnel.find_binary() == str(custom)


def test_spawn_falls_back_when_breakaway_from_job_is_denied(tmp_path, monkeypatch):
    from scripts import run

    calls: list[int] = []

    class DummyProc:
        pid = 4321

    def fake_popen(cmd, **kwargs):
        flags = kwargs.get("creationflags", 0)
        calls.append(flags)
        if flags & getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000):
            raise PermissionError("Access is denied")
        return DummyProc()

    monkeypatch.setattr(run, "IS_WINDOWS", True)
    monkeypatch.setattr(run.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    monkeypatch.setattr(run.subprocess, "DETACHED_PROCESS", 0x008, raising=False)
    monkeypatch.setattr(run.subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000, raising=False)
    monkeypatch.setattr(run.subprocess, "Popen", fake_popen)

    proc = run._spawn(["python", "--version"], tmp_path / "out.log")
    assert proc.pid == 4321
    assert len(calls) == 2
    assert calls[0] & 0x01000000
    assert not (calls[1] & 0x01000000)


def test_windows_daemon_starts_tunnel_when_cloudflared_exists_and_posix_leaves_it_to_systemd(tmp_path, monkeypatch):
    import itertools
    from scripts import run, serve_slim, tunnel

    assert hasattr(tunnel, "TunnelSupervisor")
    assert hasattr(tunnel, "tunnel_env")
    assert hasattr(tunnel, "tunnel_command")
    assert hasattr(tunnel, "ssh_tunnel_command")

    spawned: list[tuple[list[str], Path]] = []

    class FakeProc:
        def __init__(self, pid: int):
            self.pid = pid

    pids = itertools.count(101)
    monkeypatch.setattr(run, "LOG_DIR", tmp_path / "logs")
    run.LOG_DIR.mkdir()
    monkeypatch.setattr(run, "PID_FILE", tmp_path / "doomscroller.pids")
    monkeypatch.setattr(run, "_running", lambda: {})
    monkeypatch.setattr(run, "_port_in_use", lambda port: False)
    monkeypatch.setattr(run.time, "sleep", lambda s: None)
    monkeypatch.setattr(run, "_spawn", lambda cmd, log_path: spawned.append((cmd, log_path)) or FakeProc(next(pids)))

    # Unified single-process runner on both Windows and POSIX; Windows embeds TunnelSupervisor inside serve_slim
    monkeypatch.delenv("DOOMSCROLLER_EMBED_TUNNEL", raising=False)
    monkeypatch.setattr(run, "IS_WINDOWS", True)
    monkeypatch.setattr(serve_slim, "IS_WINDOWS", True)
    assert serve_slim.should_embed_tunnel() is True
    assert run._start(daemon=True) == 0
    assert run._read_pids() == {"slim": 101}
    assert len(spawned) == 1
    assert "scripts/serve_slim.py" in " ".join(spawned[0][0]).replace("\\", "/")

    # POSIX -> single slim server process; doomscroller-tunnel.service runs scripts/tunnel.py separately
    spawned.clear()
    run.PID_FILE.unlink()
    monkeypatch.setattr(run, "IS_WINDOWS", False)
    monkeypatch.setattr(serve_slim, "IS_WINDOWS", False)
    assert serve_slim.should_embed_tunnel() is False
    assert run._start(daemon=True) == 0
    assert run._read_pids() == {"slim": 102}


