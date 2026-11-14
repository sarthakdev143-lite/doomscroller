"""Tests for Windows low-resource optimizations, slim single-process server, and auto-start watchdog."""
from __future__ import annotations

from pathlib import Path
from sqlalchemy import text


def test_windows_background_popen_kwargs_hides_window_and_lowers_priority(monkeypatch):
    from app import winproc

    monkeypatch.setattr(winproc, "IS_WINDOWS", True)
    monkeypatch.setattr(winproc, "CREATE_NO_WINDOW", 0x08000000)
    monkeypatch.setattr(winproc, "BELOW_NORMAL_PRIORITY_CLASS", 0x00004000)

    low = winproc.background_subprocess_kwargs(low_priority=True)
    assert low == {"creationflags": 0x08000000 | 0x00004000}

    quiet = winproc.background_subprocess_kwargs(low_priority=False)
    assert quiet == {"creationflags": 0x08000000}

    monkeypatch.setattr(winproc, "IS_WINDOWS", False)
    assert winproc.background_subprocess_kwargs(low_priority=True) == {}


def test_ffmpeg_helpers_apply_thread_cap_and_windows_flags(monkeypatch):
    import subprocess
    from app.clipper import media
    from app.config import settings
    from app.transformers.ffmpeg_transformer import FFmpegTransformer

    seen: list[tuple[list[str], dict]] = []

    class OkProc:
        returncode = 0
        stdout = b"frame"
        stderr = ""

    def fake_run(cmd, **kwargs):
        seen.append((list(cmd), kwargs))
        return OkProc()

    monkeypatch.setattr(media.subprocess, "run", fake_run)
    monkeypatch.setattr(media, "background_subprocess_kwargs", lambda low_priority=True: {"creationflags": 0x08004000})

    monkeypatch.setattr(settings, "ffmpeg_threads", 2)
    media.run_ffmpeg(["-i", "in.mp4", "-c:v", "libx264", "out.mp4"])
    cmd, kwargs = seen[-1]
    assert kwargs.get("creationflags") == 0x08004000
    assert kwargs.get("stdin") == subprocess.DEVNULL
    assert cmd[:8] == [
        "ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
        "-filter_threads", "2",
    ]
    assert cmd[8:10] == ["-filter_complex_threads", "2"]
    # Output encoder gets -threads 2 immediately before output path
    assert cmd[-3:] == ["-threads", "2", "out.mp4"]

    # FFmpegTransformer delegates to media.run_ffmpeg and never duplicates an explicit -threads flag
    seen.clear()
    FFmpegTransformer._run(["-i", "in.mp4", "-threads", "1", "-movflags", "+faststart", "out.mp4"])
    t_cmd, t_kwargs = seen[-1]
    assert t_kwargs.get("stdin") == subprocess.DEVNULL
    assert "-nostdin" in t_cmd
    assert t_cmd.count("-threads") == 1

    # When ffmpeg_threads == 0, leave threading completely untouched
    seen.clear()
    monkeypatch.setattr(settings, "ffmpeg_threads", 0)
    media.run_ffmpeg(["-i", "in.mp4", "out.mp4"])
    cmd0, _ = seen[-1]
    assert "-threads" not in cmd0
    assert "-filter_threads" not in cmd0


def test_sqlite_file_engine_enables_wal_and_bounded_cache(tmp_path):
    from app.db import create_engine_for_url

    db_file = tmp_path / "wal_check.db"
    eng = create_engine_for_url(f"sqlite:///{db_file.as_posix()}")
    try:
        with eng.connect() as conn:
            journal = conn.execute(text("PRAGMA journal_mode")).scalar()
            sync = conn.execute(text("PRAGMA synchronous")).scalar()
            cache = conn.execute(text("PRAGMA cache_size")).scalar()
        assert str(journal).lower() == "wal"
        assert int(sync) == 1  # NORMAL
        assert int(cache) == -4000
    finally:
        eng.dispose()


def test_scheduler_respects_configured_max_workers(monkeypatch):
    from app import scheduler
    from app.config import settings

    monkeypatch.setattr(settings, "scheduler_max_workers", 2)
    sched = scheduler._make_scheduler()
    executor = sched._executors["default"]
    assert executor._pool._max_workers == 2


