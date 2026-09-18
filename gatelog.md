# GATELOG — COGITATOR feature work tracker

Format: current phase first. A phase is DONE only when its dedicated e2e suite is green and
the regression suite (`python3 test_e2e.py`) still reports `0 FAILURES`.

---

## Phase 0 — Readiness & frontend test rig
Status: **IN PROGRESS**
Plan ref: `PLAN.md` Phase 0

- [x] Baseline confirmed: `python3 test_e2e.py` -> 0 FAILURES; Node v24.18.1; jsdom installed.
- [ ] 0.1 `appcore.js` shared UMD module
- [ ] 0.2 `tests/frontend/` jsdom harness + `npm run test:front`
- [ ] 0.3 smoke: app boots in jsdom, settings modal opens
- [ ] 0.4 regression green

### info to know (Phase 0)
- jsdom lacks a real `fetch`, real `AbortSignal.timeout`, and real `localStorage`; all must be
  stubbed. Service-worker register is guarded by `location.protocol.startsWith('http')` in the
  code, so jsdom (non-http) skips it safely.
- index.html runs its whole script at load and calls `testConnection(true)` + daemon polls
  (`setInterval` 3s) immediately — the harness must stub `fetch` to reject cleanly and keep
  the timer from crashing the run. Use `--unhandled-rejections=none` or catch in the stub.

---

## Phase 1 — Provider endpoint profiles
Status: **NOT STARTED**
Plan ref: `PLAN.md` Phase 1

### info to know (Phase 1)
- (filled at gate)

---

## Phase 2 — Agentic system prompt + workdir context
Status: **NOT STARTED**
Plan ref: `PLAN.md` Phase 2

### info to know (Phase 2)
- (filled at gate)

---

## Phase 3 — Attachments
Status: **NOT STARTED**
Plan ref: `PLAN.md` Phase 3

### info to know (Phase 3)
- (filled at gate)

---

## Phase 4 — Integration close-out
Status: **NOT STARTED**
Plan ref: `PLAN.md` Phase 4

### info to know (Phase 4)
- (filled at gate)