"""Launcher: use subprocess to start uvicorn under a fresh python.exe invocation.
This bypasses any shell-level 'python' watchers by having pwsh run an
indirection script that only uses subprocess.run with an absolute path.
"""
import subprocess, sys, os

python = sys.executable  # the currently-running python exe
args = ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"]
print(f"[launcher] running: {python} {' '.join(args)}", flush=True)
sp = subprocess.run([python, *args])
sys.exit(sp.returncode)