def test_run_slim_daemon_spawns_single_process_and_ensure_is_idempotent(tmp_path, monkeypatch):
    from scripts import run

    spawned: list[tuple[list[str], Path]] = []

    class FakeProc:
        pid = 7777

    monkeypatch.setattr(run, "LOG_DIR", tmp_path / "logs")
    run.LOG_DIR.mkdir()
    monkeypatch.setattr(run, "PID_FILE", tmp_path / "doomscroller.pids")
    monkeypatch.setattr(run, "IS_WINDOWS", True)
    monkeypatch.setattr(run, "_running", lambda: {})
    # Even if dashboard port 8501 were busy, slim mode ignores it and only checks 8000 & 8600
    monkeypatch.setattr(run, "_port_in_use", lambda port: False)
    monkeypatch.setattr(run.time, "sleep", lambda s: None)
    monkeypatch.setattr(
        run, "_spawn",
        lambda cmd, log_path, **kw: spawned.append((cmd, log_path)) or FakeProc(),
    )

    assert run._start(daemon=True, slim=True) == 0
    assert len(spawned) == 1
    assert "scripts/serve_slim.py" in " ".join(spawned[0][0]).replace("\\", "/")
    assert run._read_pids() == {"slim": 7777}

    # _ensure is a no-op when slim is alive and both ports (8000, 8600) are listening
    spawned.clear()
    monkeypatch.setattr(run, "_running", lambda: {"slim": 7777})
    monkeypatch.setattr(run, "_port_in_use", lambda port: port in (run.API_PORT, run.REVIEW_PORT))
    assert run._ensure(slim=True) == 0
    assert spawned == []

    # If a port died despite a PID entry, _ensure restarts cleanly
    monkeypatch.setattr(run, "_port_in_use", lambda port: False)
    stopped: list[int] = []
    monkeypatch.setattr(run, "_kill", lambda pid: stopped.append(pid) or True)
    monkeypatch.setattr(run, "_running", lambda: {} if stopped else {"slim": 7777})
    assert run._ensure(slim=True) == 0
    assert stopped == [7777]
    assert len(spawned) == 1


def test_serve_slim_tunnel_env_caps_go_runtime_memory():
    from scripts import tunnel

    env = tunnel.tunnel_env({"EXISTING": "1"})
    assert env["GOMEMLIMIT"] == "48MiB"
    assert env["GOMAXPROCS"] == "2"
    assert env["EXISTING"] == "1"

    cmd = tunnel.tunnel_command("C:/tools/cloudflared.exe", 8600)
    assert cmd == [
        "C:/tools/cloudflared.exe",
        "tunnel",
        "--no-autoupdate",
        "--edge-ip-version",
        "4",
        "--protocol",
        "http2",
        "--url",
        "http://localhost:8600",
    ]


def test_tunnel_url_extractor_ignores_api_trycloudflare_error_line():
    from scripts import serve_slim, tunnel

    err_line = 'failed to request quick Tunnel: Post "https://api.trycloudflare.com/tunnel": context deadline exceeded'
    real_line = "|  https://bright-river-maple-sound.trycloudflare.com  |"
    lhr_line = "569c5b3ff0b86f.lhr.life tunneled with tls termination, https://569c5b3ff0b86f.lhr.life"
    lhr_admin = "To set up custom domains go to https://admin.localhost.run/"

    assert serve_slim.extract_tunnel_url(err_line) is None
    assert tunnel.extract_tunnel_url(err_line) is None
    assert tunnel.extract_tunnel_url(lhr_admin) is None
    assert serve_slim.extract_tunnel_url(real_line) == "https://bright-river-maple-sound.trycloudflare.com"
    assert tunnel.extract_tunnel_url(real_line) == "https://bright-river-maple-sound.trycloudflare.com"
    assert tunnel.extract_tunnel_url(lhr_line) == "https://569c5b3ff0b86f.lhr.life"
    assert "nokey@localhost.run" in tunnel.ssh_tunnel_command("ssh", 8600)


