"""Single-process low-memory server for Doomscroller.

Runs FastAPI + APScheduler on 127.0.0.1:8000 (private local API) and the authenticated
desktop/phone control panel on 127.0.0.1:8600 inside one shared Python runtime and asyncio
event loop, cutting resident RAM by ~65-70% while keeping port 8000 unreachable from the
public tunnel. On Windows (or when DOOMSCROLLER_EMBED_TUNNEL=1), also supervises the phone
tunnel in a daemon thread; on Linux systemd, ``doomscroller-tunnel.service`` runs
``scripts/tunnel.py`` as its own unit.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# Bound BLAS/OpenMP thread arenas BEFORE numpy/opencv can ever be imported.
os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "2")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "2")
os.environ.setdefault("PYTHONUTF8", "1")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.winproc import IS_WINDOWS, lower_current_process_priority, reclaim_memory  # noqa: E402
from scripts.tunnel import TunnelSupervisor, extract_tunnel_url, find_binary  # noqa: E402,F401

def _env_port(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if raw:
        try:
            port = int(raw)
            if 1 <= port <= 65535:
                return port
        except ValueError:
            pass
    return default


HOST = os.environ.get("DOOMSCROLLER_HOST", "127.0.0.1").strip() or "127.0.0.1"
API_PORT = _env_port("DOOMSCROLLER_API_PORT", 8000)
REVIEW_PORT = _env_port("DOOMSCROLLER_REVIEW_PORT", 8600)


def should_embed_tunnel() -> bool:
    """Embed TunnelSupervisor on Windows by default; leave it to systemd on POSIX unless requested."""
    override = os.environ.get("DOOMSCROLLER_EMBED_TUNNEL", "").strip().lower()
    if override in {"1", "true", "yes"}:
        return True
    if override in {"0", "false", "no"}:
        return False
    return IS_WINDOWS


async def _serve_both() -> None:
    import asyncio
    import uvicorn

    from app.main import app as api_app
    from app.review import review_app

    common = {
        "host": HOST,
        "ws": "none",
        "access_log": False,
        "timeout_keep_alive": 5,
        "log_level": "warning",
    }
    api_server = uvicorn.Server(uvicorn.Config(api_app, port=API_PORT, **common))
    review_server = uvicorn.Server(uvicorn.Config(review_app, port=REVIEW_PORT, **common))
    # Let review_server share api_server's signal handler rather than overwriting it twice.
    review_server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

    task_api = asyncio.create_task(api_server.serve())
    task_review = asyncio.create_task(review_server.serve())

    async def _post_boot_trim() -> None:
        await asyncio.sleep(5.0)
        reclaim_memory()

    asyncio.create_task(_post_boot_trim())
    done, pending = await asyncio.wait({task_api, task_review}, return_when=asyncio.FIRST_COMPLETED)
    api_server.should_exit = True
    review_server.should_exit = True
    for task in pending:
        await task
    for task in done:
        task.result()


def main() -> int:
    import asyncio
    import gc

    lower_current_process_priority()
    supervisor: TunnelSupervisor | None = None
    if should_embed_tunnel():
        binary = find_binary()
        ssh_bin = shutil.which("ssh")
        if binary or ssh_bin:
            supervisor = TunnelSupervisor(binary, REVIEW_PORT, ssh_bin=ssh_bin)
            supervisor.start()
    gc.collect()
    try:
        asyncio.run(_serve_both())
    except KeyboardInterrupt:
        pass
    finally:
        if supervisor is not None:
            supervisor.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
