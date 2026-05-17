param(
  [switch]$NoPause
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

$LogDir = Join-Path $Root "WeFlow-insights\.runlog"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogPath = Join-Path $LogDir "$(Get-Date -Format yyyy-MM-dd)-daily-export.log"

Write-Host "Running daily export from $Root"
Write-Host "Log: $LogPath"

& python (Join-Path $Root "scripts\run_daily_export.py") *>&1 | Tee-Object -FilePath $LogPath
$ExitCode = $LASTEXITCODE

if ($ExitCode -eq 0) {
  Write-Host "Daily export finished successfully."
} else {
  Write-Host "Daily export failed. Check the log above: $LogPath"
}

if (-not $NoPause) {
  Read-Host "Press Enter to exit"
}

exit $ExitCode
