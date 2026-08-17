[CmdletBinding()]
param(
    [string]$Python = "python",
    [string]$VenvPath = ".mcp-venv",
    [string]$Repository = "https://github.com/Yang-Ya-Chao/android-mcp.git",
    [string]$Revision = "main"
)

$ErrorActionPreference = "Stop"
$workspace = (Resolve-Path (Join-Path $PSScriptRoot "..\")).Path
$venv = Join-Path $workspace $VenvPath
$pythonExe = Join-Path $venv "Scripts\python.exe"

if (-not (Test-Path -LiteralPath $pythonExe)) {
    & $Python -m venv $venv
}

& $pythonExe -m pip install --upgrade pip
$installSpec = "git+$Repository@$Revision"
& $pythonExe -m pip install --upgrade --force-reinstall --no-cache-dir $installSpec

$probe = & $pythonExe -c "import android_mcp, importlib.metadata as m; print(android_mcp.__file__); print(m.version('android-mcp'))"
Write-Host "android-mcp installed from git: $installSpec"
Write-Host "Package probe: $probe"
Write-Host "Configure the MCP client with: $pythonExe -m android_mcp"
