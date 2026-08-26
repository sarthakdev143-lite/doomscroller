#!/data/data/com.termux/files/usr/bin/sh
# One-time setup for Vivo Y69 (3GB, Android 7, not rooted)
# Run in Termux: bash scripts/termux_setup.sh

set -e
pkg update -y
pkg install -y python ffmpeg git termux-api termux-tools
pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium
# Optional deps
pip install google-api-python-client google-auth google-auth-oauthlib 2>/dev/null || true

echo "Setup done. Now:"
echo "1. Install Termux:Boot from F-Droid"
echo "2. mkdir -p ~/.termux/boot && cp scripts/termux_boot.sh ~/.termux/boot/doomscroller.sh && chmod +x ~/.termux/boot/doomscroller.sh"
echo "3. Disable battery optimization: Settings -> Battery -> Termux -> Don't optimize"
echo "4. Copy secrets/client_secret.json and run: python -m uvicorn app.main:app --port 8000  (first time will prompt OAuth)"
echo "5. Keep charger plugged, enable Termux:Wake lock"
