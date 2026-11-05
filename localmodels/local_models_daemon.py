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

Routes this phase: GET /health, POST /repair, POST /select, POST /decide, POST /ledger. The
Origin guard, CORS headers, POST size cap (413 BEFORE routing), banner and flags copy bridge.py
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

/select contract (same §4, F3 pre-router): {input, candidates:[ToolSchema<=10]} -> the same
response envelope, and it obeys the same rule - calls: [] means "no proposal", never a
manufactured one. It shares _repair_call's bounded, single-worker, never-500 path under its
own budget (--needle-select-timeout-ms, default 800 ms).

The Needle call runs inside a bounded timeout (--needle-timeout-ms, default 800ms), is
serialised by the module lock (the package keeps ONE active instance per generation
process-wide) and NEVER wedges the server: a timeout or a missing model answers
{ok:false, degraded:true} - never a 500.
"""
import argparse, atexit, collections, hmac, json, os, secrets, shutil, signal, subprocess, sys, threading, time
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
NEEDLE_SELECT_TIMEOUT_MS = 800    # bounded budget around ONE /select proposal
LAYA_TIMEOUT_MS = 500       # bounded timeout around one child request
LAYA_IDLE_S = 120           # reap the child after N idle seconds (0 disables)
LAYA_CHILD_PATH = os.path.join(DAEMON_DIR, 'laya_child.mjs')
LAYA_NODE = os.environ.get('LAYA_NODE') or 'node'
LEDGER_PATH = ledger.default_path()
ALLOW_ANY_ORIGIN = False
ALLOW_FILE_ORIGIN = False
#: The Host header must name loopback. The whole value is split on the last
#: colon and the NAME must equal one of these exactly, so neither
#: "localhost.evil.example" nor "127.0.0.1.evil.example" can pass as
#: "localhost". A missing Host is refused: HTTP/1.1 requires one, so its
#: absence means a crafted request rather than a browser. The bracketed IPv6
#: literal "[::1]" is handled separately in _host_allowed, because it carries
#: its port inside the brackets and would not survive a naive split on ":".
LOOPBACK_HOSTS = ("localhost", "127.0.0.1")
#: The per-install token, next to THIS script. The sidecar owns its own token; it
#: does not share the bridge daemon's, because it is a separate install surface.
TOKEN_PATH = os.path.join(DAEMON_DIR, '.cogitator-token')
TOKEN = ''


def load_or_create_token(path):
    """Per-install token, 0600. Read-or-create so the operator pastes it into the UI once."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            tok = f.read().strip()
        if tok:
            return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(tok + '\n')
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return tok

BACKEND = needle_backend.NeedleBackend(generation=NEEDLE_GENERATION)
# /repair runs the (possibly slow) model call on this single worker so the HTTP handler
# thread can return a bounded, degraded answer when the engine overruns. /select shares it:
# the engine bakes the candidate tools in, so both are the same model call on the same
# process-global instance, and serialising them keeps that instance single-threaded.
#
# The queue is BOUNDED (CR-Nanites-harness-0019). A ThreadPoolExecutor's work queue is
# unbounded by default, so N concurrent /repair calls queued N items behind one worker -
# and because the timeout path did not cancel, a caller that had ALREADY given up left
# its item behind as well. The worker then spent its life producing answers nobody would
# ever read, while every later request timed out behind them. Refusing past the bound is
# the honest answer: the engine is saturated, and saying so beats an unbounded queue.
MODEL_POOL_MAX_QUEUE = 4


class ModelBusy(Exception):
    """The single model worker is saturated past MODEL_POOL_MAX_QUEUE."""


class _BoundedModelPool:
    """The single needle worker behind a FINITE admission queue.

    `submit` raises ModelBusy rather than queueing without limit. The count is
    admission-based (submitted, not yet finished), so a finished item frees its slot
    whether it succeeded, raised, or timed out.
    """

    def __init__(self, max_workers=1, max_queue=MODEL_POOL_MAX_QUEUE, **kw):
        self._pool = ThreadPoolExecutor(max_workers=max_workers, **kw)
        self._max_queue = max_queue
        self._lock = threading.Lock()
        self._admitted = 0

    @property
    def max_queue(self):
        return self._max_queue

    def submit(self, fn, *args, **kwargs):
        # The whole admission check happens under one lock, and the increment happens
        # with it: two callers racing past the bound cannot both observe the last slot
        # as free.
        with self._lock:
            if self._admitted >= self._max_queue:
                raise ModelBusy('model pool saturated (%d in flight)' % self._admitted)
            self._admitted += 1
        try:
            fut = self._pool.submit(fn, *args, **kwargs)
        except Exception:
            self._release()
            raise
        fut.add_done_callback(lambda _f: self._release())
        return fut

    def _release(self):
        with self._lock:
            if self._admitted > 0:
                self._admitted -= 1

    def shutdown(self, wait=True):
        return self._pool.shutdown(wait=wait)


