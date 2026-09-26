'use strict';
/*
 * PHASE 13 E2E — Laya gates (`CogCore.cortexLayaGates`) + agent-loop wiring (F2a/F2b).
 *
 * Contract under test (plan §4, phase13-workstream-B.md):
 *  - `CogCore.cortexLayaGates(plan, deps)` = ONE batched `/decide` per turn asking at most two
 *    kinds of question, both FAIL-OPEN:
 *      F2a pre-flight (outbound): is each MUTATING rite's args plausible for its schema?
 *          below `laya.minConfidence` ⇒ that index is `held` and is NOT dispatched.
 *      F2b reply-anomaly (inbound): on a prose-only reply, does it look like an error page / a
 *          refusal / a loop? at/above threshold ⇒ `anomaly.flagged` (flag only — content kept).
 *  - Local inference is NEVER in the critical path: sidecar down/disabled/slow ⇒ fail open and
 *    the harness behaves byte-identically to Phase 12 (a `localModels.enabled:false` run).
 *  - A held mutating rite still goes through the Phase 7 validator → operator approval → bridge;
 *    the local model can never bypass a gate or widen the allow-list.
 *  - Pre-flight and anomaly are mutually exclusive, so a single model reply issues AT MOST ONE
 *    `/decide`; `preflight:false`/`anomaly:false` remove exactly their own call.
 *  - `/decide` and `/ledger` carry NO `Authorization` header and never the provider API key.
 *
 * RED-FIRST: this file was written before `cortexLayaGates` existed. Against the pre-change tree
 * the `cortexLayaGates exists` checks and every driven-gate assertion FAIL; see the reported RED
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

/* ---- SSE helpers (same shape the Phase 7/8/11/12 suites use) ---- */
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
  '/tools/execute': { status: 200, json: { ok: true, result: 'wrote out.txt' } },
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
/* Full laya block so a per-test override replaces it cleanly (Object.assign is shallow). */
function lmCfg(over) {
  return Object.assign({
    enabled: true,
    needle: { enabled: true, minConfidence: 0.75, confirmBand: [0.5, 0.75], timeoutMs: 800 },
    sanitizer: { enabled: true, mode: 'auto', deterministicPass: true },
    laya: { enabled: true, minConfidence: 0.70, timeoutMs: 500, preflight: true, anomaly: true },
    port: 8932
  }, over || {});
}
const LM = lmCfg();
const LM_NO_ANOM = lmCfg({ laya: { enabled: true, minConfidence: 0.70, timeoutMs: 500, preflight: true, anomaly: false } });
const LM_NO_PF = lmCfg({ laya: { enabled: true, minConfidence: 0.70, timeoutMs: 500, preflight: false, anomaly: true } });
const LM_OFF = lmCfg({ enabled: false });
/* Pure-seam settings (unit tests): what `index.html` reads out of settings.localModels. */
function layaCfg(over) { return lmCfg(over); }

/* ---- canned replies ---- */
const WRITE = { tool_calls: [{ id: 'call_w1', type: 'function', function: { name: 'write_file', arguments: '{"path":"out.txt","content":"hello"}' } }] };
const RUN = { tool_calls: [{ id: 'call_r1', type: 'function', function: { name: 'run_command', arguments: '{"command":"ls"}' } }] };
const READ = { tool_calls: [{ id: 'call_rd1', type: 'function', function: { name: 'read_file', arguments: '{"path":"main.py"}' } }] };
const PROSE_REPLY = 'The archive suggests we inspect main.py before proceeding, operator.';

