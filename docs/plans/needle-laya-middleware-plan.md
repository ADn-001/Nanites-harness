# Plan — Local Cortex: Needle + Laya tool-call middleware for the COGITATOR harness

**Status:** planned (not started). Phases 10–15 of `gatelog.md`.
**Requirements source:** `Laya_needle_expansion/needle-laya-harness-integration-spec.md` (v2 draft) +
`Laya_needle_expansion/handoff-prompt-needle-laya.md`. Both were written by a session that did
**not** have the harness codebase; where they conflict with the repo, the repo wins and the delta
is recorded in §2 below.
**Executor:** a fresh session/cron call with NO conversational context. Everything needed is in
this document plus `gatelog.md`. Read `gatelog.md` first — it holds the live status.

---

## 1. Recon summary (what actually exists)

**Product:** COGITATOR (internally "Nanites-harness") — a Warhammer-40K-styled **browser** chat
frontend for local/OpenAI-compatible model servers, plus an optional tool bridge that lets a model
inspect/modify a chosen project directory under operator control.

| Unit | Language | Role | Size |
|---|---|---|---|
| `index.html` | HTML/CSS/JS, one inline script | chat UI, settings modal, **agent loop**, stream parser | 1356 lines |
| `appcore.js` | JS UMD (`window.CogCore` in browser, `module.exports` in Node) | pure, DOM-free, side-effect-free shared logic | 398 lines |
| `bridge.py` | Python 3 | local tool executor, jailed to a root; `/tools/execute` + `/health` | 311 lines |
| `bridge_daemon.py` | Python 3 | supervisor: binds workdir, writes/spawns/scrubs bridge workers; `/status`, `/start`, `/stop`, `/set_workdir`, … | 385 lines |
| `test_e2e.py` | Python 3 | HTTP e2e suite against bridge + daemon | 169 lines |
| `tests/frontend/*.test.js` | Node + jsdom | 9 per-phase frontend suites + `helpers.js` + `run.js` | — |
| `sw.js`, `manifest.webmanifest`, icons, GIFs | — | PWA shell + assets | — |

**Commands:** `npm run test:front` → `node tests/frontend/run.js`; `npm run test:py` →
`python3 test_e2e.py`; **`npm test`** = both. Green baseline: frontend `ALL GREEN`, python
`0 FAILURES`. Node v24.18.1, Python 3.12.13 (PEP 668 — system pip refuses installs; use a venv).

**The tool-call path (this is what the middleware wraps):**

```
send()                              index.html:1052
  └─ stream(c, agentMode)           index.html:1065
      └─ runAgentLoop(c,m,sig)      index.html:959      ← agent turn (max 12 iterations)
          ├─ buildMessages()        index.html:581      ← system prompt + [WORKDIR CONTEXT]
          ├─ callModel → callOpenAI (index.html:763) | callOllamaNative (:780)
          │     └─ pumpSSE (:724) → processStreamObject (:707) → mergeToolDelta (:694)
          │        (accumulates content / thinking / toolCalls deltas; also handles the
          │         buffered `chat.end` aggregate shape and Ollama NDJSON)
          ├─ finalizeToolCalls(acc, idx)              index.html:954
          ├─ CogCore.validateStructuredOutput(calls, TOOL_SCHEMAS)   appcore.js:272  ← Phase 7 gate
          ├─ rejected → pushed back as {role:'tool', content:'[RITE REJECTED BY VALIDATOR …]'}
          └─ executeTool(tc) → approveToolCall(tc) → POST http://127.0.0.1:8931/tools/execute
```

**Tool registry:** `TOOL_SCHEMAS` (index.html:672) — exactly **6** tools: `read_file`,
`write_file`, `list_dir`, `grep`, `git`, `run_command`. Far below Needle's ~50-tool accuracy
limit ⇒ **candidate-subset selection is NOT needed** in this project (spec §8 Q3 answered by recon).

**Wire formats actually in play:** OpenAI-compatible SSE (`choices[].delta.tool_calls`, incl.
arguments split across chunks), Ollama NDJSON (`message.tool_calls`), and one buffered
`{"type":"chat.end","result":{"output":[…]}}` aggregate shape (llama.cpp-class / some LM Studio
modes). **No Anthropic `tool_use` path exists** — the golden corpus must not pretend otherwise.

**Existing gates / extension points:**
- `CogCore.validateStructuredOutput(result, allowedTools)` (appcore.js:272) is already a
  deterministic validator: name must be in the allow-list, `arguments` must parse to a plain
  object, per-tool required-field/type checks, returns `{ok, errors, sanitized}`. Its `_toolSpec`
  helper (appcore.js:217) already derives names+required+properties from `TOOL_SCHEMAS`.
- The self-correction channel already exists: validated-out calls are fed back as `role:'tool'`
  messages so the model can retry. **F2 pre-flight failures must use this same channel.**
- Config lives in one `localStorage` blob `DEF_SETTINGS` (index.html:~407) with `saveSet()` /
  modal open/close handlers; `TEST CONNECTION`, provider profiles, agent-mode flags are all there.
- Native jsdom answer to "how does the harness test frontend logic": the suites inline
  `appcore.js` into `index.html` and drive the **real** inline script with a **mocked `fetch`**
  keyed by URL path (`tests/frontend/helpers.js`). That is exactly how the sidecar will be
  mocked, so **no model weights are ever required by CI**.
- `bridge.py` establishes the house conventions the new sidecar must copy: Origin allow-list
  (`_origin_allowed`, bridge.py:223 — no `Origin` header allowed, `null` refused unless
  `--allow-file-origin`, `localhost`/`127.0.0.1` only, `--allow-any-origin` escape hatch), CORS
  headers, POST size cap, startup banner, `argparse` flags.