MODEL_POOL = _BoundedModelPool(max_workers=1, thread_name_prefix='needle')


def submit_model_call(fn, *args, **kwargs):
    """Admit one model call, or raise ModelBusy. Callers degrade, never 500.

    The returned future is cancelled by the caller on its timeout path - see
    `_repair_call` - so a stale work item does not occupy the single worker after
    the caller has stopped waiting for it. `cancel()` only succeeds while the item is
    still QUEUED; a call already executing must be allowed to finish, because the
    engine is a process-global instance and tearing it down mid-call would be worse
    than the wait. That is why the bound exists: it caps how many can be waiting.
    """
    return MODEL_POOL.submit(fn, *args, **kwargs)

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
        # Monotonic child identity (CR-Nanites-harness-0020). Every spawn bumps it.
        # A child's reader threads capture the value it was spawned with, so a DYING
        # child can tell that it is no longer the current one and must not fail the
        # live child's pending requests - a dead child's EOF says nothing about work
        # running against its successor.
        self._generation = 0
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
        # Claim the next generation BEFORE the readers start: a reader must never be
        # able to observe itself as current after a respawn has already replaced it.
        # Bumped under _fut_lock so the bump is ordered against `_fail_pending`'s
        # fence+drain critical section - that pairing is what makes CR-0020's fix
        # atomic rather than a TOCTOU (see _fail_pending).
        with self._fut_lock:
            self._generation += 1
            gen = self._generation
        threading.Thread(target=self._read_stdout, args=(proc, gen), daemon=True,
                         name='laya-stdout').start()
        threading.Thread(target=self._read_stderr, args=(proc,), daemon=True,
                         name='laya-stderr').start()
        return proc

    def _read_stdout(self, proc, generation):
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
                fut = self._take_future(msg.get('id'))
                if fut is not None:
                    if not fut.done():
                        fut.set_result(msg)
        except Exception:
            pass
        finally:
            self._fail_pending(ChildGone('child stdout closed'), generation)

    def _read_stderr(self, proc):
        try:
            for line in proc.stderr:
                self.stderr_tail.append(line.rstrip('\n'))
        except Exception:
            pass

    def _fail_pending(self, exc, generation):
        """Fail every in-flight request - but ONLY those belonging to `generation`.

        `generation` is a REQUIRED parameter, deliberately. An optional one defaulting to
        None leaves the pre-0020 "fail everything, scoped to nothing" behaviour reachable,
        so a future call site that forgets the argument silently reinstates the bug and
        every test still passes. Making the signature enforce it means a caller must
        state WHICH child its teardown is about. There is no unscoped teardown: if some
        future caller genuinely has no child in hand, it should say so out loud by
        passing the current `self._generation`, not by omitting the argument.
        """
        # The fence and the drain MUST be one critical section. Checking the generation
        # outside the lock is a TOCTOU: a dying child's reader could pass the check,
        # then a respawn could bump the generation and REGISTER a new request, and the
        # drain would then fail that brand-new request - exactly CR-0020. Because
        # `_spawn_locked` bumps `_generation` under this same lock, holding it across
        # both makes the two orderings exhaustive: either the drain completes before the
        # respawn (and only touches the old child's requests), or it starts after (and
        # sees a generation that no longer matches, so it is a no-op).
        #
        # The fence is PER REQUEST, not per child: a stale teardown must still fail the
        # requests that belonged to the child that died. Returning early on a generation
        # mismatch would leave those futures unresolved until their caller's timeout -
        # the work is genuinely gone, and a dead child's work must be failed, just not
        # its successor's. Each entry records the generation it was registered against,
        # so exactly the dying child's requests are failed and nothing else.
        with self._fut_lock:
            doomed = [fut for (gen, fut) in self._futures.values() if gen == generation]
            for rid in [r for r, (gen, _f) in self._futures.items() if gen == generation]:
                self._futures.pop(rid, None)
            # `loaded` describes THE CHILD, so it is fenced by the child's own identity
            # rather than by whether it had work in flight. Two errors meet here, and
            # both were live in the first cut of this phase:
            #   - gating the reset on `doomed` left `loaded=True` forever on a child that
            #     died between two requests (the ordinary case - an idle reap, or a crash
            #     while idle), so /health reported a live engine that no longer existed;
            #   - leaving it unfenced let a DYING child's teardown clear the flag on a
            #     NEWER, live, loaded child, which is CR-0020 all over again one line
            #     below where it was fixed.
            # It is only cleared when `generation` is still the current child, i.e. when
            # this teardown is about the child that exists. A stale teardown touches
            # nothing but its own already-dead requests.
            is_current = (generation == self._generation)
            if is_current:
                self.loaded = False
        if not doomed:
            return False
        for fut in doomed:
            if not fut.done():
                fut.set_exception(exc)
        return True

    def _register(self):
        with self._fut_lock:
            self._next_id += 1
            rid = 'ly%d' % self._next_id
            fut = Future()
            # Paired with the generation the request was issued against, so a teardown
            # can fail exactly ITS OWN child's requests and never a successor's (CR-0020).
            self._futures[rid] = (self._generation, fut)
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

    def _stamp(self, rid, generation):
        """Bind a pending request to the child it was actually WRITTEN to.

        Registration happens BEFORE `_write`, and `_write` may spawn a fresh child (the
        first request ever, or any request after the previous child died). So the
        generation captured in `_register` is the generation of the child that was
        current *then* - which is not necessarily the child that receives the bytes.
        Without this re-stamp a dying child's teardown misses the very request it was
        serving, and the caller waits out its whole timeout instead of being told the
        child is gone (CR-0020 regression, caught by the phase-13 child_gone case).

        `generation` is a REQUIRED argument and must be the identity of the child the
        bytes went to, captured by `_write` under `_proc_lock`. Reading `self._generation`
        here instead would re-open exactly the leak this exists to close: the stamp runs
        after `_proc_lock` and `_write_lock` are both released, so a respawn in that
        window (any other thread reaching `_write` while the child is being replaced)
        stamps the request with the NEW child's generation - and then the teardown of the
        child that is actually serving it never matches. The counter read at stamp time
        answers "who is current", which is a different question from "who got the bytes".
        """
        with self._fut_lock:
            entry = self._futures.get(rid)
            if entry is not None:
                self._futures[rid] = (generation, entry[1])

    def _take_future(self, rid):
        """Pop a resolved request's future under the lock every mutation holds.

        `_read_stdout` resolves a request from the reader thread while `_fail_pending`
        drains the same dict. The drain's whole atomicity claim rests on the fence and
        the drain being ONE critical section, which is only true if every writer takes
        the lock - an unlocked `pop` here lets the reader interleave with the drain's
        iteration (CR-0020).
        """
        with self._fut_lock:
            entry = self._futures.pop(rid, None)
        return entry[1] if entry is not None else None

    def _write(self, rid, payload):
        with self._proc_lock:
            if not self.alive():
                self._spawn_locked()
        with self._write_lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                raise ChildGone('child gone')
            # The child's identity is captured HERE, under the same lock that chose the
            # proc handle, and is what the bytes are about to be written to. It is read
            # once and carried, never re-read after the locks are dropped.
            gen = self._generation
            self.last_used = time.monotonic()
            try:
                proc.stdin.write(json.dumps(dict(payload, id=rid)) + '\n')
                proc.stdin.flush()
            except Exception as e:
                raise ChildGone('child stdin closed: %s' % e)
        # The bytes are with THAT child now - bind the request to it.
        self._stamp(rid, gen)

    def shutdown(self):
        """Politely close (op:'close') then kill; never raises; clears pid/loaded.

        The generation is captured together with the proc handle, so the teardown
        fails exactly the requests that were in flight against THAT child. If a
        respawn has already happened, the captured generation no longer matches and
        the teardown is a no-op instead of failing the new child's work (CR-0020).
        """
        with self._proc_lock:
            proc = self._proc
            gen = self._generation
            self._proc = None
        self.pid = None
        self.loaded = False
        if proc is None or proc.poll() is not None:
            self._fail_pending(ChildGone('child closed'), gen)
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
        self._fail_pending(ChildGone('child closed'), gen)

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


