<#
.SYNOPSIS
  Rolling offline git-bundle backup of any local git repository.

.DESCRIPTION
  Packs an entire repository (all refs + full history) into one self-contained
  `.bundle` file via `git bundle create --all`, verifies it, then prunes old
  bundles keeping the newest -Keep copies. A bundle restores anywhere with
  `git clone <file>.bundle <dir>` and needs no server.

  Topic-neutral by design: the repo path, destination and bundle name all come
  from parameters or local config -- nothing is hardcoded. Point -Destination at
  a different physical disk (cloud-synced folder or external drive) for a real
  off-machine backup; a folder on the same drive as the repo only guards against
  accidental deletion, not disk failure.

.PARAMETER RepoPath
  Path to the git repository to back up. Defaults to the current directory.

.PARAMETER Destination
  Directory where `.bundle` files are written (created if missing). Required.

.PARAMETER Name
  Base name for bundle files. Defaults to the repo directory's leaf name.

.PARAMETER Keep
  Number of most-recent bundles to retain; older ones are deleted. Default 5.

.EXAMPLE
  ./Backup-GitRepo.ps1 -RepoPath C:\repos\my-repo -Destination D:\Backups\my-repo
#>
[CmdletBinding()]
param(
  [string]$RepoPath = (Get-Location).Path,
  [Parameter(Mandatory = $true)][string]$Destination,
  [string]$Name,
  [int]$Keep = 5
)

$ErrorActionPreference = "Stop"

# --- Validate the source repo -------------------------------------------------
if (-not (Test-Path -LiteralPath $RepoPath)) { throw "RepoPath does not exist: $RepoPath" }
$RepoPath = (Resolve-Path -LiteralPath $RepoPath).Path
git -C $RepoPath rev-parse --git-dir *> $null
if ($LASTEXITCODE -ne 0) { throw "Not a git repository: $RepoPath" }

if (-not $Name) { $Name = Split-Path -Leaf $RepoPath }
if ($Keep -lt 1) { throw "Keep must be >= 1 (got $Keep)" }

# --- Ensure the destination ---------------------------------------------------
if (-not (Test-Path -LiteralPath $Destination)) {
  New-Item -ItemType Directory -Path $Destination -Force | Out-Null
}
$Destination = (Resolve-Path -LiteralPath $Destination).Path

# --- Create the bundle (one snapshot per day; same-day re-run overwrites) ------
$stamp = Get-Date -Format "yyyyMMdd"
$bundlePath = Join-Path $Destination "$Name-$stamp.bundle"
if (Test-Path -LiteralPath $bundlePath) { Remove-Item -LiteralPath $bundlePath -Force }

# Native commands write progress and even success notices to stderr -- `git
# bundle verify` reports "<file> is okay" there. The file-level "Stop"
# preference escalates any stderr write into a terminating error, so a fully
# successful backup would still exit non-zero (which a scheduled task reads as
# failure). Drop to "Continue" around the native calls and rely on
# $LASTEXITCODE for real failure detection; restore the preference afterwards.
$previousErrorAction = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
  git -C $RepoPath bundle create $bundlePath --all
  if ($LASTEXITCODE -ne 0) { throw "git bundle create failed for $RepoPath" }

  # --- Verify integrity -------------------------------------------------------
  git -C $RepoPath bundle verify $bundlePath *> $null
  if ($LASTEXITCODE -ne 0) { throw "git bundle verify failed: $bundlePath" }
}
finally {
  $ErrorActionPreference = $previousErrorAction
}
$sizeKB = [Math]::Round((Get-Item -LiteralPath $bundlePath).Length / 1KB, 1)
Write-Host "[OK] bundle created + verified: $bundlePath ($sizeKB KB)"

# --- Prune old bundles, keep the newest -Keep ---------------------------------
# Match only routine daily snapshots -- "<Name>-<yyyyMMdd>.bundle" exactly. A
# looser "$Name-*" would also sweep up one-off milestone snapshots that happen
# to share the prefix (e.g. "<Name>-pre-migration-20260704-2129.bundle") and
# silently delete them once they fall outside the retention window.
$all = @(Get-ChildItem -LiteralPath $Destination -Filter "$Name-????????.bundle" |
  Where-Object { $_.BaseName -match "^$([regex]::Escape($Name))-\d{8}$" } |
  Sort-Object LastWriteTime -Descending)
$old = @($all | Select-Object -Skip $Keep)
foreach ($f in $old) {
  Remove-Item -LiteralPath $f.FullName -Force
  Write-Host "[prune] removed: $($f.Name)"
}
$retained = [Math]::Min($Keep, $all.Count)
Write-Host "[done] retained $retained bundle(s) in $Destination"
