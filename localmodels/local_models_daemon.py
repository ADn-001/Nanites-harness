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

Routes this phase: GET /health, POST /repair, POST /decide, POST /ledger. The Origin
guard, CORS headers, POST size cap (413 BEFORE routing), banner and flags copy bridge.py
exactly.

Laya runs as a spawned Node child (localmodels/laya_child.mjs) speaking NDJSON over stdio,
because @receptron/laya is ESM-only and needs onnxruntime-node. It is LAZY: nothing is
spawned until the first /decide, /health never starts it, and an idle child is reaped
(--laya-idle-s). A missing `node`, a missing child script, a timeout or a dead child all
answer {ok:false, degraded:true, reason:...} - never a 500, never a hang.

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
import argparse, atexit, collections, json, os, shutil, signal, subprocess, sys, threading, time
from concurrent.futures import Future, ThreadPoolExecutor
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
DAEMON_DIR = os.path.dirname(os.path.abspath(__file__))

PORT = 8932
NEEDLE_ENABLED = True
LAYA_ENABLED = True
NEEDLE_TIMEOUT_MS = 800
LAYA_TIMEOUT_MS = 500       # bounded timeout around one child request
LAYA_IDLE_S = 120           # reap the child after N idle seconds (0 disables)
LAYA_CHILD_PATH = os.path.join(DAEMON_DIR, 'laya_child.mjs')
LAYA_NODE = os.environ.get('LAYA_NODE') or 'node'
LEDGER_PATH = ledger.default_path()
ALLOW_ANY_ORIGIN = False
ALLOW_FILE_ORIGIN = False

BACKEND = needle_backend.NeedleBackend(generation=NEEDLE_GENERATION)
# /repair runs the (possibly slow) model call on this single worker so the HTTP handler
# thread can return a bounded, degraded answer when the engine overruns.
MODEL_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix='needle')

# The Laya child manager; created in main() when Laya is enabled (None => never spawned).
LAYA = None


class ChildGone(Exception):
    """The Laya child died (or its stdio closed) while a request was in flight."""


