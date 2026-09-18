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
--allow-any-origin       disable localhost/null Origin guard
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
  bridge.py
  bridge_daemon.py
  sw.js
  manifest.webmanifest
  icon.svg
  test_e2e.py
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
- null Origin allowance
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

Run it with:

```powershell
python test_e2e.py
```

## License / usage

Use at your own risk. This project can execute filesystem, git, and optional shell operations on your machine. Keep the bridge local, keep mutating rites behind approval, and only enable `--allow-exec` or `--allow-git-write` when you actually need them.