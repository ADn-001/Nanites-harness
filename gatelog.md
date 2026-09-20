# GATELOG — COGITATOR feature work tracker

Format: current phase first. A phase is DONE only when its dedicated e2e suite is green and
the regression suite (`python3 test_e2e.py`) still reports `0 FAILURES`.

---

## Phase 0 — Readiness & frontend test rig
Status: **DONE**
Plan ref: `PLAN.md` Phase 0

- [x] Baseline confirmed: `python3 test_e2e.py` -> 0 FAILURES; Node v24.18.1; jsdom installed.
- [x] 0.1 `appcore.js` shared UMD module
- [x] 0.2 `tests/frontend/` jsdom harness + `npm run test:front`
- [x] 0.3 smoke: app boots in jsdom, settings modal opens
- [x] 0.4 regression green

### info to know (Phase 0)
- jsdom lacks a real `fetch`, real `AbortSignal.timeout`, and real `localStorage`; all must be
  stubbed. Service-worker register is guarded by `location.protocol.startsWith('http')` in the
  code, so jsdom (non-http) skips it safely.
- index.html runs its whole script at load and calls `testConnection(true)` + daemon polls
  (`setInterval` 3s) immediately — the harness must stub `fetch` to reject cleanly and keep
  the timer from crashing the run. Use `--unhandled-rejections=none` or catch in the stub.
- **CRITICAL (fixed this cycle):** jsdom **v30 removed `ResourceLoader` and the whole external
  `<script src>` fetch pipeline** — `class LocalLoader extends ResourceLoader` throws
  `TypeError: Class extends value undefined`. The harness must instead inline `appcore.js`
  into the html (replace `<script src="appcore.js">` with an inline `<script>` of its UMD
  content) so the module runs before the inline main script, faithful to real load order.
- jsdom `beforeParse` has no bare `localStorage` global (only `window.localStorage` after the
  window vends it); don't reference the bare identifier there.
- `setInterval` over the jsdom window keeps Node alive — always `window.close()` (helpers
  `teardownApp`) before the test process exits.

---

## Phase 1 — Provider endpoint profiles
Status: **DONE**
Plan ref: `PLAN.md` Phase 1

- [x] 1.1 `appcore.js` `profileStore` (list/get/save/remove/apply) over injected storage adapter; shape `{id,name,backend,endpoint,model}`.
- [x] 1.2 `settings.profiles` + `settings.activeProfile` in the settings blob.
- [x] 1.3 Settings modal PROVIDER PROFILES section (name/backend/endpoint/model fields, SAVE AS PROFILE / LOAD / DELETE, saved-profile dropdown) is wired (markup was scaffolded; handlers added this cycle).
- [x] 1.4 Loading a profile rewrites `endpoint/backend/model` live and triggers a model re-fetch (`testConnection(true)`); saving persists to localStorage.
- [x] 1.5 E2E `tests/frontend/phase1_profiles.test.js` green (0 failures); regression (`run.js` + `test_e2e.py`) green.

