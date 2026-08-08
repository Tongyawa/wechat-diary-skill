<#
.SYNOPSIS
  Run the rolling git-bundle cold backup for every repository in [backup].

.DESCRIPTION
  Reads the [backup] section of config.toml, snapshots each configured
  repository with Backup-GitRepo.ps1, then records the outcome to
  <bundle_dest>/last-run.json so freshness can be checked later.

  Designed to be driven by a scheduler. Window behaviour is deliberately
  asymmetric:

    success -> nothing pops up (one summary line on stdout for CLI callers)
    failure -> a report file opens and STAYS open until dismissed

  Under a scheduler no window appears at all; that is guaranteed by registering
  the task with -WindowStyle Hidden, not by suppressing the summary line.

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

# Write-Error 在 $ErrorActionPreference = "Stop" 下是**终止性**错误：脚本会当场
# 结束，后面的 `exit 2` 根本执行不到，实际退出码变成 1。文档承诺的是 2，两者
# 必须一致——否则调用方按文档判断退出码会判错。故统一走这个函数。
function Fail-Fast([string]$Message, [int]$Code = 2) {
  [Console]::Error.WriteLine($Message)
  exit $Code
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$skillRoot = Split-Path -Parent $scriptDir
$backupScript = Join-Path $scriptDir "Backup-GitRepo.ps1"
$readConfig = Join-Path $scriptDir "print_backup_config.py"

if (-not $Workspace) { $Workspace = (Get-Location).Path }
if (-not $Config) { $Config = Join-Path $Workspace "config.toml" }

if (-not (Test-Path $Config)) {
  Fail-Fast "找不到 config.toml：$Config。用 -Workspace 或 -Config 指定工作区。"
}

# --- 读配置（复用项目自己的 TOML 解析，禁止在 ps1 里手写）---------------------
# 🔴 stdout 必须保持纯 JSON：load_config 在遇到旧 [automation] 段时会向 **stderr**
# 打一次迁移提示（这是现役支持的 legacy 路径）。早先这里用 `2>&1`，把提示混进
# JSON，ConvertFrom-Json 必然失败且不写状态文件——即「配置合法却整轮静默失效」。
# 因此两条流分文件捕获，stderr 只在 Python 非零时作为错误详情呈现。
$stdoutFile = [System.IO.Path]::GetTempFileName()
$stderrFile = [System.IO.Path]::GetTempFileName()
try {
  # -ArgumentList 会把各元素用空格拼成一整条命令行，**不会自动加引号**：
  # 路径里只要有空格（"C:\My Files\config.toml"）就被拆成两个参数。故显式加引号。
  $proc = Start-Process -FilePath "python" `
    -ArgumentList @("`"$readConfig`"", "--config", "`"$Config`"") `
    -NoNewWindow -Wait -PassThru `
    -RedirectStandardOutput $stdoutFile -RedirectStandardError $stderrFile
  $readExit = $proc.ExitCode
  $configJson = (Get-Content -LiteralPath $stdoutFile -Raw -Encoding UTF8)
  $configErr = (Get-Content -LiteralPath $stderrFile -Raw -Encoding UTF8)
} finally {
  Remove-Item -LiteralPath $stdoutFile, $stderrFile -Force -ErrorAction SilentlyContinue
}

if ($readExit -ne 0) {
  Fail-Fast "读取 [backup] 配置失败（退出码 $readExit）：$configErr"
}

try {
  $backup = $configJson | ConvertFrom-Json
} catch {
  Fail-Fast "解析 [backup] 配置失败：$($_.Exception.Message)`n原始输出：$configJson"
}

# 配置有误时拒绝运行：备份一部分却报成功，正是本功能要根除的失败形态。
if ($backup.problems -and $backup.problems.Count -gt 0) {
  $detail = ($backup.problems | ForEach-Object { "  - $_" }) -join "`n"
  Fail-Fast "[backup] 配置有误，拒绝运行（否则受影响的仓会被静默漏备）：`n$detail"
}

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
