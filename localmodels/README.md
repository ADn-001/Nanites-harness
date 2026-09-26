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
python localmodels/local_models_daemon.py --laya-timeout-ms 1500 --laya-idle-s 300
```

Flags: `--port` (default 8932), `--no-needle`, `--no-laya`, `--ledger PATH`,
`--allow-any-origin`, `--allow-file-origin`, `--needle-timeout-ms` (default 800 — the
BOUNDED TIMEOUT around the model call), `--preload-needle` (eager background load at
boot), `--laya-timeout-ms` (default 500 — the BOUNDED TIMEOUT around one Laya child
request), `--laya-idle-s` (default 120 — kill the Laya child after N idle seconds, `0`
disables reaping), `--laya-child PATH` (default `<daemon dir>/laya_child.mjs`; the Node
binary comes from `LAYA_NODE`, default `node`). Absence of `--no-needle` means the engine
is *enabled*; it is *loaded* lazily on the first `/repair` (or at boot with
`--preload-needle`).

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

`laya.child_pid` is the live Node child's pid (or `null`); `laya.loaded` is true only once
the child has actually reported a loaded model. `laya.cache` is the cache dir **as you
wrote it** (`LAYA_CACHE`, else the `~/.cache/receptron-laya` string form) — never an
expanded absolute home path. **`/health` never spawns the child**, so a status probe can
never trigger a 1.7 GB download.

`degraded` lists one reason per **enabled** feature that cannot serve right now. A feature
you deliberately disabled with `--no-needle`/`--no-laya` is not degraded — it is off. A
Laya that is merely *not started yet* is **lazy**, not degraded: the child is spawned on
demand by the first `/decide`. An ENABLED Laya is degraded only when it cannot serve at
all — no `node` on `PATH` or no child script on disk (`'laya engine missing'`).

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

`POST /decide` (Phase 13) — batched calibrated decisions from **Laya**:

```json
{"state": {"utterance": "write the cleaned log to out/final.log", "tool": "write_file"},
 "questions": {"gate": {"type": "noul", "instructions": "Do these arguments plausibly satisfy the schema?",
                        "criteria": "true when the arguments look like a valid invocation"},
               "severity": {"type": "score", "instructions": "How severe is this?",
                            "criteria": ["harmless", "mild", "risky", "destructive"]},
               "kind": {"type": "choice", "instructions": "Which kind of tool is this?",
                        "criteria": {"read": "reads data", "write": "mutates data"}}},
 "trace_id": "tr…"}  // optional
```

→ `{ok, answers: {gate: {noul}, severity: {score}, kind: {choice, probabilities}},
usage: {input_tokens}, latency_ms, trace_id, degraded?, reason?}`. `state` and `questions`
are passed straight through to the package's real
`laya.systemOne(state, questions)`; the package's own envelope is the contract. Every
question of one call is batched into a **single forward pass**, so send a whole turn's
questions in ONE request.

The child (`laya_child.mjs`) answers exactly one JSON line per inbound line:

```
{id, op:'health'}                    -> {id, ok:true, loaded:<bool>, pid, cache, node}
{id, op:'load'}                      -> {id, ok:true, loaded:true, load_ms}
{id, op:'decide', state, questions}  -> {id, ok:true, answers, usage, latency_ms}
{id, op:'close'}                     -> {id, ok:true} then exit 0
```

Any other op → `{id, ok:false, error:'unknown op: …'}`; a handler exception → the same
shape with the message (the process stays alive); an unparseable line → `{id:null,
ok:false, error:'bad json'}`. EOF on stdin exits 0. **stdout carries protocol JSON and
nothing else** — `console.log`/`info`/`warn` are re-routed to stderr at the top of the
file, and the package is imported lazily inside the load path so a missing/incomplete
`node_modules` is a JSON error rather than a startup crash. Download progress goes to
stderr as JSON lines: `{"type":"progress", file, received, total}`. The model is loaded
lazily on the first `load`/`decide`; `health` reports `loaded:false` **without** loading.

Load options are exactly `{cacheDir: LAYA_CACHE || undefined, executionProviders: ['cpu'],
onProgress}`. There is **no** `precision`/`float16` option in the package — do not invent
one.

The daemon spawns ONE child lazily (first `/decide`), from a local file with `node` — no
network endpoint is ever contacted by the daemon. It reaps the child after `--laya-idle-s`
idle seconds (a polite `close`, then a kill) and respawns it on demand; on `SIGTERM`/exit
it always reaps, so no orphan `node` survives. Every failure mode answers HTTP 200 with
`{ok:false, degraded:true, reason:…}` — never a 500, never a hang:

| Situation | `reason` |
|---|---|
| `--no-laya` | `disabled` |
| no `node` on PATH, or the child script is missing | `engine_missing` |
| nothing answered inside `--laya-timeout-ms` | `timeout` |
| the child died mid-request (the next request respawns) | `child_gone` |
| the child itself answered `{ok:false, error:'…'}` | that error string |

Every `/decide` appends **one** redacted ledger record carrying the child's raw answers
(so it holds the probability distributions) plus `confidence` = the max numeric
`noul`/`score` seen, or `null` when there is nothing to grade — a choice-only batch is
never given an invented confidence:

```json
{"trace_id": "tr…", "model": "laya", "op": "decide", "input_redacted": true,
 "request": {"state": {…}, "questions": [{"key": "team", "type": "choice"}, …]},
 "output": {"answers": {"team": {"choice": "billing", "probabilities": {"billing": 0.5, "support": 0.5}}}},
 "confidence": null, "latency_ms": 2, "degraded": false, "action": null}