**Verified facts about the two models (checked against PyPI/npm/HF, not taken on faith):**

| Fact | Evidence |
|---|---|
| `cactus-needle` 3.0.4 exists, Apache-2.0, Python ≥3.9, deps: `huggingface_hub` | wheel downloaded from PyPI |
| It is a ctypes wrapper over a downloaded C engine + a downloaded `.cact` base weight; engine platforms include **`linux-arm64`** | `needle/__init__.py:_library_path`, `needle/agent/fetch.py:PLATFORMS` |
| Real API: `Needle(tools=…, weights=None, generation=3)`; `.complete(text, max_new_tokens)` → dict; `.run(query, max_steps)`; `.extract(text, schema)`; `.embed(text)`; `.reset()`; `.close()` | `needle/__init__.py:157-344` |
| There is a process-global single active instance per generation (`_active` dict) ⇒ **all Needle calls must be serialized** | `needle/__init__.py:199-213` |
| `@receptron/laya` 0.1.2 exists, MIT, **ESM-only (`"type":"module"`)**, Node ≥20, needs `onnxruntime-node` + `@huggingface/tokenizers` | npm pack of the tarball |
| `onnxruntime-node@1.22.0` ships `bin/napi-v6/linux/arm64/` prebuilds ⇒ runs on this machine | tarball listing |
| Laya real API: `Laya.load({modelDir|cacheDir|repo|subfolder|revision|onProgress|executionProviders|sessionOptions})`, then `laya.systemOne(state, {key:{type:'choice'\|'score'\|'noul', instructions, criteria}})` → `result.answers.<key>.choice/.probabilities/.score/.noul`, `result.usage.input_tokens`, `laya.close()` | `package/README.md` |
| Weights: HF repo `receptron/laya-onnx` resolves (`laya.onnx`, `laya.onnx.data`, `laya_config.json`, `tokenizer/`), ~1.7 GB, cached to `~/.cache/receptron-laya` (`LAYA_CACHE` overrides) | HF API + README |
| Disk: 88 GB free; network to PyPI/npm/HF works; CPU: aarch64 (postmarketOS) | local checks |

**Must be verified during implementation (do NOT assume; record findings in `gatelog.md`):**
1. The exact Needle response envelope — the spec claims `{function_calls, reasoning, confidence}`;
   the code shows `type` (`"call"`), `function_calls`, `confidence` (possibly `None`), plus
   grounding annotations (`_ungrounded_paths` / `_annotate_ungrounded`) that may mark fabricated
   numeric/date arguments. Phase 12 starts by probing this and writing the real shape down.
2. Whether the unpacked base weights report a calibrated `confidence` (the code path says an
   untuned model is `_calibrated = True`, but a *tuned* `.cact` without a confidence head returns
   `confidence: None` — the frontend must treat `null` confidence as "not above threshold").
3. Laya load options: the spec's `precision: float16` preset **does not exist** in the README's
   option list — do not invent a flag. Size reduction, if any, must come from a smaller
   `subfolder`/`revision` on the HF repo.
4. Laya's real token limits (~192 tokens per question's options, state truncated ~512) — measure,
   don't trust the spec's numbers.

---

## 2. Spec vs. repo deltas (and the adaptations)

| # | Spec says | Repo reality | Adaptation |
|---|---|---|---|
| D1 | "Middleware between harness and LLM models"; runtime split "if the harness is TS/Node → Laya in-process, Needle sidecar; if Python → reverse" | The harness UI is a **browser** app; the only local processes are Python (`bridge.py`, `bridge_daemon.py`) | **Everything runs in one Python sidecar supervisor** on `127.0.0.1:8932` that imports Needle in-process and spawns a Node ESM child for Laya. The browser calls it over HTTP exactly like the bridge. (Owner decision, §3.) |
| D2 | `interface LocalModels { selectTool, repairCall, decide(state, LayaQuestion[]) }` | Laya's real call is `systemOne(state, {key:{type,…}})` and answers are keyed by name, batched **per call**; Needle's real call is `complete(text)` | Keep the seam (`health`/`repair`/`decide`/`selectTool`) but with the **real** payload shapes; F1/F2/F3 code goes through one pure `CogCore.localModels` client so it is testable without a server. |
| D3 | F1 stage 1 "deterministic pass" implies it needs building | `validateStructuredOutput` (Phase 7) already validates name-vs-registry + JSON args + required/typed fields | Stage 1 becomes **`salvageToolCalls`**: repair *before* validation (fences, trailing commas, string→object args, near-miss names, narration recovery). The existing validator stays the post-repair gate — one gate, not two. |
| D4 | Registry may exceed ~50 tools → candidate subsets required | Registry is 6 tools | No subset stage. Present all 6. Recorded so nobody "optimizes" it later. |
| D5 | Retrofit contract mentions OpenAI `tool_calls` **and Anthropic `tool_use`** | Only OpenAI-style + Ollama NDJSON + buffered `chat.end` | Corpus covers the two real ones + the aggregate shape. Anthropic is a non-goal. |
| D6 | F4 natural-language **admin** commands (add/configure providers, keys, models) | There is no admin-operation layer — settings are DOM-only, keys live in `localStorage` | **Dropped from v1** (non-goal). F5 (guardrail on mutating ops) is dropped with it; the natural future hook is the existing mutating-rite approval modal (`approveToolCall`, index.html:930). |
| D7 | Ledger at `./var/local-models.jsonl`, "inputs (redacted)" | Repo is share-ready (public GitHub, friends install it); tracked files must hold no machine-specific values | Ledger stays at `var/local-models.jsonl` but **`var/` is git-ignored**; redaction is enforced in code (§4) and asserted by tests. |
| D8 | "Laya ≈ 2 GB RAM + 1.7 GB download; Needle needs a Python runtime" | True, and the harness's operator may be on Windows 11 (README) or this Linux phone | Both models are **per-machine opt-in**, off by default, lazy-loaded, with a `--no-needle`/`--no-laya` switch and a `/health` that reports exactly what is loaded. A friend who never installs them sees today's behaviour. |

