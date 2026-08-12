$WorkspaceEnvironmentVariable = "WECHAT_DIARY_WORKSPACE"

function Get-AbsolutePath {
  param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][string]$BasePath
  )

  $Expanded = [Environment]::ExpandEnvironmentVariables($Path)
  if ([System.IO.Path]::IsPathRooted($Expanded)) {
    return [System.IO.Path]::GetFullPath($Expanded)
  }
  return [System.IO.Path]::GetFullPath((Join-Path $BasePath $Expanded))
}

function Resolve-WeChatDiaryWorkspace {
  param(
    [string]$Workspace = "",
    [string]$Config = "",
    [string]$ExplicitOption = "-Workspace",
    [switch]$AllowMissingExplicitConfig
  )

  $CurrentDirectory = [System.IO.Path]::GetFullPath((Get-Location).Path)
  $Probes = New-Object System.Collections.Generic.List[object]

  if (-not [string]::IsNullOrWhiteSpace($Config)) {
    $ExplicitConfig = Get-AbsolutePath -Path $Config -BasePath $CurrentDirectory
    $Probes.Add([pscustomobject]@{ Label = "显式 -Config"; Path = $ExplicitConfig })
    if ((Test-Path -LiteralPath $ExplicitConfig -PathType Leaf) -or $AllowMissingExplicitConfig) {
      return [pscustomobject]@{
        ConfigPath = $ExplicitConfig
        WorkspaceRoot = Split-Path -Parent $ExplicitConfig
      }
    }
    throw (Format-WorkspaceResolutionError -Probes $Probes)
  } elseif (-not [string]::IsNullOrWhiteSpace($Workspace)) {
    $ExplicitWorkspace = Get-AbsolutePath -Path $Workspace -BasePath $CurrentDirectory
    $ExplicitConfig = Join-Path $ExplicitWorkspace "config.toml"
    $Probes.Add([pscustomobject]@{ Label = "显式 $ExplicitOption"; Path = $ExplicitConfig })
    $ExplicitWorkspaceExists = Test-Path -LiteralPath $ExplicitWorkspace -PathType Container
    if ((Test-Path -LiteralPath $ExplicitConfig -PathType Leaf) -or ($AllowMissingExplicitConfig -and $ExplicitWorkspaceExists)) {
      return [pscustomobject]@{
        ConfigPath = $ExplicitConfig
        WorkspaceRoot = $ExplicitWorkspace
      }
    }
    throw (Format-WorkspaceResolutionError -Probes $Probes)
  }

  $CurrentConfig = Join-Path $CurrentDirectory "config.toml"
  $Probes.Add([pscustomobject]@{ Label = "当前目录"; Path = $CurrentConfig })
  if (Test-Path -LiteralPath $CurrentConfig -PathType Leaf) {
    return [pscustomobject]@{
      ConfigPath = $CurrentConfig
      WorkspaceRoot = $CurrentDirectory
    }
  }

  $EnvironmentWorkspace = [Environment]::GetEnvironmentVariable($WorkspaceEnvironmentVariable)
  if (-not [string]::IsNullOrWhiteSpace($EnvironmentWorkspace)) {
    $EnvironmentWorkspace = $EnvironmentWorkspace.Trim()
    $EnvironmentRoot = Get-AbsolutePath -Path $EnvironmentWorkspace -BasePath $CurrentDirectory
    $EnvironmentConfig = Join-Path $EnvironmentRoot "config.toml"
    $Probes.Add([pscustomobject]@{ Label = "环境变量 $WorkspaceEnvironmentVariable"; Path = $EnvironmentConfig })
    if (Test-Path -LiteralPath $EnvironmentConfig -PathType Leaf) {
      return [pscustomobject]@{
        ConfigPath = $EnvironmentConfig
        WorkspaceRoot = $EnvironmentRoot
      }
    }
  }

  throw (Format-WorkspaceResolutionError -Probes $Probes)
}

function Format-WorkspaceResolutionError {
  param([System.Collections.Generic.List[object]]$Probes)

  $Lines = New-Object System.Collections.Generic.List[string]
  $Lines.Add("找不到 config.toml。已按顺序探测以下绝对路径：")
  foreach ($Probe in $Probes) {
    $Lines.Add("  - $($Probe.Label)：$($Probe.Path)")
  }
  $Lines.Add("下一步任选一种：")
  $Lines.Add('  1. cd "<工作区目录>" 后重试；')
  $Lines.Add('  2. 传 -Workspace "<工作区目录>"；')
  $Lines.Add("  3. 设置环境变量 $WorkspaceEnvironmentVariable 为工作区目录后重试。")
  return ($Lines -join [Environment]::NewLine)
}

Export-ModuleMember -Function Resolve-WeChatDiaryWorkspace
