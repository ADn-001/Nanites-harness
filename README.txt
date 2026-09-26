# Nanites-chat

internally called **COGITATOR** is a local, Warhammer 40K / Adeptus Mechanicus styled chat frontend for talking to local model servers, with an optional tool bridge that lets the model inspect and modify a chosen project directory under operator control.

It is built as a small static web app plus two Python helper programs:

- `index.html` — the chat terminal UI
- `bridge.py` — local tool executor for filesystem, grep, git, and optional shell rites
- `bridge_daemon.py` — supervisor that binds the bridge to a working directory and manages worker lifecycle
- `sw.js` — service worker for offline app-shell caching
- `manifest.webmanifest` — PWA manifest
- `icon.svg` — app icon
- `test_e2e.py` — end-to-end test suite for the bridge and daemon

## Features

- Chat UI with a retro machine-spirit terminal aesthetic
- LM Studio, Ollama, and OpenAI-compatible backend support
- Model detection and dropdown selection
- LM Studio native load/unload controls
- Streaming responses with reasoning/cogitation display
- Chat history stored locally in the browser
- Context usage gauge
- Manual and automatic compaction of long conversations
- Export/wipe local archive
- Optional **agent mode**:
  - model can invoke bridge tools
  - streamed `tool_calls` support
  - approval modal for sensitive rites
  - read-only auto-authorization option
  - transcript mapping for tool calls and tool results
- Local bridge daemon for project-directory tool execution
- Path jail, origin checks, request size caps, and read-only git restrictions by default

### Provider endpoint profiles

In **RITES/CONFIG** → **PROVIDER PROFILES**, you can name and save a
`{name, backend, endpoint, model, apiKey}` combination, list saved profiles, load one to
apply it live, or delete it. Profiles persist in `localStorage`. This replaces the old
workflow of re-typing the backend + endpoint + model every time you switch providers — e.g.
save a "DeepSeek" profile and an "LM Studio" profile and flip between them from the dropdown.

### Per-endpoint API key (bearer auth)

The endpoint block also has an **API KEY (BEARER)** field (masked, with a show/hide eye
toggle). When it is non-empty every model request — `/v1/chat/completions` and the model-list
probes (`/v1/models`, `/api/v1/models`, `/api/v0/models`, `/api/tags`) — carries
`Authorization: Bearer <key>`. Leave it empty for local backends (Ollama/LM Studio) and no
header is sent. The key is saved per profile and per settings blob; it is never written into
the system prompt, the `[WORKDIR CONTEXT]` block, or an attachment payload, and the tool
bridge (`/tools/execute`) is never given it.

### Agentic system prompt + workdir context

In agent mode the app now injects a short, precise system-orientation prompt telling the
model that the bridge jail **is** the bound workdir, that all paths are **project-relative**
to it, that `list_dir "."` shows the workdir contents, and that host-absolute paths are
refused. It also injects a `[WORKDIR CONTEXT]` block carrying the bound workdir path plus a
fresh listing of its contents, re-read at send time.

This fixes the earlier failure where the model read the host project's root files instead of
the bound workdir's contents.

### Attachments (files / folders / images)

Use the **`[+]`** ATTACH menu next to the composer to add:

- **File** / **Image** — pick a file from your local disk. Text files/folders are inlined
  into the message as fenced, path-labelled blocks (capped at ~20k chars; binary is skipped).
  Images are sent as multimodal `image_url` data-URL parts.
- **From workdir** — browse the bridge's bound workdir and pick a file to attach (read via
  `read_file`).

Each attachment becomes a removable chip above the textarea and is sent with your prompt.

## What the bridge does

The bridge is the **hands** of the system. It does not decide anything by itself; it only executes requested rites inside a chosen project directory.

Supported tools:

- `read_file`
- `write_file`
- `list_dir`
- `grep`
- `git`
- `run_command` — disabled unless explicitly enabled

The frontend agent loop is the **brain**. When agent mode is enabled, the frontend sends tool schemas to the model, receives tool calls, asks for approval when needed, executes the rite through the bridge, and feeds the result back to the model.