class LayaChild:
    """One lazily spawned `node laya_child.mjs` process, NDJSON over stdio.

    Design notes (hard rules from the plan):
      - nothing is ever awaited without a bounded timeout (the caller passes one),
      - /health never touches the child, so a status probe cannot trigger a model load,
      - a dead child is detected (reader EOF / poll() != None) and lazily respawned,
      - one request at a time is written, but a slow/timed-out request never blocks the
        daemon: the HTTP thread just stops waiting and answers degraded.
    """

    def __init__(self, node_bin, child_path):
        self.node_bin = node_bin
        self.child_path = child_path
        self.pid = None                 # live child pid, else None
        self.loaded = False             # True once the child reported a loaded model
        self.last_used = time.monotonic()
        self._proc = None
        self._proc_lock = threading.RLock()   # spawn/kill/state
        self._fut_lock = threading.RLock()    # futures + inflight counter
        self._write_lock = threading.Lock()   # serialise stdin writes
        self._futures = {}
        self._inflight = 0
        self._next_id = 0
        self.stderr_tail = collections.deque(maxlen=20)

    # ---- process lifecycle ----

    def alive(self):
        return self._proc is not None and self._proc.poll() is None

    def _spawn_locked(self):
        """Start the child. Caller holds _proc_lock. Raises on failure (a real reason)."""
        proc = subprocess.Popen(
            [self.node_bin, self.child_path],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=DAEMON_DIR, env=dict(os.environ),  # LAYA_CACHE (if set) rides along
            text=True, bufsize=1)
        self._proc = proc
        self.pid = proc.pid
        self.loaded = False
        self.last_used = time.monotonic()
        threading.Thread(target=self._read_stdout, args=(proc,), daemon=True,
                         name='laya-stdout').start()
        threading.Thread(target=self._read_stderr, args=(proc,), daemon=True,
                         name='laya-stderr').start()
        return proc

    def _read_stdout(self, proc):
        """Parse one JSON object per stdout line and hand it to the waiting future."""
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    # Nothing but protocol JSON should ever be on stdout; a stray line is
                    # dropped rather than allowed to kill the reader.
                    continue
                if not isinstance(msg, dict):
                    continue
                fut = self._futures.pop(msg.get('id'), None)
                if fut is not None and not fut.done():
                    fut.set_result(msg)
        except Exception:
            pass
        finally:
            self._fail_pending(ChildGone('child stdout closed'))

    def _read_stderr(self, proc):
        try:
            for line in proc.stderr:
                self.stderr_tail.append(line.rstrip('\n'))
        except Exception:
            pass

    def _fail_pending(self, exc):
        with self._fut_lock:
            pending = list(self._futures.values())
            self._futures = {}
        self.loaded = False
        for fut in pending:
            if not fut.done():
                fut.set_exception(exc)

    def _register(self):
        with self._fut_lock:
            self._next_id += 1
            rid = 'ly%d' % self._next_id
            fut = Future()
            self._futures[rid] = fut
            self._inflight += 1
            return rid, fut

    def _unregister(self, rid):
        with self._fut_lock:
            self._futures.pop(rid, None)
            self._inflight -= 1

    def request(self, payload, timeout_s):
        """Send one request, wait at most timeout_s. Raises TimeoutError / ChildGone."""
        rid, fut = self._register()
        try:
            self._write(rid, payload)
            # NOTE: a timeout deliberately does NOT kill the child. The first real /decide
            # pays for a ~1.7 GB model load; killing on a short budget would throw that
            # load away every time. The idle reaper cleans up an unresponsive child, and
            # the very next request simply gets its own bounded wait.
            return fut.result(timeout=max(timeout_s, 0.001))
        finally:
            self._unregister(rid)

    def _write(self, rid, payload):
        with self._proc_lock:
            if not self.alive():
                self._spawn_locked()
        with self._write_lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                raise ChildGone('child gone')
            self.last_used = time.monotonic()
            try:
                proc.stdin.write(json.dumps(dict(payload, id=rid)) + '\n')
                proc.stdin.flush()
            except Exception as e:
                raise ChildGone('child stdin closed: %s' % e)

    def shutdown(self):
        """Politely close (op:'close') then kill; never raises; clears pid/loaded."""
        with self._proc_lock:
            proc = self._proc
            self._proc = None
        self.pid = None
        self.loaded = False
        if proc is None or proc.poll() is not None:
            self._fail_pending(ChildGone('child closed'))
            return
        try:
            stdin = proc.stdin
            if stdin and not stdin.closed:
                stdin.write(json.dumps({'id': 'close', 'op': 'close'}) + '\n')
                stdin.flush()
        except Exception:
            pass
        try:
            proc.wait(timeout=0.4)
        except Exception:
            pass
        if proc.poll() is None:
            try: proc.kill()
            except Exception: pass
            try: proc.wait(timeout=1.0)
            except Exception: pass
        self._fail_pending(ChildGone('child closed'))

    def maybe_reap(self, idle_s):
        """Kill the child if it has been idle for more than idle_s (0 disables)."""
        if idle_s <= 0:
            return False
        with self._proc_lock:
            if not self.alive():
                return False
            with self._fut_lock:
                if self._inflight > 0:
                    return False
            if time.monotonic() - self.last_used <= idle_s:
                return False
        self.shutdown()
        return True


def origins_desc():
    if ALLOW_ANY_ORIGIN:
        return "ANY"
    if ALLOW_FILE_ORIGIN:
        return "localhost / null (file:// trusted)"
    return "localhost only (null refused)"


def laya_cache_path():
    """The cache dir AS THE OPERATOR WROTE IT - a `~` string, never expanded.

    /health is a status line the operator reads back; leaking an absolute personal path
    (or a hostname) into a tracked-file-shaped response is exactly what the share-ready
    rule forbids. The child receives LAYA_CACHE through the environment and expands it
    itself.
    """
    return os.environ.get('LAYA_CACHE') or DEFAULT_LAYA_CACHE


def needle_weights_present():
    """REAL probe (Phase 12): the package's own cache path - no model load."""
    return needle_backend.weights_present(NEEDLE_GENERATION)


def laya_node_path():
    """Resolve the Node binary (LAYA_NODE, default `node`); None when absent."""
    try:
        return shutil.which(LAYA_NODE)
    except Exception:
        return None


