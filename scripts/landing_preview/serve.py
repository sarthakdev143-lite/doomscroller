"""Serve an exported site (scripts/saas_export_static.py --previews) the way Cloudflare Pages will, for recording.

    python scripts/landing_preview/serve.py <site-folder> [port]

HTML gets the same Content-Security-Policy that edge/functions/_middleware.js sends (without the per-response
nonce: the preview pages have no inline scripts), so a page that breaks the policy breaks here too. Nothing is
cached, so a recording always sees the files it was built with.
"""
from __future__ import annotations

import functools
import http.server
import sys

CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' https://challenges.cloudflare.com",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self' data:",
    "connect-src 'self' https://challenges.cloudflare.com",
    "frame-src https://challenges.cloudflare.com",
    "worker-src 'self' blob:",
    "manifest-src 'self'",
    "media-src 'self' blob:",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])


class Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".js": "text/javascript",
        ".css": "text/css",
        ".json": "application/json",
        ".webp": "image/webp",
        ".woff2": "font/woff2",
        ".bin": "application/octet-stream",
    }

    def end_headers(self) -> None:
        path = self.path.split("?", 1)[0]
        if path.endswith("/") or path.endswith(".html"):
            self.send_header("Content-Security-Policy", CSP)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def log_message(self, *args) -> None:  # quiet
        pass


def main() -> int:
    root = sys.argv[1]
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8080
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), functools.partial(Handler, directory=root))
    print(f"serving {root} on http://127.0.0.1:{port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
