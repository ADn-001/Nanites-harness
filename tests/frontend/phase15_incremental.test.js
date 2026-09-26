'use strict';
/*
 * PHASE 15 E2E — incremental (streaming) detection (`CogCore.cortexStreamDetect`) and its
 * agent-loop wiring.
 *
 * Contract under test (plan §"Phase 15" task 1):
 *  - As the deltas ACCUMULATE, the cheap deterministic checks run as soon as a tool name
 *    (+ partial arguments) exist. A near-miss name or JSON that is ALREADY malformed starts
 *    AT MOST ONE fire-and-forget `/repair` probe per TURN.
 *  - The stream is never buffered, the probe is NEVER awaited inside `pumpSSE`, and a probe
 *    result that settles after the turn decided is DISCARDED (its outcome is still ledgered
 *    with an action word from the existing vocabulary).
 *  - The early result is folded into the SAME `sanitizeReply` verdict the end-of-stream path
 *    computes — it never fights it over `m.toolCalls`, and the Phase 7 validator still gates
 *    anything it produces.
 *  - Probes are opt-in: any flag off, mode 'off', or no client ⇒ ZERO requests and a
 *    byte-identical turn.
 *
 * Harness copied wholesale from phase14_dispatcher.test.js (SSE routes, driveAgentTurn,
 * modelDispatches, stableTranscript, ev8932) — see the Phase 11/12/13 gatelog findings for the
 * landmines those helpers exist to neutralise.
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

/* ---- SSE helpers (same shape the Phase 7/8/11/12/13/14 suites use) ---- */
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
/* A stream that PAUSES after `pauseAfter` chunks until `release()` is called — the only honest
   way to prove the probe fires from PARTIAL deltas, before [DONE], mid-stream. */
