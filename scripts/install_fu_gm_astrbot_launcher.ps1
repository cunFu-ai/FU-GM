[CmdletBinding()]
param(
    [string]$ProjectDir = "",
    [string]$LauncherDataRoot = "",
    [string]$InstanceId = "",
    [string]$RuntimeHome = "",
    [string]$TaskName = "FU-GM HTTP Server",
    [string]$HostName = "127.0.0.1",
    [int]$Port = 8766,
    [switch]$NoSchedule,
    [switch]$Offline,
    [switch]$SkipHealthCheck
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Resolve-FullPath {
    param([string]$Path)
    return [System.IO.Path]::GetFullPath($Path)
}

function Assert-ChildPath {
    param(
        [string]$Child,
        [string]$Parent
    )
    $childFull = Resolve-FullPath $Child
    $parentFull = (Resolve-FullPath $Parent).TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
    if (-not $childFull.StartsWith($parentFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to modify path outside expected root: $childFull"
    }
}

function Resolve-AstrBotInstance {
    param(
        [string]$Root,
        [string]$RequestedInstanceId
    )

    $instancesRoot = Join-Path $Root "instances"
    if (-not (Test-Path -LiteralPath $instancesRoot)) {
        throw "AstrBot Launcher instances directory not found: $instancesRoot"
    }

    if ($RequestedInstanceId) {
        $instance = Join-Path $instancesRoot $RequestedInstanceId
        if (-not (Test-Path -LiteralPath (Join-Path $instance "core\data\plugins"))) {
            throw "AstrBot instance does not contain core\data\plugins: $instance"
        }
        return (Resolve-Path -LiteralPath $instance).Path
    }

    $candidates = Get-ChildItem -LiteralPath $instancesRoot -Directory |
        Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "core\data\plugins") } |
        Sort-Object LastWriteTime -Descending

    if (-not $candidates) {
        throw "No AstrBot Launcher instance with core\data\plugins was found under $instancesRoot"
    }

    return $candidates[0].FullName
}

if (-not $ProjectDir) {
    $ProjectDir = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
} else {
    $ProjectDir = (Resolve-Path -LiteralPath $ProjectDir).Path
}

if (-not $LauncherDataRoot) {
    $LauncherDataRoot = Join-Path $ProjectDir ".runtime\.astrbot_launcher"
}
if (-not $RuntimeHome) {
    $RuntimeHome = Join-Path $ProjectDir ".runtime\.fu-gm"
}

$LauncherDataRoot = (Resolve-Path -LiteralPath $LauncherDataRoot).Path
$RuntimeHome = Resolve-FullPath $RuntimeHome
$instanceRoot = Resolve-AstrBotInstance -Root $LauncherDataRoot -RequestedInstanceId $InstanceId
$instanceCore = Join-Path $instanceRoot "core"
$pluginsRoot = Join-Path $instanceCore "data\plugins"
$configRoot = Join-Path $instanceCore "data\config"
$pluginSource = Join-Path $ProjectDir "integrations\astrbot\fu_gm_bridge"
$pluginDest = Join-Path $pluginsRoot "fu_gm_bridge"

if (-not (Test-Path -LiteralPath $pluginSource)) {
    throw "FU-GM AstrBot bridge source not found: $pluginSource"
}

New-Item -ItemType Directory -Path $pluginDest -Force | Out-Null
Copy-Item -Path (Join-Path $pluginSource "*") -Destination $pluginDest -Recurse -Force

New-Item -ItemType Directory -Path $configRoot -Force | Out-Null
$pluginConfigPath = Join-Path $configRoot "fu_gm_bridge_config.json"
$serverUrl = "http://${HostName}:${Port}"
$pluginDefaults = [ordered]@{
    server_url = $serverUrl
    campaign_id = "default"
    default_session_id = "main"
    casual_prefixes = @("时悠", "gm")
    game_prefixes = @("跑团", "行动")
    campaign_bindings_path = "channel_campaigns.json"
    user_campaign_bindings_path = "user_campaigns.json"
    enable_private_safety_auto = $true
    anonymous_private_safety = $true
    enable_natural_routing = $true
    natural_route_group_messages = $true
    natural_route_private_messages = $true
    block_silent_table_talk = $true
    http_timeout_seconds = 120.0
    log_http_timing = $true
    enable_message_buffer = $true
    buffer_debounce_seconds = 3.0
    buffer_max_wait_seconds = 12.0
    buffer_max_messages = 5
}
$pluginConfig = [ordered]@{}
if (Test-Path -LiteralPath $pluginConfigPath) {
    $existingConfig = Get-Content -LiteralPath $pluginConfigPath -Raw | ConvertFrom-Json
    foreach ($property in $existingConfig.PSObject.Properties) {
        $pluginConfig[$property.Name] = $property.Value
    }
}
foreach ($key in $pluginDefaults.Keys) {
    if (-not $pluginConfig.Contains($key) -or $null -eq $pluginConfig[$key] -or ($key -eq "server_url" -and $pluginConfig[$key] -eq "http://127.0.0.1:8765")) {
        $pluginConfig[$key] = $pluginDefaults[$key]
    }
}
$pluginConfig | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $pluginConfigPath -Encoding UTF8