---

## 3. Resolved decisions (spec §8 open questions → answers)

| Spec §8 question | Decision | Rationale (from recon) |
|---|---|---|
| Q1 harness stack → sidecar vs in-process | **One Python sidecar supervisor** `localmodels/local_models_daemon.py` on `127.0.0.1:8932`: Needle imported in-process, Laya as a spawned Node child on stdio. Browser talks HTTP. | The UI is a browser; neither model can run in it. This mirrors bridge/daemon exactly, reuses the Origin allow-list pattern, and keeps `settings.bridgePort`-style config trivial. (Owner decision.) |
| Q2 streaming hooks | Hook **`processStreamObject`/`mergeToolDelta`** (index.html:707/694) for incremental cheap detection (Phase 15) and `finalizeToolCalls`/`runAgentLoop` for the decision point. Never buffer the stream. | Those are the only places deltas are accumulated; the parser already handles three wire shapes. |
| Q3 registry > 50 tools | Not applicable — 6 tools. | `TOOL_SCHEMAS`. |
| Q4 existing retry/self-correction path | Yes: validator rejections become `role:'tool'` messages (index.html:969-972). F2 pre-flight failures and Laya pre-flight refusals **reuse it**. | Already shipped in Phase 7. |
| Q5 is a Python runtime acceptable | Yes (already required by `bridge.py`/`bridge_daemon.py`). Node ≥20 is additionally required *only* for Laya, which is optional. | README requirements + npm `engines`. |
| Q6 which API formats → corpus | OpenAI `tool_calls` (incl. chunk-split arguments), Ollama `message.tool_calls`, buffered `chat.end` aggregate. No Anthropic. | Parser code. |
| F5/F4 | Out of v1 (§2 D6). | Owner decision. |
| F3 dispatcher execution policy | A proposal for a **read-only** rite auto-runs when `dispatcher.autoReadOnly` **and** the existing `settings.autoApproveRead` are both on — it still flows through the unchanged Phase 7 validator → `approveToolCall` (auto-approve) → `executeTool`, and a transcript note records that LOCAL CORTEX proposed it. A **mutating** proposal always renders the card and requires an explicit operator ACCEPT. | Owner decision 2026-09-23 (`autoApproveRead` already exists in `DEF_SETTINGS` and defaults true, so "auto-run read-only" reuses the shipped approval semantics instead of inventing new ones). |
| Sharing story | Both models are **opt-in and off by default** in the public repo. A friend who installs it and never enables LOCAL CORTEX sees exactly today's behaviour; Laya's 1.7 GB is never fetched unless Laya is explicitly enabled. | Owner decision 2026-09-23. |
| Git delivery | Long-lived branch `feat/local-cortex-needle-laya`; one commit per phase; **one PR at the end**. | Owner decision; matches "merges PRs himself in the web UI". |
| Node deps placement | `localmodels/package.json` (own `node_modules`); root `package.json` stays jsdom-only. | Keeps the frontend test install light; 200 MB of prebuilds never enters the main install path. |
| Model installs on this machine | Install **both** (Needle venv+engine+base weights, Laya 1.7 GB) so live suites genuinely run; live suites remain opt-in via `COG_LIVE_MODELS=1` so CI never needs weights. | Owner decision. |
| Cron | New job `nanites-needle-laya-dev`, every 180m, one phase per run, local output only (no ping). Old `nanites-chat-dev` stays paused. | Owner decision. |

**Non-negotiable architecture rules carried over from the spec (§5), restated as rules the
implementer must not weaken:**
1. Local inference is **never** in the critical path. Harness fully functional with both models
   unloaded; every feature degrades to deterministic behaviour / pass-through.
2. Tight, bounded timeouts on every awaited local inference (defaults: repair 800 ms, decide
   500 ms). On timeout → pass the original reply through, flag it.
3. Confidence thresholds everywhere, configurable, act/confirm/refuse bands.
4. Append-only JSONL ledger of every local-model decision (input redacted, model, request, output,
   confidence, latency, action taken) — also the future fine-tuning corpus.
5. Opt-in, lazy-loaded, per-feature flags.
6. Streaming-aware: scan accumulated deltas incrementally; never buffer a whole stream to decide.
7. Repairs fix **format, never semantics** — verbatim into the code docs. The no-hallucination
   property (Needle returns an empty list rather than guessing) plus the Laya gate are the
   mitigations for a well-formed-but-hallucinated call.
8. Prompt-injection awareness: user text reaching Needle/Laya can influence tool selection; no
   mutating rite may execute on the strength of local-model output alone (Phase 14 requires an
   explicit operator accept).
9. The middleware never sees or forwards the provider API key (`settings.apiKey`) — assert it in
   tests.

---

## 4. Contracts to implement (so a contextless executor can't guess wrong)

**Sidecar routes** (all `POST` JSON unless noted; all behind the bridge-style Origin guard):