def test_lower_current_process_priority_uses_pointer_sized_handle_on_windows(monkeypatch):
    import os
    import subprocess
    import sys

    if os.name != "nt":
        return
    code = (
        "import ctypes; from app.winproc import lower_current_process_priority, BELOW_NORMAL_PRIORITY_CLASS; "
        "k = ctypes.windll.kernel32; k.GetCurrentProcess.restype = ctypes.c_void_p; "
        "k.GetPriorityClass.argtypes = [ctypes.c_void_p]; k.GetPriorityClass.restype = ctypes.c_uint32; "
        "ok = lower_current_process_priority(); "
        "cls = k.GetPriorityClass(k.GetCurrentProcess()); "
        "assert ok and cls == BELOW_NORMAL_PRIORITY_CLASS, f'ok={ok} cls={cls}'"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_idle_server_imports_do_not_load_numpy_until_needed():
    import subprocess
    import sys

    code = "import sys, app.main, app.review; assert 'numpy' not in sys.modules, 'numpy loaded at idle'"
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr

    # Model itself keeps a clean module-level numpy import; learn.py defers importing model until needed
    from app.growth import model as model_mod
    from app.growth.model import Model, Observation

    assert hasattr(model_mod, "np"), "model.py should have a single module-level numpy import"
    m = Model([
        Observation(arms={"hook": "a"}, reward=1.2, day=1.0),
        Observation(arms={"hook": "b"}, reward=2.4, day=2.0),
    ])
    summary = m.summary(draws=200)
    assert "hook" in summary and len(summary["hook"]) == 2


def test_trim_working_set_succeeds_on_windows():
    import os
    from app import winproc

    if os.name != "nt":
        assert winproc.trim_working_set() is False
        assert winproc.trim_working_set(pid=os.getpid()) is False
        assert winproc.reclaim_memory() is False
        return
    assert winproc.trim_working_set() is True
    assert winproc.trim_working_set(pid=os.getpid()) is True
    assert winproc.reclaim_memory() is True


def test_ingest_prefers_hardware_friendly_h264_before_av1_fallback(tmp_path, monkeypatch):
    from app.clipper import ingest
    from app.config import settings

    monkeypatch.setattr(settings, "clip_source_max_height", 720)
    fmt = ingest._ydl_options(tmp_path)["format"]
    tiers = fmt.split("/")
    assert tiers[0] == "bv*[vcodec^=avc1][height<=720]+ba[format_note*=original]"
    assert tiers[1] == "bv*[vcodec^=avc1][height<=720]+ba"
    assert "bv*[height<=720]+ba[format_note*=original]" in tiers


def test_watch_job_reuses_single_http_client_across_feeds(monkeypatch):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from app.config import settings
    from app.db import init_db
    from app.growth import watch

    init_db()
    created_clients: list[object] = []

    class FakeResp:
        text = "<feed xmlns='http://www.w3.org/2005/Atom'></feed>"

        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, **kwargs):
            self.calls: list[str] = []
            created_clients.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, **kwargs):
            self.calls.append(url)
            return FakeResp()

    ch = SimpleNamespace(id="ch1", slug="default")
    monkeypatch.setattr(settings, "growth_enabled", True)
    monkeypatch.setattr(settings, "growth_watch_minutes", 5)
    monkeypatch.setattr(watch.httpx, "Client", FakeClient)
    monkeypatch.setattr(watch.channels, "active_channels", lambda db: [ch])
    monkeypatch.setattr(
        watch.channels,
        "feeds_for",
        lambda db, c: [
            "https://www.youtube.com/channel/UC1111111111111111111111",
            "https://www.youtube.com/channel/UC2222222222222222222222",
            "https://www.youtube.com/channel/UC3333333333333333333333",
        ],
    )

    res = watch.watch_job(now=datetime(2026, 11, 12, tzinfo=timezone.utc))
    assert not hasattr(watch, "_SHARED_CLIENT"), "watch.py should pass client explicitly instead of ContextVar"
    assert res == {"feeds": 3, "queued": 0}
    assert len(created_clients) == 1
    assert len(created_clients[0].calls) == 3