/* ---- stub local-models client (counts calls; never touches the network) ---- */
function stubClient(response) {
  const c = { n: 0, last: null, outcomes: [] };
  return {
    c,
    decide: function (payload) {
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
const pfPlan = (over) => Object.assign({
  mutating: [{ index: 0, name: 'write_file', args: { path: 'a.txt', content: 'b' }, description: 'write', parameters: null }],
  prose: false,
  reply: { toolCalls: [{ id: 'c', name: 'write_file', args: { path: 'a.txt', content: 'b' } }], content: '' }
}, over || {});

/* ---- app event helpers (verbatim phase-12 patterns) ---- */
const bridgeBodies = (app) => app.events
  .filter((e) => e.url === BRIDGE || e.url.endsWith('/tools/execute'))
  .map((e) => { try { return JSON.parse(e.body || '{}'); } catch (err) { return {}; } });
const chatPosts = (app) => app.events.filter((e) => e.method === 'POST' && e.url.includes('chat/completions'));
/*
 * LANDMINE (Phase 11/12 findings): `buildMessages()` POSTs {name:'list_dir',arguments:{path:'.'}} to
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
const decideEvents = (app) => localEvents(app, 'decide');
const ledgerEvents = (app) => localEvents(app, 'ledger');
const ledgerActions = (app) => ledgerEvents(app).map((e) => { try { return JSON.parse(e.body || '{}').action; } catch (x) { return null; } });
const decideBodies = (app) => decideEvents(app).map((e) => { try { return JSON.parse(e.body || '{}'); } catch (x) { return {}; } });
const transcriptText = (app) => {
  const el = app.document.getElementById('messages');
  return el ? String(el.textContent || '') : '';
};
/*
 * LANDMINE: the rendered transcript is NOT reproducible byte-for-byte across two runs. Every
 * message head carries the wall clock (`index.html`: `new Date(m.ts).toLocaleTimeString()`), so
 * a comparison of raw transcript text flakes whenever the two runs straddle a second boundary.
 * Normalise that volatile rendering out and compare what actually matters — the model's own text
 * and the tool/cortex notes, which must be identical with the sidecar down and with the local
 * models disabled. (Pinned by the kill-the-model test; do NOT "fix" a failure here by loosening
 * the comparison further.)
 */
const stableTranscript = (app) => transcriptText(app)
  .replace(/\b\d{1,2}:\d{2}(:\d{2})?\b/g, '<time>');
const lastAssistant = (app) => {
  const c = app.window.active();
  const msgs = ((c && c.messages) || []).filter((m) => m && m.role === 'assistant');
  return msgs.length ? msgs[msgs.length - 1] : null;
};

const ALL_APPS = [];
async function driveAgentTurn(opts) {
  opts = opts || {};
  const routes = Object.assign({}, BASE_ROUTES, opts.routes || {});
  routes['/v1/chat/completions'] = chatRoute(opts.script);
  const seed = { 'cogitator.settings': JSON.stringify(Object.assign(baseSettings(), { localModels: opts.localModels || LM })) };
  const app = await launchApp({ routes, seed });
  await tick(60);
  /* LANDMINE: the inline script uses TextDecoder during SSE aggregation; jsdom's is not wired to
     the fake Response-like body, so stub the window's TextDecoder BEFORE clicking send. */
  app.window.TextDecoder = class { constructor() {} decode(v) { return v == null ? '' : String(v); } };
  const $a = (id) => app.document.getElementById(id);
  $a('ta').value = opts.text || 'inspect the workdir';
  const t0 = Date.now();
  /* Headless operator: auto-authorize any mutating rite the turn asks to dispatch, so a driven
     turn reaches the bridge exactly as a real operator clicking AUTHORIZE would. */
  const approver = setInterval(() => {
    try {
      const modal = app.document.getElementById('agent-modal');
      const btn = app.document.getElementById('agent-approve');
      if (modal && modal.classList.contains('open') && btn) btn.click();
    } catch (e) { /* ignore */ }
  }, 10);
  $a('send-btn').click();
  try {
    if (opts.until) {
      const bound = opts.waitMs || 1500;
      while (Date.now() - t0 < bound && !opts.until(app)) await tick(20);
    } else {
      await tick(opts.tickMs == null ? 500 : opts.tickMs);
    }
  } finally { clearInterval(approver); }
  app._elapsed = Date.now() - t0;
  ALL_APPS.push(app);
  return app;
}

(async () => {
  clearFails();

  /* ============ live schemas (single source of truth) ============ */
  const boot = await launchApp({ routes: BASE_ROUTES });
  const shipped = boot.window.CogCore;
  teardownApp(boot);
  check(typeof shipped.cortexLayaGates === 'function', 'the SHIPPED window.CogCore exposes cortexLayaGates');
  check(typeof CogCore.cortexLayaGates === 'function', 'CogCore.cortexLayaGates exists');

  if (typeof CogCore.cortexLayaGates === 'function') {
    /* ==================== 1. unit — the pure seam: opt-out ⇒ zero probes, never throws ==== */
    console.log('--- unit 1: no client / enabled:false / laya.enabled:false ⇒ zero probes ---');
    const noClient = CogCore.cortexLayaGates(pfPlan(), { settings: layaCfg() });
    check(noClient && typeof noClient.then !== 'function',
      'no client ⇒ a PLAIN OBJECT, not a Promise (a no-deps caller stays synchronous)');
    check(noClient && noClient.ok === true && canon(noClient.held) === '[]' && noClient.anomaly === null &&
      canon(noClient.questions) === '{}' && noClient.degraded === false,
      'no client ⇒ {ok:true, held:[], anomaly:null, questions:{}, degraded:false}');
    const sOff = stubClient({ ok: true, answers: { pf_0: { noul: 0.99 } } });
    await CogCore.cortexLayaGates(pfPlan(), { client: sOff, settings: layaCfg({ enabled: false }) });
    check(sOff.c.n === 0, 'localModels.enabled:false ⇒ zero /decide (got ' + sOff.c.n + ')');
    const sLayaOff = stubClient({ ok: true, answers: { pf_0: { noul: 0.99 } } });
    await CogCore.cortexLayaGates(pfPlan(), { client: sLayaOff, settings: layaCfg({ laya: { enabled: false, minConfidence: 0.70, timeoutMs: 500, preflight: true, anomaly: true } }) });
    check(sLayaOff.c.n === 0, 'laya.enabled:false ⇒ zero /decide (got ' + sLayaOff.c.n + ')');
    /* a reply with nothing to ask about ⇒ no probe even when fully enabled */
    const sEmpty = stubClient({ ok: true, answers: {} });
    const rEmptyPlan = await CogCore.cortexLayaGates({ mutating: [], prose: false, reply: {} }, { client: sEmpty, settings: layaCfg() });
    check(sEmpty.c.n === 0 && canon(rEmptyPlan.questions) === '{}',
      'an empty plan (no mutating calls, not prose) ⇒ zero /decide, questions:{}');
    /* garbage never throws, always the documented shape */
    let threw = false, shapeOk = true;
    for (const junk of [null, 0, 'x', {}, undefined, NaN, true, [], { mutating: 'x' }, { prose: 'x' }, { mutating: [null, 1, 'z'] }]) {
      try {
        const o = await CogCore.cortexLayaGates(junk, { client: stubClient({ ok: true, answers: {} }), settings: layaCfg() });
        shapeOk = shapeOk && o && Array.isArray(o.held) && 'anomaly' in o && typeof o.questions === 'object' &&
          typeof o.ok === 'boolean' && typeof o.degraded === 'boolean' && typeof o.trace_id === 'string';
      } catch (e) { threw = true; }
    }
    check(!threw, 'garbage `plan` values never throw');
    check(shapeOk, 'garbage `plan` values always return the documented shape');

    /* ==================== 2. unit — batching & payload hygiene ==================== */
    console.log('--- unit 2: 3 mutating calls ⇒ exactly ONE /decide carrying pf_0/pf_1/pf_2 ---');
    const s3 = stubClient({ ok: true, answers: { pf_0: { noul: 0.9 }, pf_1: { noul: 0.9 }, pf_2: { noul: 0.9 } } });
    const r3 = await CogCore.cortexLayaGates({
      mutating: [{ name: 'write_file', args: { path: 'a', content: 'b' } }, { name: 'run_command', args: { command: 'ls' } }, { name: 'git', args: { args: 'commit' } }],
      prose: false, reply: {}
    }, { client: s3, settings: layaCfg() });
    check(s3.c.n === 1, 'exactly ONE /decide for a 3-call turn (got ' + s3.c.n + ')');
    check(canon(Object.keys(s3.c.last.questions)) === canon(['pf_0', 'pf_1', 'pf_2']),
      'the sent questions carry keys pf_0/pf_1/pf_2 (got ' + JSON.stringify(Object.keys(s3.c.last.questions)) + ')');
    check(canon(s3.c.last.questions) === canon(r3.questions), 'r3.questions is EXACTLY what was sent');
    check(s3.c.last.questions.pf_0.type === 'noul' && /plausibly satisfy/i.test(s3.c.last.questions.pf_0.instructions),
      'each pre-flight question is a `noul` schema-plausibility question');
    check(/write_file/.test(JSON.stringify(s3.c.last.questions.pf_0.criteria)) &&
      /a/.test(String(s3.c.last.questions.pf_0.criteria.arguments || '')),
      'the criteria carry the tool name and the JSON arguments');
    check(canon(Object.keys(s3.c.last)) === canon(['state', 'questions', 'trace_id']),
      'the /decide payload is ONLY {state, questions, trace_id} (got ' + JSON.stringify(Object.keys(s3.c.last)) + ')');
    check(typeof s3.c.last.trace_id === 'string' && s3.c.last.trace_id.length > 0, 'the payload carries a trace_id');
    check(!/settings|apiKey|SENTINEL/i.test(JSON.stringify(s3.c.last)), 'the payload never carries settings / an API key');

    /* ==================== 3. unit — verdicts, fail-open, timeout, rejecting client ======== */
    console.log('--- unit 3: verdicts (hold below threshold, flag at/above, fail open otherwise) --');
    const high = stubClient({ ok: true, answers: { pf_0: { noul: 0.95 } } });
    const rHigh = await CogCore.cortexLayaGates(pfPlan(), { client: high, settings: layaCfg() });
    check(canon(rHigh.held) === '[]' && rHigh.ok === true, 'noul 0.95 ⇒ nothing held');
    check(high.c.outcomes.map((o) => o.action).indexOf('accepted') !== -1, 'nothing fired ⇒ ledger action "accepted"');
    const low = stubClient({ ok: true, answers: { pf_0: { noul: 0.10 } } });
    const rLow = await CogCore.cortexLayaGates(pfPlan(), { client: low, settings: layaCfg() });
    check(canon(rLow.held) === canon([0]), 'noul 0.10 < 0.70 ⇒ held [0] (got ' + JSON.stringify(rLow.held) + ')');
    check(low.c.outcomes.map((o) => o.action).indexOf('rejected') !== -1, 'a hold ⇒ ledger action "rejected"');
    const ahead = stubClient({ ok: true, answers: { pf_0: { noul: 'nope' } } });
    const rAhead = await CogCore.cortexLayaGates(pfPlan(), { client: ahead, settings: layaCfg() });
    check(canon(rAhead.held) === '[]', 'a missing/non-numeric answer holds nothing — FAIL OPEN');
    const anull = stubClient({ ok: true, answers: { pf_0: { noul: null } } });
    const rAnull = await CogCore.cortexLayaGates(pfPlan(), { client: anull, settings: layaCfg() });
    check(canon(rAnull.held) === '[]', 'noul:null holds nothing — FAIL OPEN');
    const amiss = stubClient({ ok: true, answers: {} });
    const rAmiss = await CogCore.cortexLayaGates(pfPlan(), { client: amiss, settings: layaCfg() });
    check(canon(rAmiss.held) === '[]' && rAmiss.answers !== null, 'an absent answer ⇒ nothing held (fail open)');
    const aFlag = stubClient({ ok: true, answers: { anomaly: { noul: 0.9 } } });
    const rFlag = await CogCore.cortexLayaGates({ mutating: [], prose: true, reply: { content: 'x' } }, { client: aFlag, settings: layaCfg() });
    check(rFlag.anomaly && rFlag.anomaly.flagged === true && rFlag.anomaly.prob === 0.9,
      'anomaly noul 0.9 ⇒ flagged:true, prob 0.9 (got ' + JSON.stringify(rFlag.anomaly) + ')');
    check(aFlag.c.outcomes.map((o) => o.action).indexOf('flagged') !== -1, 'a flag ⇒ ledger action "flagged"');
    const aQuiet = stubClient({ ok: true, answers: { anomaly: { noul: 0.2 } } });
    const rQuiet = await CogCore.cortexLayaGates({ mutating: [], prose: true, reply: { content: 'x' } }, { client: aQuiet, settings: layaCfg() });
    check(rQuiet.anomaly && rQuiet.anomaly.flagged === false, 'anomaly noul 0.2 ⇒ flagged:false');
    check(canon(Object.keys(aFlag.c.last.questions)) === canon(['anomaly']) && aFlag.c.last.questions.anomaly.type === 'noul',
      'a prose turn sends exactly the single `anomaly` noul question');
    /* timeout via the EXISTING race helper */
    const never = { c: { n: 0, last: null, outcomes: [] }, decide: function () { this.c.n++; return new Promise(() => {}); }, outcome: function (p) { this.c.outcomes.push(p); return Promise.resolve({ ok: true }); } };
    const rTo = await CogCore.cortexLayaGates(pfPlan(), { client: never, settings: layaCfg(), timeout: (fire) => setTimeout(fire, 2) });
    check(rTo.degraded === true && rTo.reason === 'timeout' && canon(rTo.held) === '[]' && rTo.anomaly === null,
      'a timeout ⇒ degraded:true, reason:"timeout", held:[], anomaly:null (got ' + rTo.reason + ')');
    check(never.c.outcomes.map((o) => o.action).indexOf('passed_through') !== -1, 'a timeout ⇒ ledger action "passed_through"');
    /* a rejecting client degrades, never throws */
    const rejecting = { c: { outcomes: [] }, decide: () => Promise.reject(new Error('boom')), outcome: function (p) { this.c.outcomes.push(p); return Promise.resolve({ ok: true }); } };
    let rejThrew = false, rRej = null;
    try { rRej = await CogCore.cortexLayaGates(pfPlan(), { client: rejecting, settings: layaCfg() }); } catch (e) { rejThrew = true; }
    check(!rejThrew && rRej && rRej.degraded === true && canon(rRej.held) === '[]' && rRej.anomaly === null,
      'a rejecting client ⇒ degraded, held:[], anomaly:null — never a throw');
    /* an ok:false / degraded RESPONSE degrades and keeps its own reason */
    const deg = stubClient({ ok: false, degraded: true, reason: 'engine_missing' });
    const rDeg = await CogCore.cortexLayaGates(pfPlan(), { client: deg, settings: layaCfg() });
    check(rDeg.degraded === true && rDeg.reason === 'engine_missing' && canon(rDeg.held) === '[]',
      'a degraded response ⇒ degraded:true, reason from the response ("engine_missing")');

    /* ==================== 4. driven e2e — high-p accepts (bridge receives it once) ========= */
    console.log('--- e2e 4: mutating write_file + /decide pf_0 0.95 ⇒ bridge receives it exactly once');
    let app = await driveAgentTurn({
      localModels: LM,
      script: [[toolDeltaStream([{ id: 'call_w1', name: 'write_file', args: '{"path":"out.txt","content":"hello"}' }])], [contentStream('Wrote it, operator.')]],
      routes: {
        '/decide': { status: 200, json: { ok: true, answers: { pf_0: { noul: 0.95 } }, latency_ms: 3, trace_id: 'tr-pf' } },
        '/ledger': { status: 200, json: { ok: true } }
      },
      until: (a) => modelDispatches(a).length > 0, waitMs: 1400
    });
    try {
      const dispatched = modelDispatches(app);
      check(dispatched.length === 1 && dispatched[0].name === 'write_file',
        'the BRIDGE received exactly one write_file (got ' + dispatched.length + ': ' + JSON.stringify(dispatched.map((d) => d.name)) + ')');
      check(decideEvents(app).length >= 1 && canon(Object.keys(decideBodies(app)[0].questions)) === canon(['pf_0']),
        'the pre-flight /decide carried pf_0');
      check(ledgerActions(app).indexOf('rejected') === -1 && ledgerActions(app).indexOf('accepted') !== -1,
        'a high-p pre-flight posted /ledger action:"accepted" (got ' + JSON.stringify(ledgerActions(app)) + ')');
      check(app.bootErrors.length === 0, 'no uncaught boot errors in the high-p turn');
    } finally { teardownApp(app); }

    /* ==================== 5. driven e2e — low-p refuses (bridge NEVER called) ============== */
    console.log('--- e2e 5: mutating write_file + /decide pf_0 0.05 ⇒ bridge never called + correction');
    app = await driveAgentTurn({
      localModels: LM,
      script: [[toolDeltaStream([{ id: 'call_w1', name: 'write_file', args: '{"path":"out.txt","content":"hello"}' }])], [contentStream('Understood; answering in prose.')]],
      routes: {
        '/decide': { status: 200, json: { ok: true, answers: { pf_0: { noul: 0.05 } }, latency_ms: 3, trace_id: 'tr-hold' } },
        '/ledger': { status: 200, json: { ok: true } }
      },
      until: (a) => fedToolResults(a, 1).length > 0, waitMs: 1400
    });
    try {
      check(modelDispatches(app).length === 0,
        'the bridge was NEVER called on a held rite (got ' + JSON.stringify(modelDispatches(app)) + ')');
      check(!bridgeBodies(app).some((b) => b.name === 'write_file'), 'no write_file body ever reached the bridge');
      const fed = fedToolResults(app, 1);
      check(fed.some((m) => /HELD BY LOCAL CORTEX/.test(String(m.content || '')) && /write_file/.test(String(m.content || ''))),
        'a role:"tool" self-correction carrying the HOLD text is fed back to the model');
      check(ledgerActions(app).indexOf('rejected') !== -1, 'a hold posted /ledger action:"rejected"');
      check(app.bootErrors.length === 0, 'no uncaught boot errors in the held turn');
    } finally { teardownApp(app); }

    /* ==================== 6. driven e2e — batching in the loop (ONE /decide, two pf_ keys) = */
    console.log('--- e2e 6: TWO mutating calls ⇒ exactly ONE /decide carrying pf_0+pf_1');
    app = await driveAgentTurn({
      localModels: LM_NO_ANOM,   /* isolate the pre-flight: the trailing prose issues no anomaly ask */
      script: [
        [toolDeltaStream([
          { id: 'call_w1', name: 'write_file', args: '{"path":"out.txt","content":"hello"}' },
          { id: 'call_r1', name: 'run_command', args: '{"command":"ls"}' }
        ])],
        [contentStream('Done.')]
      ],
      routes: {
        '/decide': { status: 200, json: { ok: true, answers: { pf_0: { noul: 0.9 }, pf_1: { noul: 0.9 } }, latency_ms: 3, trace_id: 'tr-batch' } },
        '/ledger': { status: 200, json: { ok: true } }
      },
      until: (a) => modelDispatches(a).length >= 2, waitMs: 1400
    });
    try {
      check(decideEvents(app).length === 1,
        'exactly ONE /decide for a two-call turn (got ' + decideEvents(app).length + ')');
      check(canon(Object.keys(decideBodies(app)[0].questions)) === canon(['pf_0', 'pf_1']),
        'that single /decide carried BOTH pf_0 and pf_1 (got ' + JSON.stringify(Object.keys(decideBodies(app)[0].questions)) + ')');
      const names = modelDispatches(app).map((d) => d.name).sort();
      check(canon(names) === canon(['run_command', 'write_file']),
        'both mutating rites were dispatched after a high-p verdict (got ' + JSON.stringify(names) + ')');
      check(app.bootErrors.length === 0, 'no uncaught boot errors in the batching turn');
    } finally { teardownApp(app); }

    /* ==================== 7. driven e2e — read-only is NOT pre-flighted =================== */
    console.log('--- e2e 7: a read_file-only turn ⇒ ZERO /decide requests');
    app = await driveAgentTurn({
      localModels: LM_NO_ANOM,
      script: [[toolDeltaStream([{ id: 'call_rd1', name: 'read_file', args: '{"path":"main.py"}' }])], [contentStream('Read.')]],
      routes: {
        '/decide': () => { throw new Error('a read-only turn must NOT call /decide'); },
        '/ledger': { status: 200, json: { ok: true } }
      },
      until: (a) => modelDispatches(a).length > 0, waitMs: 1400
    });
    try {
      check(decideEvents(app).length === 0, 'ZERO /decide requests for a read-only rite (got ' + decideEvents(app).length + ')');
      check(modelDispatches(app).length === 1 && modelDispatches(app)[0].name === 'read_file',
        'the read-only rite was dispatched normally (got ' + JSON.stringify(modelDispatches(app).map((d) => d.name)) + ')');
      check(app.bootErrors.length === 0, 'no uncaught boot errors in the read-only turn');
    } finally { teardownApp(app); }

    /* ==================== 8. driven e2e — anomaly flag (content UNCHANGED) ================ */
    console.log('--- e2e 8: prose-only reply + /decide anomaly 0.93 ⇒ chip, m.cortexAnomaly, content kept');
    app = await driveAgentTurn({
      localModels: LM,
      script: [[contentStream(PROSE_REPLY)]],
      routes: {
        '/decide': { status: 200, json: { ok: true, answers: { anomaly: { noul: 0.93 } }, latency_ms: 4, trace_id: 'tr-anom' } },
        '/ledger': { status: 200, json: { ok: true } }
      },
      until: (a) => !!a.document.getElementById('cortex-anomaly'), waitMs: 1400
    });
    try {
      const chip = app.document.getElementById('cortex-anomaly');
      check(!!chip, 'a visible #cortex-anomaly chip is rendered (got ' + (chip ? 'yes' : 'no') + ')');
      check(/LOCAL CORTEX: REPLY ANOMALY FLAGGED/.test(String(chip && chip.textContent || '')),
        'the chip carries the anomaly note (got ' + JSON.stringify(chip && chip.textContent) + ')');
      check(/REPLY ANOMALY FLAGGED/.test(transcriptText(app)), 'the note is visible in the transcript');
      const am = lastAssistant(app);
      check(am && /\[LOCAL CORTEX: REPLY ANOMALY FLAGGED/.test(String(am.cortexAnomaly || '')),
        'm.cortexAnomaly is set on the message');
      check(am && am.content === PROSE_REPLY, 'the message CONTENT is byte-identical (got ' + JSON.stringify(am && am.content) + ')');
      check(decideEvents(app).length === 1 && canon(Object.keys(decideBodies(app)[0].questions)) === canon(['anomaly']),
        'exactly ONE /decide carrying only the anomaly question (got ' + decideEvents(app).length + ')');
      check(ledgerActions(app).indexOf('flagged') !== -1, 'the anomaly posted /ledger action:"flagged"');
      check(app.bootErrors.length === 0, 'no uncaught boot errors in the anomaly turn');
    } finally { teardownApp(app); }
    /* low anomaly probability ⇒ no chip */
    app = await driveAgentTurn({
      localModels: LM,
      script: [[contentStream(PROSE_REPLY)]],
      routes: {
        '/decide': { status: 200, json: { ok: true, answers: { anomaly: { noul: 0.10 } }, latency_ms: 4, trace_id: 'tr-ok' } },
        '/ledger': { status: 200, json: { ok: true } }
      },
      until: (a) => decideEvents(a).length > 0, waitMs: 1400
    });
    try {
      check(!app.document.getElementById('cortex-anomaly'), 'anomaly noul 0.10 ⇒ NO chip');
      const am2 = lastAssistant(app);
      check(!am2 || !am2.cortexAnomaly, 'm.cortexAnomaly stays unset (got ' + JSON.stringify(am2 && am2.cortexAnomaly) + ')');
      check(am2 && am2.content === PROSE_REPLY, 'the content is still byte-identical');
    } finally { teardownApp(app); }

    /* ==================== 9. driven e2e — fail open (down + slow) ======================== */
    console.log('--- e2e 9a: /decide UNMOCKED (network error) ⇒ write_file still dispatched, no throw');
    app = await driveAgentTurn({
      localModels: LM,
      script: [[toolDeltaStream([{ id: 'call_w1', name: 'write_file', args: '{"path":"out.txt","content":"hello"}' }])], [contentStream('Wrote it.')]],
      routes: { '/ledger': { status: 200, json: { ok: true } } },   /* /decide deliberately unmocked */
      until: (a) => modelDispatches(a).length > 0, waitMs: 1400
    });
    try {
      check(modelDispatches(app).length === 1 && modelDispatches(app)[0].name === 'write_file',
        'a dead /decide fails open ⇒ the mutating call IS dispatched (got ' + JSON.stringify(modelDispatches(app).map((d) => d.name)) + ')');
      check(app.bootErrors.length === 0, 'the dead /decide caused no uncaught error');
    } finally { teardownApp(app); }
    console.log('--- e2e 9b: /decide resolving after ~2 s vs laya.timeoutMs 120 ⇒ bounded, fail-open');
    const slowDecide = () => new Promise((res) => setTimeout(() => res({
      ok: true, status: 200,
      json: async () => ({ ok: true, answers: { pf_0: { noul: 0.95 } }, latency_ms: 2000, trace_id: 'tr-slow' }),
      text: async () => ''
    }), 2000));
    app = await driveAgentTurn({
      localModels: layaCfg({ laya: { enabled: true, minConfidence: 0.70, timeoutMs: 120, preflight: true, anomaly: true } }),
      script: [[toolDeltaStream([{ id: 'call_w1', name: 'write_file', args: '{"path":"out.txt","content":"hello"}' }])], [contentStream('Wrote it.')]],
      routes: { '/decide': slowDecide, '/ledger': { status: 200, json: { ok: true } } },
      until: (a) => modelDispatches(a).length > 0, waitMs: 1400
    });
    try {
      check(app._elapsed < 1400, 'the turn completed well within a healthy wall-clock bound (took ' + app._elapsed + 'ms)');
      check(modelDispatches(app).length === 1 && modelDispatches(app)[0].name === 'write_file',
        'a timed-out pre-flight fails open ⇒ the call IS dispatched (got ' + JSON.stringify(modelDispatches(app).map((d) => d.name)) + ')');
      check(app.bootErrors.length === 0, 'the slow /decide caused no uncaught error');
    } finally { teardownApp(app); }

    /* ==================== 10. toggles remove exactly their own call ====================== */
    console.log('--- e2e 10a: preflight:false on a mutating turn ⇒ zero /decide (anomaly also off) --');
    app = await driveAgentTurn({
      localModels: lmCfg({ laya: { enabled: true, minConfidence: 0.70, timeoutMs: 500, preflight: false, anomaly: false } }),
      script: [[toolDeltaStream([{ id: 'call_w1', name: 'write_file', args: '{"path":"out.txt","content":"hello"}' }])], [contentStream('Wrote it.')]],
      routes: { '/decide': () => { throw new Error('preflight:false must remove the pre-flight call'); }, '/ledger': { status: 200, json: { ok: true } } },
      until: (a) => modelDispatches(a).length > 0, waitMs: 1400
    });
    try {
      check(decideEvents(app).length === 0, 'preflight:false ⇒ zero /decide on a mutating turn (got ' + decideEvents(app).length + ')');
      check(modelDispatches(app).length === 1, 'the mutating rite is still dispatched with preflight:false');
    } finally { teardownApp(app); }
    console.log('--- e2e 10b: anomaly:false on a prose turn ⇒ zero /decide ---------------------------');
    app = await driveAgentTurn({
      localModels: LM_NO_ANOM,
      script: [[contentStream(PROSE_REPLY)]],
      routes: { '/decide': () => { throw new Error('anomaly:false must remove the anomaly call'); }, '/ledger': { status: 200, json: { ok: true } } },
      until: (a) => chatPosts(a).length > 0, waitMs: 1400
    });
    try {
      check(decideEvents(app).length === 0, 'anomaly:false ⇒ zero /decide on a prose turn (got ' + decideEvents(app).length + ')');
      check(!app.document.getElementById('cortex-anomaly'), 'anomaly:false ⇒ no chip');
    } finally { teardownApp(app); }
    console.log('--- e2e 10c: preflight:false on a prose turn ⇒ the anomaly /decide STILL happens ------');
    app = await driveAgentTurn({
      localModels: LM_NO_PF,
      script: [[contentStream(PROSE_REPLY)]],
      routes: {
        '/decide': { status: 200, json: { ok: true, answers: { anomaly: { noul: 0.9 } }, latency_ms: 3, trace_id: 'tr-an' } },
        '/ledger': { status: 200, json: { ok: true } }
      },
      until: (a) => !!a.document.getElementById('cortex-anomaly'), waitMs: 1400
    });
    try {
      check(decideEvents(app).length === 1, 'preflight:false does NOT remove the anomaly call (got ' + decideEvents(app).length + ')');
      check(!!app.document.getElementById('cortex-anomaly'), 'the anomaly chip still appears');
    } finally { teardownApp(app); }

    /* ==================== 11. kill-the-model: identical to enabled:false ================= */
    console.log('--- e2e 11: sidecar down (laya on, /decide unmocked) == enabled:false, byte-for-byte --');
    const killScript = [[toolDeltaStream([{ id: 'call_rd1', name: 'read_file', args: '{"path":"main.py"}' }])], [contentStream('Done, operator.')]];
    let appOff = await driveAgentTurn({
      localModels: LM_OFF,
      script: killScript,
      routes: {},
      until: (a) => modelDispatches(a).length > 0, waitMs: 1400
    });
    let appDown;
    try {
      appDown = await driveAgentTurn({
        localModels: LM,          /* Laya enabled but every /decide is unmocked (sidecar down) */
        script: killScript,
        routes: {},
        until: (a) => modelDispatches(a).length > 0, waitMs: 1400
      });
      try {
        const rawOff = transcriptText(appOff), rawDown = transcriptText(appDown);
        const tOff = stableTranscript(appOff), tDown = stableTranscript(appDown);
        /* Evidence for the gatelog: the two runs are only expected to differ in the rendered
           wall clock. Count them so a future agent can tell a timestamp flake from a real diff. */
        const times = (s) => (s.match(/\b\d{1,2}:\d{2}(:\d{2})?\b/g) || []).length;
        console.log('   [kill-the-model] raw identical=' + (rawOff === rawDown)
          + ' clock markers ' + times(rawOff) + '/' + times(rawDown)
          + ', normalised identical=' + (tOff === tDown));
        check(tOff === tDown,
          'the transcript text is IDENTICAL with the sidecar down and with localModels.enabled:false'
          + (tOff === tDown ? '' : '\n       enabled:false : ' + JSON.stringify(tOff)
                                + '\n       sidecar down: ' + JSON.stringify(tDown)));

        check(canon(modelDispatches(appOff)) === canon(modelDispatches(appDown)),
          'the bridge request log is IDENTICAL (canonical JSON)');
        check(appOff.bootErrors.length === 0 && appDown.bootErrors.length === 0, 'neither run threw');
      } finally { ALL_APPS.push(appOff); teardownApp(appDown); }
    } finally { teardownApp(appOff); }

    /* ==================== 12. privacy: no key, no Authorization, zero :8932 when off ===== */
    console.log('--- privacy: /decide + /ledger carry no API key and no Authorization header ---');
    let authLeak = 0, keyLeak = 0, seenDecide = 0, seenLedger = 0;
    for (const a of ALL_APPS) {
      for (const e of a.events) {
        const isLocal = /\/decide(\?|$)/.test(String(e.url)) || /\/ledger(\?|$)/.test(String(e.url));
        if (!isLocal) continue;
        if (/\/decide(\?|$)/.test(String(e.url))) seenDecide++;
        if (/\/ledger(\?|$)/.test(String(e.url))) seenLedger++;
        const hdrs = JSON.stringify(e.headers || {});
        if (/authorization/i.test(hdrs)) authLeak++;
        if (String(e.body || '').indexOf(SENTINEL) !== -1) keyLeak++;
        if (hdrs.indexOf(SENTINEL) !== -1) keyLeak++;
      }
    }
    check(seenDecide > 0 && seenLedger > 0,
      'the privacy check is not vacuous (' + seenDecide + ' /decide, ' + seenLedger + ' /ledger observed)');
    check(authLeak === 0, 'no /decide or /ledger request carried an Authorization header (got ' + authLeak + ')');
    check(keyLeak === 0, 'no /decide or /ledger request body/header carried the seeded API key sentinel (got ' + keyLeak + ')');
    let anyKeyBody = 0;
    for (const a of ALL_APPS) for (const e of a.events) if (String(e.body || '').indexOf(SENTINEL) !== -1) anyKeyBody++;
    check(anyKeyBody === 0, 'no request body anywhere contains the API key sentinel (got ' + anyKeyBody + ')');
    const offPort = appOff.events.filter((e) => String(e.url).indexOf('8932') !== -1);
    check(offPort.length === 0, 'ZERO requests to :8932 while localModels.enabled:false (got ' + offPort.length + ')');
  }

  process.exit(summary('PHASE 13 LAYA GATES'));
})().catch((e) => { console.error(e); process.exit(1); });