## Security model

By default, the bridge is intentionally conservative:

- filesystem access is jailed to the selected working directory
- symlink escapes are rejected
- foreign web origins are rejected
- POST bodies are capped
- `run_command` is disabled unless `--allow-exec` is used
- git is read-only unless `--allow-git-write` is used
- destructive read-only-looking git forms are blocked, such as:
  - `git branch -D`
  - `git tag -d`
  - `git tag -f`
  - `git stash pop/drop/clear`
  - `git checkout -- file`
- global git options that could bypass the jail are blocked

You can relax these controls with explicit flags, but only do so if you understand the risk.

## Requirements

- Python 3.9+ recommended
- A modern browser
- Optional: Node.js if you want to syntax-check extracted frontend JavaScript manually

## Quick start on Windows 11

### 1. Put the files in one folder

Example:

```powershell
C:\cogitator
```

At minimum, keep these together:

- `index.html`
- `bridge.py`
- `bridge_daemon.py`
- `sw.js`
- `manifest.webmanifest`
- `icon.svg`
- `test_e2e.py`

If you want the full visual experience, also include your image assets:

- `WH40KIDLE.gif`
- `WH40KSpinAnimation.gif`
- `icon.png`
- `icon-192.png`

### 2. Run the test suite

```powershell
cd C:\cogitator
python test_e2e.py
```

Expected result:

```text
0 FAILURES
```

### 3. Optional syntax checks

```powershell
python -m py_compile bridge.py
python -m py_compile bridge_daemon.py
```

### 4. Start the app

Simple local server:

```powershell
python -m http.server 8080
```

Then open:

```text
http://localhost:8080/index.html
```

### 5. Start the bridge daemon

In another PowerShell window:

```powershell
cd C:\cogitator
python bridge_daemon.py
```

The daemon enforces the same Origin allow-list as the bridge on every route
(`/status`, `/health`, `/pick_directory`, `/set_workdir`, `/start`, `/stop`,
`/install_autostart`, `/remove_autostart`). A page served from anywhere other
than `localhost` / `127.0.0.1` is refused with `403` and the rite does not run —
no workdir change, no worker spawn, no autostart entry. The app itself is served
from `http://localhost:8080`, so normal polling and binding keep working.

Then in the app:

- open **RITES/CONFIG**
- choose or paste a working directory
- click **BIND WORKDIR**
- start the bridge if needed

## Running the bridge manually

If you do not want the daemon, you can run the bridge directly:

```powershell
python bridge.py
```

Or with options:

```powershell
python bridge.py --root C:\path\to\project --port 8931
```

Useful flags:

```text
--root PATH              jail filesystem access to PATH
--port PORT              listen on a different port
--allow-git-write        enable mutating git rites
--allow-exec             enable run_command
--allow-any-origin       allow non-local web origins (not recommended)
--allow-file-origin      trust a null Origin (file:// page) — not recommended
```

The bridge refuses any request whose `Origin` is not `http://localhost[:port]`
or `http://127.0.0.1[:port]`. A request with **no** `Origin` header at all is
still allowed, so `curl` and native callers keep working. A `null` Origin is
**refused by default**: browsers send it for a `file://` page, a `data:`/`blob:`
document, and a sandboxed iframe, so any hostile page can present it. Use
`--allow-file-origin` only if you deliberately open the app from disk.

The supervisor daemon (`bridge_daemon.py`) accepts the same two flags and
enforces the same allow-list on every route:

```text
--port PORT              supervisor port (default: 8930)
--allow-any-origin       allow non-local web origins (not recommended)
--allow-file-origin      trust a null Origin (file:// page) — not recommended
```

## Using agent mode

In the app:

1. Open **RITES/CONFIG**
2. Enable **AGENT MODE**
3. Optionally enable **AUTO-AUTHORIZE READ-ONLY RITES**
4. Bind a working directory through the daemon
5. Start the bridge
6. Send a prompt that asks the model to inspect or modify the project

When the model requests a rite:

