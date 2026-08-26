@echo off
setlocal
title Doomscroller - One Click Start
cd /d "%~dp0"
echo ========================================================
echo   Doomscroller - One Click Start - Laptop MVP
echo   Human-in-loop - 8 per day - Snapchat to 1080x1920
echo ========================================================
echo.

REM Kill stale ports
powershell -Command "Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue | ForEach-Object { try { Stop-Process -Id $_.OwningProcess -Force } catch {} }" >nul 2>&1
powershell -Command "Get-NetTCPConnection -LocalPort 8501 -ErrorAction SilentlyContinue | ForEach-Object { try { Stop-Process -Id $_.OwningProcess -Force } catch {} }" >nul 2>&1
timeout /t 2 /nobreak >nul

where python >nul 2>&1
if %errorlevel% neq 0 (
  echo [ERROR] python not found. Install Python 3.10+ from python.org
  pause
  exit /b 1
)
python --version

if not exist "venv\Scripts\python.exe" (
  echo [1/4] Creating venv...
  python -m venv --system-site-packages venv
  if errorlevel 1 (
    echo venv failed
    pause
    exit /b 1
  )
  echo         venv created.
) else (
  echo [1/4] venv found.
)

if not exist "venv\.installed" (
  echo [2/4] Installing deps - first run 1-2 min...
  venv\Scripts\python.exe -m pip install --upgrade pip -q
  venv\Scripts\python.exe -m pip install -r requirements.txt -q
  if errorlevel 1 echo [WARN] pip warnings, continuing...
  echo %date% %time% > venv\.installed
) else (
  echo [2/4] Deps already installed - delete venv\.installed to force reinstall.
)

if not exist "venv\.playwright" (
  echo [3/4] Installing Playwright chromium - first run...
  venv\Scripts\python.exe -m playwright install chromium
  if %errorlevel% equ 0 echo installed > venv\.playwright
) else (
  echo [3/4] Playwright ready.
)

if not exist "doomscroller.db" (
  echo [4/4] Initializing DB...
  venv\Scripts\python.exe scripts\init_db.py
) else (
  echo [4/4] DB exists.
)

if not exist "secrets\client_secret.json" (
  echo.
  echo [NOTE] No secrets\client_secret.json - Publish will fail until you add it.
  echo       Download from Google Cloud Console and save as secrets\client_secret.json
  echo       Then run: venv\Scripts\python.exe scripts\youtube_oauth.py
  echo.
)

echo.
echo [5/5] Starting API port 8000 + Dashboard port 8501 ...
echo       Browser will open in 5s. Keep this window open. Ctrl+C to stop.
echo       Logs: logs\api.log , logs\dashboard.log
echo.

start /b powershell -Command "Start-Sleep 5; Start-Process 'http://localhost:8501'" >nul 2>&1

venv\Scripts\python.exe scripts\run.py
set ERR=%errorlevel%
echo.
echo Doomscroller stopped - exit %ERR%.
pause
