#!/usr/bin/env python3
"""
LOCAL CORTEX ledger - append-only, redacted JSONL writer.

Every local-model decision (repair, decision, proposal, outcome) is appended as one
compact JSON line. The ledger is the audit trail AND the future fine-tuning corpus,
so it must never contain a secret and never leave the machine.

Rules (enforced here, asserted by test_e2e.py):
  1. the configured provider API key is replaced with '[REDACTED-KEY]'
  2. `Authorization: ...` / `Bearer ...` values become '[REDACTED-AUTH]'
  3. anything shaped like `sk-xxxxxxxx` becomes '[REDACTED-KEY]'
  4. the user's home directory becomes '~'
  5. any single string field longer than 2000 chars is truncated with '…[truncated N]'
  6. a dict key named authorization/api_key/apikey/x-api-key always redacts its VALUE

`append_record` never raises: a broken ledger must not break a request path, it only
logs to stderr and returns False.

No network. No third-party imports. The ledger is the ONLY file this package writes.
"""
import json
import os
import re
import secrets
import sys
from datetime import datetime, timezone

# Never drop a __pycache__ into a share-ready working tree: this module is imported by
# the sidecar (and by tests) from inside the repo.
sys.dont_write_bytecode = True

DEFAULT_PATH = 'var/local-models.jsonl'
MAX_FIELD_CHARS = 2000
REDACTED_AUTH = '[REDACTED-AUTH]'
REDACTED_KEY = '[REDACTED-KEY]'
SECRET_KEYS = {'authorization', 'api_key', 'apikey', 'x-api-key'}

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


def append_record(record, path=None, api_key=None, home=None):
    """Redact, timestamp and append one JSONL line. Never raises; returns True/False."""
    try:
        target = path or default_path()
        rec = redact_record(record, api_key=api_key, home=home)
        if not rec.get('ts'):
            rec['ts'] = datetime.now(timezone.utc).isoformat()
        parent = os.path.dirname(os.path.abspath(target))
        if parent:
            os.makedirs(parent, exist_ok=True)
        line = json.dumps(rec, ensure_ascii=False, sort_keys=True)
        with open(target, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
            f.flush()
            os.fsync(f.fileno())
        return True
    except Exception as e:  # never raise into the request path
        sys.stderr.write('[LOCALMODELS] ledger append failed: %s: %s\n' % (type(e).__name__, e))
        return False
