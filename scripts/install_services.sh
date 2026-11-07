#!/bin/bash
# Run the app and the phone tunnel as user services: they start at boot (with lingering on)
# and restart if they crash. Re-run after moving the project or changing tools on PATH.
#   scripts/install_services.sh          install and start
#   scripts/install_services.sh remove   stop and remove
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UNITS="$HOME/.config/systemd/user"
if [ "${1:-}" = "remove" ]; then
  systemctl --user disable --now doomscroller-tunnel.service doomscroller.service 2>/dev/null || true
  rm -f "$UNITS/doomscroller.service" "$UNITS/doomscroller-tunnel.service"
  systemctl --user daemon-reload
  echo "Removed."
  exit 0
fi
mkdir -p "$UNITS"
# Services start with a bare PATH, but the clipper needs ffmpeg, node (YouTube's challenge), opencode and claude.
cat > "$UNITS/doomscroller.service" <<UNIT
[Unit]
Description=Doomscroller: API with scheduler, dashboard and review page
After=network-online.target

[Service]
WorkingDirectory=$ROOT
Environment=PATH=$PATH
Environment=PYTHONUNBUFFERED=1
ExecStartPre=/bin/rm -f $ROOT/doomscroller.pids
ExecStart=$ROOT/venv/bin/python scripts/run.py
Restart=always
RestartSec=15

[Install]
WantedBy=default.target
UNIT
cat > "$UNITS/doomscroller-tunnel.service" <<UNIT
[Unit]
Description=Doomscroller: Cloudflare tunnel to the phone review page
After=network-online.target doomscroller.service

[Service]
WorkingDirectory=$ROOT
Environment=PATH=$PATH
Environment=PYTHONUNBUFFERED=1
ExecStart=$ROOT/venv/bin/python scripts/tunnel.py
Restart=always
RestartSec=20

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable --now doomscroller.service doomscroller-tunnel.service
loginctl show-user "$USER" -p Linger | grep -q yes || echo "Note: lingering is off, so services stop when you log out."
echo "Installed. Status: systemctl --user status doomscroller doomscroller-tunnel"
