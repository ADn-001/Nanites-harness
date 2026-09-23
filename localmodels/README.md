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
```

Flags: `--port` (default 8932), `--no-needle`, `--no-laya`, `--ledger PATH`,
`--allow-any-origin`, `--allow-file-origin`. Absence of `--no-needle` means the engine is
*enabled* — but **the model is still not loaded** in Phase 10 (`needle.loaded=false`,
`laya.loaded=false`, `laya.child_pid=null`, always).

### Routes shipped in Phase 10

`GET /health` only:

```json
{"ok": true, "version": "0.1.0",
 "needle": {"enabled": true, "loaded": false, "weights": "missing", "generation": 3, "lib": null},
 "laya":   {"enabled": true, "loaded": false, "child_pid": null, "cache": "~/.cache/receptron-laya"},
 "ledger": {"path": "/abs/path/var/local-models.jsonl", "writable": true},
 "degraded": ["needle weights missing"]}
```

`degraded` lists one reason per **enabled** feature that cannot serve right now. A feature
you deliberately disabled with `--no-needle`/`--no-laya` is not degraded — it is off.
Repair/decide/select routes arrive in later phases; any other path answers
`404 {"ok": false, "error": "unknown rite"}`.

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
bash localmodels/setup.sh     # Linux/macOS
```

```powershell
powershell -ExecutionPolicy Bypass -File localmodels/setup.ps1   # Windows
```

Both scripts create `localmodels/.venv`, upgrade `pip` and `pip install cactus-needle`;
they then *print* the remaining steps (`needle download needle3`, and `npm install` inside
`localmodels/` for the optional Laya child). They do not download anything by themselves.
Weights land in `~/.cache/cactus-needle` (a `.cact` file; `NEEDLE_WEIGHTS` may point at it)
and `~/.cache/receptron-laya` (`LAYA_CACHE` overrides). Laya additionally needs Node ≥ 20;
Needle needs a Python runtime, which the bridge already requires.

## Layout

| File | Role |
|---|---|
| `local_models_daemon.py` | Origin-guarded `ThreadingHTTPServer` on 127.0.0.1:8932 |
| `ledger.py` | append-only redacted JSONL writer (`new_trace_id`, `append_record`, `redact`) |
| `setup.sh` / `setup.ps1` | documented installers for Needle (and the Laya npm step) |
| `README.md` | this file |

Tracked files here hold no hostnames, LAN IPs, keys or absolute personal paths — the repo
is share-ready; machine-specific state lives in `var/`, `localmodels/.venv/` and the model
caches, all git-ignored.
