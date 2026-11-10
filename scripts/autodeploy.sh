#!/bin/bash
# Pull master and restart the app when a merge lands, so the only step to ship is clicking "Merge" on
# GitHub. Installed as a once-a-minute user timer by scripts/install_services.sh. Safe by design: it acts
# only on the master branch, only fast-forwards (never a merge or a rebase), and never touches a dirty tree.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
stamp() { date -u +%FT%TZ; }

branch="$(git rev-parse --abbrev-ref HEAD)"
if [ "$branch" != "master" ]; then
  echo "$(stamp) autodeploy: on '$branch', not master — leaving it alone"; exit 0
fi
# Local edits mean someone is working here; don't clobber them.
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "$(stamp) autodeploy: working tree has changes — skipping"; exit 0
fi

git fetch --quiet origin master
local_head="$(git rev-parse HEAD)"
remote_head="$(git rev-parse origin/master)"
if [ "$local_head" = "$remote_head" ]; then exit 0; fi

echo "$(stamp) autodeploy: ${local_head:0:12} -> ${remote_head:0:12}, pulling and restarting"
if ! git merge --ff-only origin/master; then
  echo "$(stamp) autodeploy: not a fast-forward (local commits on master?) — leaving it for a person"; exit 1
fi
systemctl --user restart doomscroller.service
echo "$(stamp) autodeploy: done, service restarted"
