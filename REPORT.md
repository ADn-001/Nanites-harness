# COGITATOR — Developer Report / Recon

Date: 2026-09-18
Scope: before any feature work, establish what exists, what is tested, and what the
       requested improvements require. This document feeds `PLAN.md`.

## What exists today

Single-repo project of a WH40K-styled local chat frontend ("COGITATOR", aka Nanites-chat).
Five runnable units:

| File | Language | Role | Lines |
|------|----------|------|-------|
| `index.html` | HTML/CSS/JS (one ~1140-line inline script) | Chat terminal UI + settings + agent loop | 1146 |
| `bridge.py` | Python | Local tool executor (read/write/list/grep/git/run_command), jailed to a root | 293 |
| `bridge_daemon.py` | Python | Supervisor: binds workdir, writes/spawns/scrubs `bridge.py` workers | 336 |
| `test_e2e.py` | Python | HTTP e2e suite against bridge + daemon | 105 |
| `sw.js`, `manifest.webmanifest`, icons, GIFs | — | PWA shell + assets | — |

## Baseline verification (run before any changes)

- `python3 test_e2e.py` -> **0 FAILURES** (all 37 checks pass). Verified 2026-09-18.
- Node v24.18.1 + npm 12.0.2 available -> frontend logic is testable with `jsdom` (installed 2026-09-18).
- Python 3.12.13.

## Gap analysis vs. requested improvements

### 1. Provider endpoint profiles
Gaps:
- `settings.endpoint` / `settings.backend` / `settings.model` are three *independent* scalar
  settings. There is **no notion of a saved profile**. Backend type is chosen from a fixed
  `<select>` (`auto/lmstudio/ollama/openai`); an OpenAI-compatible endpoint is only ever the
  current, single `endpoint` value typed into one field and kept in one localStorage key.
- Nothing is stored per-provider; switching from, say, DeepSeek to LM Studio means re-typing
  endpoint + backend every time.
Target: a profile = `{name, backend, endpoint, model?}`; create / save / list / load / delete;
persist in localStorage; loading a profile rewrites the live settings.

### 2. Agentic system prompt + working-directory context
Gap (matches the reported failure):
- In agent mode the frontend sends the user's optional custom "canticle" (or nothing) as the
  system prompt. It never tells the model *where* the bridge's jail root is, nor that file
  paths are **project-relative** to the bound workdir.
- Consequence: the model invents host-absolute paths (e.g. `/home/...`, `C:\...\repo\...`),
  calls `read_file`/`list_dir` against the host project root instead of the jailed workdir,
  and the bridge refuses them with "path escapes project root" — the exact class of failure
  reported in the folder-read test.
- The bridge default root is `cwd` for manual runs and `--root <workdir>` for daemon spawns;
  `list_dir "."` resolves to the jail root (the bound workdir). The frontend does not surface
  this. Nothing injects a listing of the workdir into context.
Target: a precise, short, hierarchical agent-orientation prompt that is inserted whenever
agent mode is on; plus an injected `[WORKDIR CONTEXT]` block carrying the bound workdir path
and a current listing of its contents, refreshed at send time. Must not blow small contexts.

### 3. Attachments (files / folders / images)
Gap:
- The composer is a single `<textarea>`. No file/folder/image input exists. Workdir files
  cannot be selected from the UI.
Target:
- Attach files/folders/images from the local disk **and** pick files from the bound workdir.
- Text files/folders inlined into the user message as fenced, path-labelled blocks (size
  capped, binary skipped).
- Images sent as multimodal `content` arrays (`image_url` with a `data:` URL) per OpenAI-
  compatible providers; the whole message path (non-agent and the agent loop) must handle
  content-as-array, not just content-as-string.

## Testing strategy for frontend improvements

The bridge/daemon are already covered by `test_e2e.py` (Python). The three improvements are
frontend layers, so each phase ships a **Node/jsdom e2e suite** in `tests/frontend/` that:

1. Loads `index.html` into jsdom with mocked `fetch` / `localStorage` / `AbortController`.
2. Executes the real inline script (real wiring, real DOM ids).
3. Drives it like a user (set inputs, click buttons) and asserts on DOM + persisted state +
   the exact JSON payload sent to `/v1/chat/completions` and to the bridge.
Pure, provider-agnostic logic lives in a shared `appcore.js` (loaded by index.html, `require`d
by tests) so logic is unit-testable and shared, not duplicated.

Regression gate for every phase: `python3 test_e2e.py` still reports `0 FAILURES`.