- read-only rites can be auto-approved if enabled
- mutating rites open an authorization modal
- **AUTHORIZE RITE** executes the rite
- **COUNTERMAND** refuses it and returns that refusal to the model
- **ESC** also countermands the pending rite

## Local Cortex (optional)

LOCAL CORTEX is an **optional** localhost sidecar that sits in front of the model reply
and helps produce **well-formed tool calls**. It is a piece of *middleware around tool
calls* — not a chat model, and not a replacement for the model you already talk to.

It is written in Python, talks to nothing but your own machine, and offers two small
local models:

- **Needle** — repairs a malformed or misnamed tool call, and (optionally) proposes a
  cheap pre-route for a request before the big model is consulted.
- **Laya** — gates: it can hold a mutating rite for a second opinion before it runs, and
  can flag a reply that looks anomalous.

**It is OFF by default, and it stays off until you turn it on.** A fresh install — yours or
a friend's — behaves exactly as it did before LOCAL CORTEX existed. Nothing is
downloaded, spawned or loaded, and the page does not even contact the sidecar port while
the master toggle is unticked. If you never enable it, you will never notice it.

### Installing it

Linux / macOS:

```bash
bash localmodels/setup.sh
```

Windows:

```powershell
powershell -ExecutionPolicy Bypass -File localmodels\setup.ps1
```

Both scripts create a virtualenv under `localmodels/`, install the Needle package, and
really fetch the base weights plus the engine library into the package's own cache
(`~/.cache/cactus-needle/…`). The download is idempotent — a cached file is skipped, so
re-running the script is safe. Run the daemon with that venv's python:

```bash
localmodels/.venv/bin/python localmodels/local_models_daemon.py
```

```powershell
localmodels\.venv\Scripts\python.exe localmodels\local_models_daemon.py
```

The same scripts also run `npm install` inside `localmodels/` for the optional **Laya**
gate. That step needs **Node.js >= 20**, and it is the *only* reason Node is needed for
LOCAL CORTEX — skip it if you do not want Laya. Laya's weights are a **~1.7 GB download
and they are NOT fetched by the installer**; they arrive lazily on Laya's first real use,
and only if you have enabled Laya, into a cache under `~/.cache/receptron-laya`. If Node
is missing, that step is skipped with a warning and everything else still works.

### Settings

In **RITES/CONFIG** → the **LOCAL CORTEX** block. Defaults as shipped:

| Setting | Default | What it does |
|---|---|---|
| `enabled` | `false` | master switch for the whole sidecar |
| `needle.enabled` | `false` | enable the Needle repair pass |
| `needle.minConfidence` | `0.75` | below this, the original call is passed through untouched |
| `needle.confirmBand` | `[0.5, 0.75]` | reserved low-confidence band; kept in the saved settings, not currently acted on |
| `needle.timeoutMs` | `800` | bounded budget around one model call |
| `laya.enabled` | `false` | enable the Laya gates |
| `laya.minConfidence` | `0.70` | pre-flight: a mutating rite scoring below this is held for a correction instead of being dispatched. A *missing* answer is never a refusal |
| `laya.timeoutMs` | `500` | bounded budget around one gate request |
| `laya.preflight` | `true` | ask Laya about a mutating rite before it runs |
| `laya.anomaly` | `true` | ask Laya to flag a suspicious-looking reply |
| `sanitizer.enabled` | `true` | deterministic salvage pass — free, no model involved |
| `sanitizer.mode` | `'auto'` | `'auto'` (probe only when a call looks suspect), `'on'` (also probe prose-only replies), `'off'` (never probe) |
| `sanitizer.deterministicPass` | `true` | run the free deterministic pass first |
| `dispatcher.enabled` | `false` | cheap local pre-router (proposes a rite before the big model) |
| `dispatcher.autoReadOnly` | `true` | may auto-run a *read-only* proposal (also needs the existing read-only auto-approve flag) |
| `dispatcher.timeoutMs` | `800` | bounded budget around one proposal |
| `dispatcher.minConfidence` | `0.75` | placeholder, deliberately out of reach of the untuned stock model — see the note below |
| `port` | `8932` | sidecar port, `127.0.0.1` only |
| `ledger` | `var/local-models.jsonl` | append-only decision log, relative to the repo |

