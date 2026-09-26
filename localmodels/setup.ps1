# LOCAL CORTEX — Needle (+ optional Laya) installer for Windows.
#
# Installs the Needle repair engine for real: venv + cactus-needle + the base weights +
# the engine library. Per-machine opt-in: nothing runs unless you invoke this script,
# and nothing here is ever enabled by default in the harness.
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

# Weights + engine library. `needle download needle3` defaults --out to the CURRENT
# directory — running it here would dump a 35 MB binary into the repo — so the helper
# script does the downloads itself into the package cache (~/.cache/cactus-needle/v3/
# <version>/). Idempotent: a cached file is skipped. If the wheel for the exact engine
# version is unpublished, fetch_engine.py picks the highest wheel in the repo's python/
# dir matching this machine's platform tag and extracts libneedle3.dll from it.
Write-Host '[LOCAL CORTEX] downloading base weights + engine library (the only network use)'
& $vpy (Join-Path $here 'fetch_engine.py')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

# Laya (the decision engine) is OPTIONAL and separate: a Node ESM package with its OWN
# node_modules, so this step runs `npm install` right here. Non-fatal on purpose - the
# Needle repair pass, and the whole harness, work without Laya. The 1.7 GB of ONNX weights
# are NOT fetched here: the first Laya.load (the first /decide) downloads and caches them
# under %USERPROFILE%\.cache\receptron-laya (LAYA_CACHE overrides).
Write-Host '[LOCAL CORTEX] installing the Laya decision engine (Node >= 20) — optional'
$node = Get-Command node -ErrorAction SilentlyContinue
$npm = Get-Command npm -ErrorAction SilentlyContinue
if ($node -and $npm) {
    # A failing native command must not abort the script ($ErrorActionPreference = 'Stop'):
    # Laya is optional, so both a throw and a non-zero exit are a warning, not a failure.
    $npmOk = $true
    try { & npm install --no-audit --no-fund } catch { $npmOk = $false; Write-Warning $_ }
    if ((-not $npmOk) -or ($LASTEXITCODE -ne 0)) {
        Write-Warning "[LOCAL CORTEX] 'npm install' failed — Laya stays unavailable; Needle still works."
    } else {
        Write-Host "  Laya deps installed in $here\node_modules."
    }
} else {
    Write-Warning '[LOCAL CORTEX] node/npm not found on PATH — skipping Laya (optional).'
}

Write-Host ''
Write-Host '[LOCAL CORTEX] Needle installed. Next steps:'
Write-Host ''
Write-Host '  1. Run the sidecar with the VENV python (the system python has no needle package):'
Write-Host ''
Write-Host '         .venv\Scripts\python.exe local_models_daemon.py'
Write-Host '         # add --preload-needle to warm the engine at boot'
Write-Host ''
Write-Host '     The daemon sets NEEDLE_TELEMETRY=0 itself, so the anonymous usage counters never'
Write-Host '     fire — nothing about a repair leaves the machine. To point at a specific weights'
Write-Host '     file, set NEEDLE_WEIGHTS=C:\path\to\base.cact'
Write-Host ''
Write-Host '  2. Laya is OPTIONAL and separate (Node >= 20). Its npm deps were installed above'
Write-Host '     into localmodels\node_modules; re-run `npm install` in localmodels\ if that step'
Write-Host '     was skipped. The ~1.7 GB of ONNX weights download on the FIRST Laya.load (the'
Write-Host '     first /decide) and cache to %USERPROFILE%\.cache\receptron-laya (LAYA_CACHE overrides).'
Write-Host ''
Write-Host '[LOCAL CORTEX] Nothing is enabled by default: the harness works unchanged until you'
Write-Host 'turn LOCAL CORTEX on, and nothing in this package ever contacts a remote service'
Write-Host 'besides the weight/engine downloads above.'
