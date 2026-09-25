#!/usr/bin/env python3
"""
LOCAL CORTEX SIDECAR v0.1 - local-model tool-call middleware for the Cogitator harness.

Local inference is NEVER in the critical path. This sidecar is opt-in, lazy and inert:
run with the SYSTEM python it loads no model (the needle package lives in
localmodels/.venv), writes no pid/log file and makes no network call.

    python localmodels/local_models_daemon.py                       # 127.0.0.1:8932
    python localmodels/local_models_daemon.py --no-needle --no-laya # run degraded on purpose
    python localmodels/local_models_daemon.py --ledger var/local-models.jsonl
    python localmodels/local_models_daemon.py --allow-any-origin    # disable the Origin guard

Needle (Phase 12) needs the venv: localmodels/.venv/bin/python. NEEDLE_TELEMETRY=0 is
set below so the package's anonymous usage counters never fire - the privacy statement
("nothing leaves the machine") holds. The only network use is weight/engine downloads at
install time (see localmodels/setup.sh).

Routes this phase: GET /health, POST /repair, POST /ledger. The Origin guard, CORS
headers, POST size cap (413 BEFORE routing), banner and flags copy bridge.py exactly.

/repair contract (docs/plans/needle-laya-middleware-plan.md §4):
    request  {suspect:{name, arguments}, candidates:[ToolSchema<=10], schema?, trace_id?}
    response {ok, calls:[{name, arguments}], confidence, reasoning, latency_ms,
              trace_id, degraded?, reason?}
    calls: [] means "no repair" - this daemon NEVER manufactures a call.
The Needle call runs inside a bounded timeout (--needle-timeout-ms, default 800ms), is
serialised by the module lock (the package keeps ONE active instance per generation
process-wide) and NEVER wedges the server: a timeout or a missing model answers
{ok:false, degraded:true} - never a 500.
"""
import argparse, json, os, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# The repo is share-ready: running the sidecar in-place must leave no bytecode artifacts.
sys.dont_write_bytecode = True

# The package's anonymous usage counters must never fire ("nothing leaves the machine").
# setdefault so an operator's explicit choice always wins.
os.environ.setdefault('NEEDLE_TELEMETRY', '0')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger
import needle_backend

VERSION = '0.1.0'
MAX_JSON_BYTES = 1024 * 1024
NEEDLE_GENERATION = 3
DEFAULT_LAYA_CACHE = os.path.join('~', '.cache', 'receptron-laya')

PORT = 8932
NEEDLE_ENABLED = True
LAYA_ENABLED = True
NEEDLE_TIMEOUT_MS = 800
LEDGER_PATH = ledger.default_path()
ALLOW_ANY_ORIGIN = False
ALLOW_FILE_ORIGIN = False

BACKEND = needle_backend.NeedleBackend(generation=NEEDLE_GENERATION)
# /repair runs the (possibly slow) model call on this single worker so the HTTP handler
# thread can return a bounded, degraded answer when the engine overruns.
MODEL_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix='needle')


def origins_desc():
    if ALLOW_ANY_ORIGIN:
        return "ANY"
    if ALLOW_FILE_ORIGIN:
        return "localhost / null (file:// trusted)"
    return "localhost only (null refused)"


def laya_cache_path():
    return os.environ.get('LAYA_CACHE') or os.path.expanduser(DEFAULT_LAYA_CACHE)