New-Item -ItemType Directory -Path $RuntimeHome -Force | Out-Null
$runtimeSrcRoot = Join-Path $RuntimeHome "src"
$runtimePackage = Join-Path $runtimeSrcRoot "fu_gm"
New-Item -ItemType Directory -Path $runtimeSrcRoot -Force | Out-Null
if (Test-Path -LiteralPath $runtimePackage) {
    Assert-ChildPath -Child $runtimePackage -Parent $runtimeSrcRoot
    Remove-Item -LiteralPath $runtimePackage -Recurse -Force
}
Copy-Item -LiteralPath (Join-Path $ProjectDir "src\fu_gm") -Destination $runtimeSrcRoot -Recurse -Force

$projectEnv = Join-Path $ProjectDir ".env"
if (Test-Path -LiteralPath $projectEnv) {
    Copy-Item -LiteralPath $projectEnv -Destination (Join-Path $RuntimeHome "fu_gm.env") -Force
}

$runtimeRunScript = Join-Path $RuntimeHome "run_fu_gm_http.ps1"
Copy-Item -LiteralPath (Join-Path $ProjectDir "scripts\run_fu_gm_http.ps1") -Destination $runtimeRunScript -Force

$instancePython = Join-Path $instanceRoot "venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $instancePython)) {
    $instancePython = ""
}

$taskArguments = @(
    "-NoProfile",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    "`"$runtimeRunScript`"",
    "-ProjectDir",
    "`"$ProjectDir`"",
    "-RuntimeHome",
    "`"$RuntimeHome`"",
    "-HostName",
    $HostName,
    "-Port",
    $Port
)
if ($instancePython) {
    $taskArguments += @("-PythonExe", "`"$instancePython`"")
}
if ($Offline) {
    $taskArguments += "-Offline"
}

$serviceStarted = $false
$serviceStartMode = "none"
if (-not $NoSchedule) {
    try {
        $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument ($taskArguments -join " ")
        $trigger = New-ScheduledTaskTrigger -AtLogOn
        $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask `
            -TaskName $TaskName `
            -Action $action `
            -Trigger $trigger `
            -Principal $principal `
            -Description "Runs the FU-GM HTTP server for the AstrBot bridge." `
            -Force `
            -ErrorAction Stop | Out-Null

        Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        $serviceStarted = $true
        $serviceStartMode = "scheduled task: $TaskName"
    } catch {
        Write-Warning "Could not register/start scheduled task '$TaskName': $($_.Exception.Message)"
        Write-Warning "Falling back to a hidden PowerShell process for this Windows session."
        Start-Process -FilePath "powershell.exe" -ArgumentList ($taskArguments -join " ") -WindowStyle Hidden | Out-Null
        $serviceStarted = $true
        $serviceStartMode = "hidden process"
    }
}

if (-not $SkipHealthCheck -and $serviceStarted) {
    $healthUrl = "http://${HostName}:${Port}/health"
    $healthy = $false
    foreach ($attempt in 1..20) {
        try {
            $response = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
            if ($response.ok -ne $false) {
                $healthy = $true
                break
            }
        } catch {
            Start-Sleep -Seconds 1
        }
    }
    if (-not $healthy) {
        throw "FU-GM HTTP service did not pass health check: $healthUrl"
    }
}

Write-Host "FU-GM runtime: $RuntimeHome"
Write-Host "AstrBot instance: $instanceRoot"
Write-Host "Plugin installed: $pluginDest"
Write-Host "Plugin config: $pluginConfigPath"
if (-not $NoSchedule) {
    Write-Host "Service start mode: $serviceStartMode"
    Write-Host "Health check: http://${HostName}:${Port}/health"
}