A useful default to be blunt about: the shipped stock model is not calibrated, and the
`dispatcher.minConfidence` of `0.75` sits above what it actually scores. In practice the
dispatcher therefore almost never fires. That is the safe direction to fail, but it means
if you lower that number, keep the blank-argument guard in place — a required string
argument that trims to empty must still be shown to you as a card rather than run.

The daemon takes the same knobs on the command line, if you prefer:

```text
--port PORT                 sidecar port (default 8932)
--no-needle                 do not enable the Needle repair engine
--no-laya                   do not enable the Laya decision engine
--ledger PATH               ledger JSONL path
--needle-timeout-ms MS      bounded timeout around the model call (default 800)
--needle-select-timeout-ms MS   bounded timeout around one proposal (default 800)
--preload-needle            warm the engine in the background at boot
--laya-timeout-ms MS        bounded timeout around one Laya request (default 500)
--laya-idle-s N             reap the Laya child after N idle seconds (default 120, 0 disables)
--laya-child PATH           path to the Laya child script
--allow-any-origin          allow non-local web origins (not recommended)
--allow-file-origin         trust a null Origin (file:// page) — not recommended
```

### It is never in the critical path

This is the important part. **Local inference is never required for the harness to work.**

If the sidecar is down, disabled, unconfigured, slow, still loading, or the models are
simply not present, then:

- every LOCAL CORTEX feature degrades to plain deterministic behaviour or straight
  pass-through, and the original rite is used as-is;
- no request ever blocks forever — every call has a bounded timeout and answers
  "degraded" instead of hanging;
- the agent loop is completely unaffected, and no error is thrown into your turn;
- the whole harness stays fully functional. The chat, the agent mode and the bridge rites
  do not depend on any of this.

If you never install it, never start it, or start it and it dies, the only visible effect
is the LOCAL CORTEX status line reading offline.

### Privacy

- **Nothing leaves your machine.** There is no external service, endpoint, router or cloud
  fallback anywhere in this package — localhost only, and the sidecar binds `127.0.0.1`.
- The only network use is the one-time model-weight download at install or first use, and
  only if you ask for it.
- **The sidecar never receives your provider API key.** No `Authorization` header is ever
  sent to it.
- The decision ledger (`var/local-models.jsonl`) is a local, append-only JSONL file. The
  writer strips authorization/bearer/`sk-…`-looking values, your configured API key and
  absolute home paths, and truncates long fields. It is git-ignored, and the daemon writes
  no other file — no pid file, no log file.

### Safety

- Repairs fix **format, never semantics**. Needle only ever proposes a call from the tool
  schemas it was offered; it cannot invent a tool, and it never executes anything itself.
- Every tool call — repaired or not — still goes through the existing structured-output
  validator before anything runs.
- Mutating rites still require operator approval. A local model can propose, never
  authorise.
- A gate that has no answer holds nothing: absence of evidence is not a refusal, and nothing
  is blocked just because a model was unavailable, slow or missing.

### Checking it

The sidecar exposes a `GET /health` on its port. With the port left at the default:

```bash
curl http://127.0.0.1:8932/health
```

It reports whether each engine is enabled/loaded, where the ledger is, and a `degraded`
list of anything that is on but cannot serve right now. A feature you deliberately
switched off reads as *off*, not degraded. Probing health never starts a model or
triggers a download.

In the app, the LOCAL CORTEX block shows a **LOCAL CORTEX** status line, and the
**PROBE LOCAL CORTEX** button refreshes it. While the master toggle is off it reads
`LOCAL CORTEX: DISABLED`; a dead or silent sidecar reads `LOCAL CORTEX: OFFLINE (degraded)`.

If Laya is enabled and the status line is not what you expect, the usual cause is the
host rather than your install: the ONNX runtime Laya relies on ships a glibc-linked Linux
arm64 build, so Laya cannot serve on a musl host (Alpine, postmarketOS). Everything else
keeps working, exactly as if Laya were switched off.