| Route | Request | Response |
|---|---|---|
| `GET /health` | — | `{ok, version, needle:{enabled, loaded, weights:'present'\|'missing', generation, lib}, laya:{enabled, loaded, child_pid, cache}, ledger:{path, writable}, degraded:[…reasons]}` |
| `POST /repair` | `{suspect:{name, arguments}, candidates:[ToolSchema≤10], schema, trace_id?}` | `{ok, calls:[{name, arguments}], confidence, reasoning, latency_ms, trace_id, degraded?, reason?}` — `calls: []` means **no repair** |
| `POST /decide` | `{state, questions:{key:{type:'choice'\|'score'\|'noul', instructions, criteria}}}` | `{ok, answers:{key:{choice?, probabilities?, score?, noul?}}, usage, latency_ms, trace_id, degraded?}` |
| `POST /select` | `{input, candidates:[ToolSchema≤10]}` | `{ok, calls, confidence, reasoning, latency_ms, trace_id, degraded?}` |
| `POST /ledger` | `{trace_id, action:'accepted'\|'passed_through'\|'rejected'\|'timeout'\|'accepted_by_operator'\|'ignored_by_operator', note?}` | `{ok}` — appends an outcome record |

**Ledger record shape** (one JSON object per line, `var/local-models.jsonl`):
`{ts, trace_id, model:'needle'|'laya', op, input_redacted, request, output, confidence, latency_ms, degraded, action:null}` plus a later outcome line `{ts, trace_id, action, note}`.

**Redaction rules (enforced in `localmodels/ledger.py`, asserted by tests):** never write
`Authorization` values or anything matching the configured API key; truncate any single text field
to 2000 chars with a `…[truncated N]` marker; strip absolute home paths (replace the user's home
dir with `~`); redact anything that looks like `sk-…`/`Bearer …`.

**Frontend settings block** (inside `DEF_SETTINGS`, persisted by `saveSet()`):
```js
localModels:{enabled:false,
  needle:{enabled:false,minConfidence:0.75,confirmBand:[0.5,0.75],timeoutMs:800},
  laya:{enabled:false,minConfidence:0.70,timeoutMs:500,preflight:true,anomaly:true},
  sanitizer:{enabled:true,mode:'auto',deterministicPass:true},
  dispatcher:{enabled:false,autoReadOnly:true},
  port:8932, ledger:'var/local-models.jsonl'}
```
Defaults keep everything **off** ⇒ zero behaviour change until the operator opts in.

**Frontend seam** (pure, in `appcore.js`, injected deps so jsdom/Node tests need no server):
```js
CogCore.localModels.client(base, fetchImpl, clock) → {health, repair, decide, selectTool, outcome}
CogCore.salvageToolCalls(reply, allowedTools)       → {calls, changed:[…], unrepairable:[…]}
CogCore.sanitizeReply(reply, allowedTools, deps)    → {calls, source:'deterministic'|'needle'|'original',
                                                       confidence, accepted:boolean, trace_id, degraded}
```

---

## 5. Phases

Sizes: S ≈ half a session, M ≈ one session, L ≈ more than one session (the cron does one phase
per 3-hour call; a phase that overruns simply resumes next call — the gatelog is the state).

Every phase: (a) leaves the harness fully functional with both models absent, (b) ships its own
red-first e2e suite, (c) is only `done` when its gate is green, (d) writes its findings into
`gatelog.md` before the sprint ends.

---

### Phase 10 — Local Cortex plumbing: flags, sidecar skeleton, ledger, health  (size M)
**Goal:** All the machinery exists and is inert: config surface, the sidecar process, the ledger,
and a frontend client that has nothing to call yet. Zero behaviour change by default.

**Tasks:**
1. `localmodels/local_models_daemon.py` — `ThreadingHTTPServer` on `127.0.0.1:8932`. Copy
   `bridge.py`'s conventions exactly: `_origin_allowed()` allow-list (no `Origin` ⇒ allow;
   `null` ⇒ 403 unless `--allow-file-origin`; `http://localhost[:port]` / `http://127.0.0.1[:port]`
   ⇒ allow; else 403), CORS headers, `OPTIONS`, POST size cap, startup banner, `argparse` flags
   `--port`, `--no-needle`, `--no-laya`, `--ledger PATH`, `--allow-any-origin`,
   `--allow-file-origin`. Routes: `GET /health` only (the rest arrive in later phases; unknown
   routes ⇒ 404 JSON, not a crash).
2. `localmodels/ledger.py` — append-only JSONL writer: `append_record(dict)`, `new_trace_id()`,
   redaction per §4, fsync per line, creates the directory, never raises into the request path
   (log-and-continue).
3. `appcore.js` — add `CogCore.localModels.*`: `client(base, fetchImpl, clock)` with `health`,
   `repair`, `decide`, `selectTool`, `outcome`; each call uses a bounded `AbortSignal` (guarded
   like the existing `apiSig()` at index.html:693 — jsdom has no `AbortSignal.timeout`), and each
   **returns `{ok:false, degraded:true}` instead of throwing** on any network/timeout/parse error.
4. `index.html` — `settings.localModels` block (defaults off, §4) in `DEF_SETTINGS`; persist in
   `saveSet()`; restore into the modal in the settings-open handler; new **LOCAL CORTEX** section
   in RITES/CONFIG: per-model and per-feature toggles, port field, min-confidence inputs, ledger
   path, a status line fed by `/health`, and a `TEST CORTEX` button.
5. `.gitignore` — add `var/`, `localmodels/.venv/`, `localmodels/node_modules/`, model caches.
   (Do not ignore `localmodels/package.json` or its lockfile — those are share-ready artifacts.)
