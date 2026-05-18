param(
  [switch]$NoPause
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

chcp 65001 > $null
$Utf8NoBom = New-Object System.Text.UTF8Encoding $false
[Console]::OutputEncoding = $Utf8NoBom
$OutputEncoding = $Utf8NoBom
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$LogDir = Join-Path $Root "WeFlow-insights\.runlog"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogPath = Join-Path $LogDir "$(Get-Date -Format yyyy-MM-dd)-daily-export.log"

Write-Host "Running daily export from $Root"
Write-Host "Log: $LogPath"

$ScriptPath = Join-Path $Root "scripts\run_daily_export.py"
$CommandLine = '"python" "{0}" 2>&1' -f $ScriptPath

cmd /d /c $CommandLine | ForEach-Object {
  Write-Host $_
  Add-Content -LiteralPath $LogPath -Encoding UTF8 -Value $_
}
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