def needle_weights_present():
    """REAL probe (Phase 12): the package's own cache path - no model load."""
    return needle_backend.weights_present(NEEDLE_GENERATION)


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

    A deliberately disabled engine (--no-needle/--no-laya) is NOT degraded. An enabled
    needle whose weights (or package) are absent still reports 'needle weights missing'.
    """
    reasons = []
    if NEEDLE_ENABLED and not BACKEND.loaded and not needle_weights_present():
        reasons.append('needle weights missing')
    if LAYA_ENABLED:
        # The Node child is only spawned on demand (Phase 13); report it as not started.
        reasons.append('laya child not started')
    return reasons


def health_obj():
    return {
        'ok': True,
        'version': VERSION,
        'needle': {
            'enabled': NEEDLE_ENABLED,
            'loaded': BACKEND.loaded,
            'weights': 'present' if needle_weights_present() else 'missing',
            'generation': NEEDLE_GENERATION,
            'lib': BACKEND.lib or needle_backend.lib_present(NEEDLE_GENERATION),
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


def _new_trace_id():
    return ledger.new_trace_id()


def _repair_call(suspect, candidates, timeout_ms, trace_id):
    """Run one repair on the model worker with a BOUNDED timeout around the model call.

    Never raises, never wedges, never returns a 500-shaped answer: any failure (package
    or weights missing, engine exception, timeout, busy engine) degrades to
    {ok:false, degraded:true, reason:...}. On success the normalised backend result is
    augmented in place with latency_ms + trace_id and returned.
    """
    started = time.time()

    def elapsed():
        return int(round((time.time() - started) * 1000))

    if not NEEDLE_ENABLED:
        return {'ok': False, 'degraded': True, 'reason': 'disabled',
                'latency_ms': elapsed(), 'trace_id': trace_id, 'calls': [],
                'confidence': None, 'reasoning': ''}
    candidates = candidates if isinstance(candidates, list) else []
    if not candidates:
        return {'ok': False, 'degraded': True, 'reason': 'tool_unavailable',
                'latency_ms': elapsed(), 'trace_id': trace_id, 'calls': [],
                'confidence': None, 'reasoning': ''}
    timeout_s = max(timeout_ms, 1) / 1000.0
    text = needle_backend.build_repair_prompt(suspect)
    future = MODEL_POOL.submit(BACKEND.repair, text, candidates)
    try:
        res = future.result(timeout=timeout_s)
    except TimeoutError:
        if getattr(BACKEND, '_tools_key', None) is None:
            # The engine was still (re)building for this candidate set when the budget
            # expired - 'needle loading', not a model timeout.
            reason = 'needle loading'
        else:
            reason = 'timeout'
        return {'ok': False, 'degraded': True, 'reason': reason,
                'latency_ms': elapsed(), 'trace_id': trace_id, 'calls': [],
                'confidence': None, 'reasoning': ''}
    except Exception as e:
        return {'ok': False, 'degraded': True, 'reason': type(e).__name__,
                'latency_ms': elapsed(), 'trace_id': trace_id, 'calls': [],
                'confidence': None, 'reasoning': ''}
    res['latency_ms'] = elapsed()
    res['trace_id'] = trace_id
    if not res.get('ok'):
        res['degraded'] = True
        res.setdefault('calls', [])
        res.setdefault('confidence', None)
        res.setdefault('reasoning', '')
    return res


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
        # Origin guard FIRST (Phase 10 invariant): foreign Origin => 403, not 404.
        if not self._origin_allowed():
            self._json(403, {"ok": False, "error": "origin not permitted"}); return
        if self._route() == '/health':
            self._json(200, health_obj())
        else:
            self._json(404, {"ok": False, "error": "unknown rite"})

    def _read_body(self):
        """Size cap BEFORE routing (413 must outrank 404). Returns (obj, error_code)."""
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = 0
        if n > MAX_JSON_BYTES:
            return None, 413
        try:
            raw = self.rfile.read(n)
        except Exception:
            raw = b''
        if not raw:
            return {}, None
        try:
            obj = json.loads(raw.decode('utf-8', 'replace'))
            return (obj if isinstance(obj, dict) else {}), None
        except Exception:
            return None, 400

    def do_POST(self):
        # Origin guard FIRST (Phase 10 invariant): foreign Origin => 403, not 404.
        if not self._origin_allowed():
            self._json(403, {"ok": False, "error": "origin not permitted"}); return
        body, err = self._read_body()
        if err == 413:
            self._json(413, {"ok": False, "error": "request exceeds %d byte limit" % MAX_JSON_BYTES}); return
        route = self._route()
        if err == 400:
            self._json(400, {"ok": False, "error": "invalid JSON body"}); return
        if route == '/repair':
            tid = body.get('trace_id') or _new_trace_id()
            res = _repair_call(body.get('suspect'), body.get('candidates'),
                               NEEDLE_TIMEOUT_MS, tid)
            # One ledger record per /repair call (the future fine-tuning corpus);
            # never raise into the request path.
            out = res.get('calls') if res.get('ok') else {'degraded': res.get('reason')}
            ledger.append_record({
                'trace_id': tid, 'model': 'needle', 'op': 'repair',
                'input_redacted': True,
                'request': {'suspect': body.get('suspect'),
                            'candidates': [(c.get('name') or (c.get('function') or {}).get('name'))
                                           for c in (body.get('candidates') or [])
                                           if isinstance(c, dict)]},
                'output': out,
                'confidence': res.get('confidence'),
                'latency_ms': res.get('latency_ms'),
                'degraded': bool(res.get('degraded')),
                'action': None,
            }, path=LEDGER_PATH)
            self._json(200, res); return
        if route == '/ledger':
            tid = body.get('trace_id')
            action = body.get('action')
            if not isinstance(tid, str) or not tid or not isinstance(action, str) or not action:
                self._json(400, {"ok": False, "error": "trace_id + action required"}); return
            ok = ledger.append_record({'trace_id': tid, 'action': action,
                                       'note': body.get('note')}, path=LEDGER_PATH)
            self._json(200, {'ok': bool(ok)}); return
        self._json(404, {"ok": False, "error": "unknown rite"})

    def log_message(self, fmt, *args):
        sys.stderr.write("[LOCALMODELS] %s\n" % (fmt % args))


def main():
    global PORT, NEEDLE_ENABLED, LAYA_ENABLED, LEDGER_PATH
    global ALLOW_ANY_ORIGIN, ALLOW_FILE_ORIGIN, NEEDLE_TIMEOUT_MS
    ap = argparse.ArgumentParser(description="LOCAL CORTEX sidecar (Needle + Laya)")
    ap.add_argument("--port", type=int, default=8932)
    ap.add_argument("--no-needle", action="store_true", help="do not enable the Needle repair engine")
    ap.add_argument("--no-laya", action="store_true", help="do not enable the Laya decision engine")
    ap.add_argument("--ledger", default=None, help="ledger JSONL path (default: $LEDGER_PATH or var/local-models.jsonl)")
    ap.add_argument("--allow-any-origin", action="store_true", help="allow non-local web origins (not recommended)")
    ap.add_argument("--allow-file-origin", action="store_true", help="trust a null Origin (file:// page) — not recommended")
    ap.add_argument("--needle-timeout-ms", type=int, default=800,
                    help="bounded timeout AROUND THE MODEL CALL (default 800 ms); overrun => degraded, never a 500")
    ap.add_argument("--preload-needle", action="store_true",
                    help="eagerly load the Needle engine in the background at boot (weights must be cached)")
    a = ap.parse_args()
    PORT = a.port
    NEEDLE_ENABLED = not a.no_needle
    LAYA_ENABLED = not a.no_laya
    NEEDLE_TIMEOUT_MS = a.needle_timeout_ms
    LEDGER_PATH = a.ledger or ledger.default_path()
    ALLOW_ANY_ORIGIN = a.allow_any_origin
    ALLOW_FILE_ORIGIN = a.allow_file_origin
    if a.preload_needle and NEEDLE_ENABLED:
        # Background (never blocks boot): the engine's first load is the slow part.
        def _preload():
            try:
                BACKEND.load()
            except Exception as e:
                sys.stderr.write("[LOCALMODELS] needle preload failed: %s\n" % e)
        threading.Thread(target=_preload, daemon=True, name='needle-preload').start()
    weights = needle_weights_present()
    print("=" * 56)
    print(" LOCAL CORTEX SIDECAR v%s — the machine thinks locally" % VERSION)
    print("   port       : %d" % PORT)
    print("   needle     : %s" % (("enabled (weights %s%s)" % (
        'present' if weights else 'MISSING',
        ', preloading' if a.preload_needle else '')) if NEEDLE_ENABLED else "disabled"))
    print("   timeout    : %d ms (needle, around the model call)" % NEEDLE_TIMEOUT_MS)
    print("   laya       : %s" % ("enabled (unloaded)" if LAYA_ENABLED else "disabled"))
    print("   origins    : %s" % origins_desc())
    print("   ledger     : %s" % os.path.abspath(LEDGER_PATH))
    print("   telemetry  : NEEDLE_TELEMETRY=%s (nothing leaves the machine)"
          % os.environ.get('NEEDLE_TELEMETRY'))
    print("   health     : http://localhost:%d/health" % PORT)
    print("=" * 56)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
