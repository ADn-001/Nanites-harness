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
Status: **DONE**
Plan ref: `PLAN.md` Phase 6

- [x] 6.1 `appcore.js`: tool roster derived from the LIVE `TOOL_SCHEMAS` (read_file, write_file, list_dir, grep, git, run_command) via `CogCore._toolNames(opts.tools)`; no phantom `shell_exec`/`clipboard`; `run_command` flagged as requiring bridge `--allow-exec`.
- [x] 6.2 Structured tool-call contract added to the prompt: exact JSON shape `{"type":"function","function":{"name":"<tool>","arguments":"{...}"}}` with inline escaped-JSON `arguments`, exactly one call per turn, STOP → observe → continue-or-stop.
- [x] 6.3 Workdir-jail + project-relative + host-absolute-refusal rules + `[WORKDIR CONTEXT]` block preserved; orient on `settings.workdir`; `index.html` passes `tools:TOOL_SCHEMAS` so the prompt cannot drift.
- [x] 6.4 E2E `tests/frontend/phase6_sysprompt.test.js` green (0 failures); phases 0-5 + python regression green.

Gate: **Satisfied — phase 6 suite green; `npm test` full run ALL GREEN (frontend) + 0 FAILURES (python).**

### info to know (Phase 6)
- **Gate was RED on pickup with a real correctness bug.** The old `buildAgentSystemPrompt` (appcore.js:141) advertised `shell_exec` and `clipboard access` — neither exists on the bridge — and silently omitted `git` and `run_command`, the two real tools it was missing. Phase 6's RED test exposed exactly this (14 failures: phantom names present, git/run_command absent, no contract). This was the core bug the recon flagged; now fixed.
- **Roster is derived, not hardcoded.** `_toolNames(tools)` reads names off TOOL_SCHEMAS-shaped entries (`{type:'function',function:{name}}`) and dedupes. Falling back to the six real names when `tools` is empty/absent keeps `buildAgentSystemPrompt()` testable and non-drifting, so a future harness can't silently narrow the roster by passing nothing.
- **Contract phrasing is deliberately compact** — "emit EXACTLY ONE function call ... `{"type":"function","function":{"name":"<tool>","arguments":"{...}"}}` ... STOP and wait for the tool result. Observe ... then continue or stop." Stays short enough for small-ctx models (asserted `< 2200` chars).
- **`run_command` law is explicit:** "run_command is DISABLED unless the bridge was started with --allow-exec; if the model calls it and it is refused, do not retry it — choose another tool or report the limitation." This mirrors `bridge.py` `ALLOW_EXEC=False` default and prevents the agent from spinning on a tool that will always refuse.
- **The live wiring is one seam:** `index.html` `buildMessages()` now passes `tools:TOOL_SCHEMAS` into `buildAgentSystemPrompt`. Since the prompt and the `body.tools` payload now share the same source, prompt-routing and tool-allow-list can never disagree.
- Process: implemented directly (tight TDD red→green), same rationale as Phases 1/5 — the change is small, precisely test-gated, and `claude` CLI is unauthenticated in this env.

## Phase 7 — Structured output validator (deterministic protection layer)
Status: **DONE**
Plan ref: `PLAN.md` Phase 7

- [x] 7.1 `validateStructuredOutput(result, allowedTools)` — pure parse/schema/allow-list check → `{ok, errors, sanitized}`.
- [x] 7.2 `sanitizeToolCalls / rejectBeforeDispatch` — reject before `executeTool`; feed `role:'tool'` error back to the model loop.
- [x] 7.3 Wire into `runAgentLoop`/`callOpenAI` dispatch path.
- [x] 7.4 E2E `tests/frontend/phase7_validator.test.js`.

Gate: **Satisfied — phase 7 suite green (0 failures); phases 0-6 + `python3 test_e2e.py` 0 FAILURES (`npm test` ALL GREEN).**

### info to know (Phase 7)
- **Gate was RED on pickup (real vulnerability, not a missing test):** the RED run proved the
  live hole — a `read_file` call with the malformed arguments `{oops` was **dispatched to the
  bridge** (auto-approved as a read rite), so unvalidated LLM output reached the machine. The
  other two "never dispatched" assertions passed at RED *for the wrong reason*: an unknown/mutating
  tool name is a MUTATION, so `approveToolCall()` parked it on the rite-authorization modal before
  ever POSTing. Be aware of that trap — **"no bridge event" alone is not proof of a gate**; assert
  the approval modal never opened (`#agent-modal` lacks `.open`) as well.
