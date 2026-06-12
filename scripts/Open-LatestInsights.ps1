param(
  [switch]$NoPause
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$InsightsRoot = Join-Path $Root "WeFlow-insights"

function Get-DateFiles {
  param([string]$RootPath)
  if (-not (Test-Path -LiteralPath $RootPath)) { return @() }
  # Daily products may carry a title/keyword suffix (`2026-06-09 标题 #kw.md`),
  # so match on the date prefix only.
  return Get-ChildItem -LiteralPath $RootPath -Recurse -Filter "*.md" |
    Where-Object { $_.BaseName -match '^\d{4}-\d{2}-\d{2}' }
}

$DateFiles = @(Get-DateFiles $InsightsRoot)
if ($DateFiles.Count -eq 0) {
  Write-Host "No dated insights found."
  if (-not $NoPause) { Read-Host "Press Enter to exit" | Out-Null }
  exit 0
}

$LatestDate = ($DateFiles | ForEach-Object { $_.BaseName.Substring(0, 10) } |
  Sort-Object -Descending | Select-Object -First 1)
& (Join-Path $PSScriptRoot "Open-InsightsByDate.ps1") -Date $LatestDate -NoPause:$NoPause
