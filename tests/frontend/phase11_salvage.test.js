'use strict';
/*
 * PHASE 11 E2E — deterministic salvage pass (`CogCore.salvageToolCalls`) + golden corpus.
 *
 * Contract under test (plan §5 Phase 11, plan §4):
 *  - `salvageToolCalls(reply, allowedTools)` is PURE (no network/fs/DOM/clock), never throws,
 *    accepts every reply shape the harness actually produces (OpenAI `tool_calls`, an OpenAI
 *    assistant message, the finalized `{toolCalls:[…]}` shape, Ollama `{message:{tool_calls}}`,
 *    the buffered `chat.end` aggregate, a bare call object, or plain prose), and repairs
 *    FORMAT ONLY: fences, trailing commas, string/double-encoded arguments, truncated JSON
 *    (close-balance, only when the closed text parses), near-miss tool names, and narrations.
 *  - It NEVER invents an argument value, never fills a required field, and never guesses
 *    between two equally-close tool names.
 *  - Measured on the golden corpus: deterministic fix rate >= 80% of the repairable cases,
 *    FALSE-REPAIR RATE EXACTLY 0, ambiguous names never guessed.
 *  - END-TO-END through the real agent loop: a fenced + near-miss stream reaches the bridge
 *    with the canonical tool name and object arguments; a narrated call is dispatched; a clean
 *    legitimate turn is still dispatched exactly once (no happy-path regression); an
 *    unrepairable call still never reaches the bridge (the Phase 7 gate is untouched).
 */
const path = require('path');
const fs = require('fs');
const { check, summary, clearFails, launchApp, teardownApp } = require('./helpers');
const CogCore = require(path.join(__dirname, '..', '..', 'appcore.js'));

const CORPUS = JSON.parse(
  fs.readFileSync(path.join(__dirname, '..', 'fixtures', 'toolcall-corpus', 'cases.json'), 'utf8'));

const tick = (ms = 30) => new Promise((r) => setTimeout(r, ms));
const BRIDGE = 'http://127.0.0.1:8931/tools/execute';

/* Stable deep-equality by canonical serialisation (key order must not matter). */
function canon(v) {
  if (Array.isArray(v)) return '[' + v.map(canon).join(',') + ']';
  if (v && typeof v === 'object') {
    return '{' + Object.keys(v).sort().map((k) => JSON.stringify(k) + ':' + canon(v[k])).join(',') + '}';
  }
  return JSON.stringify(v === undefined ? null : v);
}

/* ---- SSE helpers (same shape the Phase 7/8 suites use) ---- */
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
const SEED = {
  endpoint: 'http://x', model: 'test-model', backend: 'openai', system: '',
  workdir: '/proj/omega', agent: true, autoBridge: false, autoApproveRead: true,
  profiles: [], activeProfile: ''
};
const bridgeBodies = (app) => app.events
  .filter((e) => e.url === BRIDGE || e.url.endsWith('/tools/execute'))
  .map((e) => { try { return JSON.parse(e.body || '{}'); } catch (err) { return {}; } });
const chatPosts = (app) => app.events.filter((e) => e.method === 'POST' && e.url.includes('chat/completions'));

/*
 * LANDMINE: `buildMessages()` calls `getWorkdirListing()`, which POSTs
 * `{name:'list_dir',arguments:{path:'.'}}` to the SAME /tools/execute route on EVERY agent
 * iteration. So a raw count of bridge POSTs is not a count of model dispatches — a suite that
 * counts them will pass for the wrong reason (and did, on the first run of this file).
 * Everything below asserts on `modelDispatches`, which drops that exact workdir-listing body.
 */
const WORKDIR_LISTING = canon({ name: 'list_dir', arguments: { path: '.' } });
const modelDispatches = (app) => bridgeBodies(app)
  .filter((b) => canon({ name: b.name, arguments: b.arguments }) !== WORKDIR_LISTING);

/* The role:'tool' results fed back into the model loop, per model call (index 1 = after the 1st). */
const fedToolResults = (app, postIndex) => {
  const posts = chatPosts(app);
  if (posts.length <= postIndex) return [];
  return (JSON.parse(posts[postIndex].body || '{}').messages || []).filter((m) => m.role === 'tool');
};

const approvalModalOpen = (app) => {
  const el = app.document.getElementById('agent-modal');
  return !!(el && el.classList.contains('open'));
};
const assistantWithSuspects = (app) => {
  const c = app.window.active();
  return ((c && c.messages) || []).filter((m) => m && m.cortexSuspects);
};

