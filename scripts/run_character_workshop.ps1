[CmdletBinding()]
param(
    [string]$ProjectDir = "",
    [string]$RuntimeHome = "",
    [string]$PythonExe = "",
    [int]$Port = 8765,
    [string]$DataRoot = ""
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
    $ProjectDir = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
}
$ProjectDir = (Resolve-Path -LiteralPath $ProjectDir).Path

if (-not $RuntimeHome) {
    $RuntimeHome = Join-Path $ProjectDir ".runtime\character-workshop"
}
New-Item -ItemType Directory -Path $RuntimeHome -Force | Out-Null

$runtimeEnv = Join-Path $RuntimeHome "character-workshop.env"
$projectEnv = Join-Path $ProjectDir ".env"
if (Test-Path -LiteralPath $runtimeEnv) {
    Import-DotEnv $runtimeEnv
} elseif (Test-Path -LiteralPath $projectEnv) {
    Import-DotEnv $projectEnv
}

if (-not $DataRoot) {
    $DataRoot = Join-Path $ProjectDir "data\character-workshop"
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

$env:PYTHONPATH = Join-Path $ProjectDir "src"
$env:PYTHONUNBUFFERED = "1"
$env:FU_GM_PROJECT_DIR = $ProjectDir
Set-Location -LiteralPath $ProjectDir

& $PythonExe -u -m fu_gm.character_workshop_app `
    --development `
    --headless `
    --no-browser `
    --port $Port `
    --data-root $DataRoot
