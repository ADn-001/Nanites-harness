# COGITATOR — Phased Development Plan (feature work)

Rule of the road (per operator directive): **a phase advances only when its own e2e test
suite is green**; failures are debugged and the suite re-run until green; status lives in
`gatelog.md`. Each phase also logs a *"info to know"* section in the gate log for the next
agents. Work is delegated per-phase to subagents; the integrator reviews every diff.

## Phase 0 — Readiness & frontend test rig
No product features. Establishes the machinery every later phase depends on.
- [x] 0.1 Split pure frontend logic into a shared `appcore.js` (UMD: browser `window.COG`
      + Node `module.exports`) so it is unit-testable without a browser.
- [x] 0.2 Add `tests/frontend/` Node+jsdom harness + `tests/frontend/run.js` runner and an
      npm script `npm run test:front`.
- [x] 0.3 Smoke test: load `index.html` in jsdom (mocked fetch/localStorage/Abort), assert
      the app boots, no thrown errors, `RITES/CONFIG` button opens the settings modal.
- [x] 0.4 Keep `python3 test_e2e.py` green.
Gate: `npm run test:front` passes AND `python3 test_e2e.py` == 0 FAILURES.

## Phase 1 — Provider endpoint profiles
- [x] 1.1 `appcore.js`: `profileStore` (list/get/save/remove/activate/apply) over an injected
      storage adapter; profile shape `{id,name,backend,endpoint,model?}`.
- [x] 1.2 `settings.profiles` + `settings.activeProfile` in the settings blob.
- [x] 1.3 Settings modal: PROVIDER PROFILES section (name/backend/endpoint/model fields,
      SAVE AS PROFILE / LOAD / DELETE, saved-profile dropdown).
- [x] 1.4 Loading a profile rewrites `endpoint/backend/model` live and triggers a model
      re-fetch; saving persists to localStorage.
- [x] 1.5 E2E suite `tests/frontend/phase1_profiles.test.js`: CRUD roundtrip, activate
      applies backend+endpoint+model, delete clears active when it was active, persistence
      across a simulated reload.
Gate: phase 1 suite green + regression green.

## Phase 2 — Agentic system prompt + workdir context
- [x] 2.1 `appcore.js`: `buildAgentSystemPrompt({workdir})` — short, precise, cheap for small
      contexts; states bridge jail = workdir, all paths relative to it, `list_dir(".")` = workdir
      contents, absolute host paths are refused.
- [x] 2.2 `appcore.js`: `buildSystemMessages({system, summary, workdirCtx})` — inserts the
      agent prompt + a `[WORKDIR CONTEXT]` system block (path + fresh listing) when agent
      mode is on.
- [x] 2.3 Frontend `buildMessages()`: refresh workdir listing from bridge `list_dir(".")` at
      send/agent-turn time; inject path + listing. Code fix for the folder-read bug.
- [x] 2.4 E2E suite `tests/frontend/phase2_sysprompt.test.js`: prompt contains relative-path
      rule; injected WORKDIR CONTEXT block present and carries the listing; user-supplied
      custom canticle preserved when not in agent mode; payload to `/v1/chat/completions`
      asserts the block.
Gate: phase 2 suite green + phase 1 + regression green.

## Phase 3 — Attachments (files / folders / images / from workdir)
- [x] 3.1 Composer `[+]` ATTACH: attach files, attach folder, images, and "from workdir".
- [x] 3.2 Attachment chips above the textarea with remove (×).
- [x] 3.3 Text files/folders inlined as fenced, path-labelled blocks (size cap, binary skip).
- [x] 3.4 Images as multimodal `content` arrays (`image_url` data URL); message path and the
      agent loop handle content-as-array.
- [x] 3.5 "Pick from workdir": browse bridge `list_dir`, read via `read_file`, inline.
- [x] 3.6 E2E suite `tests/frontend/phase3_attachments.test.js`: text inline, binary skip,
      size cap, image `content` array shape, from-workdir flow, remove chip.
Gate: phase 3 suite green + phases 1-2 + regression green. **COMPLETE.**

## Phase 4 — Integration close-out
- [x] 4.1 Full frontend suite + full python suite in CI-style one command.
- [x] 4.2 `gatelog.md` final pass; `REPORT.md`/`PLAN.md` statuses updated.
- [x] 4.3 README updated for new features (profiles, agent prompt, attachments).
Status: **COMPLETE (all phases 0-4 done; all suites green).**
Gate: every plan checkbox above checked and all suites green. — **Satisfied: `npm test` full run green.**

---

## Phase 5 — Dynamic per-endpoint API key (bearer auth) [RECON DONE, NOT STARTED]
**Why:** Provider endpoint profiles have no way to carry a credential. OpenAI-compatible
endpoints require an API key, but the profile shape is `{id,name,backend,endpoint,model}`
and **every** request to the model sends only `Content-Type` — there is no
`Authorization: Bearer …` header anywhere, and no key field. Recon of the full git history
(`90e51f8` incl.) confirms the key was **never** implemented (not hardcoded, not dynamic) in
either `index.html` or `bridge.py`; the old "hardcoded key" the operator remembers is not in
any committed revision. This phase makes the key a first-class, per-profile, dynamic value.
- [x] 5.1 `appcore.js` `profileStore`: extend profile shape to
      `{id,name,backend,endpoint,model,apiKey}`; `save()` persists it, `get()`/`apply()`
      return it; backward-compatible with profiles saved without `apiKey` (default `''`).
- [x] 5.2 Settings modal: add **API KEY (BEARER)** password input `#set-apikey` (with a
      show/hide eye toggle) in the MACHINE-SPIRIT ENDPOINT field block, next to `#set-endpoint`.
