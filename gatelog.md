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
Status: **PENDING (next agent)**
Plan ref: `PLAN.md` Phase 4

### info to know (Phase 4)
- (filled at gate)