def laya_engine_present():
    """A Laya we CAN serve with: node on PATH and the child script on disk."""
    return bool(laya_node_path()) and os.path.isfile(LAYA_CHILD_PATH)


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
    An ENABLED Laya is degraded only when it cannot serve AT ALL - no `node` on PATH, or
    no child script on disk. A child that is merely not started yet is LAZY (it is spawned
    on the first /decide), so a lazy engine is never a degraded reason.
    """
    reasons = []
    if NEEDLE_ENABLED and not BACKEND.loaded and not needle_weights_present():
        reasons.append('needle weights missing')
    if LAYA_ENABLED and not laya_engine_present():
        reasons.append('laya engine missing')
    return reasons


def health_obj():
    laya_loaded = bool(LAYA.loaded) if LAYA is not None else False
    laya_pid = LAYA.pid if LAYA is not None else None
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
            'loaded': laya_loaded,
            # The live child's pid, or None - /health never SPAWNS it.
            'child_pid': laya_pid if (LAYA_ENABLED and laya_pid) else None,
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


def _laya_confidence(answers):
    """The `confidence` a decision is ledged with: the max numeric noul/score seen.

    `null` means "nothing to grade" (e.g. a choice-only batch) and must stay null rather
    than being invented - the frontend treats a missing confidence as below-threshold.
    """
    best = None
    if isinstance(answers, dict):
        for answer in answers.values():
            if not isinstance(answer, dict):
                continue
            for key in ('noul', 'score'):
                value = answer.get(key)
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                best = value if best is None else max(best, value)
    return best


def _questions_summary(questions):
    """The ledger's compact view: [{'key': k, 'type': t}, ...] in request order."""
    if not isinstance(questions, dict):
        return []
    return [{'key': k, 'type': (v or {}).get('type') if isinstance(v, dict) else None}
            for k, v in questions.items()]


def _decide_error(reason, latency_ms, trace_id):
    return {'ok': False, 'degraded': True, 'reason': reason, 'latency_ms': latency_ms,
            'trace_id': trace_id, 'answers': {}, 'usage': None}