def test_vectorized_signals_match_reference_on_cuts_motion_and_partial_audio(monkeypatch):
    import math
    import numpy as np
    from app.clipper import signals

    rng = np.random.default_rng(42)
    seconds = 4
    count = seconds * signals._FPS + 1  # includes trailing frame mapped to last second
    frames = rng.integers(0, 60, size=(count, signals._HEIGHT, signals._WIDTH), dtype=np.uint8)
    frames[8] = 250  # synthetic hard cut at 2.0s

    audio_samples = rng.integers(-12000, 12000, size=signals._AUDIO_RATE * 2 + 1234, dtype=np.int16)

    def fake_read_ffmpeg(args):
        if "rawvideo" in args:
            return frames.tobytes()
        return audio_samples.tobytes()

    monkeypatch.setattr(signals, "read_ffmpeg", fake_read_ffmpeg)

    got_v = signals._video_signals("fake.mp4", seconds)
    got_a = signals._loudness("fake.mp4", seconds, has_audio=True)

    # Reference scalar calculation from original implementation
    diffs = np.zeros(max(count, 1))
    columns = np.zeros((max(count, 1), signals._WIDTH))
    for i in range(1, count):
        delta = np.abs(frames[i].astype(np.int16) - frames[i - 1].astype(np.int16))
        diffs[i] = delta.mean() / 255
        columns[i] = delta.sum(axis=0)
    cuts = signals._find_cuts(diffs)
    is_cut = {int(cut * signals._FPS) for cut in cuts}
    motion = np.zeros(seconds)
    brightness = np.zeros(seconds)
    weight = np.zeros(seconds)
    position = np.zeros(seconds)
    samples = np.zeros(seconds)
    xs = np.arange(signals._WIDTH) / (signals._WIDTH - 1)
    for i in range(count):
        sec = min(seconds - 1, i // signals._FPS)
        brightness[sec] += frames[i].mean() / 255
        samples[sec] += 1
        if i in is_cut:
            continue
        motion[sec] += diffs[i]
        col_sum = columns[i].sum()
        if col_sum > 0:
            position[sec] += (columns[i] * xs).sum()
            weight[sec] += col_sum
    filled = samples > 0
    motion[filled] /= samples[filled]
    brightness[filled] /= samples[filled]
    centroid = np.where(weight > 0, position / np.maximum(weight, 1e-6), 0.5)

    ref_a = np.full(seconds, -90.0)
    for sec in range(seconds):
        chunk = audio_samples[sec * signals._AUDIO_RATE:(sec + 1) * signals._AUDIO_RATE].astype(np.float32)
        if chunk.size:
            rms = math.sqrt(float((chunk * chunk).mean()))
            ref_a[sec] = 20 * math.log10(max(rms / 32768, 1e-5))

    assert got_v["cuts"] == cuts
    assert np.allclose(got_v["motion"], motion, atol=1e-9)
    assert np.allclose(got_v["brightness"], brightness, atol=1e-9)
    assert np.allclose(got_v["centroid"], centroid, atol=1e-9)
    assert np.allclose(got_v["centroid_weight"], weight, atol=1e-9)
    assert np.allclose(got_a, ref_a, atol=1e-5)


def test_shots_uses_pillow_without_importing_cv2():
    import subprocess
    import sys

    code = "import sys, app.clipper.shots; assert 'cv2' not in sys.modules, 'shots imported cv2'"
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_faces_uses_640px_fast_bilinear_decode(monkeypatch):
    import numpy as np
    from app.clipper import faces

    captured = []

    def fake_read_ffmpeg(args):
        captured.append(list(args))
        return np.zeros((360 * 640 * 2,), dtype=np.uint8).tobytes()

    monkeypatch.setattr(faces, "read_ffmpeg", fake_read_ffmpeg)
    assert faces._DETECT_WIDTH == 640
    f1, f2 = faces._frames_at("sample.mp4", 1.5, 360)
    assert f1.shape == (360, 640) and f2.shape == (360, 640)
    assert any("scale=640:360:flags=fast_bilinear,format=gray" in str(x) for x in captured[0])


def test_pipeline_does_not_import_celery():
    import subprocess
    import sys

    code = "import sys, tasks.pipeline; assert 'celery' not in sys.modules, 'pipeline imported celery'"
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_remaining_background_subprocesses_hide_windows_console_and_close_stdin(tmp_path, monkeypatch):
    import subprocess
    from app import metadata
    from app.clipper import audio_qa
    from app.config import settings
    from app.scrapers import mock_scraper
    from app.transformers import ffmpeg_transformer
    from tasks import pipeline

    seen: list[tuple[list[str], dict]] = []

    class FakeProc:
        returncode = 0
        stdout = '{"verdict":"launch-grade","score":0.95,"choices":[{"message":{"content":"{}"}}]}'
        stderr = ""

    def fake_run(cmd, **kwargs):
        seen.append((list(cmd), kwargs))
        return FakeProc()

    flags = {"creationflags": 0x08004000}
    monkeypatch.setattr("app.winproc.background_subprocess_kwargs", lambda low_priority=True: dict(flags))
    monkeypatch.setattr(subprocess, "run", fake_run)

    sample = tmp_path / "sample.mp4"
    sample.write_bytes(b"0" * 2048)

    monkeypatch.setattr(metadata.shutil, "which", lambda name: "C:/bin/z-ai.exe")
    metadata._call_llm("sys", "usr")
    assert seen[-1][1].get("creationflags") == 0x08004000
    assert seen[-1][1].get("stdin") == subprocess.DEVNULL

    monkeypatch.setattr(settings, "earshot_qa", True)
    monkeypatch.setattr(audio_qa, "earshot_bin", lambda: "C:/bin/earshot.exe")
    audio_qa.score(str(sample))
    assert seen[-1][1].get("creationflags") == 0x08004000
    assert seen[-1][1].get("stdin") == subprocess.DEVNULL

    ffmpeg_transformer._probe_dims(str(sample))
    assert seen[-1][1].get("creationflags") == 0x08004000
    assert seen[-1][1].get("stdin") == subprocess.DEVNULL

    ffmpeg_transformer._probe_duration(str(sample))
    assert seen[-1][1].get("creationflags") == 0x08004000
    assert seen[-1][1].get("stdin") == subprocess.DEVNULL

    ffmpeg_transformer._has_audio(str(sample))
    assert seen[-1][1].get("creationflags") == 0x08004000
    assert seen[-1][1].get("stdin") == subprocess.DEVNULL

    pipeline._probe_quality(str(sample))
    assert seen[-1][1].get("creationflags") == 0x08004000
    assert seen[-1][1].get("stdin") == subprocess.DEVNULL

    mock_out = tmp_path / "master.mp4"
    mock_scraper._ensure_master_dummy(str(tmp_path), str(mock_out))
    assert seen[-1][1].get("creationflags") == 0x08004000
    assert seen[-1][1].get("stdin") == subprocess.DEVNULL


def test_review_app_uses_lifespan_without_on_event_deprecation():
    import warnings
    from fastapi.testclient import TestClient
    from app.review import review_app

    assert not review_app.router.on_startup, "review_app should use lifespan instead of deprecated on_event"
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DeprecationWarning)
        with TestClient(review_app) as client:
            client.get("/")
    on_event_warnings = [w for w in caught if "on_event is deprecated" in str(w.message)]
    assert on_event_warnings == []


