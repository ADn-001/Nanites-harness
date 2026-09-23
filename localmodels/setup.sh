#!/usr/bin/env bash
# LOCAL CORTEX — Needle (+ optional Laya) installer.
#
# DOCUMENTED BUT NOT RUN automatically by the test suite or CI: installing the models is
# a deliberate, per-machine opt-in (Phase 12 runs the Needle half; Phase 13 the Laya half).
# Nothing here happens unless you run this script yourself.
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

cat <<'EOT'

[LOCAL CORTEX] Needle installed. Next steps:

  1. Download the base weights (this is the only network use, ~1 download):

         .venv/bin/needle download needle3

     Weights land in ~/.cache/cactus-needle (a .cact file). To point the daemon at a
     specific file instead, export NEEDLE_WEIGHTS=/path/to/base.cact
     Run the sidecar with the venv python:
         .venv/bin/python local_models_daemon.py
     (on Windows: .venv\Scripts\python.exe local_models_daemon.py)

  2. Laya is OPTIONAL and separate (Node >= 20, ~1.7 GB of ONNX weights on first use):

         npm install            # inside localmodels/ (its own node_modules)
         # weights cache to ~/.cache/receptron-laya (override with LAYA_CACHE)

[LOCAL CORTEX] Nothing is enabled by default: the harness works unchanged until you
turn LOCAL CORTEX on, and nothing in this package ever contacts a remote service
besides the weight downloads above.
EOT
