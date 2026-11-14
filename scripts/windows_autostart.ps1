<#
.SYNOPSIS
  Install, inspect, or remove Windows Auto-Start + 10-minute self-healing watchdog for Doomscroller.

.USAGE
  powershell -ExecutionPolicy Bypass -File scripts\windows_autostart.ps1 install
  powershell -ExecutionPolicy Bypass -File scripts\windows_autostart.ps1 status
  powershell -ExecutionPolicy Bypass -File scripts\windows_autostart.ps1 remove
#>
param(
    [ValidateSet("install", "status", "remove")]
    [string]$Action = "install"
)

$ErrorActionPreference = "Stop"
$TaskName = "Doomscroller"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PythonW = Join-Path $ProjectRoot "venv\Scripts\pythonw.exe"
$PythonExe = if (Test-Path $PythonW) { $PythonW } else { Join-Path $ProjectRoot "venv\Scripts\python.exe" }
$RunScript = Join-Path $ProjectRoot "scripts\run.py"

if ($Action -eq "remove") {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'." -ForegroundColor Green
    } else {
        Write-Host "Scheduled task '$TaskName' is not installed." -ForegroundColor Yellow
    }
    exit 0
}

if ($Action -eq "status") {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-Host "Auto-start task '$TaskName': NOT INSTALLED" -ForegroundColor Yellow
    } else {
        $info = Get-ScheduledTaskInfo -TaskName $TaskName
        Write-Host "Auto-start task '$TaskName': $($task.State)" -ForegroundColor Green
        Write-Host "  Last Run:   $($info.LastRunTime) (Result: $($info.LastTaskResult))"
        Write-Host "  Next Run:   $($info.NextRunTime)"
    }
    & (Join-Path $ProjectRoot "venv\Scripts\python.exe") $RunScript --status
    exit $LASTEXITCODE
}

if (-not (Test-Path $PythonExe)) {
    throw "Virtualenv Python not found at $PythonExe"
}

# Build Scheduled Task:
#  - Trigger 1 ($LogonTrigger): At user logon + 45s delay (avoids competing with Windows desktop boot).
#  - Trigger 2 ($WatchdogTrigger): Starts 10m from now and repeats every 10m indefinitely so the
#    watchdog is active immediately in the current session without waiting for the next reboot.
$TaskAction = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "`"$RunScript`" --ensure --slim" `
    -WorkingDirectory $ProjectRoot

$LogonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$LogonTrigger.Delay = "PT45S"

$WatchdogTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(10) `
    -RepetitionInterval (New-TimeSpan -Minutes 10) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -Priority 6

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "Doomscroller low-resource autonomous daemon + Cloudflare phone tunnel (45s boot delay + 10m self-heal watchdog)" `
    -Action $TaskAction `
    -Trigger @($LogonTrigger, $WatchdogTrigger) `
    -Settings $Settings `
    -Force | Out-Null

Write-Host "Installed Windows auto-start task '$TaskName' (45s logon delay + 10m watchdog, Priority=BelowNormal)." -ForegroundColor Green
