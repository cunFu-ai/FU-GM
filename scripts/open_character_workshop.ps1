[CmdletBinding()]
param(
    [switch]$NoBrowser
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectDir = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$runner = Join-Path $PSScriptRoot "run_character_workshop.ps1"
$hostName = "127.0.0.1"
$preferredPort = 8765
$port = 0
$dataRoot = Join-Path $projectDir "data\character-workshop"
$runtimeHome = Join-Path $projectDir ".runtime\character-workshop"
$logsDir = Join-Path $projectDir "logs"
$stdoutLog = Join-Path $logsDir "character-workshop.out.log"
$stderrLog = Join-Path $logsDir "character-workshop.err.log"

function Test-WorkshopHealth {
    param([int]$CandidatePort)
    try {
        $response = Invoke-RestMethod -Uri "http://${hostName}:${CandidatePort}/health" -TimeoutSec 2
        return $response.ok -eq $true -and $response.service -eq "fu-character-workshop"
    } catch {
        return $false
    }
}

function Test-PortInUse {
    param([int]$CandidatePort)
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $task = $client.ConnectAsync($hostName, $CandidatePort)
        return $task.Wait(500) -and $client.Connected
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

if (-not (Test-Path -LiteralPath $runner)) {
    throw "Cannot find the Character Workshop launcher: $runner"
}

foreach ($candidate in $preferredPort..($preferredPort + 19)) {
    $inUse = Test-PortInUse -CandidatePort $candidate
    if ($inUse -and (Test-WorkshopHealth -CandidatePort $candidate)) {
        $port = $candidate
        break
    }
    if (-not $inUse) {
        $port = $candidate
        break
    }
}
if ($port -eq 0) {
    throw "No available local port was found between $preferredPort and $($preferredPort + 19)."
}

$healthUrl = "http://${hostName}:${port}/health"
$workshopUrl = "http://${hostName}:${port}/characters"

if (-not (Test-WorkshopHealth -CandidatePort $port)) {

    New-Item -ItemType Directory -Path $logsDir -Force | Out-Null
    New-Item -ItemType Directory -Path $dataRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $runtimeHome -Force | Out-Null

    $arguments = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", ('"{0}"' -f $runner),
        "-ProjectDir", ('"{0}"' -f $projectDir),
        "-RuntimeHome", ('"{0}"' -f $runtimeHome),
        "-Port", [string]$port,
        "-DataRoot", ('"{0}"' -f $dataRoot)
    )
    $env:PYTHONIOENCODING = "utf-8"
    $env:PYTHONUTF8 = "1"
    $serverProcess = Start-Process `
        -FilePath "powershell.exe" `
        -ArgumentList $arguments `
        -WorkingDirectory $projectDir `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog `
        -PassThru

    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline -and -not (Test-WorkshopHealth -CandidatePort $port)) {
        if ($serverProcess.HasExited) {
            $details = if (Test-Path -LiteralPath $stderrLog) {
                (Get-Content -LiteralPath $stderrLog -Tail 12) -join [Environment]::NewLine
            } else {
                "No error log was created."
            }
            throw "Character Workshop failed to start.$([Environment]::NewLine)$details"
        }
        Start-Sleep -Milliseconds 500
    }

    if (-not (Test-WorkshopHealth -CandidatePort $port)) {
        throw "Character Workshop did not become ready within 30 seconds. See: $stderrLog"
    }
}

if (-not $NoBrowser) {
    Start-Process $workshopUrl
}

Write-Host "Character Workshop is ready: $workshopUrl"
