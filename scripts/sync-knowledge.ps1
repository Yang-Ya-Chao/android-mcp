[CmdletBinding()]
param(
    [string]$VenvPath = ".venv",
    [int]$TimeoutSeconds = 20,
    [int]$MaxBytes = 2000000,
    [string[]]$SourceId = @()
)

$ErrorActionPreference = "Stop"
$workspace = (Resolve-Path (Join-Path $PSScriptRoot "..\")).Path
$pythonExe = Join-Path (Join-Path $workspace $VenvPath) "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw "Virtual environment Python not found: $pythonExe. Run scripts\install.ps1 first."
}

$idsValue = [string]::Join(",", @($SourceId))
$pythonCode = @'
import json
import sys
from android_mcp.services.kb_catalog import OfficialSourceCatalog

requested = [item for item in sys.argv[1].split(chr(44)) if item] if sys.argv[1] else []
catalog = OfficialSourceCatalog()
if not requested:
    requested = [
        item["id"]
        for item in catalog.sources()
        if item.get("kind") != "source_repository"
    ]
result = catalog.sync(
    source_ids=requested,
    timeout_seconds=int(sys.argv[2]),
    max_bytes=int(sys.argv[3]),
)
print(json.dumps(result, ensure_ascii=False, indent=2))
'@

$env:PYTHONPATH = Join-Path $workspace "src"
& $pythonExe -c $pythonCode $idsValue $TimeoutSeconds $MaxBytes
if ($LASTEXITCODE -ne 0) {
    throw "Knowledge sync failed; exit code: $LASTEXITCODE"
}