def test_ensure_env_and_review_token_bootstraps_fresh_clone_idempotently(tmp_path, monkeypatch):
    from scripts import run

    example = tmp_path / ".env.example"
    example.write_text("APP_ENV=dev\nREVIEW_TOKEN=\nGROWTH_ENABLED=false\n", encoding="utf-8")
    env_file = tmp_path / ".env"

    monkeypatch.delenv("REVIEW_TOKEN", raising=False)
    token1, created1 = run.ensure_env_and_review_token(env_file, example)
    assert created1 is True
    assert len(token1) >= 24
    written = env_file.read_text(encoding="utf-8")
    assert f"REVIEW_TOKEN={token1}" in written
    assert "APP_ENV=dev" in written

    # Second run preserves the existing token untouched
    token2, created2 = run.ensure_env_and_review_token(env_file, example)
    assert created2 is False
    assert token2 == token1
    assert env_file.read_text(encoding="utf-8") == written

    url = run.local_panel_url(env_file, port=8600)
    assert url == f"http://localhost:8600/?key={token1}"

    # Under DOOMSCROLLER_ENV_FILE="" (test isolation), default call touches nothing
    monkeypatch.setenv("DOOMSCROLLER_ENV_FILE", "")
    t_empty, c_empty = run.ensure_env_and_review_token()
    assert c_empty is False and t_empty == ""


def test_windows_autostart_script_registers_immediate_repeating_watchdog():
    script = Path("scripts/windows_autostart.ps1").read_text(encoding="utf-8")
    assert "$LogonTrigger" in script and "$WatchdogTrigger" in script
    assert "-Trigger @($LogonTrigger, $WatchdogTrigger)" in script


def test_scripts_have_no_removed_streamlit_references_and_final_audit_runs_offline():
    for script_path in Path("scripts").iterdir():
        if script_path.is_file() and script_path.suffix in (".py", ".sh", ".ps1"):
            text = script_path.read_text(encoding="utf-8")
            assert "dashboard/app.py" not in text, f"{script_path} still references removed dashboard/app.py"
            assert "streamlit run" not in text, f"{script_path} still references removed streamlit"

    audit_src = Path("scripts/final_audit.py").read_text(encoding="utf-8")
    assert "D:\\Sarthak" not in audit_src, "final_audit.py must not hardcode a developer machine path"

    from scripts import final_audit

    report = final_audit.run_audit(live=False)
    assert report["platforms_ok"] is True
    assert "storage" in report