- **Where the gate lives:** `appcore.js` gained `validateStructuredOutput` + three pure helpers
  (`_toolSpec`, `_normalizeToolCalls`, `_argTypeOk`); `index.html` `runAgentLoop` (~line 966) runs
  `CogCore.validateStructuredOutput(m.toolCalls, TOOL_SCHEMAS)` and dispatches **only**
  `gate.sanitized`. `TOOL_SCHEMAS` is the single source for both the allow-list and the per-tool
  required/typed-parameter checks, so prompt, `body.tools` and the validator can never disagree.
- **Rejection is recoverable, not fatal:** each rejected call pushes
  `{role:'tool', name, toolCallId, content:'[RITE REJECTED BY VALIDATOR — NOT dispatched: <reason>…]'}`
  so the model corrects on the next iteration. `finalizeToolCalls` always mints an id, so every
  assistant `tool_call` gets exactly one correlated tool result — rejected **or** executed — which
  keeps strict OpenAI-compatible endpoints happy.
- **`sanitized[].args` is a parsed object, not a string.** `safeToolArgs()`/`approveToolCall()`/
  `executeTool()` already accepted objects (`typeof tc.args==='string'?JSON.parse:tc.args`), so
  parsed args flow through unchanged and reach the bridge as real JSON objects.
- **Accepted input shapes (deliberate leniency):** array of calls, `{toolCalls:[{name,args}]}`
  (this app's finalized shape) or `{tool_calls:[{id,type,function:{name,arguments}}]}` (raw OpenAI
  assistant message). Garbage in (`null`, `0`, `'nope'`, `{}`, `{tool_calls:'x'}`) returns
  `{ok:false, errors:[…], sanitized:[]}` — it never throws, because a throw inside the agent loop
  would abort the whole turn.
- **`allowedTools` accepts schema objects OR plain name strings.** An empty/absent list means "no
  name filtering" (still parses arguments), which keeps the helper usable standalone; passing the
  real `TOOL_SCHEMAS` is what enables the allow-list + required/type checks.
- **Harness notes for the next agent:** the inline script declares `const TOOL_SCHEMAS`, so it is
  **not** a `window` property — grab it with `app.window.eval('JSON.stringify(TOOL_SCHEMAS)')` and
  `JSON.parse` in the test realm. To drive the SSE agent loop in jsdom you must (a) supply a
  function-valued route returning a Response-**like** object with a fake `body.getReader()` that
  yields `data: {…}\n\n` chunks then `{done:true}`, and (b) stub `app.window.TextDecoder`
  (`decode(v){return String(v)}`) *before* clicking send — jsdom ships neither. Route functions are
  used verbatim by `helpers.js` (only object routes get wrapped into a `Response`).
- Process: implemented directly (tight TDD red→green), same rationale as Phases 1/5/6 — the change
  is small and precisely test-gated, and the `claude` CLI is unauthenticated in this env.