function gatedSseBody(events, pauseAfter) {
  const chunks = events.map((e) => 'data: ' + JSON.stringify(e) + '\n\n');
  chunks.push('data: [DONE]\n\n');
  let i = 0, released = null, open = true;
  const gate = new Promise((r) => { released = r; });
  return {
    get open() { return open; },
    get released() { return i >= chunks.length; },
    release() { if (released) released(); },
    getReader() {
      return {
        read: async () => {
          if (i === pauseAfter) await gate;
          if (i >= chunks.length) { open = false; return { done: true, value: undefined }; }
          const v = { done: false, value: chunks[i++] };
          if (i >= chunks.length) open = false;
          return v;
        }
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
/* Full localModels block so a per-test override replaces it cleanly (Object.assign is shallow). */
function lmCfg(over) {
  return Object.assign({
    enabled: true,
    needle: { enabled: true, minConfidence: 0.75, confirmBand: [0.5, 0.75], timeoutMs: 800 },
    laya: { enabled: false, minConfidence: 0.70, timeoutMs: 500, preflight: true, anomaly: true },
    sanitizer: { enabled: true, mode: 'auto', deterministicPass: true },
    dispatcher: { enabled: false, autoReadOnly: true, timeoutMs: 800, minConfidence: 0.75 },
    port: 8932
  }, over || {});
}
const LM = lmCfg();
const LM_OFF = lmCfg({ enabled: false });
const NEEDLE_OFF = lmCfg({ needle: { enabled: false, minConfidence: 0.75, confirmBand: [0.5, 0.75], timeoutMs: 800 } });
const SAN_OFF = lmCfg({ sanitizer: { enabled: false, mode: 'auto', deterministicPass: true } });
const MODE_OFF = lmCfg({ sanitizer: { enabled: true, mode: 'off', deterministicPass: true } });

/* ---- stub local-models client (counts calls; never touches the network) ---- */
function stubClient(response) {
  const c = { n: 0, last: null, outcomes: [] };
  return {
    c,
    repair: function (payload) {
      c.n++;
      c.last = payload;
      try {
        return Promise.resolve(typeof response === 'function' ? response(payload) : response);
      } catch (e) { return Promise.reject(e); }
    },
    outcome: function (payload) {
      c.outcomes.push(payload);
      return Promise.resolve({ ok: true });
    }
  };
}
const REPAIR_OK = { ok: true, calls: [{ name: 'read_file', arguments: { path: 'main.py' } }], confidence: 0.93, trace_id: 'tr-inc' };

/* ---- accumulated-delta states (what `mergeToolDelta` has built so far) ---- */
const accOf = (calls) => ({ content: '', thinking: '', toolCalls: calls, _fallback: 0 });
const GOOD = accOf([{ id: 'c1', name: 'read_file', args: '{"path":"main.py"}' }]);
const PARTIAL = accOf([{ id: 'c1', name: 'read_file', args: '{"path":"mai' }]);
const NEARMISS = accOf([{ id: 'c1', name: 'raed_file', args: '{"path":"mai' }]);
const MALFORMED = accOf([{ id: 'c1', name: 'read_file', args: "{'path': 'main.py'}" }]);
const MALFORMED2 = accOf([{ id: 'c1', name: 'read_file', args: '{"path" "main.py"}' }]);
const FENCED = accOf([{ id: 'c1', name: 'read-file', args: '```json\n{"path":"main.py"}\n```' }]);
const TRUNC = accOf([{ id: 'c1', name: 'read_file', args: '{"path":"main.py","line":' }]);

/* ---- app event helpers (verbatim phase-13/14 patterns) ---- */
const bridgeBodies = (app) => app.events
  .filter((e) => e.url === BRIDGE || e.url.endsWith('/tools/execute'))
  .map((e) => { try { return JSON.parse(e.body || '{}'); } catch (err) { return {}; } });
const chatPosts = (app) => app.events.filter((e) => e.method === 'POST' && e.url.includes('chat/completions'));
/*
 * LANDMINE (Phase 11/12 findings): `buildMessages()` POSTs {name:'list_dir',arguments:{path:'.'}}
 * to the SAME bridge route on every agent iteration, so raw bridge POSTs over-count. Every
 * dispatch assertion here goes through `modelDispatches`.
 */
const WORKDIR_LISTING = canon({ name: 'list_dir', arguments: { path: '.' } });
const modelDispatches = (app) => bridgeBodies(app)
  .filter((b) => canon({ name: b.name, arguments: b.arguments }) !== WORKDIR_LISTING);
const ev8932 = (app) => app.events.filter((e) => String(e.url).indexOf('8932') !== -1);
const localEvents = (app, route) => app.events.filter((e) => e.method === 'POST' && new RegExp('/' + route + '(\\?|$)').test(e.url));
const repairEvents = (app) => localEvents(app, 'repair');
const ledgerActions = (app) => localEvents(app, 'ledger').map((e) => { try { return JSON.parse(e.body || '{}').action; } catch (x) { return null; } });
const transcriptText = (app) => {
  const el = app.document.getElementById('messages');
  return el ? String(el.textContent || '') : '';
};
/*
 * LANDMINE (Phase 13 finding): the rendered transcript embeds the wall clock
 * (`new Date(m.ts).toLocaleTimeString()`), so two runs are never byte-identical. Normalise
 * that volatile rendering out and compare what actually matters. Do NOT loosen this further.
 */
const stableTranscript = (app) => transcriptText(app)
  .replace(/\b\d{1,2}:\d{2}(:\d{2})?\b/g, '<time>');
const messagesWithNote = (app) => {
  const c = app.window.active();
  return ((c && c.messages) || []).filter((m) => m && (m.cortexNote || m.cortexDegraded));
};
const $ = (app, id) => app.document.getElementById(id);

const ALL_APPS = [];
async function driveAgentTurn(opts) {
  opts = opts || {};
  const routes = Object.assign({}, BASE_ROUTES, opts.routes || {});
  if (!opts.rawBody) routes['/v1/chat/completions'] = chatRoute(opts.script);
  const seed = { 'cogitator.settings': JSON.stringify(Object.assign(baseSettings(), {
    localModels: opts.localModels || LM,
    autoApproveRead: opts.autoApproveRead === undefined ? true : opts.autoApproveRead
  })) };
  const app = await launchApp({ routes, seed });
  await tick(60);
  /* LANDMINE: the inline script uses TextDecoder during SSE aggregation; jsdom's is not wired to
     the fake Response-like body, so stub the window's TextDecoder BEFORE clicking send. */
  app.window.TextDecoder = class { constructor() {} decode(v) { return v == null ? '' : String(v); } };
  const $a = (id) => app.document.getElementById(id);
  $a('ta').value = opts.text || 'inspect the workdir';
  const t0 = Date.now();
  /* Headless operator: auto-authorize any mutating rite the turn asks to dispatch. */
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
  const LIVE_TOOLS = JSON.parse(String(boot.window.eval('JSON.stringify(TOOL_SCHEMAS)')));
  const shipped = boot.window.CogCore;
  teardownApp(boot);
  check(LIVE_TOOLS.length === 6, 'live TOOL_SCHEMAS has the 6 expected rites (got ' + LIVE_TOOLS.length + ')');
  check(typeof shipped.cortexStreamDetect === 'function', 'the SHIPPED window.CogCore exposes cortexStreamDetect');
  check(typeof CogCore.cortexStreamDetect === 'function', 'CogCore.cortexStreamDetect exists');

  if (typeof CogCore.cortexStreamDetect === 'function') {
    /* ==================== 1. the JSON-prefix classifier ==================== */
    console.log('--- unit 1: can more deltas still complete this text? (the "already malformed" test) ---');
    const pre = CogCore._cortexJsonPrefix;
    check(typeof pre === 'function', 'the SHIPPED classifier _cortexJsonPrefix exists');
    if (typeof pre === 'function') {
      check(pre('{"path":"main.py"}') === 'complete', 'a whole object is "complete" (got ' + pre('{"path":"main.py"}') + ')');
      check(pre('{"path":"mai') === 'partial', 'a mid-argument object is "partial" (got ' + pre('{"path":"mai') + ')');
      check(pre('{"path":"main.py","line":') === 'partial', 'a value that has not arrived yet is "partial", not broken');
      check(pre('{"path" "main.py"}') === 'malformed', 'a missing colon is "malformed" (got ' + pre('{"path" "main.py"}') + ')');
      check(pre("{'path': 'main.py'}") === 'malformed', 'single-quoted JSON is "malformed" (got ' + pre("{'path': 'main.py'}") + ')');
      check(pre('{"path":"a.py"}}') === 'malformed', 'an extra closing brace is "malformed" (got ' + pre('{"path":"a.py"}}') + ')');
      check(pre('nonsense') === 'malformed', 'text that cannot start an object is "malformed" (got ' + pre('nonsense') + ')');
      check(pre('') === 'partial', 'nothing has arrived yet ⇒ "partial" (got ' + pre('') + ')');
      check(pre('[1,2,') === 'partial' && pre('[1,2,]') === 'malformed', 'arrays classify the same way');
    }

    /* ==================== 2. detection on the ACCUMULATED deltas ==================== */
    console.log('--- unit 2: detection reads the accumulated deltas, not a finished reply ---');
    const clean = stubClient(REPAIR_OK);
    const dClean = CogCore.cortexStreamDetect(GOOD, { allowedTools: LIVE_TOOLS, client: clean, settings: lmCfg() });
    check(dClean && typeof dClean.then !== 'function',
      'a non-probing call returns a PLAIN OBJECT, never a Promise (the pump must not await it)');
    check(clean.c.n === 0 && dClean.probeStarted === false,
      'a well-formed streamed call starts NO probe (got ' + clean.c.n + ')');
    check(dClean.sawCall === true, 'the accumulator is still READ (sawCall:true on a tool-call slot)');
    check(dClean.reason === 'clean', 'reason:"clean" for a well-formed call (got ' + dClean.reason + ')');

    const clean2 = stubClient(REPAIR_OK);
    const dNoCall = CogCore.cortexStreamDetect(accOf([]), { allowedTools: LIVE_TOOLS, client: clean2, settings: lmCfg() });
    check(clean2.c.n === 0 && dNoCall.sawCall === false && dNoCall.probeStarted === false,
      'a prose-only accumulator starts no probe and reports sawCall:false');

    const near = stubClient(REPAIR_OK);
    const dNear = CogCore.cortexStreamDetect(NEARMISS, { allowedTools: LIVE_TOOLS, client: near, settings: lmCfg() });
    check(near.c.n === 1 && dNear.probeStarted === true,
      'a near-miss tool name in the accumulated deltas starts the probe (got ' + near.c.n + ')');
    check(near.c.last && Array.isArray(near.c.last.suspect) && near.c.last.suspect.length === 1,
      'the /repair payload carries exactly one suspect');
    check(near.c.last && near.c.last.suspect[0].name === 'raed_file',
      'the payload NAMES the suspect (got ' + JSON.stringify(near.c.last && near.c.last.suspect[0] && near.c.last.suspect[0].name) + ')');
    check(near.c.last && /raed_file/.test(String(near.c.last.suspect[0].reason || '')),
      'the suspect carries a reason the model can act on');
    check(near.c.last && near.c.last.suspect[0].arguments === '{"path":"mai',
      'the payload carries the PARTIAL arguments seen so far, not an invented value');
    check(near.c.last && Array.isArray(near.c.last.candidates) && near.c.last.candidates.length === 6
      && typeof near.c.last.trace_id === 'string' && near.c.last.trace_id.length > 0,
      'the payload offers the allowed schemas and a trace_id, like every other cortex payload');
    check(near.c.last && !/settings|apiKey|Authorization/i.test(JSON.stringify(near.c.last)),
      'the payload never carries settings or an API key');
    check(dNear.probe && typeof dNear.probe.then === 'function',
      'the started probe is handed back as a promise the caller may ignore entirely');

    for (const bad of [MALFORMED, MALFORMED2]) {
      const sb = stubClient(REPAIR_OK);
      const db = CogCore.cortexStreamDetect(bad, { allowedTools: LIVE_TOOLS, client: sb, settings: lmCfg() });
      check(sb.c.n === 1 && db.probeStarted === true,
        'already-malformed JSON in the accumulated deltas starts the probe: ' + JSON.stringify(bad.toolCalls[0].args));
      check(sb.c.last && /malformed|not a parseable/i.test(String(sb.c.last.suspect[0].reason || '')),
        'the malformed suspect says so in its reason');
    }

    const sbPart = stubClient(REPAIR_OK);
    const dPart = CogCore.cortexStreamDetect(PARTIAL, { allowedTools: LIVE_TOOLS, client: sbPart, settings: lmCfg() });
    check(sbPart.c.n === 0 && dPart.probeStarted === false,
      'a merely PARTIAL argument is not "already malformed" ⇒ NO probe (got ' + sbPart.c.n + ')');
    const sbTrunc = stubClient(REPAIR_OK);
    const dTrunc = CogCore.cortexStreamDetect(TRUNC, { allowedTools: LIVE_TOOLS, client: sbTrunc, settings: lmCfg() });
    check(sbTrunc.c.n === 0 && dTrunc.reason === 'still_streaming',
      'a truncated-but-extendable argument waits for more deltas (reason:"' + dTrunc.reason + '")');
    const sbFence = stubClient(REPAIR_OK);
    const dFence = CogCore.cortexStreamDetect(FENCED, { allowedTools: LIVE_TOOLS, client: sbFence, settings: lmCfg() });
    check(sbFence.c.n === 0 && dFence.probeStarted === false,
      'a call the deterministic pass already fixes (fence + separator) spends NO model call');

    /* ==================== 3. the hard opt-in gate ==================== */
    console.log('--- unit 3: probes are opt-in; any flag off ⇒ zero requests ---');
    const noClient = CogCore.cortexStreamDetect(NEARMISS, { allowedTools: LIVE_TOOLS, settings: lmCfg() });
    check(noClient && noClient.probeStarted === false && noClient.probe === null && noClient.reason === 'off',
      'no client ⇒ reason:"off" and no probe at all');
    const gateCases = [
      ['localModels.enabled:false', lmCfg({ enabled: false })],
      ['needle.enabled:false', NEEDLE_OFF],
      ['sanitizer.enabled:false', SAN_OFF],
      ["sanitizer.mode:'off'", MODE_OFF],
      ['sanitizer.deterministicPass:false', lmCfg({ sanitizer: { enabled: true, mode: 'auto', deterministicPass: false } })]
    ];
    for (const [label, cfg] of gateCases) {
      const s = stubClient(REPAIR_OK);
      const d = CogCore.cortexStreamDetect(NEARMISS, { allowedTools: LIVE_TOOLS, client: s, settings: cfg });
      check(s.c.n === 0 && d.probeStarted === false && d.reason === 'off',
        label + ' ⇒ ZERO /repair (got ' + s.c.n + ')');
    }
    const latched = stubClient(REPAIR_OK);
    const dLatch = CogCore.cortexStreamDetect(NEARMISS, { allowedTools: LIVE_TOOLS, client: latched, settings: lmCfg(), alreadyProbed: true });
    check(latched.c.n === 0 && dLatch.probeStarted === false && dLatch.reason === 'already_probed',
      'the per-turn latch stops a second probe (reason:"already_probed")');

    /* ==================== 4. never throws, never rejects ==================== */
    console.log('--- unit 4: garbage accumulators and a hostile client ---');
    let threw = false, shapeOk = true;
    for (const junk of [null, undefined, 0, 'x', {}, { toolCalls: 'x' }, { toolCalls: [null, undefined, {}] },
      { toolCalls: [{ name: 'read_file' }] }, { toolCalls: [{ name: '', args: '' }] }]) {
      try {
        const o = CogCore.cortexStreamDetect(junk, { allowedTools: LIVE_TOOLS, client: stubClient(REPAIR_OK), settings: lmCfg() });
        shapeOk = shapeOk && o && typeof o.reason === 'string' && Array.isArray(o.suspects) &&
          typeof o.probeStarted === 'boolean' && Array.isArray(o.payload || []);
      } catch (e) { threw = true; }
    }
    check(!threw, 'a garbage accumulator never throws');
    check(shapeOk, 'a garbage accumulator always returns the documented shape');
    const hostile = stubClient(() => { throw new Error('client exploded'); });
    let hostileRejected = false;
    const dHostile = CogCore.cortexStreamDetect(NEARMISS, { allowedTools: LIVE_TOOLS, client: hostile, settings: lmCfg() });
    try { await dHostile.probe; } catch (e) { hostileRejected = true; }
    check(hostile.c.n === 1 && dHostile.probeStarted === true && !hostileRejected,
      'a client that throws synchronously is captured, not propagated');
    const rejecting = { repair: () => Promise.reject(new Error('network down')), outcome: () => Promise.resolve({ ok: true }) };
    const dRej = CogCore.cortexStreamDetect(NEARMISS, { allowedTools: LIVE_TOOLS, client: rejecting, settings: lmCfg() });
    let rej = null, rejected = false;
    try { rej = await dRej.probe; } catch (e) { rejected = true; }
    check(!rejected && rej && rej.ok === false && rej.degraded === true,
      'a rejecting client resolves to {ok:false,degraded:true} so the turn can never hang');

    /* ==================== 5. settling the probe at the turn's decision point ============ */
    console.log('--- unit 5: the probe is consumed ONCE, late results are discarded + ledgered ---');
    const settle = CogCore._cortexStreamSettle;
    check(typeof settle === 'function', 'the SHIPPED settle helper _cortexStreamSettle exists');
    if (typeof settle === 'function') {
      const noProbe = settle(null, { client: stubClient(REPAIR_OK), settings: lmCfg() });
      check(typeof noProbe.then !== 'function' && canon(noProbe) === canon({ present: false }),
        'no probe on the turn ⇒ {present:false} as a PLAIN OBJECT (byte-identical to no cortex at all)');

      /* an already-settled probe needs no await at all */
      const settledTurn = { probe: Promise.resolve(REPAIR_OK), trace_id: 'tr-s', settled: true, value: REPAIR_OK };
      const doneSync = settle(settledTurn, { client: stubClient(REPAIR_OK), settings: lmCfg() });
      check(typeof doneSync.then !== 'function' && doneSync.present === true && doneSync.expired === false
        && doneSync.value === REPAIR_OK,
        'an already-settled probe resolves synchronously with its value');
      check(settledTurn.decided === true, 'settling marks the turn as DECIDED (so a late result is detectable)');

      /* an in-flight probe: bounded, and it never hangs the turn */
      let releaseRepair = null;
      const hanging = new Promise((r) => { releaseRepair = r; });
      const hangTurn = { probe: hanging, trace_id: 'tr-h', settled: false, value: null };
      const hangLedger = { actions: [] };
      const hangClient = { outcome: (p) => { hangLedger.actions.push(p.action); return Promise.resolve({ ok: true }); } };
      const raced = settle(hangTurn, { client: hangClient, settings: lmCfg({ needle: { enabled: true, minConfidence: 0.75, confirmBand: [0.5, 0.75], timeoutMs: 30 } }) });
      check(typeof raced.then === 'function', 'an in-flight probe is awaited (bounded) at the decision point');
      const racedRes = await raced;
      check(racedRes.expired === true && racedRes.value === null && racedRes.present === true,
        'a probe that does not answer inside needle.timeoutMs reports {expired:true} and carries NO value');
      check(hangLedger.actions.indexOf('timeout') !== -1 && racedRes.ledgered === true,
        'the bounded wait is ledgered with the existing action word "timeout" (got ' + JSON.stringify(hangLedger.actions) + ')');
      check(hangTurn.discarded === true, 'the turn records that its one probe is spent');

      /* the late arrival itself: discarded, never applied, never ledgered a second time */
      const before = hangLedger.actions.length;
      releaseRepair({ ok: true, calls: [{ name: 'read_file', arguments: { path: 'late.py' } }], confidence: 0.99 });
      await tick(20);
      check(hangLedger.actions.length === before,
        'a probe that lands after the turn decided adds NO second ledger line for the same probe');

      /* a genuinely late result on a turn that decided without it: discarded + ledgered once */
      const straggler = { probe: Promise.resolve(REPAIR_OK), trace_id: 'tr-strag', settled: false, value: null, decided: true };
      const stragLedger = { actions: [] };
      const stragClient = { outcome: (p) => { stragLedger.actions.push(p.action); return Promise.resolve({ ok: true }); } };
      const stragRes = settle(straggler, { client: stragClient, settings: lmCfg(), alreadyDecided: true });
      await tick(20);
      check(stragRes && stragRes.expired === true && stragRes.value === null,
        'a turn that already decided gets NO value out of a straggler, even one holding a repair');
      check(stragLedger.actions.length === 1
        && ['accepted', 'passed_through', 'rejected', 'timeout', 'accepted_by_operator', 'ignored_by_operator'].indexOf(stragLedger.actions[0]) !== -1
        && stragLedger.actions[0] !== 'accepted',
        'a discarded straggler is ledgered exactly once, with a non-accepting vocabulary action (got ' + JSON.stringify(stragLedger.actions) + ')');
      let stragThrew = false;
      try { settle(null, {}); settle(undefined, null); CogCore._cortexStreamLate(null, { client: stragClient }); } catch (e) { stragThrew = true; }
      check(!stragThrew, 'settling/ledgering a straggler never throws, even with no turn and no client');
    }

    /* ==================== 6. folding the early result into the SAME verdict ============= */
    console.log('--- unit 6: sanitizeReply learns the early probe instead of fighting it ---');
    const SUSPECT = { tool_calls: [{ id: 'call_s1', type: 'function', function: { name: 'raed_file', arguments: '{"path":"main.py"}' } }] };
    const FENCED_CALL = { tool_calls: [{ id: 'f', type: 'function', function: { name: 'read-file', arguments: '```json\n{"path":"main.py"}\n```' } }] };
    const salvSuspect = CogCore.salvageToolCalls(SUSPECT, LIVE_TOOLS);
    check(salvSuspect.calls.length === 0 && salvSuspect.unrepairable.length === 1,
      'BASELINE: the suspect reply is genuinely unrepairable by the deterministic pass');
    check(CogCore.salvageToolCalls(FENCED_CALL, LIVE_TOOLS).calls.length === 1,
      'BASELINE: the fenced/near-miss-half reply really is deterministic-repairable');

    const ctrl = stubClient(REPAIR_OK);
    await CogCore.sanitizeReply(SUSPECT, LIVE_TOOLS, { client: ctrl, settings: lmCfg() });
    check(ctrl.c.n === 1, 'BASELINE: with no early probe the end-of-stream stage asks once');

    const already = stubClient(REPAIR_OK);
    const rEarly = await CogCore.sanitizeReply(SUSPECT, LIVE_TOOLS, {
      client: already, settings: lmCfg(), earlyRepair: { trace_id: 'tr-early', value: REPAIR_OK, expired: false }
    });
    check(already.c.n === 0,
      'an early probe on the turn means the end-of-stream stage asks NOTHING again (got ' + already.c.n + ' second request)');
    check(rEarly.accepted === true && rEarly.source === 'needle' && rEarly.calls.length === 1
      && rEarly.calls[0].name === 'read_file' && rEarly.calls[0].args.path === 'main.py',
      'the early result is folded into the SAME verdict shape (source:"needle")');
    check(rEarly.early === 'used', 'the verdict says the early result WAS used (early:"used", got ' + rEarly.early + ')');
    check(already.c.outcomes.map((o) => o.action).indexOf('accepted') !== -1,
      'the early repair is ledgered as accepted (got ' + JSON.stringify(already.c.outcomes) + ')');

    const expiredClient = stubClient(REPAIR_OK);
    const rExpired = await CogCore.sanitizeReply(SUSPECT, LIVE_TOOLS, {
      client: expiredClient, settings: lmCfg(), earlyRepair: { trace_id: 'tr-exp', value: null, expired: true }
    });
    check(expiredClient.c.n === 0 && rExpired.accepted === false && rExpired.source === 'original' && rExpired.reason === 'timeout',
      'an expired early probe degrades to a pass-through and never issues a second request');
    check(expiredClient.c.outcomes.map((o) => o.action).indexOf('timeout') !== -1,
      'the expiry is ledgered with the existing "timeout" action');

    const cleanEarly = stubClient(REPAIR_OK);
    const rClean = await CogCore.sanitizeReply(FENCED_CALL, LIVE_TOOLS, {
      client: cleanEarly, settings: lmCfg(), earlyRepair: { trace_id: 'tr-c', value: REPAIR_OK, expired: false }
    });
    check(cleanEarly.c.n === 0 && rClean.source === 'deterministic' && rClean.calls.length === 1,
      'an early probe on a turn that turns out NOT to need it changes nothing (deterministic)');

    const evilEarly = stubClient(REPAIR_OK);
    const rEvil = await CogCore.sanitizeReply(SUSPECT, LIVE_TOOLS, {
      client: evilEarly, settings: lmCfg(),
      earlyRepair: { trace_id: 'tr-evil', value: { ok: true, calls: [{ name: 'evil_tool', arguments: {} }], confidence: 0.99 }, expired: false }
    });
    check(rEvil.accepted === false && rEvil.calls.length === 0,
      'the Phase 7 validator still gates an early repair naming an UNKNOWN tool');
    const lowEarly = stubClient(REPAIR_OK);
    const rLow = await CogCore.sanitizeReply(SUSPECT, LIVE_TOOLS, {
      client: lowEarly, settings: lmCfg(),
      earlyRepair: { trace_id: 'tr-low', value: Object.assign({}, REPAIR_OK, { confidence: 0.4 }), expired: false }
    });
    check(rLow.accepted === false && rLow.calls.length === 0 && rLow.reason === 'below_threshold',
      'an early repair below the confidence threshold is not dispatched (got ' + rLow.reason + ')');
    const downEarly = stubClient(REPAIR_OK);
    const rDown = await CogCore.sanitizeReply(SUSPECT, LIVE_TOOLS, {
      client: downEarly, settings: lmCfg(), earlyRepair: { trace_id: 'tr-down', value: { ok: false, degraded: true }, expired: false }
    });
    check(rDown.accepted === false && rDown.calls.length === 0 && downEarly.c.n === 0,
      'a degraded early result (sidecar down) is a pass-through, never a retry');
    /* The deterministic half must be a DIFFERENT rite from the repaired one. Using a second
       read_file would make this a duplicate: `_cortexMergeCalls` dedupes on (name,args), so
       both halves collapsing to read_file/main.py correctly yields ONE call and the case
       would assert nothing about merging at all. */
    const FENCED_W = { tool_calls: [{ id: 'w', type: 'function', function: { name: 'write_file', arguments: '```json\n{"path":"a.txt","content":"hi"}\n```' } }] };
    const mixed = stubClient(REPAIR_OK);
    const rMixed = await CogCore.sanitizeReply(
      { tool_calls: FENCED_W.tool_calls.concat(SUSPECT.tool_calls) }, LIVE_TOOLS,
      { client: mixed, settings: lmCfg(), earlyRepair: { trace_id: 'tr-mix', value: REPAIR_OK, expired: false } });
    check(mixed.c.n === 0 && rMixed.calls.length === 2,
      'a mixed turn (one deterministic + one repaired) keeps BOTH calls with one probe for the turn (got ' + rMixed.calls.length + ')');
    check(rMixed.calls.some((c) => c.name === 'write_file' && c.args.path === 'a.txt'),
      'the deterministic half of a mixed turn survives the early repair');
  }

  /* ==================== 7. the WIRING, driven end-to-end ====================
     Units 1-6 drive the pure seam directly, so they cannot catch a call-site that never
     calls it, calls it twice, or awaits it. These drive the real index.html through
     `launchApp` + a real agent turn, which is the only way to pin the wiring itself. */
  console.log('--- e2e 7: the index.html wiring, driven through a real agent turn ---');

  /* (a) MANY broken deltas across MANY tool calls in one turn ⇒ still exactly ONE /repair.
     This is the property the per-turn latch exists for, and it is invisible to a seam-only
     test: removing the latch from `cortexStreamTick` changes nothing here unless the pump
     really calls it once per delta. */
  {
    const manyDeltas = [];
    for (let i = 0; i < 6; i++) {
      manyDeltas.push(toolDeltaStream([{ id: 'bad' + i, name: 'raed_file', args: '{"path":"f' + i }]));
    }
    manyDeltas.push(toolDeltaStream([{ id: 'badX', name: 'raed_file', args: '{"path":"f9' }]));
    const app = await driveAgentTurn({
      localModels: LM,
      script: [manyDeltas, [contentStream('done')]],
      routes: { '/repair': { status: 200, json: REPAIR_OK }, '/ledger': { status: 200, json: { ok: true } } }
    });
    try {
      check(repairEvents(app).length === 1,
        'WIRING: many broken deltas across many tool calls still cost exactly ONE /repair (got '
          + repairEvents(app).length + ')');
    } finally { teardownApp(app); }
  }

  /* (b) The probe fires from PARTIAL deltas — before [DONE] — and the visible stream is not
     blocked by it. `gatedSseBody` pauses the reader, which is the only honest way to observe
     the mid-stream moment; nothing here sleeps in real time waiting for the model. */
  {
    const app = await launchApp({ routes: Object.assign({}, BASE_ROUTES, {
      '/v1/chat/completions': { status: 200, json: {}, body: null },
      '/repair': { status: 200, json: REPAIR_OK }, '/ledger': { status: 200, json: { ok: true } }
    }), seed: { 'cogitator.settings': JSON.stringify(Object.assign(baseSettings(), { localModels: LM })) } });
    try {
      await tick(60);
      app.window.TextDecoder = class { constructor() {} decode(v) { return v == null ? '' : String(v); } };
      /* Drive the pump directly with a stream that stops after the first broken delta. */
      const body = gatedSseBody([toolDeltaStream([{ id: 'b1', name: 'raed_file', args: '{"path":"m' }])], 1);
      const acc = { content: '', thinking: '', toolCalls: [], _fallback: 0 };
      const win = app.window;
      const turn = win.eval('cortexTurnBegin(CogCore.localModels.client("http://127.0.0.1:8932/", window.fetch), normLocalModels(settings.localModels))');
      let deltas = 0;
      const pumping = win.eval('pumpSSE')(body, acc, () => { deltas++; });
      /* Let the microtask queue turn over, WITHOUT releasing the stream. */
      await tick(60);
      const midRepair = localEvents(app, 'repair').length;
      check(midRepair === 1,
        'WIRING: the probe fires from PARTIAL deltas, before [DONE] (got ' + midRepair + ' mid-stream)');
      /* The FIRST delta is delivered before the reader parks, so `deltas` is 1 here — that is
         the point: the probe is already in flight while the pump is still waiting for more.
         What must NOT happen is the pump awaiting the probe, which would show up as the
         stream never reaching [DONE] until the probe resolved. */
      check(deltas === 1,
        'WIRING: the pump delivered the first delta and is now parked, probe in flight (got '
          + deltas + ' deltas)');
      body.release();
      await pumping;
      win.eval('cortexTurnEnd')();
      /* Exactly one delta exists: the trailing `[DONE]` sentinel is skipped by the pump
         before `onDelta`, so 1 is the complete total, not a truncated one. */
      check(deltas === 1, 'WIRING: the stream completed through [DONE] after the release (got '
        + deltas + ' deltas)');
      check(localEvents(app, 'repair').length === 1,
        'WIRING: still exactly one /repair after the stream completed (got '
          + localEvents(app, 'repair').length + ')');
    } finally { teardownApp(app); }
  }

  /* (c) Probes are opt-in through the WIRING: `enabled:false` means the pump ticks and the
     turn settles with zero requests — the byte-identical no-cortex turn. */
  {
    const app = await driveAgentTurn({
      localModels: LM_OFF,
      script: [[toolDeltaStream([{ id: 'b1', name: 'raed_file', args: '{"path":"m' }])], [contentStream('ok')]],
      routes: { '/ledger': { status: 200, json: { ok: true } } }
    });
    try {
      check(repairEvents(app).length === 0 && ev8932(app).length === 0,
        'WIRING: localModels.enabled:false ⇒ ZERO requests to :8932 through the whole turn (got '
          + ev8932(app).length + ')');
      check(!/LOCAL CORTEX/.test(stableTranscript(app)),
        'WIRING: a cortex-off turn renders no cortex note at all');
    } finally { teardownApp(app); }
  }

  /* (d) The early probe is what the turn actually uses: the sidecar is asked ONCE and the
     repaired rite is dispatched, proving the folded result reached `m.toolCalls`. */
  {
    const app = await driveAgentTurn({
      localModels: LM,
      script: [[toolDeltaStream([{ id: 'b1', name: 'raed_file', args: '{"path":"main.py"}' }])],
        [contentStream('Read it, operator.')]],
      routes: { '/repair': { status: 200, json: REPAIR_OK }, '/ledger': { status: 200, json: { ok: true } } }
    });
    try {
      const dispatched = modelDispatches(app);
      check(repairEvents(app).length === 1,
        'WIRING: a broken streamed call costs exactly one /repair end-to-end (got '
          + repairEvents(app).length + ')');
      check(dispatched.length === 1 && dispatched[0].name === 'read_file'
        && dispatched[0].arguments && dispatched[0].arguments.path === 'main.py',
        'WIRING: the repaired rite is what actually reached the bridge (got '
          + JSON.stringify(dispatched.map((d) => d.name)) + ')');
      check(ledgerActions(app).indexOf('accepted') !== -1,
        'WIRING: the folded repair is ledgered as accepted');
      check(!chatPosts(app).some((e) => String(e.body || '').indexOf('LOCAL CORTEX') !== -1),
        'WIRING: the cortex note never leaks into an outgoing chat payload');
    } finally { teardownApp(app); }
  }

  /* (e) A HANGING sidecar must not hang the turn. The route never answers; the bounded
     `needle.timeoutMs` has to end it and the original rite must still pass through. */
  {
    const app = await driveAgentTurn({
      localModels: lmCfg({ needle: { enabled: true, minConfidence: 0.75, confirmBand: [0.5, 0.75], timeoutMs: 60 } }),
      script: [[toolDeltaStream([{ id: 'b1', name: 'raed_file', args: '{"path":"main.py"}' }])],
        [contentStream('Recovered.')]],
      tickMs: 900,
      /* A never-resolving route IS the hang: the fetch stub returns a promise that never
         settles, so only the bounded `needle.timeoutMs` can end this turn. (A route FUNCTION
         is the harness's escape hatch — a plain object always resolves.) */
      routes: { '/repair': () => new Promise(() => {}), '/ledger': { status: 200, json: { ok: true } } }
    });
    try {
      check(app.bootErrors.length === 0,
        'WIRING: a hanging /repair produced no uncaught error and the turn completed');
      check(ledgerActions(app).indexOf('timeout') !== -1,
        'WIRING: the bounded wait is ledgered with the existing "timeout" action (got '
          + JSON.stringify(ledgerActions(app)) + ')');
      check(!modelDispatches(app).some((d) => d.name === 'raed_file'),
        'WIRING: the unrepaired name never reached the bridge on the timeout path');
    } finally { teardownApp(app); }
  }

  for (const a of ALL_APPS) { try { teardownApp(a); } catch (e) { /* already torn down */ } }

  process.exit(summary('PHASE 15 INCREMENTAL DETECTION'));
})().catch((e) => { console.error(e); process.exit(1); });
