# Security Policy

## Supported Versions

Doomscroller tracks a rolling `master` deployment. Security fixes land on `next` / `master` and auto-deploy to managed Linux instances via `scripts/autodeploy.sh`.

| Branch / Version | Supported |
|------------------|-----------|
| `master` (latest) | Yes |
| Older commits | No — update to latest `master` |

## Reporting a Vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Please report vulnerabilities privately via [GitHub Private Vulnerability Reporting](https://github.com/sarthakdev143-lite/doomscroller/security/advisories/new). Include:

1. Affected file(s), route(s), or CLI entrypoint(s).
2. Minimal offline reproduction steps (never include real OAuth tokens, API keys, `.env` files, or live tunnel URLs).
3. Impact assessment (e.g., auth bypass on `:8600`, exposure of `:8000`, command/filtergraph injection, path traversal, or secret leakage).

We aim to acknowledge reports within 72 hours and ship a tested fix promptly.

## Security Model & Hard Invariants

- **Network boundary**: `scripts/serve_slim.py` binds both servers to `127.0.0.1` (`DOOMSCROLLER_HOST`). Port `8000` (`127.0.0.1:8000`, `app/main.py`) is an unauthenticated internal API and must **never** be tunnelled or bound publicly. Only port `8600` (`127.0.0.1:8600`, `app/review.py`) may be exposed via `scripts/tunnel.py`.
- **Control Panel authentication**: Every route on `app.review.review_app` except `GET /` (which serves the locked login page when unauthenticated) must call `_guard(request)` first and verify the `REVIEW_TOKEN` `HttpOnly` cookie using constant-time comparison (`secrets.compare_digest`).
- **Untrusted text & media handling**:
  - AI CLIs run with tools disabled from an empty working directory (`opencode --standalone` with tools denied; `claude --tools "" --strict-mcp-config --safe-mode`) and `stdin=subprocess.DEVNULL` unless explicit input is piped.
  - FFmpeg `drawtext` filters pass user/creator text exclusively via `textfile=` with `expansion=none` (or relative-path `libass` files), never inline `text=`.
- **Secret hygiene**: `.env`, `secrets/`, `channels.json`, `cookies*.txt`, and `doomscroller.db*` are gitignored; sensitive runtime files are written atomically with `0600` permissions; diagnostic logs and API responses pass through `app.logging_config.scrub_secrets()`.