- **Phase 8 (codereview #1, `index.html:707` `acc.content+=o.content||acc.content`) is the only
  phase still open.** Note the line number has shifted; grep for the expression, not the line.

## Phase 8 — Codereview #1 fix + close-out
Status: **DONE**
Plan ref: `PLAN.md` Phase 8

- [x] 8.1 Fix `index.html:707` `acc.content+=o.content||acc.content` → `o.content||''`.
- [x] 8.2 E2E case: content-less `message` in `chat.end` output does NOT duplicate prior content.
- [x] 8.3 Strike codereview #1 in `codereview.md` (mark FIXED, keep the rest).
- [x] 8.4 Full `npm test` green; README/PLAN/gatelog/REPORT updated; re-review pass.

Gate: **Satisfied — `npm test` ALL GREEN (frontend phases 0,1,2,3,5,6,7,8) +
`python3 test_e2e.py` 0 FAILURES; codereview #1 marked FIXED.**

### info to know (Phase 8)
- **Gate was GREEN on pickup (baseline re-verified first).** Per the standing directive, every
  suite in `tests/frontend/` + `python3 test_e2e.py` was run *before* touching anything: all
  phases 0-7 green, `0 FAILURES`. So Phase 8 was the only open work and no prior agent had left
  a half-finished phase or a stale gatelog entry.
- **The defect was real and the RED run proved it.** New suite
  `tests/frontend/phase8_streamagg.test.js` drives the REAL path end-to-end
  (`send()` → `stream()` → `callModel()` → `callOpenAI()` → `pumpSSE()` →
  `processStreamObject()`) with a scripted SSE `body` carrying
  `{type:'chat.end', result:{output:[…]}}`. At RED it printed
  `stored content is exactly "Hello world" (got "Hello worldHello world")` — the bug is exactly
  the self-append codereview #1 described. **Note the compounding:** with N content-less
  `message` objects the buggy line *doubles* the buffer each time (`"Alpha"` →
  `'Alpha'×8` for 3 empty messages; case C in the suite pins this), because each append
  re-appends the whole accumulated string.
- **Fix is one character-class:** `o.content||acc.content` → `o.content||''` (index.html:712;
  the line number has shifted from the 707 recorded in codereview — **grep the expression, not
  the line**). No other production code changed in this phase.
- **Case B/D in the suite pass at RED too — deliberately.** When the *first* `message` object is
  empty, `acc.content += acc.content` on an empty buffer is a no-op, so a leading-empty or
  reasoning/tool-call-only turn never showed the symptom. They are guards; do not read a green
  case B as "the bug is gone". The load-bearing assertions are case A and case C.
- **Harness notes reused from Phase 7** (still true): jsdom ships no `TextDecoder` — stub
  `app.window.TextDecoder` *before* clicking send; a function-valued fetch route is used
  verbatim by `helpers.js` (only object routes get wrapped into a `Response`), so a streaming
  route must return its own `{ok,status,json,text,body:{getReader(){…}}}` Response-like object.
  `active()` is a top-level function declaration so it is reachable as `app.window.active()`
  — the suite asserts both the **rendered** `#body-N` text and the **stored** message content.
- **The live OpenAI SSE path is unaffected** (case E asserts `choices[].delta` still
  concatenates in order) — this bug only ever fired on the buffered aggregate shape.
- **All phases 0-8 are now DONE.** Per the standing directive, the follow-up for the next agent
  call is `codereview.md` — it now carries the Phase 8 re-review pass (items 10-13, all read-only
  findings, none blocking). No new phase is open.
- Process: implemented directly (tight TDD red→green), same rationale as Phases 1/5/6/7 — the
  change is one character-class and precisely test-gated; the `claude` CLI for subagent handoff
  is not authenticated in this environment.
- Fresh commit made for this phase: `phase8: fix chat.end aggregate content duplication + e2e`.

---

## Post-gate pass — baseline re-verification + read-only code review (2026-09-22, cron)
Status: **NO OPEN PHASE — review pass complete; `codereview.md` updated (pass 3, items 14-26).**
Plan ref: none (all PLAN.md phases 0-8 are DONE)

- [x] Baseline re-run before touching anything: `npm test` → `FRONTEND SUITE: ALL GREEN`
      (phases 0,1,2,3,5,6,7,8) + `python3 test_e2e.py` → `0 FAILURES`. Head `b3158c9`.
- [x] No half-finished phase, no stale gate entry, no leftover failing suite from a prior agent.
- [x] All phases 0-8 verified DONE → per the standing directive, the action is a **read-only** code
      review; product code was NOT modified this run.
- [x] Review written to `codereview.md`: status table for items 1-13 + new findings 14-26, each with
      file/line and, where applicable, output of an executed probe.

### info to know (post-gate review pass)
- **Nothing is gated green that isn't green** — the suites are honest; the review found defects the
  suites simply do not cover (see item 26), which is the useful pattern here: a green phase gate
  proves what its cases assert, nothing more. Two live bugs (#16, #17) and one live security hole
  (#14) had zero coverage.
- **#14 is the top item.** `bridge_daemon.py` (port 8930) has **no** `_origin_allowed()` at all,
  unlike `bridge.py`. Reproduced: from a foreign `Origin`, `POST /set_workdir` returned `ok:true` and
  planted `bridge.py` into an attacker-chosen directory, and `POST /install_autostart` really did
  register `~/.config/autostart/CogitatorBridgeDaemon.desktop` (the probe removed it again; no repo
  file was touched, and the probe copies live under `/tmp`). **If you re-run such a probe, check for
  that autostart file and remove it.**
- **#16 is the sneakiest.** `appcore.js` has an array-aware `CogCore.tok`, but `index.html:430`
  shadows it with a local `tok` that treats a multimodal `content` array as `array.length/4`. Probed
  in the real app: a 20 000-char inline attachment prices at **1 token** and the gauge reads
  `1/131.1k` (correct value 5000). No test references `tok`/`CogCore.tok`/`ctxTokens` at all, so
  Phase 3's gatelog claim that the gauge/budget "stay sane" is true of a function the app never
  calls. Trust the shipped call path, not the helper next to it.
- **#13's original claim was stale** (recorded for accuracy): `settleApproval` has cleared
  `approvalResolve` since the initial commit. What remains true is only that a second concurrent
  `approveToolCall` would clobber the first.
- Probe technique that worked for the services (no in-repo side effects): copy `bridge.py` +
  `bridge_daemon.py` into a temp dir, run them on spare ports (`--port 8994/8995`) with a temp jail,
  and `curl -H 'Origin: …'`. The daemon writes its pid/log next to itself, so never probe the daemon
  in-place in the repo.
- **Next action for the following agent:** no phase is open. If the operator wants the findings
  actioned, open **Phase 9** in `PLAN.md` in the order given in `codereview.md`'s Summary (#14+#15
  security, then #16+#17, then #18+#19, then #10) — each with its own e2e suite and a red-first run,
  as the phase discipline requires.

---

## Phase 9 — Security hardening (codereview #14/#15) + live correctness (#16/#17)
Status: **DONE**
Plan ref: `PLAN.md` Phase 9

- [x] 9.1 [SEC #14] `bridge_daemon.py`: `Handler._origin_allowed()` mirroring `bridge.py`, enforced
      as the FIRST statement of `do_GET` and `do_POST` → 403 `{"ok":false,"error":"origin not permitted"}`
      before any route logic; `--allow-any-origin` + new `--allow-file-origin`; `/health` `/status`
      report the policy; startup banner logs `origins : <desc>`.
- [x] 9.2 [SEC #15] `bridge.py`: `Origin: null` is no longer trusted by default (missing Origin —
      curl/native — still allowed); new `--allow-file-origin` opt-in; banner/docstring/`/health`
      updated (`origins_desc()`).
- [x] 9.3 [BUG #16] `index.html:430` `const tok=s=>CogCore.tok(s);` — the array-aware estimator is
      now the live one, so the gauge, `buildMessages()` budget walk, `maybeAutoCompact` and
      `compactChat` retention all price attachments correctly and object tool-args no longer yield NaN.
- [x] 9.4 [BUG #17] `index.html:556` COPY uses `CogCore.contentText(...)`.
- [x] 9.5 RED-first suites: 15 new origin/bridge cases in `test_e2e.py`; new
      `tests/frontend/phase9_tok_copy.test.js` (37 cases).
- [x] 9.6 `codereview.md`: #14-#17 marked FIXED, pass-4 row added, new findings #28/#29 recorded;
      `README.txt` flags/verification list updated.

Gate: **Satisfied — `npm test` exit 0: `python3 test_e2e.py` → 0 FAILURES (now 66 cases, incl. the
whole daemon hostile-origin matrix) and `node tests/frontend/run.js` → FRONTEND SUITE: ALL GREEN
(phases 0,1,2,3,5,6,7,8 + new phase9: 0 FAILURES).**

### info to know (Phase 9)
- **Gate was GREEN on pickup for phases 0-8** (re-verified first, per the standing directive), so the
  only open work was codereview pass 3's priority list → Phase 9 opened in `PLAN.md`. Baseline before
  touching anything: `npm test` exit 0, python `0 FAILURES`, head `ddeefcf`.
- **Team-of-subagents model was used this phase** (operator directive): two `delegate_task` children ran
  in parallel on non-overlapping files — child A = security (#14/#15: `bridge.py`, `bridge_daemon.py`,
  `test_e2e.py`, `README.txt`), child B = frontend correctness (#16/#17: `index.html`, new phase-9
  suite). Neither was allowed to run the other's suite (`python3 test_e2e.py` and the frontend run
  would fight over ports) and neither was allowed to `git commit`; the integrator (this session)
  reviewed both diffs and re-ran the **full** `npm test` itself. Child summaries are self-reports —
  the diffs and the suites were re-read/re-run rather than trusted.
- **Both RED runs proved the real bug, not a missing test.** Python: 15 FAIL, including
  `FAIL- refused set_workdir wrote no bridge.py` (a foreign origin really did plant `bridge.py` into an
  attacker-chosen directory) and `FAIL- refused install_autostart created no autostart entry (it did;
  cleaned up)` (it really created `~/.config/autostart/CogitatorBridgeDaemon.desktop`). Frontend: 16
  FAIL, incl. `tok(20000-char attachment) >= 4000 (got 1)`, gauge `"1/131.1k"`, `tok(object) → NaN`,
  and COPY `"[object Object],[object Object]"`.
- **BEHAVIOUR CHANGE an operator must know:** `Origin: null` is now refused by **both** the bridge and
  the daemon. A page opened as `file://`, a `data:`/`blob:` document, or a sandboxed iframe loses bridge
  access unless the service is started with the new `--allow-file-origin`. The documented setup
  (`python -m http.server 8080` → `http://localhost:8080`) is unaffected and pinned by the suite.
  Also note the allow-list matches only the **http** scheme on loopback: serving the app as
  `https://localhost:8080`, or from a LAN address (`http://192.168.x.x:8080`), is refused — the bridge
  already behaved that way, the daemon now matches it. Use `--allow-any-origin` on **both** if the
  operator really serves the app off-loopback (and remember the tool bridge is then exposed too).
- **The guard is an Origin check, not authentication.** A request with **no** `Origin` header is still
  allowed (required for curl/native callers), so any local non-browser process can still drive the
  privileged daemon routes. If that matters, the next step is a per-start random token required as a
  header (codereview #14's preferred fix) — deliberately not done here to keep the phase minimal and
  fully testable.
- **`bridge_daemon.main()` now uses `argparse`** instead of hand-scanning `sys.argv` (`--port`,
  `--install-autostart`, `--remove-autostart` semantics unchanged, no new required args). Consequence:
  an *unknown* daemon flag now exits with a usage error instead of being silently ignored. The
  generated autostart entry's `Exec=` line passes no flags, so login autostart is unaffected — verified
  by inspection of `install_autostart()`.
- **Trap for whoever tests this next:** the hostile-origin probe is only safe because the guard runs
  *before* the route body. Any future route added to `do_GET`/`do_POST` must keep the guard as the first
  statement; the suite asserts the negative side effects (no `bridge.py` written, workdir unchanged, no
  autostart entry) for `/set_workdir` and `/install_autostart`, and it deletes a stray autostart entry
  if one appears before failing. Never probe the daemon in-place in the repo — it writes its pid/log
  next to itself.
- **`bridge_daemon.log` is tracked and re-dirtied by every suite run** (codereview #27). This phase's
  commit reverted the log churn before committing; expect `git status` to show it dirty again after any
  test run until #27 is fixed.
- Phase 9 commit: the single commit directly on top of `ddeefcf` (`git log --oneline -1`), carrying
  product code + tests + docs. `codereview.md` pass 4 records the same head, so the committed
  source/test bytes are exactly what `npm test` ran green.
- **New findings from the children's review of the shipped path, recorded as codereview #28/#29**
  (deliberately NOT fixed here — out of the phase's two-fix mandate): the `compactChat` compression
  prompt still stringifies an array `content`, so attachment text is lost from the summary
  (`index.html` ~631-642), and `refreshLast` renders last-message content without the array guard
  `msgHTML` has.
- **Next action for the following agent:** phases 0-9 are all DONE. Remaining open review items are
  the priority list in `codereview.md`: #18 (agent-loop-wide 180 s timeout misclassified as a rite
  failure), #19 (`compactChat` discards transcript when the summary came back empty), #28, #29, then
  #10 (oldest latent defect), then the UX/hygiene set (#20 SAVE AS PROFILE can only overwrite, #21
  `sw.js` cache never bumped, #23 dual profile source of truth, #24 duplicated bridge URL plumbing,
  #27 tracked log). Open **Phase 10** in `PLAN.md` for whichever the operator wants next — each with
  its own red-first e2e suite.