- [x] 5.3 `settings.apiKey` in the settings blob; `saveSet()` persists it; masked in the
      `[WORKDIR CONTEXT]`/system introspection and never echoed anywhere except the header.
- [x] 5.4 Auth header injection: helper `authHeaders()` → `{Authorization:'Bearer '+key}`
      when `settings.apiKey` non-empty, else `{}`. Apply to the OpenAI path
      (`/v1/chat/completions`) and the model-fetch endpoints (`/v1/models`,
      `/api/v1/models`, `/api/tags`). OLLAMA/LM-Studio paths unchanged (no header unless a
      key is set) so local no-auth backends keep working.
- [x] 5.5 SAVE CURRENT AS PROFILE includes the key; LOAD writes it back into `#set-apikey`
      and `settings.apiKey`; DELETE clears nothing global.
- [x] 5.6 E2E `tests/frontend/phase5_apikey.test.js`: save/load roundtrip carries key;
      applying a profile sets the live key; `/v1/chat/completions` request carries
      `Authorization: Bearer <key>` when set and omits it when empty; key persists across a
      simulated reload; auto-detected/keyless backends still send no header.
Gate: phase 5 suite green + phases 0-4 + python regression green.

## Phase 6 — Agentic system prompt redesign + structured-output contract [RECON DONE, NOT STARTED]
**Why:** `buildAgentSystemPrompt` advertises tools that **do not exist** (`shell_exec`,
`clipboard access`) and **omits** tools that do (`git`, `run_command`). There is no explicit
tool-call output template and no instruction to emit only valid JSON, so a small-context
model can free-form a response instead of a machine-parseable tool call. Redesign for correct,
robust agentic priming that works for small or large contexts.
- [ ] 6.1 Replace the hardcoded tool list in `buildAgentSystemPrompt` with a roster derived
      from the actual `TOOL_SCHEMAS` (read_file, write_file, list_dir, grep, git,
      run_command) and note `run_command` is disabled unless the bridge runs `--allow-exec`
      (mirror `bridge.py` `ALLOW_EXEC`). Never hardcode a stale tool name.
- [ ] 6.2 Add an explicit **structured tool-call contract** to the prompt: the exact JSON
      shape of a function call (`{"type":"function","function":{"name":…,"arguments":"{…}"}}`
      — inline escaped-JSON `arguments`, exactly as `/v1/chat/completions` expects), a rule
      that arguments must be valid JSON, one tool call per turn, observe the returned result,
      then continue or stop. Cost-aware phrasing for small contexts.
- [ ] 6.3 Keep (and verify) the workdir-jail rule, relative-path rule, `list_dir(".")`=workdir
      contents, absolute-host-path refusal, and the injected `[WORKDIR CONTEXT]` block.
      Orient on the bound workdir (`settings.workdir`), not the project root.
- [ ] 6.4 E2E `tests/frontend/phase6_sysprompt.test.js`: prompt names all six real tools,
      names **none** of `shell_exec`/`clipboard`, contains the JSON tool-call template, still
      contains the relative-path + workdir rules, and the agent-mode payload includes the
      tool schemas + `tool_choice:'auto'`.
Gate: phase 6 suite green + phase 5 + regression green.

## Phase 7 — Structured output validator (deterministic protection layer) [RECON DONE, NOT STARTED]
**Why:** The harness currently trusts whatever the LLM returns and dispatches any
`tool_calls` to the bridge. There is no deterministic schema/allow-list check between the
endpoint and the executor. This phase adds a pure validator so only safe, well-formed
structured output reaches the system the harness runs on.
- [ ] 7.1 `appcore.js`: `validateStructuredOutput(result, allowedTools)` — pure, deterministic,
      seeded: parse `tool_calls`; fail on non-JSON `arguments`; each call has `id`/`name`/
      `arguments`; `name` must be in `allowedTools` (the real `TOOL_SCHEMAS` names); arguments
      parse to a plain object; per-tool schema arg check (minimal required-field/type checks).
      Returns `{ok, errors:[…], sanitized}`. Never reaches for the network/fs.
- [ ] 7.2 `sanitizeToolCalls / rejectBeforeDispatch`: in the agent loop, run every
      `tool_calls` array through the validator **before** `executeTool`; a failed call is
      never dispatched — feed a `{role:'tool', …}` error back into the model loop instead
      (so the model can correct), keeping the bridge safe.
- [ ] 7.3 Wire into `runAgentLoop`/`callOpenAI` dispatch path so the bridge only ever
      receives validated tool names/args.
- [ ] 7.4 E2E `tests/frontend/phase7_validator.test.js`: valid call passes; malformed
      `arguments` (non-JSON, non-object) rejected; unknown tool name rejected; bridge
      dispatch is provably skipped for a rejected call (assert `events` never receive it).
Gate: phase 7 suite green + phases 5-6 + regression green.

## Phase 8 — Codereview #1 fix + close-out [RECON DONE, NOT STARTED]
**Why:** The most severe recorded issue is `index.html:707`
`acc.content+=o.content||acc.content` — on the `chat.end` aggregate stream shape a
content-less assistant message self-appends the accumulated buffer, duplicating the response.
- [ ] 8.1 Fix to `acc.content+=o.content||''` (one-character-class change).
- [ ] 8.2 Add an e2e case (phase8 or fold into existing suite): a content-less `message`
      object in `chat.end` output does **not** duplicate prior content.
- [ ] 8.3 Strike codereview issue **#1** from `codereview.md` (mark FIXED, keep the rest).
- [ ] 8.4 Full `npm test` green (frontend phases 0-7 + python regression); README/PLAN/
      gatelog/REPORT updated; read-only re-review pass for new smells → `codereview.md`.
Gate: full `npm test` green and codereview #1 struck off.