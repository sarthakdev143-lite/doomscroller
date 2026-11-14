@echo off
setlocal
cd /d "%~dp0"
echo Stopping Doomscroller...
if exist "%~dp0venv\Scripts\python.exe" (
  "%~dp0venv\Scripts\python.exe" "%~dp0scripts\run.py" --stop
) else (
  python "%~dp0scripts\run.py" --stop
)
set "EXIT_CODE=%ERRORLEVEL%"
echo Done. You can close this window.
if /i not "%~1"=="--no-pause" pause
exit /b %EXIT_CODE%
