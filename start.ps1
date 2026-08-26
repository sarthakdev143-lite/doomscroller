# Doomscroller One-Click (PowerShell)
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot
Write-Host "========================================================" -ForegroundColor Magenta
Write-Host "  Doomscroller - One Click Start" -ForegroundColor Cyan
Write-Host "========================================================"

# Kill stale
Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue | ForEach-Object { try { Stop-Process -Id $_.OwningProcess -Force } catch {} }
Get-NetTCPConnection -LocalPort 8501 -ErrorAction SilentlyContinue | ForEach-Object { try { Stop-Process -Id $_.OwningProcess -Force } catch {} }
Start-Sleep 2

if (-not (Get-Command python -ErrorAction SilentlyContinue)) { Write-Host "python not found" -ForegroundColor Red; pause; exit 1 }

if (-not (Test-Path "venv/Scripts/python.exe")) {
  Write-Host "[1/4] Creating venv..." -ForegroundColor Yellow
  python -m venv venv
}
Write-Host "[1/4] venv ready" -ForegroundColor Green

if (-not (Test-Path "venv/.installed")) {
  Write-Host "[2/4] Installing deps (first run)..." -ForegroundColor Yellow
  & "venv/Scripts/python.exe" -m pip install --upgrade pip -q
  & "venv/Scripts/python.exe" -m pip install -r requirements.txt -q
  Set-Content venv/.installed (Get-Date)
} else { Write-Host "[2/4] Deps ready" -ForegroundColor Green }

if (-not (Test-Path "venv/.playwright")) {
  Write-Host "[3/4] Installing Playwright..." -ForegroundColor Yellow
  & "venv/Scripts/python.exe" -m playwright install chromium
  if ($LASTEXITCODE -eq 0) { Set-Content venv/.playwright "ok" }
} else { Write-Host "[3/4] Playwright ready" -ForegroundColor Green }

if (-not (Test-Path "doomscroller.db")) {
  Write-Host "[4/4] Init DB..." -ForegroundColor Yellow
  & "venv/Scripts/python.exe" scripts/init_db.py
} else { Write-Host "[4/4] DB exists" -ForegroundColor Green }

if (-not (Test-Path "secrets/client_secret.json")) {
  Write-Host "`n[NOTE] No secrets/client_secret.json - Publish needs it`n" -ForegroundColor Yellow
}

Write-Host "`n[5/5] Starting API 8000 + Dashboard 8501..." -ForegroundColor Green
Start-Job { Start-Sleep 5; Start-Process "http://localhost:8501" } | Out-Null
& "venv/Scripts/python.exe" scripts/run.py
