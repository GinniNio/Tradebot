# SQLite Backup Before Cutover

PR A does not migrate local SQLite data. Before any later cutover or legacy-cohort export, stop the legacy local runner and copy the SQLite files to a dated backup directory.

PowerShell example:

```powershell
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backup = Join-Path (Get-Location) "backups\sqlite-$stamp"
New-Item -ItemType Directory -Force -Path $backup
Copy-Item -LiteralPath ".\tradebot.db" -Destination $backup -ErrorAction Stop
Copy-Item -LiteralPath ".\tradebot.db-wal" -Destination $backup -ErrorAction SilentlyContinue
Copy-Item -LiteralPath ".\tradebot.db-shm" -Destination $backup -ErrorAction SilentlyContinue
```

Keep that backup read-only. SQLite export and Neon legacy-cohort import belong to a later reviewed PR.

