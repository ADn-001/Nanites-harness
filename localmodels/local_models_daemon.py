#!/usr/bin/env python3
"""
LOCAL CORTEX SIDECAR v0.1 - local-model tool-call middleware for the Cogitator harness.

Local inference is NEVER in the critical path. This sidecar is opt-in, lazy and inert:
in Phase 10 it loads no model at all, writes no pid/log file, and makes no network call.

    python localmodels/local_models_daemon.py                       # 127.0.0.1:8932
    python localmodels/local_models_daemon.py --no-needle --no-laya # run degraded on purpose
    python localmodels/local_models_daemon.py --ledger var/local-models.jsonl
    python localmodels/local_models_daemon.py --allow-any-origin    # disable the Origin guard

Routes in Phase 10: GET /health only. Repair/decide/select arrive in Phases 12-14.
The Origin guard, CORS headers, POST size cap, banner and flags copy bridge.py exactly.
"""
import argparse, json, os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# The repo is share-ready: running the sidecar in-place must leave no bytecode artifacts.
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger

VERSION = '0.1.0'
MAX_JSON_BYTES = 1024 * 1024
NEEDLE_GENERATION = 3
DEFAULT_LAYA_CACHE = os.path.join('~', '.cache', 'receptron-laya')

PORT = 8932
NEEDLE_ENABLED = True     # enables the feature; the model stays UNLOADED in this phase
LAYA_ENABLED = True
LEDGER_PATH = ledger.default_path()
ALLOW_ANY_ORIGIN = False
ALLOW_FILE_ORIGIN = False


def origins_desc():
    if ALLOW_ANY_ORIGIN:
        return "ANY"
    if ALLOW_FILE_ORIGIN:
        return "localhost / null (file:// trusted)"
    return "localhost only (null refused)"


def laya_cache_path():
    return os.environ.get('LAYA_CACHE') or os.path.expanduser(DEFAULT_LAYA_CACHE)


def needle_weights_present():
    """Cheap env check - NO package import, no model load (Phase 12 replaces this)."""
    p = os.environ.get('NEEDLE_WEIGHTS')
    return bool(p) and os.path.isfile(p)


def ledger_writable():
    try:
        parent = os.path.dirname(os.path.abspath(LEDGER_PATH))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(LEDGER_PATH, 'a', encoding='utf-8'):
            pass
        return True
    except Exception:
        return False


def degraded_reasons():
    """One reason per ENABLED feature that cannot serve right now.

    A deliberately disabled engine (--no-needle/--no-laya) is NOT degraded.
    """
    reasons = []
    if NEEDLE_ENABLED and not needle_weights_present():
        reasons.append('needle weights missing')
    if LAYA_ENABLED:
        # Phase 10 never spawns the Node child; Phase 13 starts it on demand.
        reasons.append('laya child not started')
    return reasons


def health_obj():
    return {
        'ok': True,
        'version': VERSION,
        'needle': {
            'enabled': NEEDLE_ENABLED,
            'loaded': False,
            'weights': 'present' if needle_weights_present() else 'missing',
            'generation': NEEDLE_GENERATION,
            'lib': None,
        },
        'laya': {
            'enabled': LAYA_ENABLED,
            'loaded': False,
            'child_pid': None,
            'cache': laya_cache_path(),
        },
        'ledger': {'path': os.path.abspath(LEDGER_PATH), 'writable': ledger_writable()},
        'degraded': degraded_reasons(),
    }


class Handler(BaseHTTPRequestHandler):
    def _origin_allowed(self):
        if ALLOW_ANY_ORIGIN:
            return True
        origin = self.headers.get("Origin")
        if not origin:
            # curl / native callers send no Origin at all.
            return True
        if origin == "null":
            # A browser sends "null" for a sandboxed iframe, a data:/blob:
            # document, or a file:// page - i.e. any hostile page can get an
            # opaque origin. Trust it only behind the explicit opt-in.
            return ALLOW_FILE_ORIGIN
        low = origin.lower()
        return low.startswith("http://localhost:") or low.startswith("http://127.0.0.1:") or low in ("http://localhost", "http://127.0.0.1")

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code); self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()

    def _route(self):
        return self.path.split('?', 1)[0].rstrip('/') or '/'

    def do_GET(self):
        if not self._origin_allowed():
            self._json(403, {"ok": False, "error": "origin not permitted"}); return
        if self._route() == '/health':
            self._json(200, health_obj())
        else:
            self._json(404, {"ok": False, "error": "unknown rite"})

    def do_POST(self):
        if not self._origin_allowed():
            self._json(403, {"ok": False, "error": "origin not permitted"}); return
        # Size cap BEFORE routing: Phase 10 has no POST route at all, so an oversized
        # body must be refused as 413 rather than reported as an unknown route.
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = 0
        if n > MAX_JSON_BYTES:
            self._json(413, {"ok": False, "error": "request exceeds %d byte limit" % MAX_JSON_BYTES}); return
        try:
            self.rfile.read(n)
        except Exception:
            pass
        self._json(404, {"ok": False, "error": "unknown rite"})

    def log_message(self, fmt, *args):
        sys.stderr.write("[LOCALMODELS] %s\n" % (fmt % args))


def main():
    global PORT, NEEDLE_ENABLED, LAYA_ENABLED, LEDGER_PATH
    global ALLOW_ANY_ORIGIN, ALLOW_FILE_ORIGIN
    ap = argparse.ArgumentParser(description="LOCAL CORTEX sidecar (Needle + Laya)")
    ap.add_argument("--port", type=int, default=8932)
    ap.add_argument("--no-needle", action="store_true", help="do not enable the Needle repair engine")
    ap.add_argument("--no-laya", action="store_true", help="do not enable the Laya decision engine")
    ap.add_argument("--ledger", default=None, help="ledger JSONL path (default: $LEDGER_PATH or var/local-models.jsonl)")
    ap.add_argument("--allow-any-origin", action="store_true", help="allow non-local web origins (not recommended)")
    ap.add_argument("--allow-file-origin", action="store_true", help="trust a null Origin (file:// page) — not recommended")
    a = ap.parse_args()
    PORT = a.port
    NEEDLE_ENABLED = not a.no_needle
    LAYA_ENABLED = not a.no_laya
    LEDGER_PATH = a.ledger or ledger.default_path()
    ALLOW_ANY_ORIGIN = a.allow_any_origin
    ALLOW_FILE_ORIGIN = a.allow_file_origin
    print("=" * 56)
    print(" LOCAL CORTEX SIDECAR v%s — the machine thinks locally" % VERSION)
    print("   port       : %d" % PORT)
    print("   needle     : %s" % ("enabled (unloaded)" if NEEDLE_ENABLED else "disabled"))
    print("   laya       : %s" % ("enabled (unloaded)" if LAYA_ENABLED else "disabled"))
    print("   origins    : %s" % origins_desc())
    print("   ledger     : %s" % os.path.abspath(LEDGER_PATH))
    print("   health     : http://localhost:%d/health" % PORT)
    print("=" * 56)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
