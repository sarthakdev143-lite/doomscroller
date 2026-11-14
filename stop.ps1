# Doomscroller Stop (PowerShell)
param(
  [switch]$NoPause
)
$ErrorActionPreference = "Continue"
Set-Location -LiteralPath "$PSScriptRoot"
Write-Host "Stopping Doomscroller..." -ForegroundColor Yellow

$VenvPython = Join-Path $PSScriptRoot "venv\Scripts\python.exe"
$RunScript = Join-Path $PSScriptRoot "scripts\run.py"

if (Test-Path -LiteralPath "$VenvPython") {
  & "$VenvPython" "$RunScript" --stop
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
  & python "$RunScript" --stop
} else {
  Write-Host "[ERROR] python not found." -ForegroundColor Red
  if (-not $NoPause -and [Environment]::UserInteractive -and -not $env:CI) { pause }
  exit 1
}

$ExitCode = if ($null -ne $LASTEXITCODE) { $LASTEXITCODE } else { 0 }
Write-Host "Done." -ForegroundColor Green
exit $ExitCode
