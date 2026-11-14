# Doomscroller One-Click (PowerShell)
$ErrorActionPreference = "Continue"
Set-Location -LiteralPath "$PSScriptRoot"
Write-Host "========================================================" -ForegroundColor Magenta
Write-Host "  Doomscroller - Slim One Click Start" -ForegroundColor Cyan
Write-Host "========================================================"

$VenvDir = Join-Path $PSScriptRoot "venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$InstalledMarker = Join-Path $VenvDir ".installed"
$RequirementsFile = Join-Path $PSScriptRoot "requirements.txt"
$DbFile = Join-Path $PSScriptRoot "doomscroller.db"
$InitDbScript = Join-Path $PSScriptRoot "scripts\init_db.py"
$RunScript = Join-Path $PSScriptRoot "scripts\run.py"

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
  Write-Host "python not found" -ForegroundColor Red
  pause
  exit 1
}

if (-not (Test-Path -LiteralPath "$VenvPython")) {
  Write-Host "[1/3] Creating venv..." -ForegroundColor Yellow
  & python -m venv "$VenvDir"
  if ($LASTEXITCODE -ne 0) {
    Write-Host "venv creation failed" -ForegroundColor Red
    pause
    exit 1
  }
}
Write-Host "[1/3] venv ready" -ForegroundColor Green

if (-not (Test-Path -LiteralPath "$InstalledMarker")) {
  Write-Host "[2/3] Installing deps (first run)..." -ForegroundColor Yellow
  & "$VenvPython" -m pip install --upgrade pip -q
  & "$VenvPython" -m pip install -r "$RequirementsFile" -q
  if ($LASTEXITCODE -ne 0) {
    Write-Host "dependency install failed" -ForegroundColor Red
    pause
    exit 1
  }
  Set-Content -LiteralPath "$InstalledMarker" -Value (Get-Date).ToString("o")
} else {
  Write-Host "[2/3] Deps ready" -ForegroundColor Green
}

if (-not (Test-Path -LiteralPath "$DbFile")) {
  Write-Host "[3/3] Init DB..." -ForegroundColor Yellow
  & "$VenvPython" "$InitDbScript"
} else {
  Write-Host "[3/3] DB exists" -ForegroundColor Green
}

Write-Host "`nStarting slim API (8000) + Web Control Panel (8600)..." -ForegroundColor Green
$RawUrl = & "$VenvPython" "$RunScript" --panel-url | Select-Object -Last 1
$PanelUrl = if ($RawUrl) { "$RawUrl".Trim() } else { "" }
if (-not $PanelUrl) { $PanelUrl = "http://localhost:8600" }
Start-Job -ArgumentList $PanelUrl -ScriptBlock { param($u) Start-Sleep 3; Start-Process -FilePath $u } | Out-Null
& "$VenvPython" "$RunScript" --slim
