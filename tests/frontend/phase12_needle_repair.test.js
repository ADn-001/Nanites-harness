'use strict';
/*
 * PHASE 12 E2E — Needle repair pass (`CogCore.sanitizeReply`) + agent-loop wiring.
 *
 * Contract under test (plan §4, phase12-workstream-B.md):
 *  - `CogCore.sanitizeReply(reply, allowedTools, deps)` = stage 1 deterministic salvage
 *    (`CogCore.salvageToolCalls`, unchanged) then an optional, bounded, gated Needle repair.
 *  - Local inference is NEVER in the critical path: sidecar down/disabled/slow/empty ⇒ the
 *    ORIGINAL reply passes through and the harness behaves byte-identically to Phase 11.
 *  - A repaired call is still gated by `CogCore.validateStructuredOutput` (Phase 7) — the local
 *    model never gets to dispatch anything the deterministic validator would reject.
 *  - `/repair` and `/ledger` carry NO `Authorization` header and never the provider API key.
 *  - Exactly ONE `/repair` per suspect turn; ZERO for a clean turn and ZERO when local models
 *    are disabled (not a single request to :8932).
 *  - A successful repair is surfaced to the operator as a transcript note.
 *
 * RED-FIRST: this file was written before `sanitizeReply` existed. Against the pre-change tree
 * the `sanitizeReply exists` checks and every driven-repair assertion FAIL; see the reported RED
 * evidence.
 */
const path = require('path');
const { check, summary, clearFails, launchApp, teardownApp } = require('./helpers');
const CogCore = require(path.join(__dirname, '..', '..', 'appcore.js'));

const tick = (ms = 30) => new Promise((r) => setTimeout(r, ms));
const BRIDGE = 'http://127.0.0.1:8931/tools/execute';
const SENTINEL = 'SENTINEL-API-KEY-9f2a';

/* Stable deep-equality by canonical serialisation (key order must not matter). */
function canon(v) {
  if (Array.isArray(v)) return '[' + v.map(canon).join(',') + ']';
  if (v && typeof v === 'object') {
    return '{' + Object.keys(v).sort().map((k) => JSON.stringify(k) + ':' + canon(v[k])).join(',') + '}';
  }
  return JSON.stringify(v === undefined ? null : v);
}

/* ---- SSE helpers (same shape the Phase 7/8/11 suites use) ---- */
function sseBody(events) {
  const chunks = events.map((e) => 'data: ' + JSON.stringify(e) + '\n\n');
  chunks.push('data: [DONE]\n\n');
  let i = 0;
  return {
    getReader() {
      return {
        read: async () => (i >= chunks.length
          ? { done: true, value: undefined }
          : { done: false, value: chunks[i++] })
      };
    }
  };
}
function toolDeltaStream(calls) {
  return {
    choices: [{
      delta: {
        tool_calls: calls.map((c, ix) => ({
          index: ix, id: c.id, type: 'function',
          function: { name: c.name, arguments: c.args }
        }))
      }
    }]
  };
}
function contentStream(text) { return { choices: [{ delta: { content: text } }] }; }
function chatRoute(script) {
  let call = 0;
  return () => {
    const events = script[Math.min(call, script.length - 1)];
    call++;
    return { ok: true, status: 200, json: async () => ({}), text: async () => '', body: sseBody(events) };
  };
}
const BASE_ROUTES = {
  '/v1/models': { status: 200, json: { data: [{ id: 'test-model' }] } },
  '/tools/execute': { status: 200, json: { ok: true, result: 'd src\nf main.py' } },
  '/api/v0/models': { status: 200, json: { object: 'list', data: [] } }
};

/* ---- settings / local-models config builders ---- */
function baseSettings() {
  return {
    endpoint: 'http://x', model: 'test-model', backend: 'openai', system: '',
    workdir: '/proj/omega', agent: true, autoBridge: false, autoApproveRead: true,
    profiles: [], activeProfile: '', apiKey: SENTINEL
  };
}
function lmCfg(over) {
  return Object.assign({
    enabled: true,
    needle: { enabled: true, minConfidence: 0.75, confirmBand: [0.5, 0.75], timeoutMs: 800 },
    sanitizer: { enabled: true, mode: 'auto', deterministicPass: true },
    port: 8932
  }, over || {});
}
const LM_E2E = lmCfg();