# How many ledger lines of each kind the read-back reports, and how many lines deep it reads.
# The settings panel shows these as the LOCAL CORTEX proposal counters; the browser cannot
# read the ledger file, so the daemon has to surface them.
LEDGER_COUNT_KEYS = ('proposals', 'accepted', 'ignored', 'rejected', 'passed_through', 'timeout')
LEDGER_COUNT_TAIL = 2000
#: The hard CEILING on how much `_ledger_tail` will ever read, in bytes
#: (CR-Nanites-harness-0025). The window used to double without limit until it held
#: LEDGER_COUNT_TAIL lines, so FAT records - 10 of 100 KB each - walked it past a
#: megabyte and /health read an unbounded amount of a large ledger.
#:
#: Sized from two MEASURED fixtures, not picked round, because the number is only
#: honest if it sits between them:
#:   * a realistic 5000-line ledger needs ~232 KB to yield its last 2000 lines
#:     (2000 outcome lines at ~116 bytes) - the phase-14 guarantee. The ceiling must
#:     stay ABOVE this or it silently under-reports an ordinary ledger, which would
#:     be the very bug the growing window was written to avoid;
#:   * the finding's fat fixture - 10 records of 100 KB - is ~1 MB and must stay
#:     ABOVE this or the bound is never exercised.
#: 512 KiB sits between them: ~2.2x the headroom an ordinary ledger needs, and half
#: the fat fixture. The line count stays the primary bound; this is the backstop.
LEDGER_TAIL_MAX_BYTES = 512 * 1024
#: The FIRST window a backwards tail read opens. Named because the ceiling above is
#: only meaningful relative to it - the loop doubles from here and stops there.
LEDGER_TAIL_START_BYTES = 65536
# The outcome lines written by POST /ledger carry an `action` and NO `op` key; that is how
# accepted/ignored are counted, and is intentional.
_LEDGER_ACTIONS = {'accepted': 'accepted', 'accepted_by_operator': 'accepted',
                   'ignored_by_operator': 'ignored', 'rejected': 'rejected',
                   'passed_through': 'passed_through', 'timeout': 'timeout'}


