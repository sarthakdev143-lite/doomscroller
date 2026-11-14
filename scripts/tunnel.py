"""Keep the phone review page reachable, and tell the owner its address.

Owns the single canonical quick-tunnel supervisor used both by ``scripts/serve_slim.py``
(in-process daemon thread on Windows) and directly as ``python scripts/tunnel.py`` under
Linux systemd (``scripts/install_services.sh``). Supports Cloudflare's accountless quick
tunnel first and falls back automatically to OpenSSH ``localhost.run`` if Cloudflare is
missing or rate-limited (HTTP 429).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.winproc import background_subprocess_kwargs, trim_working_set  # noqa: E402

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


PORT = _env_port("DOOMSCROLLER_REVIEW_PORT", 8600)
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
# Match trycloudflare hyphenated slugs (excluding api.trycloudflare.com) and localhost.run .lhr.life tunnels.
_URL = re.compile(
    r"https://(?:(?!api\.)[a-z0-9]+(?:-[a-z0-9]+)+\.trycloudflare\.com|[a-z0-9]{6,}\.lhr\.life)",
    re.IGNORECASE,
)


def extract_tunnel_url(line: str) -> str | None:
    if not line:
        return None
    cleaned = _ANSI_ESCAPE_RE.sub("", line)
    cleaned = "".join(ch for ch in cleaned if ch >= " " and ch != "\x7f")
    match = _URL.search(cleaned)
    if not match:
        return None
    return match.group(0).rstrip(".,;:!?)>]}'\"")


def _is_windows() -> bool:
    """OS check behind one name so tests can simulate Windows without touching global os."""
    return os.name == "nt"


def find_binary() -> str | None:
    """Locate cloudflared from CLOUDFLARED_BIN, ~/tools/, or PATH."""
    configured = os.environ.get("CLOUDFLARED_BIN", "").strip()
    if configured:
        return configured if Path(configured).is_file() else None
    tools_dir = Path.home() / "tools"
    names = ("cloudflared.exe", "cloudflared") if _is_windows() else ("cloudflared",)
    for name in names:
        candidate = tools_dir / name
        if candidate.is_file():
            return str(candidate)
    return shutil.which("cloudflared")


def tunnel_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for cloudflared that keeps Go's heap and thread count small."""
    env = dict(os.environ if base is None else base)
    env.setdefault("GOMEMLIMIT", "48MiB")
    env.setdefault("GOMAXPROCS", "2")
    return env


def tunnel_command(binary: str, port: int = PORT) -> list[str]:
    """Build cloudflared quick-tunnel args. IPv4 + HTTP/2 avoids broken ISP IPv6 and sleep drops."""
    return [
        binary,
        "tunnel",
        "--no-autoupdate",
        "--edge-ip-version",
        "4",
        "--protocol",
        "http2",
        "--url",
        f"http://localhost:{port}",
    ]


def ssh_tunnel_command(ssh_bin: str, port: int = PORT) -> list[str]:
    """Zero-install fallback tunnel via OpenSSH + localhost.run (~4 MB RAM, immune to Cloudflare 429s)."""
    return [
        ssh_bin,
        "-4",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-o", "ExitOnForwardFailure=yes",
        "-T",
        "-R", f"80:127.0.0.1:{port}",
        "nokey@localhost.run",
    ]


class TunnelSupervisor:
    """Keeps the phone tunnel alive across sleep/wake, network drops, and Cloudflare 429 rate limits."""

    def __init__(
        self,
        binary: str | None,
        port: int = PORT,
        ssh_bin: str | None = None,
        *,
        echo_stdout: bool = False,
    ) -> None:
        self.binary = binary
        self.ssh_bin = ssh_bin if ssh_bin is not None else shutil.which("ssh")
        self.port = port
        self.echo_stdout = echo_stdout
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._thread: threading.Thread | None = None
        self._last_url = ""

    def start(self) -> None:
        self._thread = threading.Thread(target=self.run_forever, name="tunnel-supervisor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def _announce(self, url: str) -> None:
        from app.growth import notify

        clean_url = url.strip().rstrip(".,;:!?)>]}'\"")
        if not clean_url:
            return
        target = notify.TUNNEL_URL_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
        tmp.write_text(clean_url + "\n", encoding="utf-8")
        os.replace(tmp, target)
        if clean_url != self._last_url:
            self._last_url = clean_url
            if notify.configured():
                notify.send(
                    "Your review page has a new address. Enter your review key once on the new domain.",
                    title="Review link",
                    link=clean_url,
                )

    def _providers(self) -> list[tuple[str, list[str], dict[str, str]]]:
        providers: list[tuple[str, list[str], dict[str, str]]] = []
        if self.binary:
            providers.append(("cloudflared", tunnel_command(self.binary, self.port), tunnel_env()))
        if self.ssh_bin:
            providers.append(("localhost.run", ssh_tunnel_command(self.ssh_bin, self.port), dict(os.environ)))
        return providers

    def run_forever(self) -> None:
        providers = self._providers()
        if not providers:
            return
        log_path = PROJECT_ROOT / "logs" / "tunnel.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        provider_idx = 0
        backoff = 5.0
        while not self._stop.is_set():
            name, cmd, env = providers[provider_idx % len(providers)]
            started = time.monotonic()
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=env,
                    **background_subprocess_kwargs(low_priority=False),
                )
            except OSError as exc:
                with open(log_path, "a", encoding="utf-8", errors="replace") as log_file:
                    log_file.write(f"[supervisor] {name} start failed: {exc}\n")
                provider_idx += 1
                self._stop.wait(2.0)
                continue

            with self._lock:
                self._proc = proc
            announced = False
            trimmed = False
            with open(log_path, "a", encoding="utf-8", errors="replace") as log_file:
                assert proc.stdout is not None
                for line in proc.stdout:
                    log_file.write(line)
                    log_file.flush()
                    if self.echo_stdout:
                        try:
                            sys.stdout.write(line)
                        except UnicodeEncodeError:
                            enc = sys.stdout.encoding or "utf-8"
                            sys.stdout.write(line.encode(enc, errors="replace").decode(enc, errors="replace"))
                        sys.stdout.flush()
                    url = extract_tunnel_url(line)
                    if url:
                        announced = True
                        self._announce(url)
                        if not trimmed:
                            trimmed = True
                            timer = threading.Timer(3.0, trim_working_set, kwargs={"pid": proc.pid})
                            timer.daemon = True
                            timer.start()
            proc.wait()
            if self._stop.is_set():
                break
            if not announced and len(providers) > 1:
                provider_idx += 1
                self._stop.wait(2.0)
                continue
            backoff = 5.0 if (time.monotonic() - started) > 120 else min(60.0, backoff * 2)
            self._stop.wait(backoff)


def main() -> int:
    binary = find_binary()
    ssh_bin = shutil.which("ssh")
    if not binary and not ssh_bin:
        expected = os.environ.get("CLOUDFLARED_BIN") or str(
            Path.home() / "tools" / ("cloudflared.exe" if os.name == "nt" else "cloudflared")
        )
        print(f"neither cloudflared ({expected}) nor ssh found on PATH", file=sys.stderr)
        return 1
    supervisor = TunnelSupervisor(binary, port=PORT, ssh_bin=ssh_bin, echo_stdout=True)
    try:
        supervisor.run_forever()
    except KeyboardInterrupt:
        supervisor.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
