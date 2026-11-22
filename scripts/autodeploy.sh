#!/bin/bash
# Pull master and restart the app when a merge lands, so shipping is just clicking "Merge" on GitHub.
# Safety guards (verified by tests/test_autodeploy_script.py):
#   - acts only on master, only fast-forwards (never merges, rebases, resets or pushes), skips dirty trees;
#   - installs changed requirements.txt via `git show` into venv/ BEFORE fast-forwarding;
#   - polls local /health after restart and alerts via stderr + app.growth.notify on failure;
#   - records any failed commit in logs/autodeploy_bad_sha so the 1-minute timer never loops on it.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/venv/bin/python"
PIP="$ROOT/venv/bin/pip"
STATE_DIR="$ROOT/logs"
BAD_SHA_FILE="$STATE_DIR/autodeploy_bad_sha"
LOCK_FILE="$STATE_DIR/autodeploy.lock"
HEALTH_URL="http://127.0.0.1:${DOOMSCROLLER_API_PORT:-8000}/health"
HEALTH_TIMEOUT="${AUTODEPLOY_HEALTH_TIMEOUT:-60}"
HEALTH_INTERVAL="${AUTODEPLOY_HEALTH_INTERVAL:-2}"
PIP_TIMEOUT="${AUTODEPLOY_PIP_TIMEOUT:-600}"
REQ_TMP=""

stamp() { date -u +%FT%TZ; }
log() { echo "$(stamp) autodeploy: $*"; }
# shellcheck disable=SC2317
cleanup() { if [ -n "$REQ_TMP" ]; then rm -f "$REQ_TMP"; fi; }

with_timeout() {
  local secs="$1"; shift
  if command -v timeout >/dev/null 2>&1; then timeout "$secs" "$@"; else "$@"; fi
}

alert() {
  local msg="$1"
  echo "$(stamp) autodeploy: ALERT: $msg" >&2
  if [ ! -x "$PY" ]; then return 0; fi
  if ! with_timeout 30 "$PY" -c 'import sys; sys.path.insert(0, sys.argv[1])
from app.growth import notify
raise SystemExit(0 if notify.configured() and notify.send(sys.argv[2], title="Doomscroller deploy", link=notify.review_link()) else 1)
' "$ROOT" "$msg" >/dev/null 2>&1; then
    log "phone alert skipped or failed"
  fi
}

mark_bad() {
  mkdir -p "$STATE_DIR"
  printf '%s %s %s\n' "$1" "$(stamp)" "$2" >"$BAD_SHA_FILE"
}

probe() {
  if command -v curl >/dev/null 2>&1; then
    curl -fsS --max-time 5 -o /dev/null "$HEALTH_URL"
  elif [ -x "$PY" ]; then
    "$PY" -c 'import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=5).read()' "$HEALTH_URL"
  else
    return 1
  fi
}

wait_healthy() {
  local deadline
  deadline=$((SECONDS + HEALTH_TIMEOUT))
  while :; do
    if probe >/dev/null 2>&1; then return 0; fi
    if [ "$SECONDS" -ge "$deadline" ]; then return 1; fi
    sleep "$HEALTH_INTERVAL"
  done
}

install_requirements() {
  local sha="$1"
  mkdir -p "$STATE_DIR"
  REQ_TMP="$(mktemp "$STATE_DIR/autodeploy-requirements.XXXXXX")"
  git show "$sha:requirements.txt" >"$REQ_TMP"
  if [ -x "$PIP" ]; then
    with_timeout "$PIP_TIMEOUT" "$PIP" install --quiet --disable-pip-version-check --no-input -r "$REQ_TMP"
  elif [ -x "$PY" ]; then
    with_timeout "$PIP_TIMEOUT" "$PY" -m pip install --quiet --disable-pip-version-check --no-input -r "$REQ_TMP"
  else
    log "no venv pip or python under $ROOT/venv/bin"; return 1
  fi
}

main() {
  cd "$ROOT"
  trap cleanup EXIT
  mkdir -p "$STATE_DIR"
  if command -v flock >/dev/null 2>&1; then
    exec 9>"$LOCK_FILE"
    if ! flock -n 9; then log "another deploy is already running -- skipping"; exit 0; fi
  fi

  local branch
  branch="$(git rev-parse --abbrev-ref HEAD)"
  if [ "$branch" != "master" ]; then
    log "on '$branch', not master -- leaving it alone"; exit 0
  fi
  if ! git diff --quiet || ! git diff --cached --quiet; then
    log "working tree has changes -- skipping"; exit 0
  fi

  with_timeout 120 git fetch --quiet origin master
  local local_head remote_head bad_sha=""
  local_head="$(git rev-parse HEAD)"
  remote_head="$(git rev-parse origin/master)"
  if [ "$local_head" = "$remote_head" ]; then exit 0; fi

  if [ -f "$BAD_SHA_FILE" ]; then read -r bad_sha _ <"$BAD_SHA_FILE" || true; fi
  if [ "$bad_sha" = "$remote_head" ]; then
    log "${remote_head:0:12} already failed to deploy (see $BAD_SHA_FILE) -- waiting for a new commit"; exit 0
  fi
  if ! git merge-base --is-ancestor "$local_head" "$remote_head"; then
    log "not a fast-forward (local commits on master?) -- leaving it for a person"; exit 1
  fi

  log "${local_head:0:12} -> ${remote_head:0:12}"
  if ! git diff --quiet "$local_head" "$remote_head" -- requirements.txt; then
    log "requirements.txt changed, installing new requirements before fast-forwarding"
    if ! install_requirements "$remote_head"; then
      mark_bad "$remote_head" "pip-install-failed"
      alert "deploy of ${remote_head:0:12} stopped: pip install failed. Still running ${local_head:0:12}; merge a fix or delete $BAD_SHA_FILE to retry."
      exit 1
    fi
  fi

  if ! git merge --ff-only --quiet "$remote_head"; then
    log "fast-forward failed -- leaving it for a person"; exit 1
  fi
  systemctl --user restart doomscroller.service
  log "service restarted, waiting up to ${HEALTH_TIMEOUT}s for $HEALTH_URL"
  if wait_healthy; then
    rm -f "$BAD_SHA_FILE"
    log "done, ${remote_head:0:12} is live and healthy"; exit 0
  fi
  mark_bad "$remote_head" "health-check-failed"
  alert "deploy of ${remote_head:0:12} is NOT healthy: $HEALTH_URL did not answer within ${HEALTH_TIMEOUT}s. Check: journalctl --user -u doomscroller.service"
  exit 1
}

main "$@"
