@echo off
cd /d "%~dp0"
echo Stopping Doomscroller...
if exist "venv\Scripts\python.exe" (
  venv\Scripts\python.exe scripts\run.py --stop
) else (
  python scripts\run.py --stop
)
echo Done. You can close this window.
pause