/* ---- canned replies ---- */
const CLEAN = { tool_calls: [{ id: 'call_c1', type: 'function', function: { name: 'read_file', arguments: '{"path":"main.py"}' } }] };
const FENCED = { tool_calls: [{ id: 'call_f1', type: 'function', function: { name: 'read-file', arguments: '```json\n{"path":"main.py"}\n```' } }] };
/* `raed_file` is a transposition: 0.75 similarity, BELOW the 0.86 bar ⇒ unrepairable by salvage. */
const SUSPECT = { tool_calls: [{ id: 'call_s1', type: 'function', function: { name: 'raed_file', arguments: '{"path":"main.py"}' } }] };
const PROSE = 'The archive suggests we inspect main.py before proceeding, operator.';

/* ---- stub local-models client (counts calls; never touches the network) ---- */
function stubClient(response, counter) {
  const c = counter || { n: 0, last: null, outcomes: [] };
  return {
    c,
    repair: function (payload) {
      c.n++;
      c.last = payload;
      return Promise.resolve(typeof response === 'function' ? response(payload) : response);
    },
    outcome: function (payload) {
      c.outcomes.push(payload);
      return Promise.resolve({ ok: true });
    }
  };
}
const ACCEPT_RES = { ok: true, calls: [{ name: 'read_file', arguments: { path: 'main.py' } }], confidence: 0.93 };

/* ---- app event helpers ---- */
const bridgeBodies = (app) => app.events
  .filter((e) => e.url === BRIDGE || e.url.endsWith('/tools/execute'))
  .map((e) => { try { return JSON.parse(e.body || '{}'); } catch (err) { return {}; } });
const chatPosts = (app) => app.events.filter((e) => e.method === 'POST' && e.url.includes('chat/completions'));
/*
 * LANDMINE (Phase 11 findings): `buildMessages()` POSTs {name:'list_dir',arguments:{path:'.'}} to
 * the SAME bridge route on every agent iteration, so raw bridge POSTs over-count. Every dispatch
 * assertion here goes through `modelDispatches`.
 */
const WORKDIR_LISTING = canon({ name: 'list_dir', arguments: { path: '.' } });
const modelDispatches = (app) => bridgeBodies(app)
  .filter((b) => canon({ name: b.name, arguments: b.arguments }) !== WORKDIR_LISTING);
const fedToolResults = (app, postIndex) => {
  const posts = chatPosts(app);
  if (posts.length <= postIndex) return [];
  return (JSON.parse(posts[postIndex].body || '{}').messages || []).filter((m) => m.role === 'tool');
};
const localEvents = (app, route) => app.events.filter((e) => e.method === 'POST' && new RegExp('/' + route + '(\\?|$)').test(e.url));
const repairEvents = (app) => localEvents(app, 'repair');
const ledgerEvents = (app) => localEvents(app, 'ledger');
const ledgerActions = (app) => ledgerEvents(app).map((e) => { try { return JSON.parse(e.body || '{}').action; } catch (x) { return null; } });
const transcriptText = (app) => {
  const el = app.document.getElementById('messages');
  return el ? String(el.textContent || '') : '';
};
const messagesWithNote = (app) => {
  const c = app.window.active();
  return ((c && c.messages) || []).filter((m) => m && (m.cortexNote || m.cortexDegraded));
};
const messagesWithSuspects = (app) => {
  const c = app.window.active();
  return ((c && c.messages) || []).filter((m) => m && m.cortexSuspects);
};

