param(
  [string]$Workspace = "",
  [switch]$NoPause,
  [switch]$NoOpen
)

$ErrorActionPreference = "Stop"
$CodeRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Import-Module (Join-Path $PSScriptRoot "WorkspaceDiscovery.psm1") -Force
try {
  $ResolvedWorkspace = Resolve-WeChatDiaryWorkspace -Workspace $Workspace
} catch {
  [Console]::Error.WriteLine($_.Exception.Message)
  exit 2
}
$WorkspaceRoot = $ResolvedWorkspace.WorkspaceRoot
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

$InsightsRoot = Resolve-InsightsRoot $WorkspaceRoot

function Get-DateFiles {
  param([string]$RootPath)
  if (-not (Test-Path -LiteralPath $RootPath)) { return @() }
  # 日常产物可能带标题或关键词后缀，因此只匹配日期前缀。
  return Get-ChildItem -LiteralPath $RootPath -Recurse -Filter "*.md" |
    Where-Object { $_.BaseName -match '^\d{4}-\d{2}-\d{2}' }
}

$DateFiles = @(Get-DateFiles $InsightsRoot)
if ($DateFiles.Count -eq 0) {
  if ($NoOpen) {
    Write-Host "二次加工产物根目录：$InsightsRoot"
    Write-Host "命中文件：无"
  } else {
    Write-Host "未找到带日期的二次加工产物。"
  }
  if (-not $NoPause) { Read-Host "按 Enter 键退出" | Out-Null }
  exit 0
}

$LatestDate = ($DateFiles | ForEach-Object { $_.BaseName.Substring(0, 10) } |
  Sort-Object -Descending | Select-Object -First 1)
& (Join-Path $PSScriptRoot "Open-InsightsByDate.ps1") -Date $LatestDate -Workspace $WorkspaceRoot -InsightsRoot $InsightsRoot -NoPause:$NoPause -NoOpen:$NoOpen
