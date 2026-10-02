'use strict';
/* PHASE 17 R1 — three defects the phase-17 suite could not see.
 *
 * Found by an independent review that drove the real app in jsdom rather than reading it.
 * Each was reproduced by execution before being written down here, and each is a case where
 * the suite was GREEN over a broken path:
 *
 *  R1-A (0011, critical) — the catch block still re-derived `messages[length-1]`, the exact
 *         line CR-0011 was filed to remove. A mid-turn FAILURE with a tool message already
 *         appended put "[ RITE FAILED ... ]" on the TOOL bubble and left the assistant empty.
 *         The existing 0011 tests only ever drove a SUCCESSFUL stream.
 *  R1-B (0014, high) — `generating=true` is set, then FOUR statements run, and only THEN does
 *         `try{` open. A throw in that window skips the finally entirely and leaves the app in
 *         exactly the permanently-busy state 0014 exists to eliminate.
 *  R1-C (0012, high) — CogCore's validator pushes `args` already PARSED (an object), and
 *         buildMessages emits `arguments: t.args` verbatim, so the synthesised assistant
 *         declared object-valued `function.arguments` where the OpenAI wire format requires a
 *         JSON-encoded string. The turn stayed protocol-illegal on a strict server.
 */
const { check, summary, launchApp, teardownApp } = require('./helpers');

const tick = (ms = 30) => new Promise((r) => setTimeout(r, ms));

function sseBody(events) {
  const chunks = events.map((e) => 'data: ' + JSON.stringify(e) + '\n\n');
  chunks.push('data: [DONE]\n\n');
  let i = 0;
  return { getReader() { return { read: async () => (i >= chunks.length
      ? { done: true, value: undefined }
      : { done: false, value: chunks[i++] }) }; } };
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
    profiles: [], activeProfile: '', apiKey: '',
    ctxLimit: 8000, localModels: { enabled: false }
  };
}
const $ = (app, id) => app.document.getElementById(id);

/* `launchApp` reads index.html itself and takes only {routes, seed} — the settings are seeded
   into the fake localStorage under 'cogitator.settings', exactly as phase17_agent_turn does.
   LANDMINE (Phase 13): TextDecoder is not wired to a fake body in jsdom, so it must be
   installed before any streaming call or the SSE pump reads nothing. */
async function boot(opts) {
  opts = opts || {};
  const routes = Object.assign({}, BASE_ROUTES, opts.routes || {});
  if (opts.onChat) routes['/v1/chat/completions'] = opts.onChat;
  const settings = Object.assign(baseSettings(), opts.settings || {});
  const app = await launchApp({ routes, seed: { 'cogitator.settings': JSON.stringify(settings) } });
  await tick(60);
  app.window.TextDecoder = class { constructor() {} decode(v) { return v == null ? '' : String(v); } };
  return app;
}