6. `localmodels/README.md` + `localmodels/setup.sh` / `setup.ps1` — venv + `pip install
   cactus-needle` + npm install for Laya; document that weights download on first use and where
   they land. **Do not run the installs in this phase** (Phase 12/13 do, deliberately).
7. Suites: `tests/frontend/phase10_localmodels_config.test.js` — defaults are off; toggles persist
   across a simulated reload; the modal round-trips every field; with `localModels.enabled=false`
   **no fetch to :8932 is ever issued** (assert on the fetch spy); with it enabled and the sidecar
   unreachable, every client call resolves `{ok:false,degraded:true}` and **nothing throws**;
   `/health` mocked ⇒ status line renders loaded/missing per model.
   Python: extend `test_e2e.py` with localmodels-daemon checks (boot, `/health` 200 + shape,
   foreign Origin 403, `null` Origin 403, no-Origin 200, unknown route 404, ledger file created
   and a manual record redacted). Follow the existing suite's rule: **never probe a daemon
   in-place in the repo** — run it in a temp dir.

**Exit criteria (e2e gate):** `node tests/frontend/run.js` ALL GREEN (new phase 10 + existing
0,1,2,3,5,6,7,8,9) AND `python3 test_e2e.py` 0 FAILURES; `gatelog.md` Phase 10 → done with
findings.

---

### Phase 11 — Deterministic salvage pass + golden corpus  (size M/L)
**Goal:** Fix everything plain code can fix (spec D3 / "the ~0-cost bulk") and establish the
measured corpus that grades every later phase.

**Tasks:**
1. `appcore.js` — `CogCore.salvageToolCalls(reply, allowedTools)` (pure, no network/fs/DOM):
   - accept a raw assistant reply in any of: OpenAI message `{tool_calls:[…]}`,
     OpenAI delta/buffered `chat.end` form, Ollama `{message:{tool_calls:[…]}}`, or
     `{content, tool_calls:[]}`;
   - strip markdown fences and trailing commas; `arguments` as string → parse; `arguments` as
     double-encoded string → parse twice; truncated JSON → close-balance attempt **only** if it
     yields a parseable object, else mark unrepairable;
   - single call object → array;
   - **tool-name reconciliation**: exact → case-insensitive → separator-normalised
     (`run-bash`/`run_bash`/`RunBash`) → nearest by edit distance with a unique winner at
     ≥ 0.86 similarity. Ambiguous ⇒ unrepairable (never guess between two candidates);
   - **narration recovery**: when `content` is prose and there are no calls, extract a call from a
     `name({…})`-shaped span or a fenced JSON block that contains `name` + `arguments`;
   - return `{calls, changed:[…human-readable fixes], unrepairable:[…], source:'deterministic'}`.
   - **Format only, never semantics** — no inventing argument values, no filling required fields
     with guesses. Document that rule in the function doc-comment.
2. `index.html` — wire it into the agent turn: salvage runs on the accumulated reply **before**
   `validateStructuredOutput`; only what salvage cannot resolve is marked `suspect` (and will be
   offered to Needle in Phase 12). Narration-with-zero-calls is `suspect` too.
3. `tests/fixtures/toolcall-corpus/cases.json` — ~60 cases, each
   `{id, category, format:'openai'|'ollama'|'chat.end'|'content', reply, expect:{name?, arguments?, unchanged?:true, unrepairable?:true}}`.
   Categories: string args · fenced JSON · truncated JSON · trailing garbage · double-encoded args ·
   near-miss name (case/separator/typo) · ambiguous name · narration-only · schema violation
   (missing required / wrong type / enum) · **legitimate prose and code blocks that must stay
   untouched** (the false-repair guard) · legitimate calls that must be byte-identical.
4. `tests/fixtures/toolcall-corpus/README.md` — case schema + how to add a case (this is the
   corpus the fine-tuning ledger will later mirror).
5. Suites: `tests/frontend/phase11_salvage.test.js` — drives the whole corpus through
   `salvageToolCalls` + `validateStructuredOutput` and asserts: deterministic fix rate ≥ 80% of
   repairable cases, **false-repair rate exactly 0** (every `unchanged:true` case leaves `calls`
   empty/semantics identical), ambiguous names are never guessed; plus a driven end-to-end case
   that feeds a fenced + near-miss stream through the **real** agent loop and asserts the bridge
   receives the canonical tool name and object arguments (and that a clean legitimate turn is
   still dispatched exactly once — no regression in the happy path).

**Exit criteria:** phase 11 suite green + full frontend suite + `python3 test_e2e.py` 0 FAILURES;
corpus size and measured rates recorded in `gatelog.md` findings.

---

### Phase 12 — Needle repair pass (F1 ML stage)  (size L)
**Goal:** Calls that plain code cannot salvage — including narrated ones — get a
schema-constrained repair from Needle, behind a confidence gate and a hard timeout. An empty
Needle result must **never** manufacture a call.