## Autostart on Windows

Install:

```powershell
python bridge_daemon.py --install-autostart
```

Remove:

```powershell
python bridge_daemon.py --remove-autostart
```

On Windows, the daemon prefers `pythonw.exe` for autostart to avoid a console window flash at login.

## Project layout

```text
cogitator/
  index.html
  appcore.js
  bridge.py
  bridge_daemon.py
  sw.js
  manifest.webmanifest
  icon.svg
  test_e2e.py
  tests/frontend/      (jsdom e2e suites: phase0-8, incl. shared appcore helpers)
  localmodels/         (optional LOCAL CORTEX sidecar + its own README.md)
  PLAN.md / REPORT.md / gatelog.md / codereview.md   (dev tracking docs)
  README.txt
```

Optional assets:

```text
  WH40KIDLE.gif
  WH40KSpinAnimation.gif
  icon.png
  icon-192.png
```

## Notes and limitations

- The frontend is a single HTML file and stores chat history in `localStorage`.
- The bridge is meant for local use on `127.0.0.1`.
- The service worker caches the app shell for offline use after first load.
- If you serve the app from a subpath, keep `sw.js` and `manifest.webmanifest` beside `index.html`.
- If the model backend hangs, the frontend now applies stream timeouts instead of waiting forever.
- **Aggregate stream shapes are supported.** Besides the per-chunk OpenAI SSE form
  (`choices[].delta`) and Ollama's NDJSON, the parser also accepts a single buffered
  `{"type":"chat.end","result":{"output":[…]}}` frame as emitted by llama.cpp-class servers and
  some LM Studio modes. `reasoning` objects accumulate into the cogitation record, `message`
  objects append their content, and `tool_calls` are merged — a `message` object with empty
  content is a no-op and must never re-append the buffer.
- Tool execution depends on the bridge being reachable on the configured local port.

## Troubleshooting

### The app cannot reach the model server
Check:

- the endpoint URL
- whether the server is running
- CORS settings in LM Studio
- that you are serving the app over HTTP instead of opening it as a raw file

### The bridge chip says daemon offline
Run:

```powershell
python bridge_daemon.py
```

### Tool rites fail
Check:

- a workdir is bound
- the bridge worker is running
- the requested path is inside the bound project directory
- the rite is allowed by current bridge flags

### Tests fail on `/stop`
Make sure `test_e2e.py` calls `/stop` as a POST. The final version in this package does.

## Development verification

This package includes an E2E suite covering:

- bridge health
- file read/write
- directory listing
- grep
- jail escape attempts
- symlink escape attempts
- disabled command execution
- destructive git refusal
- git global-option bypass refusal
- safe read-only git acceptance
- foreign Origin refusal
- null Origin refusal (file:// pages are not trusted)
- localhost Origin allowance
- oversized POST refusal
- daemon status
- workdir binding
- worker spawn/health
- worker rebind
- foreign `bridge.py` backup
- managed bridge scrub
- invalid workdir refusal
- daemon stop behavior
- daemon foreign Origin refusal on every GET/POST route
- daemon no-Origin / localhost Origin allowance

Run it with:

```powershell
python test_e2e.py
```

The frontend improvements (profiles, per-endpoint API key, agent prompt + structured
output validator, attachments, aggregate-stream parser) are covered by the Node/jsdom
e2e suites in `tests/frontend/` (run with `node tests/frontend/run.js`). To run **everything**
— the full frontend suite then the full Python bridge/daemon suite — in one command:

```powershell
npm test
```

Expected result: frontend suite prints `ALL GREEN` and the Python suite prints `0 FAILURES`.
(Requires Node.js + `npm install` once for the jsdom dependency.)

## License / usage

Use at your own risk. This project can execute filesystem, git, and optional shell operations on your machine. Keep the bridge local, keep mutating rites behind approval, and only enable `--allow-exec` or `--allow-git-write` when you actually need them.