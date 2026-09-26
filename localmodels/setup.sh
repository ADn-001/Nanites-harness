#!/usr/bin/env bash
# LOCAL CORTEX — Needle (+ optional Laya) installer.
#
# Installs the Needle repair engine for real: venv + cactus-needle + the base weights +
# the engine library. Per-machine opt-in: nothing runs unless you invoke this script,
# and nothing here is ever enabled by default in the harness.
#
#   bash localmodels/setup.sh
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PY="${PYTHON:-python3}"
VENV="$HERE/.venv"

echo "[LOCAL CORTEX] creating virtualenv: $VENV"
"$PY" -m venv "$VENV"

if [ -x "$VENV/bin/python" ]; then
  VPY="$VENV/bin/python"
else
  echo "[LOCAL CORTEX] ERROR: venv python not found under $VENV/bin" >&2
  exit 1
fi

echo "[LOCAL CORTEX] upgrading pip"
"$VPY" -m pip install --upgrade pip

echo "[LOCAL CORTEX] installing cactus-needle (Apache-2.0) — the repair engine"
"$VPY" -m pip install cactus-needle

# Weights + engine library. `needle download needle3` defaults `--out` to the CURRENT
# DIRECTORY — running that from here would dump a 35 MB binary into the repo — so the
# helper script does the downloads itself and places everything in the package's own
# cache dir (~/.cache/cactus-needle/v3/<version>/). Idempotent: a cached file is skipped.
# The engine version the package expects is occasionally unpublished as a wheel;
# fetch_engine.py then picks the HIGHEST wheel in the repo's python/ dir that matches
# this machine's platform tag and extracts libneedle3.so from it.
echo "[LOCAL CORTEX] downloading base weights + engine library (the only network use)"
"$VPY" "$HERE/fetch_engine.py"

# Laya (the decision engine) is OPTIONAL and separate: it is a Node ESM package and needs
# its OWN node_modules, so this step runs `npm install` right here. It is deliberately
# non-fatal - the Needle repair pass, and the whole harness, work without Laya. The 1.7 GB
# of ONNX weights are NOT fetched here: the first Laya.load (the first /decide) downloads
# and caches them under ~/.cache/receptron-laya (LAYA_CACHE overrides), reporting progress
# as JSON lines on the child's stderr.
echo "[LOCAL CORTEX] installing the Laya decision engine (Node >= 20) — optional"
if command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1; then
  echo "  node $(node --version)"
  if npm install --no-audit --no-fund; then
    echo "  Laya deps installed in $HERE/node_modules."
  else
    echo "[LOCAL CORTEX] WARNING: 'npm install' failed — Laya stays unavailable; Needle still works." >&2
  fi
else
  echo "[LOCAL CORTEX] WARNING: node/npm not found on PATH — skipping Laya (optional)." >&2
fi

cat <<'EOT'

[LOCAL CORTEX] Needle installed. Next steps:

  1. Run the sidecar with the VENV python (the system python has no needle package):

         localmodels/.venv/bin/python localmodels/local_models_daemon.py
         # on Windows: localmodels\.venv\Scripts\python.exe localmodels\local_models_daemon.py

     Add --preload-needle to warm the engine at boot. The daemon sets
     NEEDLE_TELEMETRY=0 itself, so the package's anonymous usage counters never fire —
     nothing about a repair leaves the machine. To point at a specific weights file,
     export NEEDLE_WEIGHTS=/path/to/base.cact (otherwise the cache above is used).

  2. Laya is OPTIONAL and separate (Node >= 20). Its npm deps were installed above into
     localmodels/node_modules; re-run `npm install` in localmodels/ if that step was
     skipped. The ~1.7 GB of ONNX weights download on the FIRST Laya.load (the first
     /decide) and cache to ~/.cache/receptron-laya (override with LAYA_CACHE).

[LOCAL CORTEX] Nothing is enabled by default: the harness works unchanged until you
turn LOCAL CORTEX on, and nothing in this package ever contacts a remote service
besides the weight/engine downloads above.
EOT
