param(
  [string]$RawRoot = "",
  [string]$Day = "",
  [string]$Config = "config.toml",
  [switch]$RequireDay,
  [switch]$SkipVoiceFallback,
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
$LogPath = Join-Path $LogDir "$(Get-Date -Format yyyy-MM-dd)-process-existing-raw.log"

Write-Host "Processing existing raw exports from $Root"
Write-Host "Log: $LogPath"

$ScriptPath = Join-Path $Root "scripts\process_existing_raw.py"
$ArgsList = @("`"$ScriptPath`"", "--config", "`"$Config`"")
if ($RawRoot) {
  $ArgsList += @("--raw-root", "`"$RawRoot`"")
}
if ($Day) {
  $ArgsList += @("--day", "`"$Day`"")
}
if ($RequireDay) {
  $ArgsList += "--require-day"
}
if ($SkipVoiceFallback) {
  $ArgsList += "--skip-voice-fallback"
}

$CommandLine = '"python" {0} 2>&1' -f ($ArgsList -join " ")

cmd /d /c $CommandLine | ForEach-Object {
  Write-Host $_
  Add-Content -LiteralPath $LogPath -Encoding UTF8 -Value $_
}
$ExitCode = $LASTEXITCODE

if ($ExitCode -eq 0) {
  Write-Host "Existing raw processing finished successfully."
} else {
  Write-Host "Existing raw processing failed. Check the log above: $LogPath"
}

if (-not $NoPause) {
  Read-Host "Press Enter to exit"
}

exit $ExitCode
