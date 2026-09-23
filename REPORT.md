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

## Post-implementation status (2026-09-21)

All plan phases are **COMPLETE** and every suite is green.

- **Phase 0** readiness + frontend jsdom rig (`appcore.js`, `tests/frontend/`).
- **Phase 1** provider endpoint profiles (create/save/list/load/delete, activate).
- **Phase 2** agentic system prompt + injected workdir-context block (fixes the
  captured folder-read failure class).
- **Phase 3** attachments (files / folders / images / from-workdir picker).
- **Phase 4** integration close-out: CI single command (`npm test`), docs updated.
- **Phase 5** dynamic per-endpoint API key (`Authorization: Bearer`), settings input
  with show/hide eye, key in save/load/delete, header seam via `authHeaders()`.
- **Phase 6** agentic system-prompt redesign + structured tool-call contract: roster
  derived from the live `TOOL_SCHEMAS` (read_file, write_file, list_dir, grep, git,
  run_command) — removed the phantom `shell_exec`/`clipboard` names, added the JSON
  function-call contract, kept the workdir-jail / relative-path rules.
- **Phase 7** deterministic structured-output validator (`CogCore.validateStructuredOutput`)
  gating every model-authored tool call before it can reach the bridge; rejected calls are fed
  back to the model as `role:'tool'` corrections instead of being dispatched.
- **Phase 8** codereview #1 fix: a content-less `message` object in the buffered
  `chat.end` aggregate stream shape no longer self-appends the accumulator (which duplicated
  the whole response); covered by a new jsdom e2e suite driving the real send path.

## All phases complete (2026-09-22)

`PLAN.md` Phases 0-8 are **COMPLETE**; `gatelog.md` records each phase's gate and the
"info to know" quirks. Follow-up work now lives in `codereview.md` (read-only review of the
finished codebase, including the Phase 8 re-review pass).

Full verification: `npm test` → frontend suite `ALL GREEN`, `python3 test_e2e.py` → `0 FAILURES`.

---

## Appended findings (post-recon, 2026-09-23) — Local Cortex (Needle + Laya) recon

Appended, not a rewrite: everything above describes the completed phases 0-9 work and is still
accurate. This section adds what a second, code-focused recon found while planning the
Needle/Laya tool-call middleware (phases 10-15). The plan itself is
`docs/plans/needle-laya-middleware-plan.md`; progress lives in `gatelog.md`.

- **The harness UI is a browser app.** `index.html` (one inline script, 1356 lines) plus
  `appcore.js` (398 lines of pure UMD logic) run in the operator's browser; `bridge.py` (311) and
  `bridge_daemon.py` (385) are the only local processes. Consequence: neither local model can run
  in-process in the harness — both need a localhost sidecar process. That single fact drives the
  whole architecture (one Python supervisor on 127.0.0.1:8932, Needle imported in-process, Laya as
  a spawned Node ESM child).
- **The tool-call path is short and already gated.** Deltas accumulate in
  `mergeToolDelta`/`processStreamObject` (index.html:694/707), `finalizeToolCalls` (:954) hands
  them to `CogCore.validateStructuredOutput` (:968, the Phase 7 deterministic validator), and
  rejects are fed back as `role:'tool'` messages. The middleware slots into exactly two places:
  a salvage/repair step before that validator, and gates around the dispatch.
- **Registry is 6 tools** (`TOOL_SCHEMAS`, index.html:672). Needle's ~50-tool accuracy limit is
  therefore irrelevant here — no candidate-subset stage is needed, deliberately.
- **Wire formats:** OpenAI-compatible `tool_calls` (including arguments split across chunks),
  Ollama NDJSON, and one buffered `{"type":"chat.end",...}` aggregate shape. No Anthropic
  `tool_use` anywhere in the codebase.
- **Both model packages are real and were verified by downloading them** (not by trusting the
  handoff docs): `cactus-needle` 3.0.4 on PyPI (Apache-2.0, Python, ctypes over a downloaded C
  engine, `linux-arm64` engine build available, process-global single active instance ⇒ calls must
  be serialized); `@receptron/laya` 0.1.2 on npm (MIT, ESM-only, Node ≥20, `onnxruntime-node`
  1.22.0 ships `linux/arm64` prebuilds, ~1.7 GB ONNX weights from HF repo `receptron/laya-onnx`,
  cached outside the repo).
- **Both real APIs differ from the spec sketch** — Needle is `Needle(tools=…).complete(text)` →
  dict (not a `function_calls`-only shape), Laya is `Laya.load()` + `systemOne(state, {key:{…}})`
  with answers keyed by question name (not `decide(state, LayaQuestion[])`). The plan carries the
  deltas and marks the still-unverified details (exact Needle envelope, whether base weights'
  confidence is calibrated, Laya's real token limits, the non-existent `precision` load option)
  as things Phase 12/13 must probe and record.
- Baseline at planning time: `npm test` green (frontend `ALL GREEN`, python `0 FAILURES`), working
  tree clean apart from the untracked `Laya_needle_expansion/` documents.
See `gatelog.md` for per-phase notes, root causes, and "info to know".