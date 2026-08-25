[CmdletBinding()]
param(
    [string]$PythonExe = "python",
    [string]$Version = "0.2.0"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectDir = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$sourceRoot = Join-Path $projectDir "src"
$releaseRoot = [System.IO.Path]::GetFullPath((Join-Path $projectDir "release\character-workshop"))
$buildRoot = [System.IO.Path]::GetFullPath((Join-Path $projectDir "build\character-workshop"))
$stageRoot = [System.IO.Path]::GetFullPath((Join-Path $releaseRoot "Fabula Ultima 角色工房"))
$archivePath = [System.IO.Path]::GetFullPath((Join-Path $releaseRoot "Fabula-Ultima-Character-Workshop-Windows-x64-v$Version.zip"))
$projectPrefix = $projectDir.TrimEnd('\') + '\'

foreach ($path in @($releaseRoot, $buildRoot)) {
    if (-not $path.StartsWith($projectPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to clean a path outside the project: $path"
    }
    if (Test-Path -LiteralPath $path) {
        Remove-Item -LiteralPath $path -Recurse -Force
    }
}

& $PythonExe -m PyInstaller --version | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is not installed. Run: python -m pip install -e .[package]"
}

New-Item -ItemType Directory -Path $releaseRoot -Force | Out-Null
New-Item -ItemType Directory -Path $stageRoot -Force | Out-Null

$entryPoint = Join-Path $sourceRoot "fu_gm\character_workshop_app.py"
$webAssets = Join-Path $sourceRoot "fu_gm\web"
$workflowAssets = Join-Path $projectDir "config\comfyui_workflows"
$webDataArgument = "$webAssets;fu_gm/web"
$workflowDataArgument = "$workflowAssets;config/comfyui_workflows"

& $PythonExe -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name "Fabula-Ultima-Character-Workshop" `
    --paths $sourceRoot `
    --exclude-module "PIL" `
    --exclude-module "numpy" `
    --exclude-module "psutil" `
    --exclude-module "pyreadline3" `
    --add-data $webDataArgument `
    --add-data $workflowDataArgument `
    --distpath $stageRoot `
    --workpath $buildRoot `
    --specpath $buildRoot `
    $entryPoint
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed with exit code $LASTEXITCODE."
}

$usageGuide = Join-Path $projectDir "packaging\character_workshop\README.txt"
Copy-Item -LiteralPath $usageGuide -Destination $stageRoot
Copy-Item -LiteralPath $workflowAssets -Destination (Join-Path $stageRoot "workflows") -Recurse
Compress-Archive -LiteralPath $stageRoot -DestinationPath $archivePath -CompressionLevel Optimal

Write-Host "Character Workshop package created:"
Write-Host $archivePath
