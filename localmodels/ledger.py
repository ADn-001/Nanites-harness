#!/usr/bin/env python3
"""
LOCAL CORTEX ledger - append-only, redacted JSONL writer.

Every local-model decision (repair, decision, proposal, outcome) is appended as one
compact JSON line. The ledger is the audit trail AND the future fine-tuning corpus,
so it must never contain a secret and never leave the machine.

Rules actually enforced here, asserted by test_e2e.py:
  1. NOT ENFORCED, and no code path in production provides it: the configured provider
     API key is NOT replaced with '[REDACTED-KEY]'. `redact()`/`_redact_string()` honour
     an `api_key=` argument and it works (asserted by test_e2e.py), but the sidecar
     never learns the key: it lives in the browser as `settings.apiKey`, is sent only to
     the provider endpoint, and no env var, CLI flag, request field or config file
     carries it here. So the honest statement is that the redaction of the configured
     provider key DOES NOT HAPPEN, because nothing supplies the key. This line is kept
     deliberately: deleting it would make an absent guarantee indistinguishable from an
     undocumented one. If a configuration path for the key is ever wired up, pass
     `api_key=` at that call site and change this line back to a claim.
  2. `Authorization: *** / `Bearer ...` values become '[REDACTED-AUTH]'
  3. anything shaped like `sk-xxxxxxxx` becomes '[REDACTED-KEY]'
  4. the user's home directory becomes '~'
  5. any single string field longer than 2000 chars is truncated with '…[truncated N]'
  6. a dict key named authorization/api_key/apikey/x-api-key always redacts its VALUE

Rules 3 and 6 are what actually keep a provider key out of the ledger in production: a
key echoed into a request string matches `sk-…`, and a key arriving under any of those
field names is redacted regardless of whether anyone passed `api_key=`.

`append_record` never raises: a broken ledger must not break a request path, it only
logs to stderr and returns False.

No network. No third-party imports. The ledger is the ONLY file this package writes.
"""
import json
import os
import re
import secrets
import sys
import tempfile
import threading
from datetime import datetime, timezone

# Never drop a __pycache__ into a share-ready working tree: this module is imported by
# the sidecar (and by tests) from inside the repo.
sys.dont_write_bytecode = True

DEFAULT_PATH = 'var/local-models.jsonl'
MAX_FIELD_CHARS = 2000
REDACTED_AUTH = '[REDACTED-AUTH]'
REDACTED_KEY = '[REDACTED-KEY]'
SECRET_KEYS = {'authorization', 'api_key', 'apikey', 'x-api-key'}
#: The ledger is compacted in place once the active file passes this many bytes
#: (CR-Nanites-harness-0006). 8 MiB is far above a normal session's audit trail and
#: low enough that the file is never a problem to copy or ship. Overridable per call
#: via `append_record(max_bytes=...)`, which is what the test suite exercises.
MAX_BYTES = 8 * 1024 * 1024
#: Where a backwards-chunked read of the ledger starts. Matching the daemon's own
#: first window keeps the two readers' shape identical.
TAIL_START_BYTES = 65536
#: Temp-file PREFIX used while compacting, and the prefix any STALE left-over shares.
#: `_sweep_stale_temp_files` matches on this, so the name is deliberately the same for
#: every rotation - the uniqueness lives in the SUFFIX, so a crash left-over from an
#: older build is still recognised and swept.
ROTATING_PREFIX = '.rotating'
#: Serialises the whole append-then-maybe-rotate sequence (LE-2).
#:
#: Without it, rotation reads the file size, seeks, reads a window, writes a temp file
#: and os.replace()s it over the target - and any record appended by another thread
#: between that size read and the replace is silently DISCARDED, because the replace
#: swaps in a file that does not contain it. Two concurrent rotations also shared the
#: one fixed temp name, so one os.replace consumed the other's file and the loser got
#: a FileNotFoundError swallowed by the blanket except. `append_record` returned True
#: in every one of those cases: a record reported as written, absent from the audit
#: trail. This is reachable in production - the sidecar is a ThreadingHTTPServer.
#:
#: Module-level on purpose: the lock must be shared by every `append_record` in the
#: process, not per-file, because `default_path()` can point several threads at the
#: same ledger from different call sites and a per-path lock table would need its own
#: synchronisation to be worth anything.
_APPEND_LOCK = threading.Lock()

# Authorization: <anything-not-a-newline>   |   Bearer <anything-not-a-newline>
_AUTH_RE = re.compile(r'authorization\s*:\s*[^\r\n]+|bearer\s+\S+', re.IGNORECASE)
_SK_RE = re.compile(r'sk-\S{8,}')


