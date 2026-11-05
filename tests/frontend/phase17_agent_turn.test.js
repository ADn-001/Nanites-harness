'use strict';
/*
 * PHASE 17 E2E — agent-turn stream integrity.
 *
 * Tickets: CR-Nanites-harness-0011 (critical), 0012 (critical), 0013 (high),
 *          0014 (high), 0015 (high).
 *
 * Each of these five defects shipped GREEN because the suite had no assertion
 * that could see them. `tool_call_id` had ZERO occurrences across tests/frontend
 * before this file, which is exactly why 0012 shipped: nothing ever asserted
 * that an outbound request was protocol-legal.
 *
 *  0011 CRITICAL — the SSE pump and refreshLast bind `active().messages[length-1]`
 *        instead of the message the turn created. A tool message appended
 *        mid-stream makes `length-1` point at the TOOL message, so the delta
 *        renders into the wrong bubble and the assistant message stays empty.
 *  0012 CRITICAL — dispatcher results are pushed as role:'tool' with a
 *        toolCallId that NO assistant message declares. A strict
 *        OpenAI-compatible server rejects the whole request.
 *  0013 HIGH — halt() settles approvalResolve but not proposalResolve, so a
 *        proposal card open at STOP time hangs forever and `generating` sticks.
 *  0014 HIGH — stream() teardown is not in a `finally`, so anything thrown
 *        INSIDE the catch leaves the app permanently busy.
 *  0015 HIGH — mergeToolDelta compacts on every delta, so out-of-order
 *        fragments (index 1 before index 0) merge into ONE corrupt call.
 *
 * Harness copied from phase14_dispatcher.test.js — see the Phase 11/12/13
 * findings for the landmines modelDispatches and stableTranscript exist for.
 */
const { check, summary, clearFails, launchApp, teardownApp } = require('./helpers');

const tick = (ms = 30) => new Promise((r) => setTimeout(r, ms));
const BRIDGE = 'http://127.0.0.1:8931/tools/execute';

function canon(v) {
  if (Array.isArray(v)) return '[' + v.map(canon).join(',') + ']';
  if (v && typeof v === 'object') {
    return '{' + Object.keys(v).sort().map((k) => JSON.stringify(k) + ':' + canon(v[k])).join(',') + '}';
  }
  return JSON.stringify(v === undefined ? null : v);
}

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
    return [{ choices: [{ delta: { tool_calls: calls } }] }];
  }
function contentStream(text) { return [{ choices: [{ delta: { content: text } }] }]; }
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
  '/tools/execute': { status: 200, json: { ok: true, result: 'file contents here' } },
  '/api/v0/models': { status: 200, json: { object: 'list', data: [] } }
};
function baseSettings() {
  return {
    endpoint: 'http://x', model: 'test-model', backend: 'openai', system: '',
    workdir: '', agent: true, autoBridge: false, autoApproveRead: true,
    profiles: [], activeProfile: '', apiKey: 'SENTINEL',
    ctxLimit: 8000,
    localModels: { enabled: false }
  };
}

const bridgeBodies = (app) => app.events
  .filter((e) => e.url === BRIDGE || String(e.url).endsWith('/tools/execute'))
  .map((e) => { try { return JSON.parse(e.body || '{}'); } catch (err) { return {}; } });
const WORKDIR_LISTING = canon({ name: 'list_dir', arguments: { path: '.' } });
/* LANDMINE (Phase 11/12/13): buildMessages POSTs the workdir listing to the SAME
   bridge route every iteration, so raw bridge POSTs over-count. */
const modelDispatches = (app) => bridgeBodies(app)
  .filter((b) => canon({ name: b.name, arguments: b.arguments }) !== WORKDIR_LISTING);
const chatPosts = (app) => app.events.filter((e) => e.method === 'POST' && String(e.url).includes('chat/completions'));
const chatBodies = (app) => chatPosts(app).map((e) => { try { return JSON.parse(e.body || '{}'); } catch (x) { return {}; } });
const $ = (app, id) => app.document.getElementById(id);
const ALL_APPS = [];