### info to know (Phase 1)
- **Gate was RED on pickup, not started from scratch.** A prior session scaffolded the Phase 1 test + appcore `profileStore` + the settings-modal markup + the `refreshProfileList()` *call* in the settings opener, but never defined `refreshProfileList()` or wired `prof-save`/`prof-load`/`prof-del`. So Phase 0's smoke test was also regressed (settings opener threw `refreshProfileList is not defined` before opening the modal). Completing Phase 1 fixed Phase 0 again.
- **Root cause #1 (mock mismatch):** `tests/frontend/helpers.js`'s fetch stub matched mocks by the *full URL*, but Phase 1 mocks are path-keyed (`{ '/v1/models': ... }`). With seed endpoint `http://ds:11434`, `api('/v1/models')` => `http://ds:11434/v1/models` never matched, so `/v1/models` was treated as a network error, the model dropdown stayed empty, and `setVal('set-model','deepseek-r1')` silently no-op'd (jsdom refuses to set a `<select>` value to an option that isn't present). Fix: match by `pathname` first, then fall back to the exact URL key. Backward compatible — Phase 0 (no routes) is unaffected, all unmocked fetches still reject.
- **Root cause #2 (missing wiring):** added `refreshProfileList()` (clears ALL options incl. the static placeholder — the test asserts `options.length === 0` after deleting the last profile, so no placeholder is re-added) plus `prof-save`/`prof-load`/`prof-del` onclick handlers in the inline script.
- **Root cause #3 (`remove` by name):** `profileStore.remove()` originally matched by `id` only; the unit test calls `remove(ad, 'lm')` where `lm` is the profile's *name* (because `save()` auto-generates a `uid` id). Extended `remove` to match by id **or** name. (Two profiles sharing a name would both be removed; acceptable for this app's scale.)
- **`apply` is the "activate" step:** `profileStore.apply(id)` returns the profile slice; the index.html handlers write `settings.activeProfile` + the live backend/endpoint/model fields + `saveSet()`, so activation lives in the UI layer, not a separate appcore method (test spec didn't require one — kept minimal per TDD).
- **LOAD triggers `testConnection(true)`** (plan 1.4). In the test the `/v1/models` mock returns `deepseek-r1`, which equals the profile's model, so the rebuilt dropdown keeps the selection green.
- **Process note:** implemented directly (tight red→green loop) rather than delegated to a subagent. `claude` CLI is not installed/authed in this environment; for this small, precisely-root-caused gate the tight-loop verification the TDD/systematic-debugging skills mandate outweighed subagent handoff latency. Phase 0 baseline was re-verified green after changes.

---

## Phase 2 — Agentic system prompt + workdir context
Status: **DONE**
Plan ref: `PLAN.md` Phase 2

- [x] 2.1 `appcore.js`: `buildAgentSystemPrompt({workdir})` — short, precise, cheap for small
      contexts; states bridge jail = workdir, all paths relative to it, `list_dir(".")` = workdir
      contents, absolute host paths refused.
- [x] 2.2 `appcore.js`: `buildSystemMessages({system, summary, workdirCtx})` — inserts agent prompt
      + `[WORKDIR CONTEXT]` system block (path + fresh listing) when agent mode on.
- [x] 2.3 Frontend `buildMessages()`: refresh workdir listing from bridge `list_dir(".")` at
      send/agent-turn time; inject path + listing. Code fix for the folder-read bug.
- [x] 2.4 E2E `tests/frontend/phase2_sysprompt.test.js` green (0 failures); phase0+phase1+python
      regression green.

### info to know (Phase 2)
- **Gate was RED on pickup, implementation already scaffolded.** `appcore.js` already had
  `buildAgentSystemPrompt` and `buildSystemMessages` (unit-level), and `index.html` already had
  `getWorkdirListing()` (line 533), `buildMessages()` (line 542), `runAgentLoop` + agent-mode
  tool injection in `callOpenAI` (line 726: `body.tools=TOOL_SCHEMAS;body.tool_choice='auto'`),
  and the `stream()` → `runAgentLoop` vs single call dispatch. The phase2 test file was also
  already written. Nothing was implemented from scratch this cycle — the only blocker was a
  syntax error that prevented the inline script from parsing at all.
- **Root cause (syntax error):** `index.html` line 536, the `getWorkdirListing()` fetch call,
  had an extra `}` in the closing brace/paren sequence — `}}})});` (4 braces + 2 parens) instead
  of `}}}` `)` `}` `)` `;` (3 braces + 2 parens). Specifically, after `path:'.'` the ending was
  `}` `}` `}` `)` `}` `)` `;` instead of `}` `}` `)` `}` `)` `;`. The extra `}` prematurely
  closed the fetch options object, causing `SyntaxError: missing ) after argument list` which
  broke the ENTIRE inline `<script>` — Phase 0 smoke, Phase 1 save/load, and Phase 2 agent
  payload all failed at boot because the script couldn't parse.
- **Fix:** removed the single extra `}` from line 536. Verified balanced: braces 4 open /
  4 close, parens 4 open / 4 close. All suites green after fix.
- **`buildMessages()` flow (index.html:542):** calls `getWorkdirListing()` (fetches
  `list_dir` with `path:"."` from bridge at `bridgePort||8931`) only when
  `settings.agent && settings.workdir`. Passes `{path: settings.workdir, listing: <result>}`
  as `workdirCtx` to `CogCore.buildSystemMessages()`. In non-agent mode, `settings.system`
  (user canticle) is passed directly — no workdir block, no tools, no orient prompt.
- **`callOpenAI` (index.html:726):** already checks `settings.agent` and sets
  `body.tools=TOOL_SCHEMAS;body.tool_choice='auto'`. No change needed.

---

## Phase 3 — Attachments
Status: **DONE**
Plan ref: `PLAN.md` Phase 3

- [x] 3.1 Composer `[+]` ATTACH menu (file / image / folder / "from workdir") wired to hidden inputs.
- [x] 3.2 Attachment chips row `#attach-chips` above the textarea with a per-chip remove (×).
- [x] 3.3 Text files/folders inlined as fenced, path-labelled blocks (`<ATTACHMENT [name]>` + code fence) with a 20k-char cap and binary skip.
- [x] 3.4 Images attached as multimodal `image_url` data-URL parts in a `content` array; `tok()`/`contentText()`/`autoTitle`/renderer all handle array content.
- [x] 3.5 "From workdir" picker: bridge `list_dir` lists root files, picking one `read_file`s it and attaches it.
- [x] 3.6 E2E `tests/frontend/phase3_attachments.test.js` green (0 failures); phases 0-2 + python regression green.

### info to know (Phase 3)
- **jsdom ships working `File`/`FileReader`/`Blob`, including `readAsDataURL`** — the e2e drives the real code path by constructing `new app.window.File([...], 'x.png', {type})` and passing it to the window-scoped `attachFiles()`. No special stubbing needed for file reading.
- **Function-valued fetch routes must return a Response-LIKE object** (`{ok,status,json:async()=>...}`). The harness `helpers.js` wraps only the *object* form of a route into a `Response`; a function route's return value is used verbatim, so returning `{json: <plain data>}` makes `r.json` "not a function" at runtime and the picker shows `r.json is not a function` in its error line.
- **A closed jsdom window still returns stale DOM nodes.** Reusing one `getElementById` helper (`$a`) across separate `launchApp()` instances silently drives the *previous, closed* app: `send()` fires on the old app and its POST lands in the OLD app's `events`, so the NEW app's `events` looks empty. Make a fresh helper per app.
- **Cap marker is deliberately long** (`[\n… TRUNCATED — …]`, ~65 chars). The test's length bound stays generous (`<= 100`) — assert truncation *happens* (length < input) and a `TRUNCATED` marker, not an exact size.
- Agent mode passes array `content` straight into `messages` (no change needed in `buildMessages` since it forwards `content` verbatim); `tok()` was taught to sum array text parts + a flat 85-token image cost so the context gauge/budget stay sane.
- Attachments are ephemeral (cleared on send); they are not persisted into the chat message beyond the built `content`, matching the plan.

---

## Phase 4 — Integration close-out
Status: **DONE**
Plan ref: `PLAN.md` Phase 4

- [x] 4.1 Full frontend suite + full python suite in CI-style one command (`npm test`).
- [x] 4.2 gatelog final pass; `REPORT.md`/`PLAN.md` statuses updated.
- [x] 4.3 README updated for new features (profiles, agent prompt, attachments).

### info to know (Phase 4)
- Gate was green FROM THE START of this cycle: all of Phase 0-3 suites plus the Python
  regression were already passing when Phase 4 was picked up. Phase 4 is documentation /
  harness close-out only — no product code changed this phase.
- `npm test` (package.json) already chains `node tests/frontend/run.js` then
  `python3 test_e2e.py`; it is the single CI-style command 4.1 calls for. Verified via a full
  `npm test` run (frontend ALL GREEN + python 0 FAILURES).
- All plan checkboxes (Phase 0-3 in PLAN.md) were already `[x]`; only Phase 4's three were
  `[ ]`. All are now checked.
- **All phases 0-4 are DONE.** Per the operator's standing directive, the next (non-gate)
  action after an all-green state is a **read-only code review** of the finished codebase,
  saved to `codereview.md`. That review is the follow-up step for the next agent call.
- Fresh commit made for this phase: `phase4: integration close-out`.
---

## Phase 5 — Dynamic per-endpoint API key (bearer auth)
Status: **DONE**
Plan ref: `PLAN.md` Phase 5

- [x] 5.1 `profileStore` shape → `{id,name,backend,endpoint,model,apiKey}`; backward-compatible (missing key defaults `''`; upsert without new key preserves the stored one).
- [x] 5.2 Settings modal: `#set-apikey` password input + show/hide eye toggle (`#api-key-eye`) in the endpoint block.
- [x] 5.3 `settings.apiKey` in blob (`saveSet()` round-trips); masked (`type=password`), never echoed into `[WORKDIR CONTEXT]`/system blocks.
- [x] 5.4 `authHeaders()` → `Authorization: Bearer <key>` when key non-empty, else `{}`; merged into `/v1/chat/completions` + model-fetch endpoints (`/v1/models`, `/api/v1/models`, `/api/v0/models`, `/api/tags`); local no-key backends send no header.
- [x] 5.5 Save/Load/DELETE include the key (LOAD restores into input + blob; DEL clears it when it deleted the active profile).
- [x] 5.6 E2E `tests/frontend/phase5_apikey.test.js` green (0 failures); phases 0-4 + python regression green.

Gate: **Satisfied — phase 5 suite green + phases 0-4 + `python3 test_e2e.py` 0 FAILURES.**

### info to know (Phase 5)
- **The key was genuinely never implemented in any revision** (recon of full history incl. `90e51f8`); this phase added it as greenfield. `gate` was RED first (profileStore had no `apiKey`; no `#set-apikey` input existed) — TDD RED verified before any implementation.
- `authHeaders()` is a shared helper (index.html, next to `api()`), so all model fetches send the bearer header from one seam. The tool bridge (`127.0.0.1:8931 /tools/execute`) is **not** given the key — only the model endpoint gets it. Verified by inspection; the bridge calls never reference `authHeaders()`.
- **Upsert semantics for `apiKey`:** `save()` preserves an existing stored key when an update omits `apiKey` (update without touching the key); pass an explicit `''` to clear. Backward-compatible with pre-existing profiles (they simply have no key → `''`).
- **Pitfall honored:** `codereview.md` #2 already flags silent-drop UX generally, but for a **secret** the key is `type=password` + only ever put in the `Authorization` header; it is not written into `[WORKDIR CONTEXT]`/system blocks or chat payloads.
- Phase 5 was implemented directly (tight TDD red→green) rather than delegated: the diff is small and precisely test-gated; same rationale as Phase 1's process note. `claude` CLI for subagent handoff is not authenticated in this env.
- Fresh commit made for this phase: `phase5: dynamic per-endpoint API key (bearer auth) + e2e`.

## Phase 6 — Agentic system prompt redesign + structured-output contract
Status: **RECON DONE — NOT STARTED**
Plan ref: `PLAN.md` Phase 6

- [ ] 6.1 Tool roster derived from real `TOOL_SCHEMAS` (read_file, write_file, list_dir, grep, git, run_command); note `run_command` requires `--allow-exec`.
- [ ] 6.2 Structured tool-call contract (JSON shape, inline escaped-JSON `arguments`, one call/turn, observe→continue/stop).
- [ ] 6.3 Keep workdir-jail + relative-path + `[WORKDIR CONTEXT]` block; orient on `settings.workdir`.
- [ ] 6.4 E2E `tests/frontend/phase6_sysprompt.test.js`.

Gate: phase 6 suite green + phase 5 + regression green.

### info to know (Phase 6 — recon)
- **Current prompt is WRONG about tools.** `buildAgentSystemPrompt` (appcore.js:134) tells the
  model tools are `list_dir, grep, read_file, write_file, shell_exec, clipboard access`. The
  bridge (`bridge.py:204 TOOLS`) actually exposes **read_file, write_file, list_dir, grep, git,
  run_command** — so the prompt advertises two nonexistent tools (`shell_exec`, `clipboard`) and
  omits two real ones (`git`, `run_command`). THIS IS THE CORE BUG this phase fixes.
- No structured-output template exists; prompt is prose-only. A small-context model has no
  explicit JSON contract to follow → free-form responses instead of parseable tool calls.
- `TOOL_SCHEMAS` live in `index.html:667` (name/desc/params for all six real tools) and are
  attached to the body as `body.tools; tool_choice:'auto'` when `settings.agent`. The prompt
  must derive its roster from these so it can never drift from reality again.
- `[WORKDIR CONTEXT]` injection already works (buildSystemMessages, appcore.js:152) — preserve.
- The past folder-read bug (model reading project root instead of bound workdir) was fixed in
  Phase 2/3 via prompt + code; the redesign must keep that relative-path rule prominent.

## Phase 7 — Structured output validator (deterministic protection layer)
Status: **RECON DONE — NOT STARTED**
Plan ref: `PLAN.md` Phase 7

- [ ] 7.1 `validateStructuredOutput(result, allowedTools)` — pure parse/schema/allow-list check → `{ok, errors, sanitized}`.
- [ ] 7.2 `sanitizeToolCalls / rejectBeforeDispatch` — reject before `executeTool`; feed `role:'tool'` error back to the model loop.
- [ ] 7.3 Wire into `runAgentLoop`/`callOpenAI` dispatch path.
- [ ] 7.4 E2E `tests/frontend/phase7_validator.test.js`.

Gate: phase 7 suite green + phases 5-6 + regression green.

### info to know (Phase 7 — recon)
- Today the agent loop (`runAgentLoop`, index.html ~955) trusts the model: any `tool_calls` in
  the assistant delta are melted by `mergeToolDelta` (index.html ~695) and dispatched to
  `executeTool` (index.html:943 → `POST /tools/execute`) with **no schema/allow-list gate**.
  Malformed `arguments`, unknown tool names, or non-JSON args currently flow straight to the bridge.
- A pure (network/fs-free) validator is the right seam — testable in jsdom via `appcore.js`.
- **Design note:** rejection must be *recoverable* — feed a `{role:'tool', name:…, content:'ERROR …'}`
  message back so the model corrects and retries, rather than hard-failing the whole loop.
- Keep the allowed set derived from `TOOL_SCHEMAS` names (single source), same as Phase 6, so the
  validator and the prompt can never disagree about what tools exist.

## Phase 8 — Codereview #1 fix + close-out
Status: **RECON DONE — NOT STARTED**
Plan ref: `PLAN.md` Phase 8

- [ ] 8.1 Fix `index.html:707` `acc.content+=o.content||acc.content` → `o.content||''`.
- [ ] 8.2 E2E case: content-less `message` in `chat.end` output does NOT duplicate prior content.
- [ ] 8.3 Strike codereview #1 in `codereview.md` (mark FIXED, keep the rest).
- [ ] 8.4 Full `npm test` green; README/PLAN/gatelog/REPORT updated; re-review pass.

Gate: full `npm test` green and codereview #1 struck off.

### info to know (Phase 8 — recon)
- The most severe recorded defect (codereview.md #1, index.html:707) is the `chat.end` aggregate
  stream shape: `o.content||acc.content` self-appends the buffer when a message has empty content.
  One-character-class fix to `o.content||''`. It does not fire on the default OpenAI SSE path
  (which streams `choices[].delta`), so it is latent — but it is the top item to strike off.
- Other codereview items (2 UX silent-drop, 3 folder-opt ignored, 4 workdir picker files-only,
  5 port duplication, 6 magic numbers, 8 redundant looksBinary cond, 9 remove-by-name) are
  NOT in scope for Phase 8 unless re-triaged; #1 is the operator-requested fix.
