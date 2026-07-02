param(
  [string]$Date,
  [string]$Workspace = "",
  [switch]$NoPause
)

$ErrorActionPreference = "Stop"
$CodeRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$WorkspaceRoot = if ([string]::IsNullOrWhiteSpace($Workspace)) {
  $CodeRoot
} else {
  (Resolve-Path -LiteralPath $Workspace).Path
}
$InsightsRoot = Join-Path $WorkspaceRoot "WeFlow-insights"

function Open-InsightFiles {
  param([string[]]$Paths)
  foreach ($Path in $Paths | Select-Object -Unique) {
    Invoke-Item -LiteralPath $Path
    Write-Host "Opened: $Path"
  }
}

if (-not $Date) {
  $Date = Read-Host "Date (yyyy-mm-dd)"
}
$Date = $Date.Trim()
if ($Date -notmatch '^\d{4}-\d{2}-\d{2}$') {
  throw "Date must be yyyy-mm-dd, got: $Date"
}

$Year = $Date.Substring(0, 4)
$Paths = [System.Collections.Generic.List[string]]::new()

# Daily products may carry a title/keyword suffix (`<date> 标题 #kw.md`), so
# glob by date prefix instead of expecting an exact `<date>.md` name.
foreach ($Kind in @("Diary", "DoneList", "Inspirations", "ExtraNotes")) {
  $KindDir = Join-Path $InsightsRoot "$Kind\$Year"
  if (Test-Path -LiteralPath $KindDir) {
    Get-ChildItem -LiteralPath $KindDir -File -Filter "$Date*.md" |
      ForEach-Object { $Paths.Add($_.FullName) }
  }
}

# Sidecar products (any `_`-prefixed dir) open alongside when present; their
# concrete names are workspace-local and stay out of this script on purpose.
if (Test-Path -LiteralPath $InsightsRoot) {
  $SidecarRoots = @(Get-ChildItem -LiteralPath $InsightsRoot -Directory | Where-Object { $_.Name.StartsWith("_") })
  foreach ($SidecarRoot in $SidecarRoots) {
    $SidecarDaily = Join-Path $SidecarRoot.FullName "Daily\$Year\$Date.md"
    if (Test-Path -LiteralPath $SidecarDaily) {
      $Paths.Add((Resolve-Path -LiteralPath $SidecarDaily).Path)
      Get-ChildItem -LiteralPath $SidecarRoot.FullName -File -Filter "*.md" -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -notmatch '\.bak\.' } |
        ForEach-Object { $Paths.Add($_.FullName) }
    }
  }
}

if ($Paths.Count -eq 0) {
  Write-Host "No insights found for $Date."
} else {
  Open-InsightFiles $Paths.ToArray()
}

if (-not $NoPause) {
  Read-Host "Press Enter to exit" | Out-Null
}