async function boot(opts) {
  opts = opts || {};
  const routes = Object.assign({}, BASE_ROUTES, opts.routes || {});
  routes['/v1/chat/completions'] = chatRoute(opts.script || [contentStream('ok')]);
  const settings = Object.assign(baseSettings(), opts.settings || {});
  const app = await launchApp({ routes, seed: { 'cogitator.settings': JSON.stringify(settings) } });
  await tick(60);
  /* LANDMINE (Phase 13): TextDecoder is not wired to the fake body in jsdom. */
  app.window.TextDecoder = class { constructor() {} decode(v) { return v == null ? '' : String(v); } };
  ALL_APPS.push(app);
  return app;
}
function closeAll() { ALL_APPS.forEach(teardownApp); ALL_APPS.length = 0; }

/* ==================================================================
 * 0015 — mergeToolDelta must not compact inside the delta loop.
 *
 * The mutation this kills: moving `acc.toolCalls=acc.toolCalls.filter(Boolean)`
 * out of the per-delta path and doing it once at hand-off instead. Before the
 * fix, feeding index 1 BEFORE index 0 collapsed two distinct calls into one
 * with concatenated arguments (verified by direct probe: grep's args and
 * read_file's args ended up concatenated on a single slot).
 *
 * The DISTINGUISHING INPUT is the whole point: with fragments delivered in
 * order, the compacting and non-compacting implementations agree exactly, so a
 * suite written only on in-order deltas pins nothing. This test deliberately
 * delivers them out of order, and also asserts the in-order case still works.
 * ================================================================== */
