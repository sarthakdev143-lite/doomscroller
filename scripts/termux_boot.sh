#!/data/data/com.termux/files/usr/bin/sh
# Termux:Boot auto-start for Android/Termux (e.g., 3 GB RAM devices) — no root needed
# Place at ~/.termux/boot/doomscroller.sh and chmod +x
# Requires: Termux:Boot app from F-Droid, termux-wake-lock

DOOM="$HOME/doomscroller"
LOGS="$DOOM/logs"
mkdir -p "$LOGS"

termux-wake-lock
termux-wake-lock acquire 2>/dev/null

cd "$DOOM" || exit 1

# Ensure DB WAL mode for power-cut safety
python -c "import sqlite3; conn=sqlite3.connect('doomscroller.db'); conn.execute('PRAGMA journal_mode=WAL;'); conn.execute('PRAGMA synchronous=NORMAL;'); conn.commit(); conn.close()" 2>/dev/null

# Start unified slim server (API :8000 + Control Panel :8600 + scheduler + optional tunnel)
python scripts/run.py --ensure >> "$LOGS/watchdog.log" 2>&1

# Watchdog: idempotently ensure the slim daemon stays alive across network/power hiccups
(
while true; do
  sleep 60
  python scripts/run.py --ensure >> "$LOGS/watchdog.log" 2>&1
  if ! ping -c 1 -W 5 8.8.8.8 >/dev/null 2>&1; then
    echo "$(date) offline" >> "$LOGS/watchdog.log"
  fi
done
) >> "$LOGS/watchdog.log" 2>&1 &

echo "$(date) Doomscroller boot done" >> "$LOGS/boot.log"