def _ledger_tail(path):
    """The last LEDGER_COUNT_TAIL lines of `path`, bounded by LINES and by BYTES.

    A session ledger is append-only and grows without bound, so /health must never read
    all of it. The window GROWS backwards in chunks from EOF until it holds 2000 lines
    or the start of the file is reached - a fixed byte window would silently
    under-report whenever the lines are long (measured: a 64 KiB window held only 1489
    of 2000 lines and reported 1489 instead of 2000).

    The growth is CEILINGED by LEDGER_TAIL_MAX_BYTES (CR-Nanites-harness-0025), because
    growing "until 2000 lines" is not a bound at all when the lines are fat: 10 records
    of 100 KB each never reach 2000 lines, so the loop kept doubling and read whatever
    the file held. Two bounds, and the loop stops at whichever comes first:

      * the line count (2000), which is what the counters are specified over, and
      * LEDGER_TAIL_MAX_BYTES, which is the backstop for pathological records.

    WHEN THE BYTE CEILING IS HIT FIRST: the tail is TRUNCATED, not failed. It returns
    the MOST RECENT lines that fit within the ceiling - the same lines it would have
    returned had they been shorter - and it TERMINATES. That is the honest answer: a
    silently shortened counter is visibly short, whereas an unbounded read is a
    /health that stops answering at all. The alternative, refusing to report anything
    past the ceiling, would make the counters read as all-zero for a large ledger,
    which is a fabricated measurement wearing a measurement's clothes.

    Returns [] for a missing/unreadable file; never raises.
    """
    try:
        with open(path, 'rb') as f:
            size = f.seek(0, os.SEEK_END)
            window = LEDGER_TAIL_START_BYTES
            start = max(0, size - window)
            while True:
                f.seek(start)
                data = f.read(size - start)
                lines = data.decode('utf-8', 'replace').splitlines()
                enough_lines = len(lines) > LEDGER_COUNT_TAIL
                at_start = start == 0
                at_ceiling = window >= LEDGER_TAIL_MAX_BYTES
                if enough_lines or at_start or at_ceiling:
                    # Drop the first line when the window started mid-line.
                    if start > 0 and lines:
                        lines = lines[1:]
                    return lines[-LEDGER_COUNT_TAIL:]
                window = min(window * 2, LEDGER_TAIL_MAX_BYTES)
                start = max(0, size - window)
    except Exception:
        return []


def ledger_size(path=None):
    """The active ledger's size in bytes, for /health. NEVER raises; 0 when absent.

    Surfaced so an operator can see a ledger approaching its rotation bound
    (CR-Nanites-harness-0006) without reading the file, and so the bound is
    observable from outside the process at all.
    """
    try:
        return os.path.getsize(path or LEDGER_PATH)
    except OSError:
        return 0


def _ledger_counts(path):
    """Proposal/outcome counters read back out of the ledger. NEVER raises.

    `proposals` counts the lines the daemon wrote for a /select proposal (`op:'select'`);
    every OTHER line is counted by its `action` field. A missing file, an unreadable file
    or a ledger of nothing but malformed lines all read as all-zero - a broken ledger must
    never break /health.
    """
    counts = dict.fromkeys(LEDGER_COUNT_KEYS, 0)
    for line in _ledger_tail(path):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue                      # not JSON: skip, never fail the probe
        if not isinstance(rec, dict):
            continue                      # a JSON array/scalar is not a record
        if rec.get('op') == 'select':
            counts['proposals'] += 1
            continue                      # a proposal line has no outcome of its own
        key = _LEDGER_ACTIONS.get(rec.get('action'))
        if key:
            counts[key] += 1
        # Any other / missing action is deliberately ignored.
    return counts


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
        'ledger': {'path': os.path.abspath(LEDGER_PATH), 'writable': ledger_writable(),
                   # `bytes` + `max_bytes` make the rotation bound (CR-0006) observable
                   # from outside the process: an operator can see the ledger
                   # approaching its limit without reading the file.
                   'bytes': ledger_size(LEDGER_PATH),
                   'max_bytes': ledger.MAX_BYTES,
                   'counts': _ledger_counts(LEDGER_PATH)},
        'degraded': degraded_reasons(),
    }


