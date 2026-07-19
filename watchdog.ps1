# watchdog.ps1
# Liveness check for the Tradebot. Registered as a 5-minute recurring
# scheduled task. Restarts pythonw.exe run_all.py if either:
#   1. No pythonw.exe process is running.
#   2. tradebot.log hasn't been written to in the last 10 minutes.
#
# Logs every check to watchdog.log so we can audit when it restarted things.
# Designed to be quiet on the happy path (writes one line per check), loud
# when it has to intervene.

$ErrorActionPreference = "Stop"

$projectDir = "C:\Users\hp\OneDrive\Documents\Claude\Projects\Tradebot"
$logPath    = Join-Path $projectDir "tradebot.log"
$watchdogLog = Join-Path $projectDir "watchdog.log"
$runAllPath = Join-Path $projectDir "run_all.py"

# Stale threshold: if last log write is older than this, assume the bot
# is hung / dead even if the process is technically alive.
$staleMinutes = 10

function Write-WatchdogLog($message) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $watchdogLog -Value "$ts | $message" -Encoding UTF8
}

function Start-Tradebot() {
    Push-Location $projectDir
    try {
        Start-Process pythonw.exe -ArgumentList "run_all.py" -WindowStyle Hidden
        Write-WatchdogLog "STARTED pythonw.exe run_all.py"
    } catch {
        Write-WatchdogLog "START FAILED: $($_.Exception.Message)"
    } finally {
        Pop-Location
    }
}

try {
    $pythonwProc = Get-Process pythonw -ErrorAction SilentlyContinue

    $logStale = $true
    if (Test-Path $logPath) {
        $lastWrite = (Get-Item $logPath).LastWriteTime
        $ageMin = [math]::Round(((Get-Date) - $lastWrite).TotalMinutes, 1)
        $logStale = $ageMin -gt $staleMinutes
    } else {
        $ageMin = "n/a"
    }

    if (-not $pythonwProc) {
        Write-WatchdogLog "DOWN: no pythonw.exe process. log age=$ageMin min. restarting."
        Start-Tradebot
    } elseif ($logStale) {
        Write-WatchdogLog "STALE: pythonw running (pid=$($pythonwProc.Id -join ',')) but log age=$ageMin min > $staleMinutes. killing and restarting."
        $pythonwProc | Stop-Process -Force
        Start-Sleep 2
        Start-Tradebot
    } else {
        # Happy path. Keep this line short so watchdog.log doesn't bloat.
        Write-WatchdogLog "OK pid=$($pythonwProc.Id -join ',') log_age=$ageMin"
    }
} catch {
    Write-WatchdogLog "WATCHDOG ERROR: $($_.Exception.Message)"
    throw
}
