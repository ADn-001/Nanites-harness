# LOCAL CORTEX — Needle (+ optional Laya) installer for Windows.
#
# DOCUMENTED BUT NOT RUN automatically by the test suite or CI: installing the models is
# a deliberate, per-machine opt-in (Phase 12 runs the Needle half; Phase 13 the Laya half).
# Nothing here happens unless you run this script yourself.
#
#   powershell -ExecutionPolicy Bypass -File localmodels\setup.ps1
#
$ErrorActionPreference = 'Stop'

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

$python = if ($env:PYTHON) { $env:PYTHON } else { 'python' }
$venv = Join-Path $here '.venv'

Write-Host "[LOCAL CORTEX] creating virtualenv: $venv"
& $python -m venv $venv

$vpy = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path $vpy)) {
    Write-Error "[LOCAL CORTEX] venv python not found at $vpy"
    exit 1
}

Write-Host '[LOCAL CORTEX] upgrading pip'
& $vpy -m pip install --upgrade pip

Write-Host '[LOCAL CORTEX] installing cactus-needle (Apache-2.0) — the repair engine'
& $vpy -m pip install cactus-needle

Write-Host ''
Write-Host '[LOCAL CORTEX] Needle installed. Next steps:'
Write-Host ''
Write-Host '  1. Download the base weights (the only network use, ~1 download):'
Write-Host ''
Write-Host '         .venv\Scripts\needle.exe download needle3'
Write-Host ''
Write-Host '     Weights land in %USERPROFILE%\.cache\cactus-needle (a .cact file). To point the'
Write-Host '     daemon at a specific file instead, set NEEDLE_WEIGHTS=C:\path\to\base.cact'
Write-Host '     Run the sidecar with the venv python:'
Write-Host '         .venv\Scripts\python.exe local_models_daemon.py'
Write-Host ''
Write-Host '  2. Laya is OPTIONAL and separate (Node >= 20, ~1.7 GB of ONNX weights on first use):'
Write-Host ''
Write-Host '         npm install            # inside localmodels\ (its own node_modules)'
Write-Host '         # weights cache to %USERPROFILE%\.cache\receptron-laya (override with LAYA_CACHE)'
Write-Host ''
Write-Host '[LOCAL CORTEX] Nothing is enabled by default: the harness works unchanged until you'
Write-Host 'turn LOCAL CORTEX on, and nothing in this package ever contacts a remote service'
Write-Host 'besides the weight downloads above.'