def _new_trace_id():
    return ledger.new_trace_id()


def _candidate_name(c):
    """The rite NAME of one candidate entry, or None. Accepts every shape; never raises.

    Three shapes reach this daemon and all three are legitimate:
      {"function": {"name": "read_file"}}   the nested OpenAI tool schema,
      {"name": "read_file"}                the flat schema the frontend sends,
      {"function": "read_file"}            the flattened form, where `function` is
                                          already the NAME and not a wrapper dict.

    The third one is why this is a function and not an inline expression
    (CR-Nanites-harness-0022). `(c.get('function') or {}).get('name')` raises
    AttributeError ON THE STRING - and because that expression is an ARGUMENT to
    `ledger.append_record`, it is evaluated before the call, so append_record's own
    blanket `except` never runs and the exception escapes into the HTTP handler.
    The result was not a 500 but a dead connection: BaseHTTPRequestHandler had no
    answer left to send. Anything unusable yields None (the ledger records a null
    name, which is honest - the request carried none) and never an exception.
    """
    if not isinstance(c, dict):
        return None
    fn = c.get('function')
    if isinstance(fn, dict):
        name = fn.get('name')
        return name if isinstance(name, str) and name else None
    if isinstance(fn, str) and fn:
        return fn                     # the flattened shape: function IS the name
    name = c.get('name')
    return name if isinstance(name, str) and name else None


def _candidate_names(candidates):
    """The names of a candidate list, in request order. Never raises."""
    if not isinstance(candidates, list):
        return []
    return [_candidate_name(c) for c in candidates if isinstance(c, dict)]


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
    try:
        future = submit_model_call(BACKEND.repair, text, candidates)
    except ModelBusy:
        return {'ok': False, 'degraded': True, 'reason': 'engine busy',
                'latency_ms': elapsed(), 'trace_id': trace_id, 'calls': [],
                'confidence': None, 'reasoning': ''}
    try:
        res = future.result(timeout=timeout_s)
    except TimeoutError:
        # Abandon the work item (CR-Nanites-harness-0019). Without this the caller
        # stops waiting but the item stays queued, and the single worker keeps
        # computing an answer nobody will read while every later request queues
        # behind it. cancel() is False when the call is already RUNNING; that is
        # deliberate and is why the queue is bounded.
        future.cancel()
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