def new_trace_id():
    """Short unique id correlating a request, its ledger record and its outcome."""
    return 'tr' + secrets.token_hex(5)


def default_path():
    return os.environ.get('LEDGER_PATH') or DEFAULT_PATH


def _redact_string(s, api_key=None, home=None):
    if api_key and len(api_key) >= 8:
        s = s.replace(api_key, REDACTED_KEY)
    s = _AUTH_RE.sub(REDACTED_AUTH, s)
    s = _SK_RE.sub(REDACTED_KEY, s)
    home = home or os.path.expanduser('~')
    if home:
        s = s.replace(home, '~')
    if len(s) > MAX_FIELD_CHARS:
        s = s[:MAX_FIELD_CHARS] + '…[truncated %d]' % (len(s) - MAX_FIELD_CHARS)
    return s


def redact(value, api_key=None, home=None):
    """Recursively redact a JSON-shaped value. Non-strings pass through unchanged."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and k.lower() in SECRET_KEYS:
                out[k] = REDACTED_AUTH
            else:
                out[k] = redact(v, api_key=api_key, home=home)
        return out
    if isinstance(value, list):
        return [redact(v, api_key=api_key, home=home) for v in value]
    if isinstance(value, str):
        return _redact_string(value, api_key=api_key, home=home)
    return value


def redact_record(record, api_key=None, home=None):
    """Redact a whole record dict. Always returns a dict."""
    red = redact(record, api_key=api_key, home=home)
    return red if isinstance(red, dict) else {'record': red}


def append_record(record, path=None, api_key=None, home=None, max_bytes=None):
    """Redact, timestamp and append one JSONL line. Never raises; returns True/False.

    SIZE-BASED ROTATION (CR-Nanites-harness-0006). The ledger is append-only and used
    to grow without bound, so once the active file passes `max_bytes` (default
    MAX_BYTES) it is compacted IN PLACE: the OLDEST complete lines are dropped and the
    MOST RECENT ones are kept.

    Three deliberate decisions:

    * **In place, not a rotated sibling file.** The sidecar's own directory is
      asserted to hold exactly two filenames (`local-models.jsonl` and
      `local-models-2.jsonl` in test_e2e.py's `lm_no_stray_files`), so writing
      `local-models.jsonl.1` would break a real, existing invariant. Compacting in
      place is therefore right - but it is NOT true that it "introduces no filename
      at all", and this docstring used to say so. Rotation DOES create one temp file
      in that same directory, and it is SHORT-LIVED: a `<ledger>.rotating.<random>`
      name, renamed over the target with os.replace. So the honest form of the
      guarantee is "no PERMANENT third filename", not "no filename": the name exists
      between open(tmp) and os.replace, and a crash inside that window can leave it
      behind. That is why the temp file is removed in a `finally` in
      `_rotate_if_oversize` (the remove is a no-op after a successful rename), and why
      `_sweep_stale_temp_files` deletes any stale `*.rotating*` left-over on the next
      append instead of letting `lm_no_stray_files` fail for that install permanently.
    * **Keep the newest, drop the oldest.** The reverse - keeping the oldest and
      discarding what the operator just recorded - would be worse than no rotation:
      it would silently destroy the current session's evidence while looking healthy.
    * **The append and the rotation happen under one lock** (`_APPEND_LOCK`, LE-2).
      Rotation reads the size, rewrites the tail and os.replace()s the result over the
      target, so an append landing in that window is discarded by the replace. An
      unlocked append could report True for a record that was never in the file -
      silent loss on an append-only audit trail, which is the worst outcome available.

    The compaction is best-effort and inside the same never-raise try: a ledger that
    cannot be compacted still gets its line appended. Only whole lines are ever
    dropped, so the file is never left starting mid-record.
    """
    try:
        target = path or default_path()
        rec = redact_record(record, api_key=api_key, home=home)
        if not rec.get('ts'):
            rec['ts'] = datetime.now(timezone.utc).isoformat()
        parent = os.path.dirname(os.path.abspath(target))
        if parent:
            os.makedirs(parent, exist_ok=True)
        line = json.dumps(rec, ensure_ascii=False, sort_keys=True)
        # One lock around the append AND the rotate decision (LE-2). Not just around
        # the append: rotation reads the size and then os.replace()s a file that does
        # not contain anything appended after that read, so the two must not interleave.
        with _APPEND_LOCK:
            with open(target, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
                f.flush()
                os.fsync(f.fileno())
            _sweep_stale_temp_files(target)
            _rotate_if_oversize(target, max_bytes or MAX_BYTES)
        return True
    except Exception as e:  # never raise into the request path
        sys.stderr.write('[LOCALMODELS] ledger append failed: %s: %s\n' % (type(e).__name__, e))
        return False


def _sweep_stale_temp_files(target):
    """Remove any `<target><ROTATING_PREFIX>*` left behind by an interrupted rotation.

    A crash between `open(tmp, 'w')` and `os.replace(tmp, target)` leaves the temp
    name behind, and `lm_no_stray_files` - which allows exactly two filenames in the
    sidecar's directory - would then fail for that install permanently. Matched on the
    shared PREFIX rather than the exact name, because temp names are now unique per
    rotation (LE-2), so the survivor of any given crash is unpredictable; the old
    fixed `target + '.rotating'` name is included because that is the shape a crash
    from a PREVIOUS build leaves.

    Called with `_APPEND_LOCK` held, so it cannot delete the temp file a rotation in
    this process is legitimately writing. Never raises: a sweep failure must not fail
    the append that triggered it.
    """
    parent = os.path.dirname(os.path.abspath(target)) or '.'
    prefix = os.path.basename(target) + ROTATING_PREFIX
    try:
        for name in os.listdir(parent):
            if not name.startswith(prefix):
                continue
            try:
                os.remove(os.path.join(parent, name))
            except OSError as e:
                sys.stderr.write('[LOCALMODELS] stale rotation temp file %s not removed: '
                                 '%s: %s\n' % (name, type(e).__name__, e))
    except Exception as e:
        sys.stderr.write('[LOCALMODELS] stale rotation sweep skipped: %s: %s\n'
                         % (type(e).__name__, e))


def _rotate_if_oversize(target, max_bytes):
    """Compact `target` in place to its most recent lines once it passes `max_bytes`.

    Keeps the newest whole lines and drops the oldest, so the surviving content is
    the most recent. NEVER raises: a ledger that cannot be rotated is still a usable
    append-only ledger, and failing the rotation must not fail the append that
    triggered it (that is the caller's return value, not this function's).

    The read is bounded by seeking to `size - max_bytes` from EOF - the same
    backwards-chunked read `_ledger_tail` uses - so rotating a huge ledger does not
    read the whole thing either.

    CALLER CONTRACT: `_APPEND_LOCK` must be held. This function is not safe to call
    concurrently on the same `target` and there is nothing to stop it: the whole
    point of the lock is that the size read, the window read, the temp write and the
    os.replace cannot have another writer interleaved between them.

    The temp file is created with `tempfile.mkstemp` in the target's OWN directory, so
    the rename stays within one filesystem (os.replace is not atomic across mounts)
    and the name is unique per rotation - two concurrent rotations can no longer
    consume each other's file. It is removed in a `finally`: after a SUCCESSFUL
    os.replace the name is already gone (the rename moved it), so the remove is a
    no-op, and after any FAILURE it is what stops a half-written compaction from
    becoming a permanent third filename in a directory an existing invariant counts.
    """
    tmp = None
    try:
        if max_bytes <= 0:
            return False
        with open(target, 'rb') as f:
            size = f.seek(0, os.SEEK_END)
            if size <= max_bytes:
                return False
            window = min(max_bytes, TAIL_START_BYTES)
            while True:
                f.seek(size - window)
                chunk = f.read(window)
                if len(chunk) < window or window >= max_bytes:
                    break
                window = min(window * 2, max_bytes)
            # The window starts mid-line, so the first element is a partial record:
            # drop it, or the file would begin with half a JSON object.
            lines = chunk.decode('utf-8', 'replace').splitlines()
            if size - window > 0:
                lines = lines[1:]
        if not lines:
            return False
        kept = '\n'.join(lines) + '\n'
        # Unique per rotation, and in the SAME directory as the target: a crash
        # mid-compaction can then never leave a half-written ledger, and os.replace
        # can rename within the one filesystem it requires.
        parent = os.path.dirname(os.path.abspath(target)) or '.'
        fd, tmp = tempfile.mkstemp(dir=parent,
                                   prefix=os.path.basename(target) + ROTATING_PREFIX + '.')
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(kept)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
        tmp = None
        return True
    except Exception as e:
        sys.stderr.write('[LOCALMODELS] ledger rotation skipped: %s: %s\n'
                         % (type(e).__name__, e))
        return False
    finally:
        # Only reached with a name still on disk, i.e. os.replace did NOT run.
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
