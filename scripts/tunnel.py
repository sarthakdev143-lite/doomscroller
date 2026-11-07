"""Keep the phone review page reachable, and tell the owner its address.

Runs Cloudflare's quick tunnel to the review server (port 8600), which needs no account. Its
address changes whenever the tunnel restarts, so this records it in logs/tunnel_url.txt, where
the review page's alerts and `scripts/growth.py report` read it, and sends it to the phone.
Meant to run under systemd (scripts/install_services.sh), which restarts it if it dies.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.growth import notify  # noqa: E402

PORT = int(os.environ.get("DOOMSCROLLER_REVIEW_PORT", "8600"))
BINARY = os.environ.get("CLOUDFLARED_BIN", str(Path.home() / "tools" / "cloudflared"))
_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def main() -> int:
    if not Path(BINARY).is_file():
        print(f"cloudflared not found at {BINARY}", file=sys.stderr)
        return 1
    process = subprocess.Popen([BINARY, "tunnel", "--no-autoupdate", "--url", f"http://localhost:{PORT}"],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    announced = False
    for line in process.stdout:
        sys.stdout.write(line)
        found = _URL.search(line)
        if found and not announced:
            announced = True
            url = found.group(0)
            notify.TUNNEL_URL_FILE.parent.mkdir(parents=True, exist_ok=True)
            notify.TUNNEL_URL_FILE.write_text(url + "\n", encoding="utf-8")
            if notify.configured():
                notify.send("Your review page has a new address. You stay signed in.", title="Review link", link=url)
    return process.wait()


if __name__ == "__main__":
    sys.exit(main())