async function runAgentTurn(script, text = 'inspect the workdir') {
  const routes = Object.assign({}, BASE_ROUTES, { '/v1/chat/completions': chatRoute(script) });
  const app = await launchApp({ routes, seed: { 'cogitator.settings': JSON.stringify(SEED) } });
  await tick(60);
  app.window.TextDecoder = class { constructor() {} decode(v) { return v == null ? '' : String(v); } };
  const $a = (id) => app.document.getElementById(id);
  $a('ta').value = text;
  $a('send-btn').click();
  await tick(240);
  return app;
}

(async () => {
  clearFails();

  /* ============ live schemas (single source of truth) ============ */
  const boot = await launchApp({ routes: BASE_ROUTES });
  const LIVE_TOOLS = JSON.parse(String(boot.window.eval('JSON.stringify(TOOL_SCHEMAS)')));
  teardownApp(boot);
  const LIVE_NAMES = LIVE_TOOLS.map((t) => t.function.name).sort();
  check(canon(CORPUS.tools.slice().sort()) === canon(LIVE_NAMES),
    'corpus default tool list matches the live TOOL_SCHEMAS names (' + LIVE_NAMES.join(', ') + ')');

  check(typeof CogCore.salvageToolCalls === 'function', 'CogCore.salvageToolCalls exists');

  if (typeof CogCore.salvageToolCalls === 'function') {
    /* ==================== A. the golden corpus ==================== */
    console.log('--- corpus: ' + CORPUS.cases.length + ' cases ---');
    let repairable = 0, fixed = 0, falseRepairs = 0, ambiguousGuessed = 0, unrepairableCases = 0;
    const unrepairableSeen = [];
    const failedIds = [];

    for (const cse of CORPUS.cases) {
      const tools = cse.tools || CORPUS.tools;
      const exp = cse.expect || {};
      let out = null, threw = null;
      try { out = CogCore.salvageToolCalls(cse.reply, tools); } catch (e) { threw = e; }
      const idTag = '[' + cse.id + ']';
      if (threw || !out || !Array.isArray(out.calls) || !Array.isArray(out.changed) || !Array.isArray(out.unrepairable)) {
        check(false, idTag + ' salvage returned the documented shape and never threw (' +
          (threw ? threw.message : JSON.stringify(out)) + ')');
        failedIds.push(cse.id);
        continue;
      }
      check(out.source === 'deterministic', idTag + ' source is "deterministic"');

      let bad = [];
      if (exp.never) {
        for (const forbidden of exp.never) {
          if (out.calls.some((c) => c.name === forbidden)) {
            ambiguousGuessed++;
            bad.push('guessed the forbidden name "' + forbidden + '"');
          }
        }
      }
      if (exp.ambiguous) {
        if (!out.unrepairable.some((u) => /ambiguous/i.test(String(u.reason || '')))) {
          bad.push('did not report the ambiguity in unrepairable[].reason');
        }
      }
      if (exp.unrepairable) {
        unrepairableCases++;
        unrepairableSeen.push(cse.id);
        /* In a mixed turn (expect.callCount set) the turn still has a dispatchable sibling. */
        if (exp.callCount === undefined && out.calls.length !== 0) {
          bad.push('expected no dispatchable call, got ' + out.calls.length);
        }
        if (out.unrepairable.length === 0) bad.push('expected an unrepairable entry, got none');
      }
      if (exp.name) {
        repairable++;
        const got = out.calls.find((c) => c.name === exp.name);
        if (!got) {
          bad.push('expected a call named "' + exp.name + '"');
        } else {
          if (exp.args !== undefined && canon(got.args) !== canon(exp.args)) {
            bad.push('args mismatch: expected ' + canon(exp.args) + ' got ' + canon(got.args));
          }
          if (!bad.length) fixed++;
        }
      }
      if (exp.callCount !== undefined && out.calls.length !== exp.callCount) {
        bad.push('expected ' + exp.callCount + ' dispatchable call(s), got ' + out.calls.length);
      }
      if (exp.unchanged) {
        /* the false-repair guard: nothing may have been repaired at all */
        if (out.changed.length !== 0) {
          falseRepairs++;
          bad.push('FALSE REPAIR: changed=' + JSON.stringify(out.changed));
        }
        if (!exp.name && out.calls.length !== 0) {
          falseRepairs++;
          bad.push('FALSE REPAIR: manufactured ' + out.calls.length + ' call(s) from a non-call reply');
        }
      }
      if (exp.validatorOk !== undefined) {
        const v = CogCore.validateStructuredOutput(
          out.calls.map((c) => ({ id: c.id || 'call_x', name: c.name, args: c.args })), LIVE_TOOLS);
        if (v.ok !== exp.validatorOk) {
          bad.push('validator.ok expected ' + exp.validatorOk + ', got ' + v.ok +
            ' (' + JSON.stringify(v.errors) + ')');
        }
      }
      if (bad.length) {
        failedIds.push(cse.id);
        check(false, idTag + cse.category + ': ' + bad.join(' | '));
      }
    }

    const fixRate = repairable ? fixed / repairable : 0;
    console.log('--- corpus metrics: cases=' + CORPUS.cases.length +
      ' repairable=' + repairable + ' fixed=' + fixed +
      ' fixRate=' + (fixRate * 100).toFixed(1) + '%' +
      ' unrepairable=' + unrepairableCases + ' falseRepairs=' + falseRepairs +
      ' ambiguousGuessed=' + ambiguousGuessed + ' ---');
    check(repairable >= 30, 'corpus declares enough repairable cases to measure (' + repairable + ')');
    check(fixRate >= 0.80, 'deterministic fix rate >= 80% of repairable cases (got ' +
      (fixRate * 100).toFixed(1) + '%)');
    check(falseRepairs === 0, 'FALSE-REPAIR RATE IS EXACTLY 0 (got ' + falseRepairs + ')');
    check(ambiguousGuessed === 0, 'ambiguous / forbidden names were never guessed (got ' + ambiguousGuessed + ')');
    if (failedIds.length) console.log('failed case ids: ' + failedIds.join(', '));

    /* ==================== B. purity / robustness ==================== */
    console.log('--- purity + robustness ---');
    /* SEAM CHECK: index.html inlines appcore.js, so the browser realm holds its OWN copy of
       CogCore. The corpus above drove the Node `require`d module — assert the shipped inline
       copy is byte-for-byte the same implementation, so a stale/duplicated file cannot pass. */
    const realm = await launchApp({ routes: BASE_ROUTES });
    const shipped = realm.window.CogCore;
    let parity = 0, parityBad = [];
    for (const cse of CORPUS.cases) {
      const tools = cse.tools || CORPUS.tools;
      const a = canon(CogCore.salvageToolCalls(cse.reply, tools));
      let b;
      try { b = canon(shipped.salvageToolCalls(JSON.parse(JSON.stringify(cse.reply)), tools)); }
      catch (e) { b = 'THREW:' + e.message; }
      if (a === b) parity++; else parityBad.push(cse.id);
    }
    teardownApp(realm);
    check(parity === CORPUS.cases.length,
      'the SHIPPED inline CogCore (window.CogCore) matches the module on all ' + CORPUS.cases.length +
      ' corpus cases (mismatches: ' + (parityBad.join(', ') || 'none') + ')');
    check(typeof shipped.salvageToolCalls === 'function', 'window.CogCore.salvageToolCalls is the shipped door');

    const junk = [null, undefined, 0, 'nope', {}, { tool_calls: 'x' }, [], { message: null },
      { result: {} }, { content: 42 }, NaN, true, [{}, null, 'x']];
    let allOk = true, threwAny = false;
    for (const j of junk) {
      try {
        const o = CogCore.salvageToolCalls(j, LIVE_TOOLS);
        allOk = allOk && o && Array.isArray(o.calls) && Array.isArray(o.changed) && Array.isArray(o.unrepairable);
      } catch (e) { threwAny = true; }
    }
    check(!threwAny, 'garbage input never throws');
    check(allOk, 'garbage input always returns the documented shape');
    const noTools = CogCore.salvageToolCalls([{ id: 'c', name: 'whatever', args: '{"a":1}' }], []);
    check(noTools.calls.length === 1 && noTools.calls[0].name === 'whatever',
      'an empty allow-list means "no name filtering" (the validator keeps that job)');
    const twice = CogCore.salvageToolCalls([{ id: 'c', name: 'read-file', args: '{"path":"a"}' }], LIVE_TOOLS);
    const twiceAgain = CogCore.salvageToolCalls(twice.calls, LIVE_TOOLS);
    check(twiceAgain.changed.length === 0 && canon(twiceAgain.calls) === canon(twice.calls),
      'salvage is idempotent: repairing an already-repaired call changes nothing');
  }

  /* ==================== C. driven e2e: fenced + near-miss ==================== */
  console.log('--- e2e A: fenced + near-miss stream reaches the bridge canonically ---');
  let app = await runAgentTurn([
    [toolDeltaStream([{ id: 'call_f1', name: 'read-file', args: '```json\n{"path":"main.py"}\n```' }])],
    [contentStream('Read it, operator.')]
  ]);
  try {
    const dispatched = modelDispatches(app);
    check(dispatched.length === 1, 'exactly one model rite was dispatched (got ' + dispatched.length +
      ': ' + JSON.stringify(dispatched) + ')');
    check(dispatched[0] && dispatched[0].name === 'read_file',
      'the bridge received the CANONICAL name read_file (got ' + JSON.stringify(dispatched[0] && dispatched[0].name) + ')');
    check(dispatched[0] && dispatched[0].arguments && dispatched[0].arguments.path === 'main.py',
      'the bridge received OBJECT arguments {path:main.py} (got ' + JSON.stringify(dispatched[0] && dispatched[0].arguments) + ')');
    check(fedToolResults(app, 1).some((m) => m.name === 'read_file'),
      'the repaired call was fed back to the model as a role:"tool" result for read_file');
    check(app.bootErrors.length === 0, 'no uncaught boot errors in the fenced+near-miss turn');
  } finally { teardownApp(app); }

  /* ==================== D. driven e2e: narrated call ==================== */
  console.log('--- e2e B: a narrated call (no tool_calls at all) is recovered + dispatched ---');
  app = await runAgentTurn([
    [contentStream('Let me check the notes: read_file({"path": "notes.md"})')],
    [contentStream('Read it.')]
  ]);
  try {
    const dispatched = modelDispatches(app).filter((b) => b.name === 'read_file');
    check(dispatched.length === 1,
      'the narrated call was recovered and dispatched exactly once (got ' + dispatched.length + ')');
    check(dispatched.length === 1 && dispatched[0].arguments && dispatched[0].arguments.path === 'notes.md',
      'it reached the bridge as read_file {path:notes.md} (got ' + JSON.stringify(dispatched) + ')');
    check(fedToolResults(app, 1).some((m) => m.name === 'read_file'),
      'the recovered call produced a real role:"tool" result in the model loop');
    check(app.bootErrors.length === 0, 'no uncaught boot errors in the narrated turn');
  } finally { teardownApp(app); }

  /* ==================== E. driven e2e: happy path is unchanged ==================== */
  console.log('--- e2e C: a clean legitimate turn is still dispatched exactly once ---');
  app = await runAgentTurn([
    [toolDeltaStream([{ id: 'call_ok', name: 'read_file', args: '{"path":"main.py"}' }])],
    [contentStream('Done.')]
  ]);
  try {
    const dispatched = modelDispatches(app).filter((b) => b.name === 'read_file');
    check(dispatched.length === 1, 'PROOF: the clean call was dispatched exactly once (got ' + dispatched.length + ')');
    check(dispatched.length === 1 && dispatched[0].arguments && dispatched[0].arguments.path === 'main.py',
      'clean args still reach the bridge parsed (path=main.py)');
    check(app.bootErrors.length === 0, 'no uncaught boot errors in the clean turn');
  } finally { teardownApp(app); }

  /* ==================== F. driven e2e: the Phase 7 gate is untouched ==================== */
  console.log('--- e2e D: an unrepairable call still never reaches the bridge ---');
  app = await runAgentTurn([
    [toolDeltaStream([
      { id: 'call_bad_name', name: 'evil_tool', args: '{}' },
      { id: 'call_bad_args', name: 'read_file', args: '{oops' }
    ])],
    [contentStream('Understood; answering in prose.')]
  ]);
  try {
    const dispatched = modelDispatches(app);
    check(dispatched.length === 0,
      'PROOF: no unrepairable rite was dispatched at all (got ' + JSON.stringify(dispatched) + ')');
    check(!bridgeBodies(app).some((b) => b.name === 'evil_tool'), 'PROOF: an unknown rite was never dispatched');
    check(!approvalModalOpen(app), 'PROOF: neither ever reached the approval/dispatch stage');
    const fed = fedToolResults(app, 1);
    check(fed.length >= 2, 'both rejections were fed back as role:"tool" corrections (got ' + fed.length + ')');
    check(fed.some((m) => /reject/i.test(String(m.content || '')) && /evil_tool/.test(String(m.content || ''))),
      'the fed-back correction names the rejected rite');
    const suspects = assistantWithSuspects(app);
    check(suspects.length >= 1, 'the unrepairable calls were recorded as cortexSuspects for the ML stage');
    check(suspects.length >= 1 && suspects[0].cortexSuspects.length === 2,
      'both unrepairable calls are listed as suspects (got ' +
      JSON.stringify(suspects.length ? suspects[0].cortexSuspects.map((s) => s.reason) : null) + ')');
    check(app.bootErrors.length === 0, 'no uncaught boot errors in the rejection path');
  } finally { teardownApp(app); }

  process.exit(summary('PHASE 11 DETERMINISTIC SALVAGE + GOLDEN CORPUS'));
})().catch((e) => { console.error(e); process.exit(1); });
