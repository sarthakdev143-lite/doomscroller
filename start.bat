@echo off
setlocal
title Doomscroller - Slim Start
cd /d "%~dp0"
echo ========================================================
echo   Doomscroller - Slim One Click Start
echo ========================================================
echo.

where python >nul 2>&1
if %errorlevel% neq 0 (
  echo [ERROR] python not found. Install Python 3.10+ from python.org
  pause
  exit /b 1
)

if not exist "%~dp0venv\Scripts\python.exe" (
  echo [1/3] Creating venv...
  python -m venv "%~dp0venv"
  if errorlevel 1 (
    echo venv failed
    pause
    exit /b 1
  )
) else (
  echo [1/3] venv found.
)

if not exist "%~dp0venv\.installed" (
  echo [2/3] Installing deps...
  "%~dp0venv\Scripts\python.exe" -m pip install --upgrade pip -q
  "%~dp0venv\Scripts\python.exe" -m pip install -r "%~dp0requirements.txt" -q
  if errorlevel 1 (
    echo dependency install failed
    pause
    exit /b 1
  )
  >"%~dp0venv\.installed" echo %date% %time%
) else (
  echo [2/3] Deps ready.
)

if not exist "%~dp0doomscroller.db" (
  echo [3/3] Initializing DB...
  "%~dp0venv\Scripts\python.exe" "%~dp0scripts\init_db.py"
) else (
  echo [3/3] DB exists.
)

echo.
echo Starting slim API port 8000 + Control Panel port 8600...
for /f "usebackq delims=" %%U in (`""%~dp0venv\Scripts\python.exe" "%~dp0scripts\run.py" --panel-url"`) do set "PANEL_URL=%%U"
if not defined PANEL_URL set "PANEL_URL=http://localhost:8600"
start /b powershell -NoProfile -Command "Start-Sleep 3; Start-Process -FilePath $env:PANEL_URL" >nul 2>&1

"%~dp0venv\Scripts\python.exe" "%~dp0scripts\run.py" --slim
pause