const ALL_APPS = [];
async function driveAgentTurn(opts) {
  opts = opts || {};
  const routes = Object.assign({}, BASE_ROUTES, opts.routes || {});
  routes['/v1/chat/completions'] = chatRoute(opts.script);
  const seed = { 'cogitator.settings': JSON.stringify(Object.assign(baseSettings(), { localModels: opts.localModels || LM_E2E })) };
  const app = await launchApp({ routes, seed });
  await tick(60);
  /* LANDMINE: the inline script uses TextDecoder during SSE aggregation; jsdom's is not wired to
     the fake Response-like body, so stub the window's TextDecoder BEFORE clicking send. */
  app.window.TextDecoder = class { constructor() {} decode(v) { return v == null ? '' : String(v); } };
  const $a = (id) => app.document.getElementById(id);
  $a('ta').value = opts.text || 'inspect the workdir';
  const t0 = Date.now();
  $a('send-btn').click();
  if (opts.until) {
    const bound = opts.waitMs || 1500;
    while (Date.now() - t0 < bound && !opts.until(app)) await tick(20);
  } else {
    await tick(opts.tickMs == null ? 500 : opts.tickMs);
  }
  app._elapsed = Date.now() - t0;
  ALL_APPS.push(app);
  return app;
}

(async () => {
  clearFails();

  /* ============ live schemas (single source of truth) ============ */
  const boot = await launchApp({ routes: BASE_ROUTES });
  const LIVE_TOOLS = JSON.parse(String(boot.window.eval('JSON.stringify(TOOL_SCHEMAS)')));
  const shipped = boot.window.CogCore;
  teardownApp(boot);
  check(LIVE_TOOLS.length === 6, 'live TOOL_SCHEMAS has the 6 expected rites (got ' + LIVE_TOOLS.length + ')');
  check(typeof shipped.sanitizeReply === 'function', 'the SHIPPED window.CogCore exposes sanitizeReply');
  check(typeof CogCore.sanitizeReply === 'function', 'CogCore.sanitizeReply exists');

  if (typeof CogCore.sanitizeReply === 'function') {
    /* ==================== 1. no-deps deterministic pass-through + garbage ==================== */
    console.log('--- unit 1: deterministic pass-through, never throws ---');
    const detSalv = CogCore.salvageToolCalls(FENCED, LIVE_TOOLS);
    const det = await CogCore.sanitizeReply(FENCED, LIVE_TOOLS);
    check(det && det.source === 'deterministic',
      'no deps ⇒ source:"deterministic" (got ' + (det && det.source) + ')');
    check(det && det.accepted === true, 'no deps on a repairable turn ⇒ accepted:true');
    check(det && canon(det.calls) === canon(detSalv.calls),
      'no deps ⇒ calls equal the deterministic salvage result');
    check(det && Array.isArray(det.changed) && Array.isArray(det.unrepairable) && typeof det.trace_id === 'string',
      'no deps ⇒ documented shape (changed/unrepairable arrays, trace_id string)');
    const detClean = await CogCore.sanitizeReply(CLEAN, LIVE_TOOLS);
    check(detClean.source === 'deterministic' && canon(detClean.calls) === canon(CogCore.salvageToolCalls(CLEAN, LIVE_TOOLS).calls),
      'a clean legitimate call is byte-identical through sanitizeReply');
    let threwAny = false, shapeOk = true;
    for (const junk of [null, 0, 'x', {}, undefined, NaN, true, [], { tool_calls: 'x' }]) {
      try {
        const o = await CogCore.sanitizeReply(junk, LIVE_TOOLS);
        shapeOk = shapeOk && o && Array.isArray(o.calls) && Array.isArray(o.changed) &&
          Array.isArray(o.unrepairable) && typeof o.source === 'string';
      } catch (e) { threwAny = true; }
    }
    check(!threwAny, 'garbage input never throws');
    check(shapeOk, 'garbage input always returns the documented shape');
    /* Stability: sanitizeReply must NOT mutate its inputs (a later phase depends on it). */
    const replyClone = JSON.parse(JSON.stringify(SUSPECT));
    const toolsClone = JSON.parse(JSON.stringify(LIVE_TOOLS));
    const mut = stubClient(ACCEPT_RES);
    await CogCore.sanitizeReply(replyClone, toolsClone, { client: mut, settings: lmCfg() });
    let mutReply = canon(replyClone) !== canon(SUSPECT);
    let mutTools = canon(toolsClone) !== canon(LIVE_TOOLS);
    /* the probe payload IS allowed to reference the suspects, but must not write back to them */
    check(!mutReply && !mutTools, 'sanitizeReply does not mutate `reply` or `allowedTools`');
    check(mut.c.last && canon(mut.c.last.candidates.map((t) => t.name)) === canon(['read_file', 'write_file', 'list_dir', 'grep', 'git', 'run_command'].filter((n) => LIVE_TOOLS.some((t) => t.function.name === n))),
      'the /repair payload offers the allowed tool schemas as candidates (≤10)');
    check(mut.c.last && String(mut.c.last.trace_id || '').length > 0, 'the /repair payload carries a trace_id');

    /* ==================== 2. confidence gate ==================== */
    console.log('--- unit 2: below-threshold confidence ⇒ original passes through ---');
    const salvSuspect = CogCore.salvageToolCalls(SUSPECT, LIVE_TOOLS);
    check(salvSuspect.calls.length === 0 && salvSuspect.unrepairable.length === 1,
      'BASELINE: the suspect reply is genuinely unrepairable by salvage');
    const low = stubClient(Object.assign({}, ACCEPT_RES, { confidence: 0.4 }));
    const rLow = await CogCore.sanitizeReply(SUSPECT, LIVE_TOOLS, { client: low, settings: lmCfg() });
    check(low.c.n === 1, 'Needle was probed exactly once for the suspect turn (got ' + low.c.n + ')');
    check(rLow.accepted === false && rLow.source === 'original',
      'confidence 0.4 < 0.75 ⇒ accepted:false, source:"original" (got ' + rLow.source + '/' + rLow.accepted + ')');
    check(rLow.calls.length === 0, 'no repaired call is dispatched below threshold (got ' + rLow.calls.length + ')');
    check(rLow.reason === 'below_threshold', 'reason is "below_threshold" (got ' + rLow.reason + ')');
    check(canon(rLow.unrepairable) === canon(salvSuspect.unrepairable),
      'the ORIGINAL unrepairable suspects pass through untouched');
    const nul = stubClient(Object.assign({}, ACCEPT_RES, { confidence: null }));
    const rNul = await CogCore.sanitizeReply(SUSPECT, LIVE_TOOLS, { client: nul, settings: lmCfg() });
    check(rNul.accepted === false && rNul.calls.length === 0,
      'confidence:null (a tuned .cact without a head) is BELOW threshold ⇒ rejected');
    check(rNul.confidence === null, 'the null confidence is reported as-is (got ' + JSON.stringify(rNul.confidence) + ')');

    /* ==================== 3. calls:[] never manufactures a call ==================== */
    console.log('--- unit 3: an empty Needle result never invents a rite ---');
    const empty = stubClient({ ok: true, calls: [], confidence: 0.99 });
    const rEmpty = await CogCore.sanitizeReply(SUSPECT, LIVE_TOOLS, { client: empty, settings: lmCfg() });
    check(empty.c.n === 1, 'Needle was probed exactly once');
    check(rEmpty.accepted === false, 'calls:[] ⇒ accepted:false');
    check(rEmpty.calls.length === 0, 'calls:[] ⇒ NO call invented');
    check(typeof rEmpty.reason === 'string' && rEmpty.reason.length > 0,
      'calls:[] ⇒ a reason is set (got ' + JSON.stringify(rEmpty.reason) + ')');

    /* ==================== 4. the Phase 7 validator is the gate ==================== */
    console.log('--- unit 4: validator (not the local model) decides dispatchability ---');
    const good = stubClient(ACCEPT_RES);
    const rGood = await CogCore.sanitizeReply(SUSPECT, LIVE_TOOLS, { client: good, settings: lmCfg() });
    check(rGood.accepted === true && rGood.source === 'needle' && rGood.calls.length === 1 &&
      rGood.calls[0].name === 'read_file' && rGood.calls[0].args.path === 'main.py',
      'CONTROL: a canonical above-threshold repair IS accepted and dispatchable');
    const evil = stubClient({ ok: true, calls: [{ name: 'evil_tool', arguments: {} }], confidence: 0.99 });
    const rEvil = await CogCore.sanitizeReply(SUSPECT, LIVE_TOOLS, { client: evil, settings: lmCfg() });
    check(rEvil.accepted === false && rEvil.calls.length === 0,
      'a repaired call naming an UNKNOWN tool is rejected by the Phase 7 validator');
    const badArgs = stubClient({ ok: true, calls: [{ name: 'read_file', arguments: '{oops' }], confidence: 0.99 });
    const rBadArgs = await CogCore.sanitizeReply(SUSPECT, LIVE_TOOLS, { client: badArgs, settings: lmCfg() });
    check(rBadArgs.accepted === false && rBadArgs.calls.length === 0,
      'a repaired call with unparseable arguments is rejected by the Phase 7 validator');

    /* ==================== 5. mode / flag semantics ==================== */
    console.log('--- unit 5: mode + flag semantics (exactly one probe per suspect turn) ---');
    const tryCfg = async (over, reply) => {
      const s = stubClient(ACCEPT_RES);
      const r = await CogCore.sanitizeReply(reply, LIVE_TOOLS, { client: s, settings: lmCfg(over) });
      return { n: s.c.n, r };
    };
    const off = await tryCfg({ sanitizer: { enabled: true, mode: 'off', deterministicPass: true } }, SUSPECT);
    check(off.n === 0, 'mode:"off" never probes even with suspects (got ' + off.n + ')');
    const autoSuspect = await tryCfg({ sanitizer: { enabled: true, mode: 'auto', deterministicPass: true } }, SUSPECT);
    check(autoSuspect.n === 1, 'mode:"auto" probes a suspect turn exactly once (got ' + autoSuspect.n + ')');
    const autoProse = await tryCfg({ sanitizer: { enabled: true, mode: 'auto', deterministicPass: true } }, PROSE);
    check(autoProse.n === 0, 'mode:"auto" does NOT probe a prose-only turn (got ' + autoProse.n + ')');
    const onProse = await tryCfg({ sanitizer: { enabled: true, mode: 'on', deterministicPass: true } }, PROSE);
    check(onProse.n === 1, 'mode:"on" DOES probe a prose-only turn exactly once (got ' + onProse.n + ')');
    const onClean = await tryCfg({ sanitizer: { enabled: true, mode: 'on', deterministicPass: true } }, CLEAN);
    check(onClean.n === 0, 'mode:"on" does NOT probe a clean dispatchable turn (got ' + onClean.n + ')');
    const sanOff = await tryCfg({ sanitizer: { enabled: false, mode: 'on', deterministicPass: true } }, SUSPECT);
    check(sanOff.n === 0, 'sanitizer.enabled:false never probes (got ' + sanOff.n + ')');
    const ndOff = await tryCfg({ needle: { enabled: false, minConfidence: 0.75, timeoutMs: 800 } }, SUSPECT);
    check(ndOff.n === 0, 'needle.enabled:false never probes (got ' + ndOff.n + ')');
    const lmOff = await tryCfg({ enabled: false }, SUSPECT);
    check(lmOff.n === 0, 'localModels.enabled:false never probes (got ' + lmOff.n + ')');
    const noClient = await CogCore.sanitizeReply(SUSPECT, LIVE_TOOLS, { settings: lmCfg() });
    check(noClient.accepted === false && noClient.calls.length === 0,
      'no client injected ⇒ pure deterministic pass-through, never throws');
    /* prose-only accepted repair ends the turn as a real dispatchable call */
    const proseAccept = stubClient(ACCEPT_RES);
    const rProse = await CogCore.sanitizeReply(PROSE, LIVE_TOOLS, { client: proseAccept, settings: lmCfg({ sanitizer: { enabled: true, mode: 'on', deterministicPass: true } }) });
    check(rProse.accepted === true && rProse.source === 'needle' && rProse.calls.length === 1,
      'mode:"on" prose probe above threshold ⇒ accepted:true, source:"needle"');

    /* ==================== 6. driven e2e: an accepted repair is dispatched ==================== */
    console.log('--- e2e 6: unrepairable stream + /repair ⇒ bridge receives the canonical rite ---');
    let app = await driveAgentTurn({
      localModels: LM_E2E,
      script: [
        [toolDeltaStream([{ id: 'call_bad', name: 'raed_file', args: '{"path":"main.py"}' }])],
        [contentStream('Read it, operator.')]
      ],
      routes: {
        '/repair': { status: 200, json: { ok: true, calls: [{ name: 'read_file', arguments: { path: 'main.py' } }], confidence: 0.93, trace_id: 'tr-accept' } },
        '/ledger': { status: 200, json: { ok: true } }
      }
    });
    try {
      const dispatched = modelDispatches(app);
      check(dispatched.length === 1 && dispatched[0].name === 'read_file',
        'the BRIDGE received exactly one read_file (got ' + dispatched.length + ': ' + JSON.stringify(dispatched.map((d) => d.name)) + ')');
      check(dispatched.length === 1 && dispatched[0].arguments && dispatched[0].arguments.path === 'main.py',
        'with object arguments {path:main.py} (got ' + JSON.stringify(dispatched[0] && dispatched[0].arguments) + ')');
      check(!bridgeBodies(app).some((b) => b.name === 'raed_file'), 'the raw unrepairable name never reached the bridge');
      check(/\[LOCAL CORTEX: RITE REPAIRED/.test(transcriptText(app)),
        'the operator note "[LOCAL CORTEX: RITE REPAIRED" is rendered in the transcript');
      check(repairEvents(app).length === 1, 'exactly one POST /repair (got ' + repairEvents(app).length + ')');
      check(fedToolResults(app, 1).some((m) => m.name === 'read_file'),
        'the repaired rite was fed back as a role:"tool" result in the next iteration');
      check(messagesWithSuspects(app).length === 0, 'm.cortexSuspects was CLEARED on the successful repair');
      check(ledgerActions(app).indexOf('accepted') !== -1, 'the accepted repair posted /ledger action:"accepted"');
      check(!chatPosts(app).some((e) => String(e.body || '').indexOf('LOCAL CORTEX') !== -1),
        'the transcript note never leaks into an outgoing /v1/chat/completions payload');
      check(app.bootErrors.length === 0, 'no uncaught boot errors in the accepted-repair turn');
    } finally { teardownApp(app); }

    /* ==================== 7. driven e2e: below threshold ⇒ no dispatch ==================== */
    console.log('--- e2e 7: below-threshold repair ⇒ bridge never called, rejection still fed back ---');
    app = await driveAgentTurn({
      localModels: LM_E2E,
      script: [
        [toolDeltaStream([{ id: 'call_bad', name: 'raed_file', args: '{"path":"main.py"}' }])],
        [contentStream('Understood; answering in prose.')]
      ],
      routes: {
        '/repair': { status: 200, json: { ok: true, calls: [{ name: 'read_file', arguments: { path: 'main.py' } }], confidence: 0.4, trace_id: 'tr-low' } },
        '/ledger': { status: 200, json: { ok: true } }
      }
    });
    try {
      check(modelDispatches(app).length === 0,
        'the bridge was NEVER called on a below-threshold repair (got ' + JSON.stringify(modelDispatches(app)) + ')');
      check(repairEvents(app).length === 1, 'exactly one POST /repair was still attempted (got ' + repairEvents(app).length + ')');
      const fed = fedToolResults(app, 1);
      check(fed.some((m) => /reject/i.test(String(m.content || '')) && /raed_file/.test(String(m.content || ''))),
        'the Phase 7 rejection still reaches the model as a role:"tool" correction');
      check(ledgerActions(app).indexOf('passed_through') !== -1, 'the below-threshold case posted /ledger action:"passed_through"');
      check(app.bootErrors.length === 0, 'no uncaught boot errors in the below-threshold turn');
    } finally { teardownApp(app); }

    /* ==================== 8. driven e2e: timeout is bounded + flagged ==================== */
    console.log('--- e2e 8: /repair resolving after ~2s vs needle.timeoutMs ~120 ⇒ turn completes flagged ---');
    const slowRepair = () => new Promise((res) => setTimeout(() => res({
      ok: true, status: 200,
      json: async () => ({ ok: true, calls: [{ name: 'read_file', arguments: { path: 'main.py' } }], confidence: 0.99 }),
      text: async () => ''
    }), 2000));
    app = await driveAgentTurn({
      localModels: lmCfg({ needle: { enabled: true, minConfidence: 0.75, timeoutMs: 120 } }),
      script: [
        [toolDeltaStream([{ id: 'call_bad', name: 'raed_file', args: '{"path":"main.py"}' }])],
        [contentStream('Answering in prose, operator.')]
      ],
      routes: { '/repair': slowRepair, '/ledger': { status: 200, json: { ok: true } } },
      until: (a) => messagesWithNote(a).length > 0,
      waitMs: 1400
    });
    try {
      check(app._elapsed < 1400, 'the turn completed well within a healthy wall-clock bound (took ' + app._elapsed + 'ms)');
      check(modelDispatches(app).length === 0,
        'a timed-out repair dispatched nothing (got ' + JSON.stringify(modelDispatches(app)) + ')');
      const flagged = messagesWithNote(app);
      check(flagged.length >= 1, 'the timed-out turn is FLAGGED to the operator (m.cortexDegraded / m.cortexNote set)');
      check(flagged.some((m) => m.cortexDegraded === true || /TIMED OUT|DEGRADED/i.test(String(m.cortexNote || ''))),
        'the flag marks the local-cortex degradation (note="' + (flagged[0] && flagged[0].cortexNote) + '", cortexDegraded=' + (flagged[0] && flagged[0].cortexDegraded) + ')');
      check(ledgerActions(app).indexOf('timeout') !== -1, 'the timeout posted /ledger action:"timeout" (got ' + JSON.stringify(ledgerActions(app)) + ')');
      check(app.bootErrors.length === 0, 'no uncaught boot errors in the timeout turn');
    } finally { teardownApp(app); }

    /* ==================== 9. driven e2e: sidecar down = Phase 11 ==================== */
    console.log('--- e2e 9: sidecar down (fetch rejects) ⇒ byte-identical Phase 11 behaviour ---');
    app = await driveAgentTurn({
      localModels: LM_E2E,
      script: [
        [toolDeltaStream([
          { id: 'call_f1', name: 'read-file', args: '```json\n{"path":"main.py"}\n```' },
          { id: 'call_bad', name: 'raed_file', args: '{"path":"other.py"}' }
        ])],
        [contentStream('Done, operator.')]
      ],
      routes: { '/ledger': { status: 200, json: { ok: true } } }   /* /repair deliberately unmocked */
    });
    try {
      const dispatched = modelDispatches(app);
      check(dispatched.length === 1 && dispatched[0].name === 'read_file',
        'deterministic salvage still dispatched the single repairable rite (got ' + dispatched.length + ')');
      check(repairEvents(app).map((e) => e.url).length >= 0 && app.bootErrors.length === 0,
        'the dead sidecar caused no uncaught error and the turn completed');
      check(repairEvents(app).length === 0 || repairEvents(app).length === 1, 'at most one /repair attempt was made');
    } finally { teardownApp(app); }

    /* ==================== 10. driven e2e: a clean turn issues ZERO /repair ==================== */
    console.log('--- e2e 10: a clean legitimate turn ⇒ zero /repair, dispatched exactly once ---');
    let repairHits = 0;
    app = await driveAgentTurn({
      localModels: LM_E2E,   /* mode 'auto': a clean turn + its trailing prose issue zero probes */
      script: [
        [toolDeltaStream([{ id: 'call_ok', name: 'read_file', args: '{"path":"main.py"}' }])],
        [contentStream('Done.')]
      ],
      routes: {
        '/repair': () => { repairHits++; return { status: 200, json: { ok: true, calls: [], confidence: 0.99 } }; },
        '/ledger': { status: 200, json: { ok: true } }
      }
    });
    try {
      const dispatched = modelDispatches(app).filter((b) => b.name === 'read_file');
      check(dispatched.length === 1, 'PROOF: the clean call was dispatched exactly once (got ' + dispatched.length + ')');
      check(repairHits === 0 && repairEvents(app).length === 0,
        'PROOF: ZERO /repair requests for a clean turn (route hits=' + repairHits + ')');
      check(app.bootErrors.length === 0, 'no uncaught boot errors in the clean turn');
    } finally { teardownApp(app); }

    /* ==================== 11. driven e2e: local models disabled ⇒ zero :8932 ==================== */
    console.log('--- e2e 11: localModels.enabled:false ⇒ ZERO requests to :8932 ---');
    app = await driveAgentTurn({
      localModels: lmCfg({ enabled: false }),
      script: [
        [toolDeltaStream([{ id: 'call_bad', name: 'raed_file', args: '{"path":"main.py"}' }])],
        [contentStream('Answering in prose.')]
      ],
      routes: {
        '/repair': { status: 200, json: { ok: true, calls: [{ name: 'read_file', arguments: { path: 'main.py' } }], confidence: 0.99 } },
        '/ledger': { status: 200, json: { ok: true } }
      }
    });
    try {
      const port = app.events.filter((e) => String(e.url).indexOf('8932') !== -1);
      check(port.length === 0, 'ZERO requests to :8932 while disabled (got ' + port.length + ': ' + JSON.stringify(port.map((e) => e.url)) + ')');
      check(modelDispatches(app).length === 0, 'and nothing was dispatched (Phase 11 rejection path unchanged)');
      check(app.bootErrors.length === 0, 'no uncaught boot errors while disabled');
    } finally { teardownApp(app); }

    /* ==================== 12. privacy: no key, no Authorization ==================== */
    console.log('--- privacy: /repair + /ledger carry no API key and no Authorization header ---');
    let authLeak = 0, keyLeak = 0, seenRepair = 0, seenLedger = 0;
    for (const a of ALL_APPS) {
      for (const e of a.events) {
        const isLocal = /\/repair(\?|$)/.test(String(e.url)) || /\/ledger(\?|$)/.test(String(e.url));
        if (!isLocal) continue;
        if (/\/repair(\?|$)/.test(String(e.url))) seenRepair++;
        if (/\/ledger(\?|$)/.test(String(e.url))) seenLedger++;
        const hdrs = JSON.stringify(e.headers || {});
        if (/authorization/i.test(hdrs)) authLeak++;
        if (String(e.body || '').indexOf(SENTINEL) !== -1) keyLeak++;
        if (hdrs.indexOf(SENTINEL) !== -1) keyLeak++;
      }
    }
    check(seenRepair > 0 && seenLedger > 0,
      'the privacy check is not vacuous (' + seenRepair + ' /repair, ' + seenLedger + ' /ledger observed)');
    check(authLeak === 0, 'no /repair or /ledger request carried an Authorization header (got ' + authLeak + ')');
    check(keyLeak === 0, 'no /repair or /ledger request body carried the seeded API key sentinel (got ' + keyLeak + ')');
    /* belt and braces: no request body anywhere contains the sentinel */
    let anyKeyBody = 0;
    for (const a of ALL_APPS) for (const e of a.events) if (String(e.body || '').indexOf(SENTINEL) !== -1) anyKeyBody++;
    check(anyKeyBody === 0, 'no request body anywhere contains the API key sentinel (got ' + anyKeyBody + ')');
  }

  process.exit(summary('PHASE 12 NEEDLE REPAIR PASS'));
})().catch((e) => { console.error(e); process.exit(1); });
