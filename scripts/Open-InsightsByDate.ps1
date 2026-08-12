param(
  [string]$Date,
  [string]$Workspace = "",
  [string]$InsightsRoot = "",
  [switch]$NoPause,
  [switch]$NoOpen
)

$ErrorActionPreference = "Stop"
$CodeRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Import-Module (Join-Path $PSScriptRoot "WorkspaceDiscovery.psm1") -Force
if ([string]::IsNullOrWhiteSpace($InsightsRoot)) {
  try {
    $ResolvedWorkspace = Resolve-WeChatDiaryWorkspace -Workspace $Workspace
  } catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 2
  }
  $WorkspaceRoot = $ResolvedWorkspace.WorkspaceRoot
}
function Resolve-InsightsRoot {
  param([string]$RootPath)

  $FallbackRoot = Join-Path $RootPath "WeFlow-insights"
  $ConfigPath = $ResolvedWorkspace.ConfigPath
  $ConfigReader = Join-Path $CodeRoot "scripts\print_config_path.py"
  # 原生命令往 stderr 写一个字，$ErrorActionPreference = "Stop" 就会把它升级成终止性
  # 错误。config 加载时的「建议迁移到 [export_backend.weflow]」提示正是走 stderr，真实
  # 配置因此会被误判成「读不出」而静默回退到空壳目录。这里必须局部降级再调用。
  $PreviousErrorAction = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    $Output = @(& python -u $ConfigReader --config $ConfigPath --key "paths.insights" 2>$null)
    $ExitCode = $LASTEXITCODE
  } catch {
    $Output = @()
    $ExitCode = 1
  } finally {
    $ErrorActionPreference = $PreviousErrorAction
  }

  if ($ExitCode -eq 0 -and $Output.Count -gt 0) {
    $ConfiguredRoot = ([string]($Output | Select-Object -Last 1)).Trim()
    if (-not [string]::IsNullOrWhiteSpace($ConfiguredRoot)) {
      return $ConfiguredRoot
    }
  }

  Write-Host "无法读取 $ConfigPath 的 [paths].insights，已使用回退路径：$FallbackRoot。请对照 config.example.toml 修正配置后重试。"
  return $FallbackRoot
}

if ([string]::IsNullOrWhiteSpace($InsightsRoot)) {
  $InsightsRoot = Resolve-InsightsRoot $WorkspaceRoot
}

function Open-InsightFiles {
  param([string[]]$Paths)
  foreach ($Path in $Paths | Select-Object -Unique) {
    Invoke-Item -LiteralPath $Path
    Write-Host "已打开：$Path"
  }
}

if (-not $Date) {
  $Date = Read-Host "日期（yyyy-mm-dd）"
}
$Date = $Date.Trim()
if ($Date -notmatch '^\d{4}-\d{2}-\d{2}$') {
  throw "日期必须是 yyyy-mm-dd，实际收到：$Date"
}

$Year = $Date.Substring(0, 4)
$Paths = [System.Collections.Generic.List[string]]::new()

# 日常产物可能带标题或关键词后缀，因此按日期前缀查找，而不是固定查找日期文件名。
foreach ($Kind in @("Diary", "DoneList", "Inspirations", "ExtraNotes")) {
  $KindDir = Join-Path $InsightsRoot "$Kind\$Year"
  if (Test-Path -LiteralPath $KindDir) {
    Get-ChildItem -LiteralPath $KindDir -File -Filter "$Date*.md" |
      ForEach-Object { $Paths.Add($_.FullName) }
  }
}

# 存在时一并打开侧车产物（任意以下划线开头的目录）；具体名称由工作区决定，不写死在脚本中。
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
  if ($NoOpen) {
    Write-Host "二次加工产物根目录：$InsightsRoot"
    Write-Host "命中文件：无"
  } else {
    Write-Host "未找到 $Date 的二次加工产物。"
  }
} else {
  if ($NoOpen) {
    Write-Host "二次加工产物根目录：$InsightsRoot"
    foreach ($Path in $Paths | Select-Object -Unique) {
      Write-Host "命中文件：$Path"
    }
  } else {
    Open-InsightFiles $Paths.ToArray()
  }
}

if (-not $NoPause) {
  Read-Host "按 Enter 键退出" | Out-Null
}
