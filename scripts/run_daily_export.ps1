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

$EmptySessionSkips = 0
# Console whitelist: stages, results, warnings, errors and the first-run
# wizard prompts. Everything (WeFlow/WCDB chatter included) still lands in
# the runlog file above.
function ShouldShowDailyExportLine {
  param([string]$Line)

  if ([string]::IsNullOrWhiteSpace($Line)) {
    return $true
  }

  if ($Line -match '^导出 .+ 失败:.*没有消息') {
    $script:EmptySessionSkips += 1
    return $false
  }

  if ($Line -match '^(DETAIL:|\[weflow\]|导出 .+ 失败:|.*InitExportCursorHeap|.*QueryMessageBatch|.*fetch_message_batch|.*WCDB日志)') {
    return $false
  }

  if ($Line.Length -gt 240) {
    return $false
  }

  if ($Line -match '^\[\d{2}:\d{2}:\d{2}\] .+') {
    return $true
  }

  if ($Line -match '^\[WARN\]') {
    return $true
  }

  if ($Line -match '^(Daily export day|Raw root|Processed root|Target sidecar contacts|Self moments contacts):') {
    return $true
  }

  if ($Line -match '^(voice_transcribe|export_target_moments|export_self_moments|voice_fallback) skipped:') {
    return $true
  }

  if ($Line -match '^(FAILED at stage|FAILED before export completed|Reason|Next step):') {
    return $true
  }

  if ($Line -match '^(Daily export completed\.|Day:|Archive root:|Diary processed files:|Self moments files:|Sidecar chat files:|Sidecar moments files:)') {
    return $true
  }

  # First-run wizard output must never be filtered away.
  if ($Line -match '^(Created local config:|WeFlow\.exe path:|self moments wxid:|未配置)') {
    return $true
  }

  return $false
}

$ScriptPath = Join-Path $Root "scripts\run_daily_export.py"
$CommandLine = '"python" "{0}" 2>&1' -f $ScriptPath

cmd /d /c $CommandLine | ForEach-Object {
  $Line = [string]$_
  Add-Content -LiteralPath $LogPath -Encoding UTF8 -Value $Line
  if (ShouldShowDailyExportLine $Line) {
    Write-Host $Line
  }
}
$ExitCode = $LASTEXITCODE

if ($EmptySessionSkips -gt 0) {
  Write-Host "[stage] $EmptySessionSkips 个空会话跳过（详见 runlog）"
}

if ($ExitCode -eq 0) {
  Write-Host "Daily export finished successfully."
} else {
  Write-Host "Daily export failed. Check the full log: $LogPath"
}

if (-not $NoPause) {
  Read-Host "Press Enter to exit"
}

exit $ExitCode