```

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
**Run the daemon with the venv python**
(`localmodels/.venv/bin/python localmodels/local_models_daemon.py`; it sets
`NEEDLE_TELEMETRY=0` itself, so nothing about a repair leaves the machine).

The same scripts also install **Laya** for real, as a separate optional step: they run
`npm install` **inside `localmodels/`** (its own `node_modules`, keeping the ~200 MB of
`onnxruntime-node` prebuilds out of the root install), which needs **Node ≥ 20**. Laya's
weights (about 1.7 GB) are **not** downloaded by the script — they are fetched on the
FIRST `Laya.load`, i.e. the first `/decide` (or `/load`), and cache under
`~/.cache/receptron-laya` (`LAYA_CACHE` overrides; the download reports progress as JSON
lines on the child's stderr). If `node` is absent the step is skipped with a warning and
everything else still works — Laya is optional. The daemon finds the child at
`localmodels/laya_child.mjs` and the Node binary via `LAYA_NODE` (default `node`).

### Known blocker: musl hosts (Alpine / postmarketOS)

`onnxruntime-node` publishes **one** Linux arm64 prebuild and it is linked against **glibc**.
On a musl host `require('onnxruntime-node')` therefore fails to relocate
(`__getauxval`/`fcntl64`/`open64`/… symbol not found). Preloading a glibc shim layer
(`gcompat` plus a small library exporting those aliases) makes the module *load*, but creating
the ONNX session then **segfaults** — so Laya cannot serve on such a machine. Everything else
below is unaffected: `/decide` answers `{ok:false, degraded:true}` in bounded time, the child is
reaped lazily, and the harness behaves exactly as it does with Laya switched off. A host with a
glibc runtime (any mainstream Linux, macOS, Windows) is unaffected. The live Laya suite fails
loudly in that case rather than skipping — set `COG_LIVE_LAYA_ALLOW_UNSERVABLE=1` to skip it
deliberately.

## Layout

| File | Role |
|---|---|
| `local_models_daemon.py` | Origin-guarded `ThreadingHTTPServer` on 127.0.0.1:8932 (`/health`, `/repair`, `/decide`, `/ledger`); owns the lazy Laya child + idle reaper |
| `laya_child.mjs` | Node ≥ 20 ESM child: NDJSON over stdio, lazy `Laya.load`, `systemOne` decisions, protocol JSON only on stdout |
| `needle_backend.py` | lazy, lock-serialised Needle wrapper (`repair`, `weights_present`, `lib_present`) |
| `ledger.py` | append-only redacted JSONL writer (`new_trace_id`, `append_record`, `redact`) |
| `setup.sh` / `setup.ps1` | installers for Needle (venv + weights + engine) and Laya (`npm install`) |
| `fetch_engine.py` | weights + engine download helper (version-fallback wheel scan) |
| `README.md` | this file |

`tests/fixtures/laya_stub_child.mjs` (in the test tree, not shipped) implements the same
protocol with no model and no downloads, so the daemon's `/decide`, timeout, `child_gone`
and reap paths are all covered by `python3 test_e2e.py`.

Tracked files here hold no hostnames, LAN IPs, keys or absolute personal paths — the repo
is share-ready; machine-specific state lives in `var/`, `localmodels/.venv/` and the model
caches, all git-ignored.
