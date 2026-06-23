[CmdletBinding()]
param(
    [string]$ProjectDir = "",
    [string]$RuntimeHome = "",
    [string]$PythonExe = "",
    [string]$HostName = "",
    [int]$Port = 0,
    [string]$DataRoot = "",
    [switch]$Offline
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Import-DotEnv {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }

    foreach ($rawLine in Get-Content -LiteralPath $Path) {
        $line = $rawLine.Trim()
        if (-not $line -or $line.StartsWith("#") -or -not $line.Contains("=")) {
            continue
        }

        $parts = $line.Split("=", 2)
        $name = $parts[0].Trim()
        $value = $parts[1].Trim()
        if (-not $name) {
            continue
        }
        if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        [Environment]::SetEnvironmentVariable($name, $value, "Process")
    }

    [Environment]::SetEnvironmentVariable("FU_GM_DOTENV_PATH", $Path, "Process")
}

if (-not $ProjectDir) {
    if ($env:FU_GM_PROJECT_DIR) {
        $ProjectDir = $env:FU_GM_PROJECT_DIR
    } else {
        $candidate = Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..") -ErrorAction SilentlyContinue
        if ($candidate -and (Test-Path -LiteralPath (Join-Path $candidate.Path "src\fu_gm"))) {
            $ProjectDir = $candidate.Path
        } else {
            $ProjectDir = (Get-Location).Path
        }
    }
}

$ProjectDir = (Resolve-Path -LiteralPath $ProjectDir).Path
if (-not $RuntimeHome) {
    $RuntimeHome = Join-Path $ProjectDir ".runtime\.fu-gm"
}
$RuntimeHome = [System.IO.Path]::GetFullPath($RuntimeHome)
New-Item -ItemType Directory -Path $RuntimeHome -Force | Out-Null

$runtimeEnv = Join-Path $RuntimeHome "fu_gm.env"
$projectEnv = Join-Path $ProjectDir ".env"
if (Test-Path -LiteralPath $runtimeEnv) {
    Import-DotEnv $runtimeEnv
} elseif (Test-Path -LiteralPath $projectEnv) {
    Import-DotEnv $projectEnv
}

$runtimeSrc = Join-Path $RuntimeHome "src"
$projectSrc = Join-Path $ProjectDir "src"
if (Test-Path -LiteralPath (Join-Path $runtimeSrc "fu_gm")) {
    $env:PYTHONPATH = $runtimeSrc
} else {
    $env:PYTHONPATH = $projectSrc
}
$env:PYTHONUNBUFFERED = "1"

if (-not $HostName) {
    $HostName = if ($env:FU_GM_HTTP_HOST) { $env:FU_GM_HTTP_HOST } else { "127.0.0.1" }
}
if ($Port -le 0) {
    $Port = if ($env:FU_GM_HTTP_PORT) { [int]$env:FU_GM_HTTP_PORT } else { 8766 }
}
if (-not $DataRoot) {
    $DataRoot = if ($env:FU_GM_DATA_ROOT) { $env:FU_GM_DATA_ROOT } else { Join-Path $RuntimeHome "data\campaigns" }
}
New-Item -ItemType Directory -Path $DataRoot -Force | Out-Null

if (-not $PythonExe) {
    if ($env:FU_GM_PYTHON) {
        $PythonExe = $env:FU_GM_PYTHON
    } elseif (Test-Path -LiteralPath (Join-Path $ProjectDir ".venv\Scripts\python.exe")) {
        $PythonExe = Join-Path $ProjectDir ".venv\Scripts\python.exe"
    } else {
        $PythonExe = "python"
    }
}

Set-Location -LiteralPath $ProjectDir
$serverArgs = @(
    "-u",
    "-m",
    "fu_gm.http_server",
    "--host",
    $HostName,
    "--port",
    $Port,
    "--data-root",
    $DataRoot
)
if ($Offline) {
    $serverArgs += "--offline"
}

& $PythonExe @serverArgs