**Tasks:**
1. **Probe first, then code:** write a throwaway probe (`python3 -c` or
   `localmodels/probe_needle.py`) against freshly installed weights; capture the *real*
   `complete()` envelope for a tool-call prompt and a no-match prompt. Record the exact keys in
   `gatelog.md` findings (this settles §1's "must verify" items 1–2).
2. `localmodels/needle_backend.py` — `NeedleBackend`: lazy `Needle(tools=<candidates json>,
   generation=3)`; weights from `NEEDLE_WEIGHTS` env, else the package's `_base_weights_path(3)`;
   **a module-level lock serialising every call** (the package keeps one active instance per
   generation process-wide); `repair(query, candidates, schema)`; `weights_present()` without
   loading; returns `{ok:false, reason:'weights_missing'|'tool_unavailable'}` rather than raising.
   Note: `confidence: null` ⇒ treat as below threshold.
3. Daemon — `POST /repair` route (shape in §4) with `--needle-timeout-ms` (default 800) enforced
   **around the model call** (not the whole request), `--preload-needle` for eager load, ledger
   record per call. A timed-out repair must not wedge the server (lock + timeout + thread-per-
   request via `ThreadingHTTPServer`; verify with two concurrent requests).
4. `appcore.js` — `CogCore.sanitizeReply(reply, allowedTools, deps)`: deterministic salvage →
   if still `suspect` **and** `sanitizer.enabled` **and** `needle.enabled` → `POST /repair` with
   the top candidates (all 6 today) + the suspects → validate the repaired calls with
   `validateStructuredOutput` → accept only if `confidence ≥ needle.minConfidence`; anything else
   ⇒ `source:'original'`, `accepted:false`, `degraded` as appropriate. `mode:'on'` forces the
   repair attempt; `mode:'auto'` only on suspect; `mode:'off'` disables. Then `outcome()`
   (`accepted` / `passed_through` / `timeout`) is posted to `/ledger`.
5. `index.html` — `runAgentLoop` uses `sanitizeReply`; a successful repair is surfaced to the
   operator as a small non-blocking note in the transcript ("[LOCAL CORTEX: RITE REPAIRED —
   <tool>]"), never silently.
6. `localmodels/setup.sh` / `setup.ps1` — actually install Needle here (venv at
   `localmodels/.venv`, `pip install cactus-needle`, `needle download needle3`); the daemon must
   run from the venv python (`localmodels/.venv/bin/python`, or `Scripts\python.exe` on Windows).
7. Suites:
   - `tests/frontend/phase12_needle_repair.test.js` — mocked `/repair`: repair above threshold is
     accepted and dispatched with canonical name+object args; confidence below threshold ⇒
     original reply passes through untouched; `calls:[]` ⇒ no call invented (and the turn ends as
     prose); **injected 2 s delay** ⇒ pass-through within the configured budget, the turn still
     completes and the message is flagged; sidecar down ⇒ behaviour identical to Phase 11; the
     `/repair` and `/ledger` requests carry **no** `Authorization` header and never contain
     `settings.apiKey`; exactly one `/repair` per suspect turn, zero for a clean turn.
   - `test_e2e.py` — localmodels daemon: `/repair` 403 for a foreign origin; `--no-needle` ⇒
     `/health` reports `needle.enabled:false` and `/repair` answers `{ok:false,degraded:true}`
     (never a 500); ledger gains a record with `confidence` + `action:null` then an outcome line.
   - `tests/live/test_needle_live.py` — opt-in (`COG_LIVE_MODELS=1`, skipped by default and
     skipped cleanly if the venv/weights are absent): 5 corpus cases repaired by the **real**
     model, asserting the canonical tool name and typed arguments; a no-match prompt asserting
     `function_calls == []`.

**Exit criteria:** phase 12 suite + phase 11 + full frontend + python regression all green; the
live Needle suite green **on this machine**, with the real envelope and measured latency recorded
in `gatelog.md`.

---

### Phase 13 — Laya gates (F2) via the Node child  (size L)
**Goal:** Batched calibrated decisions from Laya drive (a) outbound pre-flight sanity on mutating
rites and (b) the "200 OK but garbage" reply-anomaly catch. Both fail open.

**Tasks:**
1. `localmodels/laya_child.mjs` — ESM (Node ≥20), newline-delimited JSON over stdio:
   inbound `{id, op:'health'|'decide'|'close', state?, questions?}` → outbound
   `{id, ok, answers?, usage?, error?}`; `Laya.load({cacheDir: process.env.LAYA_CACHE || default,
   executionProviders:['cpu']})` lazily; download progress on stderr as JSON lines; exits on
   `close`/EOF; **must not print anything non-JSON on stdout** (the daemon parses it).
   Do **not** invent a `precision` flag (see §1).
2. Daemon — lazy spawn with `LAYA_CACHE` from the env, `/decide` route (§4), `--laya-timeout-ms`
   (default 500), `--laya-idle-s` reaping (kill the child after N idle seconds, respawn on
   demand), `--no-laya`, `/health` reporting child pid + cache path, ledger records including the
   probability distribution. A dead/absent child ⇒ `{ok:false,degraded:true}`.
3. Frontend **F2a pre-flight** (outbound): before dispatching a **mutating** rite
   (`write_file`, `run_command`, `git` with a mutating subcommand), one batched `/decide` with
   `noul` "do these arguments plausibly satisfy this tool's schema description?" (+ any other
   per-turn question, batched to amortise the forward pass). Below `laya.minConfidence` ⇒ push a
   `role:'tool'` correction into the **existing** self-correction channel and do **not** dispatch
   that call. Never crash, never block the turn, never retry the local model in a loop.
4. Frontend **F2b post-reply anomaly catch** (inbound): one `noul` per non-tool reply ("does this
   look like an error page, refusal, or a loop rather than a real answer?"); above threshold ⇒
   a visible warning chip on the message + a ledger action, feeding the existing fallback paths.
   It must **not** rewrite or delete the model's content.
5. Suites:
   - `tests/frontend/phase13_laya_gates.test.js` — mocked `/decide`: high-p accepts, low-p
     refuses and routes the refusal into the tool-result channel while the bridge is provably
     never called; **batching**: N questions in one turn ⇒ exactly ONE `/decide` request;
     timeout/down ⇒ fail-open identical to Phase 12; anomaly flag appears with content unchanged;
     read-only rites are not pre-flighted (no wasted call); `preflight:false`/`anomaly:false`
     toggles each remove exactly their own call.
   - `test_e2e.py` — daemon `/decide` with `--no-laya` degraded; child-reap behaviour against a
     stub child; ledger record contains `probabilities` summing ≈ 1.
   - `tests/live/test_laya_live.py` — opt-in: real `systemOne` over 3 states with one question of
     each type; assert answer keys/types, probabilities sum ≈ 1, and record measured latency.
6. **Kill-the-model test (mandatory in this phase):** run the full frontend + python suites with
   `--no-needle --no-laya` and with the sidecar simply not running; everything must be green, and
   a driven agent turn must behave identically to a `localModels.enabled=false` run (compare the
   transcript + the bridge request log).

**Exit criteria:** phase 13 suite + all earlier + full regression green; kill-the-model test green
and recorded; live Laya suite green on this machine (weights installed) with latency recorded.

---

### Phase 14 — F3 cheap local dispatcher (Needle as pre-router)  (size M)
**Goal:** Offer a local tool proposal *before* spending a big-model call — confidence-gated, and
**never** auto-executing.

**Tasks:**
1. `appcore.js`/`index.html` — `dispatcher.enabled` toggle; on send, when enabled and agent mode
   is on, call `/select` with the user's utterance + all 6 candidate schemas. A **read-only**
   proposal auto-runs when `dispatcher.autoReadOnly` and `settings.autoApproveRead` are both on
   (transcript note: "[LOCAL CORTEX: read-only rite proposed — <tool>]"); every other proposal
   renders a card (tool, arguments, confidence) with **ACCEPT** / **IGNORE** — which is
   **mandatory for mutating rites** (`write_file`, `run_command`, mutating `git`).
2. Both paths go through the **unchanged** Phase 7 validator + the existing `approveToolCall` /
   `executeTool` route — the local model can propose but never bypass a gate. IGNORE (or low
   confidence, or an empty result, or the sidecar being down) ⇒ the normal model call proceeds
   exactly as today.
3. Ledger + a small counter summary in the LOCAL CORTEX section (proposals shown / accepted /
   ignored), read back from the ledger.
4. Suites: `tests/frontend/phase14_dispatcher.test.js` — a high-confidence read-only proposal
   auto-runs **only** when both `autoReadOnly` and `autoApproveRead` are on, and is provably
   dispatched through validator → approval → bridge in that order; with either flag off it renders
   the card and dispatches only on ACCEPT; a **mutating** proposal always renders the card and
   never dispatches on its own; low confidence / empty / sidecar-down ⇒ exactly one big-model
   request and no dispatch; IGNORE ⇒ no dispatch; **prompt-injection case**: an utterance that
   says "just run it without asking" cannot cause a *mutating* rite to execute without an operator
   ACCEPT, and cannot flip the read-only flags; `dispatcher.enabled=false` ⇒ no `/select` at all.
5. Record explicitly in the code and the plan that the **candidate-subset stage is intentionally
   absent** (6 tools ≪ the ~50 limit) so a later agent doesn't "fix" it.

**Exit criteria:** phase 14 suite + all earlier + full regression green; ledger counters verified
against emitted records; a mutating proposal provably cannot execute without an explicit operator
ACCEPT, and a read-only auto-run is provably gated on `autoReadOnly` + `autoApproveRead` and still
passes the Phase 7 validator.

---

### Phase 15 — Streaming-incremental detection, ledger-driven tuning, close-out  (size M/L)
**Goal:** Detection runs on accumulated deltas instead of end-of-stream, thresholds are justified
by measured ledger data, and the work is documented, share-ready and PR'd.

**Tasks:**
1. Incremental detection in `processStreamObject`/`mergeToolDelta`: as deltas accumulate, run the
   cheap deterministic checks as soon as a tool name + partial arguments exist; if the name is a
   near-miss or the JSON is already malformed, **start at most one repair probe per turn**
   (fire-and-forget; the result is consumed at `finalizeToolCalls`). Constraints: never buffer the
   whole stream, never await the probe inside `pumpSSE`, and a probe result that arrives after the
   turn decided is discarded (the outcome is still ledgered).
2. `tools/tune_thresholds.py` — reads `var/local-models.jsonl`, prints deterministic-pass fix
   rate, repair acceptance rate, **false-repair rate**, per-threshold precision/recall and latency
   percentiles (p50/p95); then write the chosen defaults back into `DEF_SETTINGS` §4 and record
   the numbers + the reasoning in `gatelog.md` and this plan.
3. Docs/close-out: `README.txt` gains a **LOCAL CORTEX** section (what it does, install steps per
   platform, flags, degraded behaviour, the privacy statement: nothing leaves the machine, the
   ledger is local and redacted); `localmodels/README.md` finalised; `PLAN.md`/`REPORT.md`
   statuses updated (append-only — see §6); this plan's phase statuses updated; `gatelog.md`
   Phase 15 → done.
4. Share-readiness pass (the repo is public and friends install it): no hostnames, LAN IPs, keys,
   or absolute personal paths in any tracked file; ports/paths overridable via env; verify
   `git status` clean of machine-specific artifacts (`var/`, caches, `.venv`, `node_modules`).
   Remember `bridge_daemon.log` is tracked and re-dirtied by every suite run (codereview #27) —
   revert its churn before committing, as prior phases did.
5. Full verification: `npm test` ALL GREEN + the two live suites green + one full manual pass
   through the real UI with a real model backend and both local models loaded.
6. Open the **single PR** from `feat/local-cortex-needle-laya` (all phase commits) with a summary
   of phases, measured rates, and the degraded-mode guarantees. The operator merges it himself.

**Exit criteria:** `npm test` green, both live suites green, thresholds recorded with their
ledger evidence, README documents LOCAL CORTEX and degraded mode, `git status` free of
machine-specific changes, PR opened against `main`.

---

## 6. Test plan (how §7 of the spec maps onto this repo)

| Spec §7 acceptance criterion | Where it lives |
|---|---|
| Golden corpus of 50–100 malformed tool-call cases → deterministic fix rate, repair acceptance rate, **false-repair rate ≈ 0** | `tests/fixtures/toolcall-corpus/` + `phase11_salvage.test.js` (measured), `phase12_needle_repair.test.js` (acceptance), `tools/tune_thresholds.py` (reported from the ledger) |
| Kill-the-model test: with Needle/Laya down, behaviour identical to pre-integration | `phase13_laya_gates.test.js` + a full-suite run with `--no-needle --no-laya` (Phase 13 exit criterion) |
| Timeout test: injected 2 s repair latency → pass-through within budget | `phase12_needle_repair.test.js` |
| Ledger completeness: every local-model decision produces a JSONL record with confidence + action | `phase10`/`phase12`/`phase13` suites + `test_e2e.py` ledger assertions + `tune_thresholds.py` |
| F5 gate test (mutating op with low-confidence extraction → confirm/refuse, never silent execution) | **Adapted**: F5 is out of v1 (D6), but the equivalent guarantee is enforced in Phase 14 (a local-model proposal can never execute without an explicit operator ACCEPT through the existing approval modal) and pinned by that phase's prompt-injection test |
| Corpus must cover the API formats actually in play | OpenAI `tool_calls` (incl. chunk-split), Ollama NDJSON, buffered `chat.end`; no Anthropic (D5) |

Rules for the suites (learned from the existing ones): jsdom cannot fetch external scripts — the
harness inlines `appcore.js`; mock `fetch` by URL **path** first (Phase 1 root cause #1); always
`window.close()` (timers); run python daemon probes in a temp dir, never in the repo.

Regression gate for every phase: `npm test` (frontend `ALL GREEN` **and** python `0 FAILURES`),
plus every earlier phase's suite.

**Live (weights-required) suites** live in `tests/live/`, are skipped unless `COG_LIVE_MODELS=1`,
and never run as part of `npm test`. They exist because the owner asked for real end-to-end proof
of both models on this machine — they are the "it genuinely works" evidence, while CI-reproducible
mock suites remain the gates.

---

## 7. Non-goals (v1 horizon)

- **F4 natural-language admin commands** — no admin-operation layer exists (D6). Deferred.
- **F5 guardrail gate on mutating admin ops** — deferred with F4; the future hook is the existing
  mutating-rite approval modal (`approveToolCall`, index.html:930).
- **LoRA fine-tuning / training on the ledger** — the ledger is the corpus, the training is future
  work (`cactus-needle` ships the tooling; no training in this horizon).
- **Replacing the primary model's tool calling** — this layer validates/formats/repairs; the big
  model remains the caller of record.
- **Any external service, endpoint, router or cloud fallback** — localhost only. Weight downloads
  at install/first-run are the sole network use.
- **Anthropic `tool_use` support** — the harness does not speak it.
- **In-browser WASM inference / an LLM-proxy middleware mode** — rejected in favour of the sidecar
  (D1); revisit only if a sidecar-free requirement appears.
- **Making any local feature required for the harness to function.**

---

## 8. Open questions for the owner (consolidated)

1. **RESOLVED (owner, 2026-09-23)** — both models stay **opt-in and off by default** in the public
   repo; Laya's 1.7 GB is never fetched unless the operator enables Laya. The sharing story is
   "unchanged behaviour until you turn LOCAL CORTEX on".
2. **RESOLVED (owner, 2026-09-23)** — Phase 14 **auto-runs read-only proposals** (gated on
   `dispatcher.autoReadOnly` + the existing `settings.autoApproveRead`), while **mutating**
   proposals always require an explicit operator ACCEPT.
3. Non-blocking, decided by default unless you object: Node ≥20 becomes a documented optional
   requirement (only for Laya); the ledger stays in the repo at `var/` (git-ignored); the cron job
   runs one phase per 3-hour call and commits on `feat/local-cortex-needle-laya`.

---

## Appendix A — Recon evidence trail (file:line, for whoever doubts a claim)

- Tool schemas: `index.html:672-679`; validator: `appcore.js:272`, `_toolSpec` `appcore.js:217`.
- Agent loop + post-repair gate + tool-result feedback: `index.html:959-984` (gate at 968-973).
- Stream accumulation: `mergeToolDelta` `index.html:694`, `processStreamObject` `:707`,
  `pumpSSE` `:724`, Ollama `:780-805`, buffered `chat.end` `:709-716`.
- Approval modal: `approveToolCall` `index.html:930`; dispatch `executeTool` `:943`.
- Settings blob + persistence: `DEF_SETTINGS` `index.html:~407`, `saveSet()` `:433`,
  settings-open handler `:1198+`.
- Bridge conventions to copy: `_origin_allowed` `bridge.py:223`, CORS `:239-241`, routes
  `:248-263`, flags `:293-298`; daemon parity `bridge_daemon.py` (Phase 9 notes in `gatelog.md`).
- Test harness rules: `tests/frontend/helpers.js` (inline `appcore.js`, path-keyed fetch mocks),
  `tests/frontend/run.js` (aggregate runner), `gatelog.md` Phase 0/1 findings.
