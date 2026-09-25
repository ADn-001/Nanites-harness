# LOCAL CORTEX — local-model tool-call middleware

LOCAL CORTEX is an **optional** sidecar for the COGITATOR harness. When enabled it can

- **repair** malformed tool calls with a small local model (Needle), and
- **gate** risky dispatches / odd replies with a second local model (Laya).

It is a convenience layer, never a requirement. The harness is fully functional with both
models absent, unloaded, or crashed: every feature degrades to plain deterministic
behaviour (pass the original reply through and flag it).

## Privacy

- **Nothing leaves the machine.** There is no external service, endpoint, router or cloud
  fallback anywhere in this package — localhost only. The only network use is the one-time
  weight download at install/first run, and only if you ask for it.
- The middleware **never sees or forwards your provider API key** (`settings.apiKey`).
- The **ledger is local and redacted**: `var/local-models.jsonl` (git-ignored). The writer
  in `ledger.py` strips `Authorization`/`Bearer`/`sk-…` values, the configured API key, and
  absolute home paths, and truncates any single field to 2000 characters.

## Opt-in, off by default

Both models are **off by default**. A fresh install — or a friend's install — behaves
exactly as it did before LOCAL CORTEX existed. Nothing is downloaded, spawned or loaded
until the operator turns it on (`settings.localModels`, the LOCAL CORTEX section in
RITES/CONFIG) or starts this daemon deliberately.

## Run the sidecar

```bash
python localmodels/local_models_daemon.py                     # 127.0.0.1:8932
python localmodels/local_models_daemon.py --no-needle         # Needle disabled, Laya optional
python localmodels/local_models_daemon.py --no-needle --no-laya
python localmodels/local_models_daemon.py --ledger var/local-models.jsonl
python localmodels/local_models_daemon.py --preload-needle    # warm the engine at boot
python localmodels/local_models_daemon.py --needle-timeout-ms 1200
```

Flags: `--port` (default 8932), `--no-needle`, `--no-laya`, `--ledger PATH`,
`--allow-any-origin`, `--allow-file-origin`, `--needle-timeout-ms` (default 800 — the
BOUNDED TIMEOUT around the model call), `--preload-needle` (eager background load at
boot). Absence of `--no-needle` means the engine is *enabled*; it is *loaded* lazily on
the first `/repair` (or at boot with `--preload-needle`).

### Routes

`GET /health`:

```json
{"ok": true, "version": "0.1.0",
 "needle": {"enabled": true, "loaded": false, "weights": "missing", "generation": 3, "lib": null},
 "laya":   {"enabled": true, "loaded": false, "child_pid": null, "cache": "~/.cache/receptron-laya"},
 "ledger": {"path": "/abs/path/var/local-models.jsonl", "writable": true},
 "degraded": ["needle weights missing"]}
```

`needle.weights` is a REAL probe (Phase 12): `NEEDLE_WEIGHTS` (a file path) first, else
the needle package's own cache path (`~/.cache/cactus-needle/v3/<version>/needle3.cact`).
A daemon run with the SYSTEM python has no needle package, so the probe answers
`'missing'` — that is the intended degraded-posture proof.

`degraded` lists one reason per **enabled** feature that cannot serve right now. A feature
you deliberately disabled with `--no-needle`/`--no-laya` is not degraded — it is off.

`POST /repair` (Phase 12):

```json
{"suspect": {"name": "read-file", "arguments": "{\"path\": \"a.py\"}"},
 "candidates": [{"name": "read_file", "description": "...", "parameters": {...}}, ...],
 "trace_id": "tr…"}  // optional
```

→ `{ok, calls: [{name, arguments}], confidence, reasoning, latency_ms, trace_id,
degraded?, reason?}`. **`calls: []` means "no repair"** — the daemon never manufactures
a call. `confidence: null` means the weights carry no confidence head; treat it as
below-threshold. The candidate schemas are the FLAT shape (`{name, description,
parameters}`); an OpenAI-shaped entry (`{type:'function', function:{…}}`) is unwrapped.
The model call runs on a single worker inside `--needle-timeout-ms`; an overrun (or the
package/weights being absent) answers `{ok:false, degraded:true}` — never a 500, never a
wedged socket. Every call appends one redacted ledger record
(`{trace_id, model:'needle', op:'repair', input_redacted, request, output, confidence,
latency_ms, degraded, action:null}`).

`POST /ledger` appends the matching outcome line `{trace_id, action, note?}` → `{ok:true}`.

Any other path answers `404 {"ok": false, "error": "unknown rite"}`.

### Security conventions

Copied from `bridge.py`: the Origin guard is the first statement of every handler
(`Origin` absent ⇒ allowed for curl/native callers; `null` ⇒ refused unless
`--allow-file-origin`; `http://localhost[:port]` / `http://127.0.0.1[:port]` ⇒ allowed;
anything else ⇒ `403 {"ok": false, "error": "origin not permitted"}`), CORS headers,
`OPTIONS` ⇒ 204, and a 1 MiB POST size cap (`413` past it). The daemon binds `127.0.0.1`
only. Unlike `bridge_daemon.py` it writes **no pid or log file** — the ledger is the only
file it ever writes.

## Install the models (Phase 12/13 do this deliberately — nothing runs automatically)

```bash
bash localmodels/setup.sh     # Linux/macOS  — installs the Needle venv + weights + engine
```

```powershell
powershell -ExecutionPolicy Bypass -File localmodels/setup.ps1   # Windows
```

Both scripts create `localmodels/.venv`, upgrade `pip`, `pip install cactus-needle` and
then REALLY download the base weights + engine library into the package's cache dir
(`localmodels/fetch_engine.py` → `~/.cache/cactus-needle/v3/<version>/`; idempotent —
a cached file is skipped). If the exact engine version was never published as a wheel,
the helper lists the repo's `python/` directory and extracts `libneedle3.*` from the
highest version matching this machine's platform tag, so install does not silently
break. `NEEDLE_WEIGHTS` may point at a specific `.cact` instead of the cache.
Laya additionally needs Node ≥ 20; its weights land in `~/.cache/receptron-laya`
(`LAYA_CACHE` overrides). **Run the daemon with the venv python**
(`localmodels/.venv/bin/python localmodels/local_models_daemon.py`; it sets
`NEEDLE_TELEMETRY=0` itself, so nothing about a repair leaves the machine).

## Layout

| File | Role |
|---|---|
| `local_models_daemon.py` | Origin-guarded `ThreadingHTTPServer` on 127.0.0.1:8932 (`/health`, `/repair`, `/ledger`) |
| `needle_backend.py` | lazy, lock-serialised Needle wrapper (`repair`, `weights_present`, `lib_present`) |
| `ledger.py` | append-only redacted JSONL writer (`new_trace_id`, `append_record`, `redact`) |
| `setup.sh` / `setup.ps1` | installers for Needle (and the Laya npm step) |
| `fetch_engine.py` | weights + engine download helper (version-fallback wheel scan) |
| `README.md` | this file |

Tracked files here hold no hostnames, LAN IPs, keys or absolute personal paths — the repo
is share-ready; machine-specific state lives in `var/`, `localmodels/.venv/` and the model
caches, all git-ignored.
