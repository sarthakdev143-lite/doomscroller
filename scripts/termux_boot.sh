#!/data/data/com.termux/files/usr/bin/sh
# Termux:Boot auto-start for Vivo Y69 (3GB) — no root needed
# Place at ~/.termux/boot/doomscroller.sh  and chmod +x
# Requires: Termux:Boot app from F-Droid, termux-wake-lock

DOOM="$HOME/doomscroller"
LOGS="$DOOM/logs"
mkdir -p "$LOGS"

termux-wake-lock
termux-wake-lock acquire 2>/dev/null

cd "$DOOM" || exit 1

# Ensure DB WAL mode for power-cut safety
python -c "import sqlite3; conn=sqlite3.connect('doomscroller.db'); conn.execute('PRAGMA journal_mode=WAL;'); conn.execute('PRAGMA synchronous=NORMAL;'); conn.commit(); conn.close()" 2>/dev/null

# Kill stale
pkill -f "uvicorn app.main:app" 2>/dev/null
pkill -f "streamlit run dashboard" 2>/dev/null
sleep 1

nohup python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 > "$LOGS/api.log" 2>&1 &
echo $! > "$DOOM/.api.pid"
sleep 3
nohup python -m streamlit run dashboard/app.py --server.port 8501 --server.headless true > "$LOGS/dashboard.log" 2>&1 &
echo $! > "$DOOM/.dash.pid"

# Watchdog: restart if dead (handles network/power hiccups)
(
while true; do
  sleep 60
  if ! pgrep -f "uvicorn app.main:app" >/dev/null; then
    echo "$(date) API dead, restarting" >> "$LOGS/watchdog.log"
    nohup python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 > "$LOGS/api.log" 2>&1 &
  fi
  if ! pgrep -f "streamlit run dashboard" >/dev/null; then
    echo "$(date) Dashboard dead, restarting" >> "$LOGS/watchdog.log"
    nohup python -m streamlit run dashboard/app.py --server.port 8501 --server.headless true > "$LOGS/dashboard.log" 2>&1 &
  fi
  # Network cut fallback: ping and log, scheduler will coalesce missed jobs on reconnect (misfire_grace 3600)
  if ! ping -c 1 -W 5 8.8.8.8 >/dev/null 2>&1; then
    echo "$(date) offline" >> "$LOGS/watchdog.log"
  fi
done
) > "$LOGS/watchdog.log" 2>&1 &

echo "$(date) Doomscroller boot done" >> "$LOGS/boot.log"
