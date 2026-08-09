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
# 因此两条流分别捕获，stderr 只在 Python 非零时作为错误详情呈现。
# 不用 Start-Process：Windows PowerShell 5.1 会把继承环境重建成大小写不敏感
# 字典；父进程若带大写 PATH、机器环境又有 Path，会在启动前因重复键崩溃。
# 直接使用 ProcessStartInfo 会原样继承环境块，也保留两条独立管道。
$startInfo = New-Object System.Diagnostics.ProcessStartInfo
$startInfo.FileName = "python"
# Windows 路径不能含双引号，且这里两个值都是文件路径；显式引用即可同时保护空格。
$startInfo.Arguments = "`"$readConfig`" --config `"$Config`""
$startInfo.UseShellExecute = $false
$startInfo.CreateNoWindow = $true
$startInfo.RedirectStandardOutput = $true
$startInfo.RedirectStandardError = $true
$proc = New-Object System.Diagnostics.Process
$proc.StartInfo = $startInfo
try {
  if (-not $proc.Start()) { throw "python 进程未能启动" }
  $configJson = $proc.StandardOutput.ReadToEnd()
  $configErr = $proc.StandardError.ReadToEnd()
  $proc.WaitForExit()
  $readExit = $proc.ExitCode
} catch {
  Fail-Fast "读取 [backup] 配置失败：无法启动 Python 配置读取器。$($_.Exception.Message)"
} finally {
  $proc.Dispose()
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

# 批量入口自己也要串行化：单仓 mutex 只保护 bundle/pending，保护不了共享的
# last-run.json 与 last-run-failure.txt。两次批量运行重叠时，最后落笔者会覆盖
# 状态，成功者还可能删掉失败者刚写的报告。重入不是失败——已有一轮正在完成
# 同一目标，所以当前调用幂等 skip，不触碰任何状态或报告。
$batchKey = $dest.TrimEnd([char[]]"\/").ToLowerInvariant()
$hashAlgorithm = [System.Security.Cryptography.SHA256]::Create()
try {
  $digest = $hashAlgorithm.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($batchKey))
} finally {
  $hashAlgorithm.Dispose()
}
$hashHex = [System.BitConverter]::ToString($digest).Replace("-", "").Substring(0, 16).ToLowerInvariant()
$batchMutexName = "Global\wechat-diary-batch-$hashHex"
$batchMutex = $null
$batchMutexAcquired = $false

try {
  $batchMutex = New-Object -TypeName System.Threading.Mutex -ArgumentList @($false, $batchMutexName)
} catch {
  Write-Warning "[WARN] Global batch mutex unavailable; concurrency protection is degraded to Local scope: $($_.Exception.Message)"
  $batchMutexName = "Local\wechat-diary-batch-$hashHex"
  $batchMutex = New-Object -TypeName System.Threading.Mutex -ArgumentList @($false, $batchMutexName)
}

try {
  $batchMutexAcquired = $batchMutex.WaitOne(0)
} catch [System.Threading.AbandonedMutexException] {
  $batchMutexAcquired = $true
}
if (-not $batchMutexAcquired) {
  Write-Host "[skip] 已有批量 bundle 冷备正在写同一目标，当前调用不重复执行：$dest"
  $batchMutex.Dispose()
  exit 0
}

try {

# 选本轮写哪个槽位：先补空缺，再覆盖最旧的一个。
# 刻意**不**从 last-run.json 读上次的槽位——那样状态文件一丢就不知道该写哪，
# 而按磁盘上的实际情况推断可以自愈。mtime 万一不可信，最坏也只是覆盖了一个
# 不是最旧的槽位，其余 keep-1 份仍在。
function Select-Slot([string]$Destination, [string]$Name, [int]$Keep) {
  for ($i = 1; $i -le $Keep; $i++) {
    $candidate = Join-Path $Destination "$Name-slot-$i.bundle"
    if (-not (Test-Path -LiteralPath $candidate)) { return $i }
  }
  $oldest = Get-ChildItem -LiteralPath $Destination -Filter "$Name-slot-*.bundle" |
    Where-Object { $_.BaseName -match "-slot-(\d+)$" -and [int]$Matches[1] -le $Keep } |
    Sort-Object LastWriteTime | Select-Object -First 1
  if ($oldest -and $oldest.BaseName -match "-slot-(\d+)$") { return [int]$Matches[1] }
  return 1
}

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

  $slot = Select-Slot $dest $repo.name $backup.keep
  $entry['slot'] = $slot

  try {
    $ErrorActionPreference = "Continue"
    $output = & powershell -NoProfile -ExecutionPolicy Bypass -File $backupScript `
      -RepoPath $repo.path -Destination $dest -Name $repo.name -Keep $backup.keep -Slot $slot 2>&1
    $code = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = "Stop"
  }

  if ($code -ne 0) {
    $entry['result'] = "failed"
    $entry['error'] = ($output | Out-String).Trim()
  } else {
    # 按确切的槽位文件名取，不按「最新」猜——同名前缀下还可能有历史日期命名的
    # bundle 或一次性里程碑快照，按 mtime 排序会取错。
    $written = Get-Item -LiteralPath (Join-Path $dest "$($repo.name)-slot-$slot.bundle") -ErrorAction SilentlyContinue
    if ($written) {
      $entry['bundle'] = $written.Name
      $entry['bytes'] = $written.Length
    }
  }
  $results += $entry
}

# --- 落状态 ------------------------------------------------------------------
$failed = @($results | Where-Object { $_['result'] -ne "ok" })
$overall = "ok"
if ($failed.Count -gt 0) { $overall = "failed" }
$stateFile = $backup.stateFile
$finishedAt = (Get-Date).ToString("o")

# 槽位索引：跨轮累积「每个仓的每个槽位分别是什么时候写的」。
# 必要性——槽位命名把日期从文件名里拿掉了，灾难恢复时人多半在网盘 web UI 里翻，
# mtime 未必可靠。没有这份索引就答不出「该从哪个槽位还原」。
# 只更新本轮真正写成功的槽位，其余原样保留。
$slotIndex = [ordered]@{}
if (Test-Path -LiteralPath $stateFile) {
  try {
    $prior = Get-Content -LiteralPath $stateFile -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($prior.slotIndex) {
      foreach ($prop in $prior.slotIndex.PSObject.Properties) {
        $inner = [ordered]@{}
        foreach ($slotProp in $prop.Value.PSObject.Properties) { $inner[$slotProp.Name] = $slotProp.Value }
        $slotIndex[$prop.Name] = $inner
      }
    }
  } catch {
    # 旧索引读不出就从空开始重建：宁可丢历史映射，也不能因此中断本轮备份。
    $slotIndex = [ordered]@{}
  }
}
foreach ($entry in $results) {
  if ($entry['result'] -ne "ok") { continue }
  if (-not $slotIndex.Contains($entry['name'])) { $slotIndex[$entry['name']] = [ordered]@{} }
  $slotIndex[$entry['name']]["$($entry['slot'])"] = $finishedAt
}

$state = [ordered]@{
  startedAt  = $startedAt
  finishedAt = $finishedAt
  overall    = $overall
  repos      = $results
  slotIndex  = $slotIndex
}
$state | ConvertTo-Json -Depth 6 | Set-Content -Path $stateFile -Encoding UTF8

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
}
finally {
  if ($batchMutexAcquired -and $batchMutex) {
    try { $batchMutex.ReleaseMutex() } catch { }
  }
  if ($batchMutex) { $batchMutex.Dispose() }
}