(async () => {
  clearFails();

  console.log('--- 0015: mergeToolDelta is order-independent and does not compact per delta ---');
  {
    const app = await boot({ script: [contentStream('x')] });
    const mergeToolDelta = app.window.eval('mergeToolDelta');

    const outOfOrder = app.window.eval('newAcc()');
    // index 1 FIRST, then index 0 — the case that corrupts under per-delta compaction.
    mergeToolDelta(outOfOrder, { index: 1, id: 'c2', function: { name: 'grep', arguments: '{"pattern":"b"}' } });
    mergeToolDelta(outOfOrder, { index: 0, id: 'c1', function: { name: 'read_file', arguments: '{"path":"a"}' } });

    check(outOfOrder.toolCalls.length === 2,
      '0015: two out-of-order fragments yield TWO tool calls, not one merged call (got ' +
      outOfOrder.toolCalls.length + ': ' + JSON.stringify(outOfOrder.toolCalls) + ')');

    const names = (outOfOrder.toolCalls || []).map((t) => t.name).sort();
    check(canon(names) === canon(['grep', 'read_file']),
      '0015: BOTH call names survive an out-of-order delivery (got ' + JSON.stringify(names) + ')');

    const byId = {};
    (outOfOrder.toolCalls || []).forEach((t) => { byId[t.id] = t; });
    check(byId.c1 && byId.c1.args === '{"path":"a"}',
      '0015: c1 (index 0) keeps exactly its own arguments, uncontaminated (got ' + JSON.stringify(byId.c1 && byId.c1.args) + ')');
    check(byId.c2 && byId.c2.args === '{"pattern":"b"}',
      '0015: c2 (index 1) keeps exactly its own arguments, uncontaminated (got ' + JSON.stringify(byId.c2 && byId.c2.args) + ')');

    // In-order must still be correct (the fix must not regress the common path).
    const inOrder = app.window.eval('newAcc()');
    mergeToolDelta(inOrder, { index: 0, id: 'd1', function: { name: 'read_file', arguments: '{"path":"x"}' } });
    mergeToolDelta(inOrder, { index: 1, id: 'd2', function: { name: 'grep', arguments: '{"pattern":"y"}' } });
    check(inOrder.toolCalls.length === 2, '0015: in-order fragments still yield two calls (got ' + inOrder.toolCalls.length + ')');
    check((inOrder.toolCalls[0].name === 'read_file' && inOrder.toolCalls[0].args === '{"path":"x"}') &&
          (inOrder.toolCalls[1].name === 'grep' && inOrder.toolCalls[1].args === '{"pattern":"y"}'),
      '0015: in-order fragments keep their own slots and arguments');

    // Index-less fragments (some servers omit index entirely) must still accumulate.
    const noIndex = app.window.eval('newAcc()');
    mergeToolDelta(noIndex, { id: 'n1', function: { name: 'read_file', arguments: '{"path":' } });
    mergeToolDelta(noIndex, { function: { arguments: '"a"}' } });
    check(noIndex.toolCalls.length === 1 && noIndex.toolCalls[0].args === '{"path":"a"}',
      '0015: index-less fragments accumulate onto ONE call (got ' + JSON.stringify(noIndex.toolCalls) + ')');

    teardownApp(app); ALL_APPS.splice(ALL_APPS.indexOf(app), 1);
  }

  /* ==================================================================
   * 0012 — every outbound request must be protocol-legal: every role:'tool'
   * message is preceded by an assistant declaring that tool_call_id.
   *
   * This is the assertion whose total absence let 0012 ship. It walks the REAL
   * outbound body of every chat/completions request, in order.
   *
   * TWO dispatch paths, because the dispatcher has two and they push tool results by
   * different code (`auto` vs `card`). Mutation testing found the ACCEPTED path was
   * untested — dropping its declaration left the suite green (M6b escaped). The
   * distinguishing input is the operator decision: `autoRun` comes from the sidecar's
   * confidence crossed with `autoApproveRead`, so `autoReadOnly:false` forces EVERY
   * proposal onto the card, and accepting it is what exercises the second push site.
   * ================================================================== */
  async function driveDispatcher(label, opts) {
    opts = opts || {};
    const lm = {
      enabled: true, port: 8932,
      needle: { enabled: false, minConfidence: 0.75, confirmBand: [0.5, 0.75], timeoutMs: 800 },
      laya: { enabled: false, minConfidence: 0.70, timeoutMs: 500, preflight: true, anomaly: true },
      sanitizer: { enabled: true, mode: 'auto', deterministicPass: true },
      dispatcher: { enabled: true, autoReadOnly: opts.autoReadOnly !== false, timeoutMs: 800, minConfidence: 0.75 }
    };
    const app = await boot({
      settings: { workdir: '/proj/omega', localModels: lm },
      script: [contentStream('inspected'), contentStream('done reading')]
    });
    // The sidecar answers /select on 8932; the bridge answers /tools/execute on 8931.
    app.window.fetch = ((orig) => (input, init) => {
      const key = typeof input === 'string' ? input : String(input);
      const p = (() => { try { return new URL(key, 'http://localhost:8080').pathname; } catch (e) { return key; } })();
      if (p === '/select') {
        app.events.push({ url: key, method: (init && init.method) || 'GET', body: init && init.body ? String(init.body) : null, headers: {} });
        return Promise.resolve({
          ok: true, status: 200,
          json: async () => ({ ok: true, calls: [{ name: 'read_file', arguments: { path: 'main.py' } }], confidence: 0.91, latency_ms: 3, trace_id: 'tr-sel' })
        });
      }
      return orig(input, init);
    })(app.window.fetch);

    // Headless operator: accept the proposal card when (and only when) it appears.
    const carder = opts.accept ? setInterval(() => {
      try {
        const m = $(app, 'cortex-proposal-modal');
        const b = $(app, 'cortex-proposal-accept');
        if (m && m.classList.contains('open') && b) b.click();
      } catch (e) { /* ignore */ }
    }, 10) : null;

    $(app, 'ta').value = 'inspect the workdir';
    $(app, 'send-btn').click();
    await tick(1100);
    if (carder) clearInterval(carder);

    const bodies = chatBodies(app);
    check(bodies.length > 0, label + ': the turn actually dispatched model requests (got ' + bodies.length + ')');
    check(modelDispatches(app).length > 0,
      label + ': the pre-router dispatched a read rite through the bridge (got ' + modelDispatches(app).length + ' dispatches)');

    let illegal = [];
    bodies.forEach((b, bi) => {
      const msgs = b.messages || [];
      const declared = new Set();
      msgs.forEach((m) => {
        if (m && m.role === 'assistant' && Array.isArray(m.tool_calls)) {
          m.tool_calls.forEach((tc) => { if (tc && tc.id) declared.add(tc.id); });
        }
        if (m && m.role === 'tool') {
          const id = m.tool_call_id;
          if (!id) illegal.push('request#' + bi + ': tool message with EMPTY tool_call_id');
          else if (!declared.has(id)) illegal.push('request#' + bi + ': tool_call_id ' + id + ' declared by no preceding assistant');
        }
      });
    });
    check(illegal.length === 0,
      label + ': NO outbound request carries an undeclared tool_call_id' +
      (illegal.length ? ' — ' + illegal.slice(0, 4).join(' | ') : ''));

    teardownApp(app); ALL_APPS.splice(ALL_APPS.indexOf(app), 1);
  }

  await driveDispatcher('0012 auto-run path', { autoReadOnly: true });
  await driveDispatcher('0012 operator-accepted card path', { autoReadOnly: false, accept: true });

  /* ==================================================================
   * 0011 — the pump must bind the message the turn created, not
   * active().messages[length-1].
   *
   * The distinguishing scenario: a tool message appended MID-STREAM. Under the
   * old code `refreshLast()` renders into `messages[length-1]`, which by then is
   * the TOOL message — so the assistant bubble is left empty while the tool
   * bubble shows the assistant's prose. Assert on the MESSAGE OBJECTS, which is
   * the artefact that survives, not on the DOM alone.
   * ================================================================== */
  console.log('--- 0011: stream deltas land on the message the turn created ---');
  {
    /* A SLOW stream, so the append lands while the pump is genuinely mid-flight rather than
       after the turn finished (the first draft of this test raced and passed vacuously). */
    const slowBody = (events, gap) => {
      const chunks = events.map((e) => 'data: ' + JSON.stringify(e) + '\n\n');
      chunks.push('data: [DONE]\n\n');
      let i = 0;
      return {
        getReader() {
          return {
            read: async () => {
              if (i >= chunks.length) return { done: true, value: undefined };
              const v = chunks[i++];
              if (i < chunks.length) await tick(gap);
              return { done: false, value: v };
            }
          };
        }
      };
    };
    const app = await boot({ settings: { workdir: '' } });
    // Replace the chat route with a slow multi-chunk prose stream.
    const origRoutes = app.events;
    app.window.fetch = ((orig) => (input, init) => {
      const key = typeof input === 'string' ? input : String(input);
      let p = key;
      try { p = new URL(key, 'http://localhost:8080').pathname; } catch (e) { /* keep */ }
      if (p === '/v1/chat/completions') {
        origRoutes.push({ url: key, method: 'POST', body: init && init.body ? String(init.body) : null, headers: {} });
        return Promise.resolve({
          ok: true, status: 200, json: async () => ({}), text: async () => '',
          body: slowBody([
            { choices: [{ delta: { content: 'THE SPIRIT ' } }] },
            { choices: [{ delta: { content: 'SPOKE AT LAST. ' } }] },
            { choices: [{ delta: { content: 'THE LOG IS SEALED.' } }] }
          ], 120)
        });
      }
      return orig(input, init);
    })(app.window.fetch);

    $(app, 'ta').value = 'speak to me';
    $(app, 'send-btn').click();
    await tick(90);
    // Mid-stream: the turn appends a tool message while deltas are still arriving.
    const c = app.window.active();
    check(app.window.eval('generating') === true, '0011: the turn is still generating when the append happens');
    c.messages.push({ role: 'tool', name: 'read_file', toolCallId: 'k1', content: 'tool output', ts: Date.now() });
    await tick(700);

    check(typeof app.window.refreshLast === 'function', '0011: refreshLast is reachable from the harness');
    const assistants = c.messages.filter((m) => m.role === 'assistant');
    const lastAssistant = assistants.length ? assistants[assistants.length - 1] : null;
    check(!!lastAssistant, '0011: the turn left an assistant message');
    check(lastAssistant && String(lastAssistant.content).indexOf('THE SPIRIT') !== -1 &&
          String(lastAssistant.content).indexOf('THE LOG IS SEALED.') !== -1,
      '0011: the ASSISTANT message holds the streamed prose even though a tool message was appended mid-stream' +
      ' (got ' + JSON.stringify(lastAssistant && lastAssistant.content) + ')');

    // The negative direction: the appended TOOL message must NOT have been written into.
    const tools = c.messages.filter((m) => m.role === 'tool');
    check(tools.every((t) => String(t.content || '').indexOf('THE SPIRIT') === -1),
      '0011: the appended tool message was never used as the stream target' +
      ' (tool contents: ' + JSON.stringify(tools.map((t) => t.content)) + ')');

    /* The RENDERED half, which the message-object assertions above cannot see. Mutation
       testing caught this: reverting `refreshLast` to "always the last message", or making
       `refreshLastFor` resolve by last-index instead of identity, left every assertion above
       GREEN — the message objects were still correct while the DOM painted the wrong bubble.
       Assert the DOM the operator actually sees, per index. */
    const ai = c.messages.indexOf(lastAssistant);
    const ti = c.messages.indexOf(tools[tools.length - 1]);
    const bodyText = (i) => {
      const el = app.document.getElementById('body-' + i);
      return el ? String(el.textContent || '') : '';
    };
    const assistantDom = ai >= 0 ? bodyText(ai) : '';
    const toolDom = ti >= 0 ? bodyText(ti) : '';
    check(assistantDom.indexOf('THE SPIRIT') !== -1 && assistantDom.indexOf('THE LOG IS SEALED.') !== -1,
      '0011: the assistant BUBBLE renders the streamed prose (body-' + ai + ' = ' + JSON.stringify(assistantDom) + ')');
    check(toolDom.indexOf('THE SPIRIT') === -1,
      '0011: the tool bubble does NOT render the assistant prose (body-' + ti + ' = ' + JSON.stringify(toolDom) + ')');

    /* The IDENTITY-RESOLUTION SEAM, asserted directly.

       Two mutation rounds shaped this. First, M4b (refreshLast ignores the bound target) and
       M5 (refreshLastFor resolves by last-index) both stayed GREEN, because they only misrender
       DURING a stream — the `finally`'s final renderMessages() repaints everything correctly
       afterwards, hiding the defect in the end state. Second, an attempt to assert this through
       `refreshLast()` after the turn had finished was WRONG and had to be discarded: once the
       turn is over the bound target is released, so `refreshLast()` legitimately falls back to
       "the last message" (its original, correct behaviour). Worse, that version appeared to pass
       only because the baseline `renderMessages()` had already painted the new text — a green
       check over stale pixels, which is exactly the failure mode this whole exercise exists to
       catch. It is called out rather than quietly rewritten.

       So bind a target explicitly and drive the two functions that do the resolving. A BASELINE
       is painted first and the content changed AFTER it, so only `refreshLastFor` itself can
       move the new text. */
    const midRun = app.window.eval(`(function(){
      const c = active();
      const asst = c.messages.filter(function(m){ return m.role === 'assistant'; }).pop();
      const tool = c.messages[c.messages.length - 1];   // the tool message appended mid-turn
      asst.content = 'BASELINE_TEXT';
      renderMessages();                        // baseline paints BOTH bubbles correctly
      asst.content = 'MIDSTREAM_PROBE';        // change it AFTER the baseline
      const t = streamTargetBegin(c, asst);    // bind the target the way a live turn does
      refreshLast();
      const out = {
        ai: c.messages.indexOf(asst),
        asst: (document.getElementById('body-' + c.messages.indexOf(asst)) || {}).textContent || '',
        tool: (document.getElementById('body-' + (c.messages.length - 1)) || {}).textContent || ''
      };
      streamTargetEnd(t);
      return out;
    })()`);
    check(midRun && String(midRun.asst).indexOf('MIDSTREAM_PROBE') !== -1,
      '0011: with a tool message LAST, a bound refreshLast() repaints the ASSISTANT bubble (got ' +
      JSON.stringify(midRun && midRun.asst) + ')');
    check(midRun && String(midRun.tool).indexOf('MIDSTREAM_PROBE') === -1,
      '0011: with a tool message LAST, a bound refreshLast() does NOT paint into the tool bubble (got ' +
      JSON.stringify(midRun && midRun.tool) + ')');

    // And the resolver itself, on a message that is NOT last — the exact case M5 broke.
    const identity = app.window.eval(`(function(){
      const c = active();
      const asst = c.messages.filter(function(m){ return m.role === 'assistant'; }).pop();
      asst.content = 'IDENTITY_TEXT';
      renderMessages();
      asst.content = 'IDENTITY_AFTER';
      refreshLastFor(c, asst);                       // name the message EXPLICITLY
      const el = document.getElementById('body-' + c.messages.indexOf(asst));
      return el ? String(el.textContent || '') : '';
    })()`);
    check(String(identity).indexOf('IDENTITY_AFTER') !== -1,
      '0011: refreshLastFor(c, msg) repaints the NAMED message, not the last one (got ' + JSON.stringify(identity) + ')');

    // The explicit binding form the fix introduces must also work.
    const bound = app.window.eval('(typeof refreshLastFor==="function")');
    check(bound === true,
      '0011: a message-bound refresh helper exists (refreshLastFor) so callers can name the message they created');

    teardownApp(app); ALL_APPS.splice(ALL_APPS.indexOf(app), 1);
  }

  /* ==================================================================
   * 0013 + 0014 — teardown on EVERY exit, and STOP settles EVERY resolver.
   * ================================================================== */
  console.log('--- 0013/0014: STOP clears generating from every modal state; teardown survives a throwing catch ---');
  {
    const app = await boot({ script: [contentStream('hello')] });

    const busy = () => app.window.eval('generating');
    const sendDisabled = () => {
      const b = $(app, 'send-btn');
      return !!(b && b.disabled);
    };

    // --- 0014: a throwing `catch` must not strand the app ---
    // Two things must line up for the catch body to run at all: the model call must FAIL, and
    // the first refreshLast call after that failure must throw. (The first draft injected only
    // the throw, so the catch never ran and every assertion below passed VACUOUSLY.)
    const unhandled = [];
    const onUnhandled = (e) => { unhandled.push(String((e && e.message) || e)); };
    process.on('unhandledRejection', onUnhandled);
    const armed = app.window.eval(`(function(){
      let armed = false;
      const realRefresh = window.refreshLast;
      window.refreshLast = function(){
        if (armed) { armed = false; throw new Error('injected catch-body fault'); }
        return realRefresh.apply(this, arguments);
      };
      window.__armCatchFault = function(){ armed = true; };
      window.__restoreRefresh = function(){ window.refreshLast = realRefresh; };
      return true;
    })()`);
    check(armed === true, '0014: armed a throwing refreshLast for the catch body');
    // Make the model call itself fail, so the catch block is genuinely entered.
    app.window.fetch = ((orig) => (input, init) => {
      const key = typeof input === 'string' ? input : String(input);
      let p = key;
      try { p = new URL(key, 'http://localhost:8080').pathname; } catch (e) { /* keep */ }
      if (p === '/v1/chat/completions') {
        app.window.__armCatchFault();
        return Promise.reject(new TypeError('NetworkError: induced endpoint failure'));
      }
      return orig(input, init);
    })(app.window.fetch);

    $(app, 'ta').value = 'hello there';
    $(app, 'send-btn').click();
    await tick(700);

    const caughtRan = (function () {
      try {
        return app.window.active().messages.some(
          (m) => String(m.content || '').indexOf('RITE FAILED') !== -1);
      } catch (e) { return false; }
    })();
    check(caughtRan === true,
      '0014: the model call failed and the catch body actually ran (the test is not vacuous)');

    check(busy() === false,
      '0014: generating is FALSE after a fault thrown INSIDE the catch body (got ' + busy() + ')');
    check(sendDisabled() === false,
      '0014: the send button is re-enabled after a fault inside the catch (got disabled=' + sendDisabled() + ')');
    check(unhandled.length === 0,
      '0014: the fault does not escape stream() as an unhandled rejection (got ' + JSON.stringify(unhandled) + ')');
    process.removeListener('unhandledRejection', onUnhandled);
    app.window.eval('window.__restoreRefresh && window.__restoreRefresh()');

    // --- 0013: STOP with a PROPOSAL CARD open must settle proposalResolve ---
    const cardOpen = () => {
      const m = $(app, 'cortex-proposal-modal');
      return !!(m && m.classList.contains('open'));
    };
    // PROVE the guard ran, rather than letting a `settled:false` result read as "no pending
    // resolver existed" — a resolver that silently vanished is exactly the 0013 defect.
    const settled = app.window.eval(`(function(){
      settleCortexProposal(false);          // clear any stale resolver first
      let done = false;
      proposalResolve = function(){ done = true; };   // arm a REAL pending resolver
      const armed = !!proposalResolve;                 // <-- the guard: it really was pending
      const modal = document.getElementById('cortex-proposal-modal');
      if (modal) modal.classList.add('open');
      halt();
      return { armed: armed, settled: done, stillOpen: modal ? modal.classList.contains('open') : false };
    })()`);
    check(settled && settled.armed === true,
      '0013: a proposal resolver really was pending before halt() (the test is not vacuous)');
    check(settled && settled.settled === true,
      '0013: halt() settles proposalResolve — a proposal card open at STOP does not hang forever');
    check(settled && settled.stillOpen === false,
      '0013: halt() closes the proposal modal as it settles it (got stillOpen=' + (settled && settled.stillOpen) + ')');

    // --- 0013: STOP with the RITE-APPROVAL MODAL open must settle approvalResolve ---
    /* The SECOND modal state. The gate reads "STOP from every modal state clears `generating`",
       and there are exactly two resolvers halt() owns: proposalResolve (above) and
       approvalResolve. Only the proposal one was asserted, which left the approval settle
       unpinned — a mutation that deletes `settleApproval(false)` from halt() left the whole
       suite GREEN. The pre-existing code did settle it, so this is a COVERAGE gap rather than a
       live defect, and that distinction matters: nothing was broken, but nothing was holding the
       line either, and the next edit to halt() could have dropped it silently.

       Drives the real modal (`#agent-modal`), the real resolver and the real halt(), and proves
       the resolver was genuinely pending BEFORE halt() — otherwise a `settled:false` would read
       as "nothing was armed" rather than "halt() skipped it".

       Mutation this test kills: `if(approvalResolve){try{settleApproval(false);}...}` removed
       from halt(). */
    const approvalOpen = () => {
      const m = $(app, 'agent-modal');
      return !!(m && m.classList.contains('open'));
    };
    const approvalSettled = app.window.eval(`(function(){
      settleApproval(false);                  // clear any stale resolver first
      let done = false;
      approvalResolve = function(){ done = true; };   // arm a REAL pending resolver
      const armed = !!approvalResolve;                // <-- the guard: it really was pending
      const modal = document.getElementById('agent-modal');
      if (modal) modal.classList.add('open');
      halt();
      return { armed: armed, settled: done, stillOpen: modal ? modal.classList.contains('open') : false };
    })()`);
    check(approvalSettled && approvalSettled.armed === true,
      '0013: an approval resolver really was pending before halt() (the test is not vacuous)');
    check(approvalSettled && approvalSettled.settled === true,
      '0013: halt() settles approvalResolve — a rite-approval modal open at STOP does not hang forever');
    check(approvalSettled && approvalSettled.stillOpen === false,
      '0013: halt() closes the approval modal as it settles it (got stillOpen=' + (approvalSettled && approvalSettled.stillOpen) + ')');
    check(approvalOpen() === false,
      '0013: the approval modal is not left on screen after STOP');

    // The negative direction: halt() with NOTHING pending must not throw.
    const idleHalt = app.window.eval('(function(){ try { halt(); return { ok: true }; } catch (e) { return { ok: false, err: String(e) }; } })()');
    check(idleHalt && idleHalt.ok === true,
      '0013: halt() with no pending resolver is a safe no-op (got ' + JSON.stringify(idleHalt) + ')');

    // --- destructive chat actions must refuse while generating ---
    const guardSrc = app.window.eval('[typeof clearActive, typeof deleteChat].join(",")');
    check(guardSrc === 'function,function', 'the destructive actions are reachable');
    // BEHAVIOURAL, not a source-shape grep: arm `generating` for real and drive both.
    const wipeGuard = app.window.eval(`(function(){
      const c = active();
      c.messages.push({ role: 'user', content: 'precious', ts: Date.now() });
      const realConfirm = window.confirm;
      window.confirm = function(){ return true; };        // operator says YES to the wipe
      generating = true;
      clearActive();
      const afterWipe = c.messages.length;
      generating = false;
      window.confirm = realConfirm;
      return { afterWipe: afterWipe };
    })()`);
    check(wipeGuard && wipeGuard.afterWipe > 0,
      '0013: clearActive REFUSES to wipe while generating — a mid-stream wipe destroys the turn (left ' +
      (wipeGuard && wipeGuard.afterWipe) + ' messages)');

    const purgeGuard = app.window.eval(`(function(){
      const id = active().id;
      const realConfirm = window.confirm;
      window.confirm = function(){ return true; };
      generating = true;
      deleteChat(id, { stopPropagation: function(){} });
      generating = false;
      window.confirm = realConfirm;
      return { stillThere: chats.some(function(x){ return x.id === id; }) };
    })()`);
    check(purgeGuard && purgeGuard.stillThere === true,
      '0013: deleteChat REFUSES to purge while generating — the active log survives the turn');

    // The structural backstop: stream() must tear down in a finally.
    const hasFinally = app.window.eval('/\\bfinally\\b/.test(stream.toString())');
    check(hasFinally === true, '0014: stream() performs teardown inside a finally');

    teardownApp(app); ALL_APPS.splice(ALL_APPS.indexOf(app), 1);
  }

  closeAll();
  process.exit(summary('PHASE 17: agent-turn stream integrity'));
})();