def _select_call(input_text, candidates, timeout_ms, trace_id):
    """Propose one tool call for the operator's utterance, with a BOUNDED timeout.

    Deliberately the same contract as `_repair_call`, because a proposal is a repair that
    starts from an utterance instead of from a malformed call: the same `NEEDLE_ENABLED`
    short-circuit, the same empty-`candidates` guard, the same single-worker
    `MODEL_POOL.submit(...)` + `future.result(timeout=...)`, the same `elapsed()` latency and
    the same never-raise/never-500 rule. `calls: []` means "no proposal" - this daemon NEVER
    manufactures a call.
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
        # No tool to propose: a degraded, empty answer beats a manufactured one.
        return {'ok': False, 'degraded': True, 'reason': 'tool_unavailable',
                'latency_ms': elapsed(), 'trace_id': trace_id, 'calls': [],
                'confidence': None, 'reasoning': ''}
    timeout_s = max(timeout_ms, 1) / 1000.0
    text = needle_backend.build_select_prompt(input_text, candidates)
    # The Needle engine bakes the tools in at construction and answers one prompt with one
    # envelope, so a proposal is the same model call as a repair - only the prompt differs.
    # Same bounded admission and same cancel-on-timeout as _repair_call (CR-Nanites-harness-0019):
    # /select shares the ONE worker, so an unbounded queue here starves /repair identically.
    try:
        future = submit_model_call(BACKEND.repair, text, candidates)
    except ModelBusy:
        return {'ok': False, 'degraded': True, 'reason': 'engine busy',
                'latency_ms': elapsed(), 'trace_id': trace_id, 'calls': [],
                'confidence': None, 'reasoning': ''}
    try:
        res = future.result(timeout=timeout_s)
    except TimeoutError:
        future.cancel()
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
    def _host_allowed(self):
        """The Host header must name loopback, with or without a port."""
        host = (self.headers.get("Host") or "").strip().lower()
        if not host:
            # HTTP/1.1 requires a Host; its absence means a crafted request, not a browser.
            return False
        if host.startswith("["):
            if host == "[::1]":
                return True
            return host.startswith("[::1]:") and host[6:].isdigit()
        name, sep, port = host.partition(":")
        if name not in LOOPBACK_HOSTS:
            return False
        return not sep or port.isdigit()

    def _token_ok(self):
        """Constant-time compare of X-Cogitator-Token against the per-install token.

        Compare the ASCII-safe form: hmac.compare_digest raises TypeError on any
        str holding a byte >= 0x80, and http.server decodes header bytes as
        latin-1, so a malformed token would crash the gate instead of being
        refused with a 403. See bridge.py for the full note.
        """
        sent = self.headers.get("X-Cogitator-Token") or ""
        if not sent or not TOKEN:
            return False
        try:
            return hmac.compare_digest(sent.encode("utf-8"), TOKEN.encode("utf-8"))
        except (UnicodeError, TypeError):
            return False

    def _privileged(self):
        """Every POST on the sidecar is privileged; GET /health is not."""
        return self.command == "POST"

    def _origin_allowed(self):
        if ALLOW_ANY_ORIGIN:
            return True
        origin = self.headers.get("Origin")
        if not origin:
            # Absent Origin on a PRIVILEGED route is refused (CR-Nanites-harness-0017):
            # only curl and native callers send none, and they must carry the token.
            # GET /health keeps treating it as allowed, so the settings panel's status
            # line is alive before the operator has paired.
            return not self._privileged()
        if origin == "null":
            # A browser sends "null" for a sandboxed iframe, a data:/blob:
            # document, or a file:// page - i.e. any hostile page can get an
            # opaque origin. Trust it only behind the explicit opt-in.
            return ALLOW_FILE_ORIGIN
        low = origin.lower()
        return low.startswith("http://localhost:") or low.startswith("http://127.0.0.1:") or low in ("http://localhost", "http://127.0.0.1")

    def _auth_gate(self):
        """Return an error string when the request must be refused, else None.

        The three checks are ANDed on a privileged route: a correct token never
        buys a past a foreign Origin, and a local Origin never buys a past a
        missing token.
        """
        if not self._host_allowed():
            return "host not permitted"
        if self._privileged() and not self._token_ok():
            return "X-Cogitator-Token required"
        if not self._origin_allowed():
            return "origin not permitted"
        return None

    def _cors(self, reflect=True):
        """Reflect the SPECIFIC request Origin. Never `*`.

        `reflect=False` on a refusal: a wildcard ACAO on a 403 is part of what
        CR-Nanites-harness-0017 calls out, so no ACAO is emitted at all then.
        ALLOW-Methods/ALLOW-HEADERS stay unconditional - they leak nothing and
        the preflight needs them (X-Cogitator-Token especially, or the browser
        refuses the real request and the UI breaks silently).
        """
        if reflect:
            origin = self.headers.get("Origin")
            if origin and self._origin_allowed():
                self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Cogitator-Token")

    def _json(self, code, obj, cors=True):
        body = json.dumps(obj).encode()
        self.send_response(code); self._cors(cors)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def do_OPTIONS(self):
        # A preflight cannot carry the token's VALUE, so the token check is not
        # performed here - the real request is where it is enforced. The Host and
        # Origin checks still gate the 204.
        if not self._host_allowed():
            self._json(403, {"ok": False, "error": "host not permitted"}, cors=False); return
        if not self._origin_allowed():
            self._json(403, {"ok": False, "error": "origin not permitted"}, cors=False); return
        self.send_response(204); self._cors(True); self.end_headers()

    def _route(self):
        return self.path.split('?', 1)[0].rstrip('/') or '/'

    def do_GET(self):
        # Host + token + Origin gate FIRST (Phase 10 invariant): a foreign Origin
        # or a missing token => 403, not 404.
        denied = self._auth_gate()
        if denied:
            self._json(403, {"ok": False, "error": denied}, cors=False); return
        if self._route() == '/health':
            self._json(200, health_obj())
        else:
            self._json(404, {"ok": False, "error": "unknown rite"})

    def _read_body(self):
        """Size cap BEFORE routing (413 must outrank 404). Returns (obj, error_code).

        The Content-Length is validated as a WHOLE number in [0, MAX_JSON_BYTES] and
        anything outside that is a 400, not a body. Three ways it used to go wrong
        (CR-Nanites-harness-0025, LE-1), all of which let an unread request continue
        as though it had arrived:
          * a NEGATIVE value passed `if n > MAX_JSON_BYTES` and reached
            `self.rfile.read(-1)`, which reads until EOF - an unbounded read, and a
            hang for as long as the client holds the socket open;
          * a NON-NUMERIC value raised ValueError, was swallowed into `n = 0`, and
            returned ({}, None) - so /repair answered a success-shaped 200 for a
            body that was never read;
          * a VALID positive length whose bytes never arrived took the same route.
            `except Exception: raw = b''` turned a socket error into an empty body,
            and `if not raw: return {}, None` then treated a SHORT read (a client
            truncated mid-flight, or a peer that half-closed early) as a legitimately
            empty body. Refusing is the honest answer: the bytes that were promised
            never arrived, so there is nothing to serve.

        The rule this function now implements: a length of 0, or NO length at all,
        is an empty body (every route validates its own fields, so that is legal);
        but `n > 0` means the caller PROMISED `n` bytes, and anything short of that -
        including a read that raises - is a truncated request and a 400. The error is
        a distinct 'truncated' code rather than the plain 400 for bad JSON, because
        those are different diagnoses: the payload may be perfectly well formed and
        merely incomplete.
        """
        raw_len = self.headers.get("Content-Length")
        if raw_len is None:
            # No length at all: an empty body is a legitimate request on these
            # routes (every one of them validates its own fields), so this is NOT
            # a refusal - only a length that was stated and is unusable is.
            return {}, None
        try:
            n = int(str(raw_len).strip())
        except (TypeError, ValueError):
            return None, 'bad-length'
        if n < 0:
            return None, 'bad-length'    # read(-n) is not a bounded read; refuse
        if n > MAX_JSON_BYTES:
            return None, 413
        if n == 0:
            return {}, None
        try:
            raw = self.rfile.read(n)
        except Exception:
            # A read that RAISES is an I/O failure, not an empty body. Swallowing it
            # into b'' is what produced the success-shaped 200 for a request whose
            # body was never read.
            return None, 'truncated'
        if not raw or len(raw) < n:
            # Promised n bytes, got fewer: the request was cut off. This is the case
            # the phase-22 length suite could not see, because every case it wrote
            # used an UNUSABLE length rather than a short one.
            return None, 'truncated'
        try:
            obj = json.loads(raw.decode('utf-8', 'replace'))
            return (obj if isinstance(obj, dict) else {}), None
        except Exception:
            return None, 400

    def do_POST(self):
        # Host + token + Origin gate FIRST (Phase 10 invariant): a foreign Origin
        # or a missing token => 403, not 404.
        denied = self._auth_gate()
        if denied:
            self._json(403, {"ok": False, "error": denied}, cors=False); return
        body, err = self._read_body()
        if err == 413:
            self._json(413, {"ok": False, "error": "request exceeds %d byte limit" % MAX_JSON_BYTES}); return
        route = self._route()
        if err == 'bad-length':
            # Distinct from "your body was not JSON": one is a framing bug the
            # caller can fix by not sending that header value, the other is a
            # payload bug. Saying which is the difference between a diagnosable
            # 400 and a mystery.
            self._json(400, {"ok": False, "error": "invalid Content-Length"}); return
        if err == 400:
            self._json(400, {"ok": False, "error": "invalid JSON body"}); return
        if err == 'truncated':
            # LE-1. A positive Content-Length whose bytes never arrived: the client was
            # cut off mid-body, or the socket raised. Refused BEFORE the route runs,
            # so no handler sees a half-body and - the part that matters - no ledger
            # record is written for a request that never arrived. Kept distinct from
            # the "invalid JSON body" 400 above: the payload may be perfectly well
            # formed and merely incomplete, and a caller can only fix this by resending
            # the whole request.
            self._json(400, {"ok": False,
                             "error": "request body truncated: Content-Length promised "
                                      "%s bytes that never arrived"
                                      % self.headers.get("Content-Length")})
            return
        if route == '/repair':
            tid = body.get('trace_id') or _new_trace_id()
            res = _repair_call(body.get('suspect'), body.get('candidates'),
                               NEEDLE_TIMEOUT_MS, tid)
            # One ledger record per /repair call (the future fine-tuning corpus).
            #
            # The BUILD is inside the try as well as the append, and that is the
            # whole point (CR-Nanites-harness-0022): a record is constructed as an
            # ARGUMENT EXPRESSION, so any failure while building it happens BEFORE
            # append_record is entered and therefore escapes the blanket except
            # inside it. `append_record never raises` is true and was never enough -
            # only "the whole build-and-append never raises" is. The response below
            # is computed and is correct regardless of what the telemetry does.
            out = res.get('calls') if res.get('ok') else {'degraded': res.get('reason')}
            try:
                ledger.append_record({
                    'trace_id': tid, 'model': 'needle', 'op': 'repair',
                    'input_redacted': True,
                    'request': {'suspect': body.get('suspect'),
                                'candidates': _candidate_names(body.get('candidates'))},
                    'output': out,
                    'confidence': res.get('confidence'),
                    'latency_ms': res.get('latency_ms'),
                    'degraded': bool(res.get('degraded')),
                    'action': None,
                }, path=LEDGER_PATH)
            except Exception as e:
                sys.stderr.write('[LOCALMODELS] repair ledger skipped: %s: %s\n'
                                 % (type(e).__name__, e))
            self._json(200, res); return
        if route == '/select':
            tid = body.get('trace_id') or _new_trace_id()
            res = _select_call(body.get('input'), body.get('candidates'),
                               NEEDLE_SELECT_TIMEOUT_MS, tid)
            # One ledger record per /select proposal; the operator's ACCEPT/IGNORE arrives
            # later as a POST /ledger outcome line on the same trace_id. Build inside the
            # try for the reason given on /repair (CR-0022).
            out = res.get('calls') if res.get('ok') else {'degraded': res.get('reason')}
            try:
                ledger.append_record({
                    'trace_id': tid, 'model': 'needle', 'op': 'select',
                    'input_redacted': True,
                    'request': {'input': body.get('input'),
                                'candidates': _candidate_names(body.get('candidates'))},
                    'output': out,
                    'confidence': res.get('confidence'),
                    'latency_ms': res.get('latency_ms'),
                    'degraded': bool(res.get('degraded')),
                    'action': None,
                }, path=LEDGER_PATH)
            except Exception as e:
                sys.stderr.write('[LOCALMODELS] select ledger skipped: %s: %s\n'
                                 % (type(e).__name__, e))
            self._json(200, res); return
        if route == '/decide':
            tid = body.get('trace_id') or _new_trace_id()
            res = _laya_decide(body.get('state'), body.get('questions'), LAYA_TIMEOUT_MS, tid)
            # One ledger record per /decide call, carrying the child's raw answers INCLUDING
            # the probability distributions (the future fine-tuning corpus). Build inside
            # the try for the reason given on /repair (CR-0022).
            try:
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
            except Exception as e:
                sys.stderr.write('[LOCALMODELS] decide ledger skipped: %s: %s\n'
                                 % (type(e).__name__, e))
            self._json(200, res); return
        if route == '/ledger':
            tid = body.get('trace_id')
            action = body.get('action')
            if not isinstance(tid, str) or not tid or not isinstance(action, str) or not action:
                self._json(400, {"ok": False, "error": "trace_id + action required"}); return
            # Server-side vocabulary check (CR-Nanites-harness-0006). The old gate was
            # `isinstance(action, str) and action`, so "accepted " (trailing space) and
            # "ACCEPTED" were written to the ledger. `_ledger_counts` ignores them, but
            # `tools/tune_thresholds.py` keeps its OWN copy of this vocabulary, and the
            # acceptance rate Phase 15's tuning rests on is computed from it: an unknown
            # word one side ignores and the other folds in silently moves the measured
            # rate. Refusing is the honest answer, and it is refused HERE rather than
            # left to the reader.
            #
            # Validation is on the KEYS. `_LEDGER_ACTIONS` maps an action word to the
            # folded counter it contributes to ('accepted_by_operator' -> 'accepted'),
            # and it is the keys that are the vocabulary.
            if action not in _LEDGER_ACTIONS:
                self._json(400, {"ok": False,
                                 "error": "unknown action %r; expected one of %s"
                                          % (action, ', '.join(sorted(_LEDGER_ACTIONS)))}); return
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
    global TOKEN, TOKEN_PATH
    global ALLOW_ANY_ORIGIN, ALLOW_FILE_ORIGIN, NEEDLE_TIMEOUT_MS, NEEDLE_SELECT_TIMEOUT_MS
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
    ap.add_argument("--needle-select-timeout-ms", type=int, default=800,
                    help="bounded timeout around ONE /select proposal (default 800 ms); "
                         "overrun => degraded with calls:[], never a 500, never a wedge")
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
    NEEDLE_SELECT_TIMEOUT_MS = a.needle_select_timeout_ms
    LAYA_TIMEOUT_MS = a.laya_timeout_ms
    LAYA_IDLE_S = a.laya_idle_s
    if a.laya_child:
        LAYA_CHILD_PATH = os.path.abspath(a.laya_child)
    LEDGER_PATH = a.ledger or ledger.default_path()
    ALLOW_ANY_ORIGIN = a.allow_any_origin
    ALLOW_FILE_ORIGIN = a.allow_file_origin
    TOKEN = load_or_create_token(TOKEN_PATH)
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
    print("   select     : %d ms per proposal (F3 pre-router)" % NEEDLE_SELECT_TIMEOUT_MS)
    print("   laya       : %s" % (("enabled (lazy, %s engine)" % ('node + child present' if engine_ok
                                                                 else 'ENGINE MISSING'))
                                  if LAYA_ENABLED else "disabled"))
    if LAYA_ENABLED:
        print("   laya child : %s" % LAYA_CHILD_PATH)
        print("   laya budget: %d ms per request, reaped after %d s idle"
              % (LAYA_TIMEOUT_MS, LAYA_IDLE_S))
        print("   laya cache : %s" % laya_cache_path())
    print("   origins    : %s" % origins_desc())
    # Never the token itself - only the path to read it from.
    print("   token file : %s" % TOKEN_PATH)
    print("   paste      : cat \"%s\"" % TOKEN_PATH)
    print("   ledger     : %s" % os.path.abspath(LEDGER_PATH))
    print("   telemetry  : NEEDLE_TELEMETRY=%s (nothing leaves the machine)"
          % os.environ.get('NEEDLE_TELEMETRY'))
    print("   health     : http://localhost:%d/health" % PORT)
    print("=" * 56)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
