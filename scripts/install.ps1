[CmdletBinding()]
param(
    [string]$Python = "python",
    [string]$VenvPath = ".venv"
)

$ErrorActionPreference = "Stop"
$workspace = (Resolve-Path (Join-Path $PSScriptRoot "..\")).Path
$venv = Join-Path $workspace $VenvPath
$pythonExe = Join-Path $venv "Scripts\python.exe"

if (-not (Test-Path -LiteralPath $pythonExe)) {
    & $Python -m venv $venv
}

& $pythonExe -m pip install --upgrade pip
& $pythonExe -m pip install --editable $workspace
Write-Host "android-mcp installed in $venv"
Write-Host "Configure the MCP client with: $pythonExe -m android_mcp"
