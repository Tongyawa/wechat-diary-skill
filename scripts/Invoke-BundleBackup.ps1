<#
.SYNOPSIS
  Run the rolling git-bundle cold backup for every repository in [backup].

.DESCRIPTION
  Reads the [backup] section of config.toml, snapshots each configured
  repository with Backup-GitRepo.ps1, then records the outcome to
  <bundle_dest>/last-run.json so freshness can be checked later.

  Designed to be driven by a scheduler. Window behaviour is deliberately
  asymmetric:

    success -> completely silent (nothing pops up, nothing flashes)
    failure -> a report file opens and STAYS open until dismissed

  Rationale: a window that flashes identically on success and failure carries
  no information while implying activity, and trains the user to ignore it.
  Silence must mean "fine"; anything visible must mean "act".

  One repository failing does not stop the others, but the overall exit code is
  non-zero. A configured repo whose path is missing is a hard failure, never a
  silent skip -- that exact case previously went unnoticed for a month.

.PARAMETER Config
  Path to config.toml. Defaults to <Workspace>\config.toml.

.PARAMETER Workspace
  Workspace directory holding config.toml. Defaults to the current directory.

.PARAMETER NoPopup
  Never open the failure report window. For unattended/automated runs; the
  exit code and last-run.json still report the failure.

.EXAMPLE
  ./Invoke-BundleBackup.ps1 -Workspace E:\path\to\workspace
#>
[CmdletBinding()]
param(
  [string]$Config = "",
  [string]$Workspace = "",
  [switch]$NoPopup
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$skillRoot = Split-Path -Parent $scriptDir
$backupScript = Join-Path $scriptDir "Backup-GitRepo.ps1"
$readConfig = Join-Path $scriptDir "print_backup_config.py"

if (-not $Workspace) { $Workspace = (Get-Location).Path }
if (-not $Config) { $Config = Join-Path $Workspace "config.toml" }

if (-not (Test-Path $Config)) {
  Write-Error "找不到 config.toml：$Config。用 -Workspace 或 -Config 指定工作区。"
  exit 2
}

# --- 读配置（复用项目自己的 TOML 解析，禁止在 ps1 里手写）---------------------
# 原生命令写 stderr 时不应升级为终止性错误，故此处局部降级。
$configJson = $null
try {
  $ErrorActionPreference = "Continue"
  $configJson = & python $readConfig --config $Config 2>&1
  $readExit = $LASTEXITCODE
} finally {
  $ErrorActionPreference = "Stop"
}

if ($readExit -ne 0) {
  Write-Error "读取 [backup] 配置失败（退出码 $readExit）：$configJson"
  exit 2
}

$backup = $configJson | ConvertFrom-Json

if (-not $backup.enabled) {
  Write-Host "[skip] [backup] 未配置（缺 bundle_dest 或 repos 为空），无事可做。"
  exit 0
}

$dest = $backup.bundleDest
if (-not (Test-Path $dest)) { New-Item -ItemType Directory -Force $dest | Out-Null }

# --- 逐仓备份 ----------------------------------------------------------------
$startedAt = (Get-Date).ToString("o")
$results = @()

foreach ($repo in $backup.repos) {
  $entry = [ordered]@{ name = $repo.name; path = $repo.path; result = "ok" }

  if (-not (Test-Path $repo.path)) {
    # 硬失败：配置指向了不存在的路径。上次事故正是这种情况被静默跳过。
    $entry['result'] = "failed"
    $entry['error'] = "仓路径不存在：$($repo.path)"
    $results += $entry
    continue
  }
  if (-not (Test-Path (Join-Path $repo.path ".git"))) {
    $entry['result'] = "failed"
    $entry['error'] = "不是 git 仓库（缺 .git）：$($repo.path)"
    $results += $entry
    continue
  }

  try {
    $ErrorActionPreference = "Continue"
    $output = & powershell -NoProfile -ExecutionPolicy Bypass -File $backupScript `
      -RepoPath $repo.path -Destination $dest -Name $repo.name -Keep $backup.keep 2>&1
    $code = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = "Stop"
  }

  if ($code -ne 0) {
    $entry['result'] = "failed"
    $entry['error'] = ($output | Out-String).Trim()
  } else {
    $newest = Get-ChildItem $dest -Filter "$($repo.name)-*.bundle" -ErrorAction SilentlyContinue |
      Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($newest) {
      $entry['bundle'] = $newest.Name
      $entry['bytes'] = $newest.Length
    }
  }
  $results += $entry
}

# --- 落状态 ------------------------------------------------------------------
$failed = @($results | Where-Object { $_['result'] -ne "ok" })
$overall = "ok"
if ($failed.Count -gt 0) { $overall = "failed" }
$state = [ordered]@{
  startedAt  = $startedAt
  finishedAt = (Get-Date).ToString("o")
  overall    = $overall
  repos      = $results
}
$stateFile = $backup.stateFile
$state | ConvertTo-Json -Depth 5 | Set-Content -Path $stateFile -Encoding UTF8

$reportFile = Join-Path $dest "last-run-failure.txt"

if ($failed.Count -eq 0) {
  # 成功即静默。顺手清掉上一次的失败报告，避免旧报告长期误导。
  if (Test-Path $reportFile) { Remove-Item $reportFile -Force }
  Write-Host "[ok] bundle 冷备完成：$($results.Count) 个仓全部成功。"
  exit 0
}

# --- 失败：写报告 + 弹出停留窗口 ---------------------------------------------
$lines = @()
$lines += "bundle 冷备失败报告"
$lines += "时间：$($state.finishedAt)"
$lines += "配置：$Config"
$lines += ""
$lines += "失败 $($failed.Count) / 共 $($results.Count) 个仓："
foreach ($item in $failed) {
  $lines += ""
  $lines += "  [$($item['name'])] $($item['path'])"
  $lines += "  原因：$($item['error'])"
}
$ok = @($results | Where-Object { $_['result'] -eq "ok" })
if ($ok.Count -gt 0) {
  $lines += ""
  $lines += "成功的仓（这些已备份，不必重做）：$(($ok | ForEach-Object { $_['name'] }) -join '、')"
}
$lines += ""
$lines += "怎么办："
$lines += "  1. 先看上面的原因。最常见是仓路径变了（搬代码后没同步改 config）。"
$lines += "  2. 改 config.toml 的 [backup].repos 后手动补跑："
$lines += "     powershell -NoProfile -ExecutionPolicy Bypass -File `"$($MyInvocation.MyCommand.Path)`" -Workspace `"$Workspace`""
$lines += "  3. 状态文件：$stateFile"
$lines += ""
$lines += "（这个窗口不会自动关闭——上一版每天闪一下就消失，导致连续失败一个月没被发现。）"

$lines -join "`r`n" | Set-Content -Path $reportFile -Encoding UTF8

Write-Host "[FAILED] bundle 冷备有 $($failed.Count) 个仓失败，报告：$reportFile"
if (-not $NoPopup) {
  try {
    $ErrorActionPreference = "Continue"
    Start-Process notepad.exe -ArgumentList "`"$reportFile`"" | Out-Null
  } catch {
    Write-Host "（无法打开报告窗口，请手动查看 $reportFile）"
  } finally {
    $ErrorActionPreference = "Stop"
  }
}
exit 1
