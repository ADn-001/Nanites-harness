# COGITATOR — Phased Development Plan (feature work)

Rule of the road (per operator directive): **a phase advances only when its own e2e test
suite is green**; failures are debugged and the suite re-run until green; status lives in
`gatelog.md`. Each phase also logs a *"info to know"* section in the gate log for the next
agents. Work is delegated per-phase to subagents; the integrator reviews every diff.

## Phase 0 — Readiness & frontend test rig
No product features. Establishes the machinery every later phase depends on.
- [ ] 0.1 Split pure frontend logic into a shared `appcore.js` (UMD: browser `window.COG`
      + Node `module.exports`) so it is unit-testable without a browser.
- [ ] 0.2 Add `tests/frontend/` Node+jsdom harness + `tests/frontend/run.js` runner and an
      npm script `npm run test:front`.
- [ ] 0.3 Smoke test: load `index.html` in jsdom (mocked fetch/localStorage/Abort), assert
      the app boots, no thrown errors, `RITES/CONFIG` button opens the settings modal.
- [ ] 0.4 Keep `python3 test_e2e.py` green.
Gate: `npm run test:front` passes AND `python3 test_e2e.py` == 0 FAILURES.

## Phase 1 — Provider endpoint profiles
- [ ] 1.1 `appcore.js`: `profileStore` (list/get/save/remove/activate/apply) over an injected
      storage adapter; profile shape `{id,name,backend,endpoint,model?}`.
- [ ] 1.2 `settings.profiles` + `settings.activeProfile` in the settings blob.
- [ ] 1.3 Settings modal: PROVIDER PROFILES section (name/backend/endpoint/model fields,
      SAVE AS PROFILE / LOAD / DELETE, saved-profile dropdown).
- [ ] 1.4 Loading a profile rewrites `endpoint/backend/model` live and triggers a model
      re-fetch; saving persists to localStorage.
- [ ] 1.5 E2E suite `tests/frontend/phase1_profiles.test.js`: CRUD roundtrip, activate
      applies backend+endpoint+model, delete clears active when it was active, persistence
      across a simulated reload.
Gate: phase 1 suite green + regression green.

## Phase 2 — Agentic system prompt + workdir context
- [ ] 2.1 `appcore.js`: `buildAgentSystemPrompt({workdir})` — short, precise, cheap for small
      contexts; states bridge jail = workdir, all paths relative to it, `list_dir(".")` =
      workdir contents, absolute host paths are refused.
- [ ] 2.2 `appcore.js`: `buildSystemMessages({system, summary, workdirCtx})` — inserts the
      agent prompt + a `[WORKDIR CONTEXT]` system block (path + fresh listing) when agent
      mode is on.
- [ ] 2.3 Frontend `buildMessages()`: refresh workdir listing from bridge `list_dir(".")` at
      send/agent-turn time; inject path + listing. Code fix for the folder-read bug.
- [ ] 2.4 E2E suite `tests/frontend/phase2_sysprompt.test.js`: prompt contains relative-path
      rule; injected WORKDIR CONTEXT block present and carries the listing; user-supplied
      custom canticle preserved when not in agent mode; payload to `/v1/chat/completions`
      asserts the block.
Gate: phase 2 suite green + phase 1 + regression green.

## Phase 3 — Attachments (files / folders / images / from workdir)
- [ ] 3.1 Composer `[+]` ATTACH: attach files, attach folder, images, and "from workdir".
- [ ] 3.2 Attachment chips above the textarea with remove (×).
- [ ] 3.3 Text files/folders inlined as fenced, path-labelled blocks (size cap, binary skip).
- [ ] 3.4 Images as multimodal `content` arrays (`image_url` data URL); message path and the
      agent loop handle content-as-array.
- [ ] 3.5 "Pick from workdir": browse bridge `list_dir`, read via `read_file`, inline.
- [ ] 3.6 E2E suite `tests/frontend/phase3_attachments.test.js`: text inline, binary skip,
      size cap, image `content` array shape, from-workdir flow, remove chip.
Gate: phase 3 suite green + phases 1-2 + regression green.

## Phase 4 — Integration close-out
- [ ] 4.1 Full frontend suite + full python suite in CI-style one command.
- [ ] 4.2 `gatelog.md` final pass; `REPORT.md`/`PLAN.md` statuses updated.
- [ ] 4.3 README updated for new features (profiles, agent prompt, attachments).
Gate: every plan checkbox above checked and all suites green.