def _laya_decide(state, questions, timeout_ms, trace_id):
    """One batched Laya decision through the child, inside a BOUNDED timeout.

    Never raises, never wedges, never a 500: a disabled engine, a missing node/child, a
    timeout, a dead child or a child-side error all degrade to
    {ok:false, degraded:true, reason:...} and the NEXT request still works.
    """
    started = time.time()

    def elapsed():
        return int(round((time.time() - started) * 1000))

    if not LAYA_ENABLED:
        return _decide_error('disabled', elapsed(), trace_id)
    if not laya_engine_present():
        return _decide_error('engine_missing', elapsed(), trace_id)
    if LAYA is None:
        # Enabled but never initialised (should not happen outside tests).
        return _decide_error('engine_missing', elapsed(), trace_id)
    try:
        msg = LAYA.request({'op': 'decide', 'state': state, 'questions': questions},
                           max(timeout_ms, 1) / 1000.0)
    except TimeoutError:
        return _decide_error('timeout', elapsed(), trace_id)
    except ChildGone:
        return _decide_error('child_gone', elapsed(), trace_id)
    except Exception as e:
        return _decide_error(type(e).__name__, elapsed(), trace_id)
    if not isinstance(msg, dict):
        return _decide_error('child_protocol', elapsed(), trace_id)
    if not msg.get('ok'):
        # Pass the child's own failure through, still degraded (never a 500).
        return _decide_error(str(msg.get('error') or 'child_error'), elapsed(), trace_id)
    # A successful decide means the child had the model loaded.
    LAYA.loaded = True
    return {'ok': True, 'answers': msg.get('answers') or {}, 'usage': msg.get('usage'),
            'latency_ms': elapsed(), 'trace_id': trace_id}


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
        if route == '/decide':
            tid = body.get('trace_id') or _new_trace_id()
            res = _laya_decide(body.get('state'), body.get('questions'), LAYA_TIMEOUT_MS, tid)
            # One ledger record per /decide call, carrying the child's raw answers INCLUDING
            # the probability distributions (the future fine-tuning corpus). Never raise
            # into the request path.
            ledger.append_record({
                'trace_id': tid, 'model': 'laya', 'op': 'decide',
                'input_redacted': True,
                'request': {'state': body.get('state'),
                            'questions': _questions_summary(body.get('questions'))},
                'output': {'answers': res.get('answers') or {}},
                'confidence': _laya_confidence(res.get('answers')),
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


def _laya_reaper_loop(stop_event):
    """Kill an idle child in the background; never raises, never touches /health."""
    while not stop_event.wait(0.4):
        try:
            if LAYA is not None and LAYA.maybe_reap(LAYA_IDLE_S):
                sys.stderr.write('[LOCALMODELS] laya child reaped (idle > %ds)\n' % LAYA_IDLE_S)
        except Exception:
            pass


def _shutdown_laya(*_args):
    """Reap the child on exit/SIGTERM - never leave an orphaned node process."""
    try:
        if LAYA is not None:
            LAYA.shutdown()
    except Exception:
        pass


def main():
    global PORT, NEEDLE_ENABLED, LAYA_ENABLED, LEDGER_PATH, LAYA
    global ALLOW_ANY_ORIGIN, ALLOW_FILE_ORIGIN, NEEDLE_TIMEOUT_MS
    global LAYA_TIMEOUT_MS, LAYA_IDLE_S, LAYA_CHILD_PATH
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
    ap.add_argument("--laya-timeout-ms", type=int, default=500,
                    help="bounded timeout around ONE Laya child request (default 500 ms); "
                         "overrun => degraded, never a 500, never a wedge")
    ap.add_argument("--laya-idle-s", type=int, default=120,
                    help="kill the Laya child after N idle seconds (default 120; 0 disables reaping)")
    ap.add_argument("--laya-child", default=None,
                    help="path to the Laya child script (default: <daemon dir>/laya_child.mjs)")
    a = ap.parse_args()
    PORT = a.port
    NEEDLE_ENABLED = not a.no_needle
    LAYA_ENABLED = not a.no_laya
    NEEDLE_TIMEOUT_MS = a.needle_timeout_ms
    LAYA_TIMEOUT_MS = a.laya_timeout_ms
    LAYA_IDLE_S = a.laya_idle_s
    if a.laya_child:
        LAYA_CHILD_PATH = os.path.abspath(a.laya_child)
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
    if LAYA_ENABLED:
        # The manager object exists but NOTHING is spawned until the first /decide.
        LAYA = LayaChild(LAYA_NODE, LAYA_CHILD_PATH)
        atexit.register(_shutdown_laya)
        try:
            signal.signal(signal.SIGTERM, lambda *_: (_shutdown_laya(), sys.exit(0)))
        except Exception:
            pass
        stop_event = threading.Event()
        threading.Thread(target=_laya_reaper_loop, args=(stop_event,), daemon=True,
                         name='laya-reaper').start()
    weights = needle_weights_present()
    engine_ok = laya_engine_present()
    print("=" * 56)
    print(" LOCAL CORTEX SIDECAR v%s — the machine thinks locally" % VERSION)
    print("   port       : %d" % PORT)
    print("   needle     : %s" % (("enabled (weights %s%s)" % (
        'present' if weights else 'MISSING',
        ', preloading' if a.preload_needle else '')) if NEEDLE_ENABLED else "disabled"))
    print("   timeout    : %d ms (needle, around the model call)" % NEEDLE_TIMEOUT_MS)
    print("   laya       : %s" % (("enabled (lazy, %s engine)" % ('node + child present' if engine_ok
                                                                 else 'ENGINE MISSING'))
                                  if LAYA_ENABLED else "disabled"))
    if LAYA_ENABLED:
        print("   laya child : %s" % LAYA_CHILD_PATH)
        print("   laya budget: %d ms per request, reaped after %d s idle"
              % (LAYA_TIMEOUT_MS, LAYA_IDLE_S))
        print("   laya cache : %s" % laya_cache_path())
    print("   origins    : %s" % origins_desc())
    print("   ledger     : %s" % os.path.abspath(LEDGER_PATH))
    print("   telemetry  : NEEDLE_TELEMETRY=%s (nothing leaves the machine)"
          % os.environ.get('NEEDLE_TELEMETRY'))
    print("   health     : http://localhost:%d/health" % PORT)
    print("=" * 56)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