(async () => {
  /* ================= R1-A: the FAILURE path of 0011 ================= */
  console.log('--- R1-A: a mid-turn FAILURE reports onto the assistant message, not a tool message ---');
  {
    const app = await boot();
    const unhandled = [];
    const onUnhandled = (e) => { unhandled.push(String((e && e.message) || e)); };
    process.on('unhandledRejection', onUnhandled);

    /* The model call must NOT fail instantly: the tool append has to land while the turn is
       still in flight, which is the whole point — if the failure lands first there is no
       tool message to be misattributed to and the test passes for the wrong reason. (The
       first draft rejected immediately, so the catch ran BEFORE the append and every
       assertion passed vacuously.) Hence a delayed rejection. */
    app.window.fetch = ((orig) => (input, init) => {
      let key = typeof input === 'string' ? input : String(input);
      let p = key;
      try { p = new URL(key, 'http://localhost:8080').pathname; } catch (e) { /* keep */ }
      if (p === '/v1/chat/completions') {
        return new Promise((_, rej) => setTimeout(
          () => rej(new TypeError('NetworkError: induced failure')), 350));
      }
      return orig(input, init);
    })(app.window.fetch);

    $(app, 'ta').value = 'hello there';
    $(app, 'send-btn').click();
    await tick(120);

    // Reproduce the 0011 hazard: a TOOL message is appended AFTER the assistant message the
    // turn created, while the turn is still in flight — exactly what the pre-router/dispatcher
    // does mid-agent-turn.
    const seeded = app.window.eval(`(function(){
      const c = active();
      const asst = c.messages.filter(function(m){ return m.role === 'assistant'; }).pop();
      c.messages.push({ role: 'tool', name: 'read_file', toolCallId: 'call_x',
        content: 'TOOLOUTPUT', ts: Date.now() });
      return { asstIdx: c.messages.indexOf(asst), total: c.messages.length };
    })()`);

    // Now let the failure land.
    await tick(900);

    const landed = app.window.eval(`(function(){
      const c = active();
      const asst = c.messages.filter(function(m){ return m.role === 'assistant'; }).pop();
      const tools = c.messages.filter(function(m){ return m.role === 'tool'; });
      return {
        asst: String(asst && asst.content || ''),
        tool: String(tools.length ? tools[tools.length-1].content : ''),
        busy: generating
      };
    })()`);

    check(seeded && seeded.asstIdx !== -1,
      'R1-A: the assistant message the turn created was found before the tool append (not vacuous)');
    check(String(landed.asst).indexOf('RITE FAILED') !== -1,
      'R1-A: the failure notice lands on the ASSISTANT message (got ' + JSON.stringify(landed.asst.slice(0, 60)) + ')');
    check(String(landed.tool).indexOf('RITE FAILED') === -1,
      'R1-A: the TOOL message does NOT receive the assistant failure notice (got ' + JSON.stringify(landed.tool.slice(0, 60)) + ')');
    check(landed.busy === false, 'R1-A: the app is not left busy after the fault');
    check(unhandled.length === 0,
      'R1-A: the failure does not escape as an unhandled rejection (got ' + JSON.stringify(unhandled) + ')');

    process.removeListener('unhandledRejection', onUnhandled);
    teardownApp(app);
  }

  /* ================= R1-B: the pre-try window ================= */
  console.log('--- R1-B: a throw BEFORE the try block still tears the app down ---');
  {
    const app = await boot();
    const unhandled = [];
    const onUnhandled = (e) => { unhandled.push(String((e && e.message) || e)); };
    process.on('unhandledRejection', onUnhandled);

    /* `send()` calls renderMessages() too, BEFORE stream() even opens — a one-shot throw
       fires there and never reaches the window under test. So the throw is gated on
       `generating` being true, which is set inside stream() and is false throughout send():
       that is exactly the pre-try preamble and nothing else. Once it has fired, the flag
       flips so the same function can be called from inside the try block without throwing
       again (that path has its own coverage in phase17_agent_turn). */
    const armed = app.window.eval(`(function(){
      let fired = false;
      const real = window.renderMessages;
      window.renderMessages = function(){
        if (!fired && generating) { fired = true; throw new Error('injected pre-try fault'); }
        return real.apply(this, arguments);
      };
      window.__restoreRender = function(){ window.renderMessages = real; };
      return true;
    })()`);
    check(armed === true, 'R1-B: armed a throwing renderMessages for the pre-try preamble');

    $(app, 'ta').value = 'hello there';
    $(app, 'send-btn').click();
    await tick(700);

    const state = app.window.eval(`(function(){
      const b = document.getElementById('send-btn');
      const s = document.getElementById('stop-btn');
      return { busy: generating, sendDisabled: !!(b && b.disabled),
               sendShown: b ? b.style.display : null,
               stopShown: s ? s.style.display : null };
    })()`);
    check(state.busy === false,
      'R1-B: generating is FALSE after a fault in the PRE-try window (got ' + state.busy + ')');
    check(state.sendDisabled === false && state.sendShown !== 'none',
      'R1-B: the send button is restored after a pre-try fault (disabled=' + state.sendDisabled +
      ' display=' + state.sendShown + ')');
    check(state.stopShown === 'none',
      'R1-B: the stop button is hidden again after a pre-try fault (got ' + state.stopShown + ')');
    check(unhandled.length === 0,
      'R1-B: the pre-try fault does not escape as an unhandled rejection (got ' + JSON.stringify(unhandled) + ')');

    process.removeListener('unhandledRejection', onUnhandled);
    app.window.eval('window.__restoreRender && window.__restoreRender()');
    teardownApp(app);
  }

  /* ================= R1-C: function.arguments must be a STRING ================= */
  console.log('--- R1-C: a declared rite ships JSON-ENCODED string arguments ---');
  {
    /* The dispatcher is driven by the LOCAL-CORTEX sidecar's /select response, not by a
       model-authored tool_call — the pre-router proposes rites, and declareRites synthesises
       the assistant declaration from them. Reusing that mechanism (the same one
       phase17_agent_turn's driveDispatcher uses) is what makes this reach the code at all;
       a model-authored tool_call would take a different path entirely. */
    const lm = {
      enabled: true, port: 8932,
      needle: { enabled: false, minConfidence: 0.75, confirmBand: [0.5, 0.75], timeoutMs: 800 },
      laya: { enabled: false, minConfidence: 0.70, timeoutMs: 500, preflight: true, anomaly: true },
      sanitizer: { enabled: true, mode: 'auto', deterministicPass: true },
      dispatcher: { enabled: true, autoReadOnly: true, timeoutMs: 800, minConfidence: 0.75 }
    };
    const app = await boot({ settings: { workdir: '/proj/omega', localModels: lm } });

    app.window.fetch = ((orig) => (input, init) => {
      const key = typeof input === 'string' ? input : String(input);
      const p = (() => { try { return new URL(key, 'http://localhost:8080').pathname; } catch (e) { return key; } })();
      if (p === '/select') {
        app.events.push({ url: key, method: (init && init.method) || 'GET',
          body: init && init.body ? String(init.body) : null, headers: {} });
        return Promise.resolve({
          ok: true, status: 200,
          json: async () => ({ ok: true, calls: [{ name: 'read_file', arguments: { path: 'main.py' } }],
            confidence: 0.91, latency_ms: 3, trace_id: 'tr-sel' })
        });
      }
      return orig(input, init);
    })(app.window.fetch);

    $(app, 'ta').value = 'inspect the workdir';
    $(app, 'send-btn').click();
    await tick(1200);

    /* Read the ACTUAL outbound request bodies and check every declared tool_call's
       function.arguments is a string. This is the wire format; `buildMessages` emits
       `arguments: t.args` verbatim, so an object-valued args reaches the server as an
       object and a strict OpenAI-compatible server rejects the turn. */
    const wire = app.events
      .filter((e) => e.method === 'POST' && String(e.url).indexOf('chat/completions') !== -1)
      .map((e) => { try { return JSON.parse(e.body || '{}'); } catch (x) { return {}; } });

    const offenders = [];
    let sawDeclared = 0;
    wire.forEach((body) => {
      (body.messages || []).forEach((msg) => {
        (msg.tool_calls || []).forEach((tc) => {
          sawDeclared++;
          const a = tc && tc.function && tc.function.arguments;
          if (typeof a !== 'string') offenders.push(typeof a);
        });
      });
    });

    check(wire.length > 0,
      'R1-C: the turn actually dispatched model requests (got ' + wire.length + ')');
    check(sawDeclared > 0,
      'R1-C: at least one assistant tool_call reached the wire (got ' + sawDeclared + ')');
    check(offenders.length === 0,
      'R1-C: every tool_call.function.arguments is a STRING on the wire (offending types: ' +
      JSON.stringify(offenders) + ')');

    teardownApp(app);
  }

  process.exit(summary('PHASE 17 R1: review-found defects'));
})();