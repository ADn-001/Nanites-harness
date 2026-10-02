/*
 * PHASE 17 R2 — five defects the phase-17 + R1 suites could not see.
 *
 * Every one of these was found by an independent review pass, not by a failing test, and
 * every one sits INSIDE phase 17's own gate ("no path leaves the app permanently busy, and
 * no outbound request can be rejected by a strict OpenAI-compatible server"). The R1 pass
 * fixed three defects and each fix comment described itself as complete; these are the
 * consequences that were not followed.
 *
 *  R2-A (0011) — runAgentLoop rebinds its own `m` each iteration but NEVER rebinds
 *         streamTarget, so from agent iteration 1 the pump writes into the FIRST assistant
 *         bubble of the turn. CR-0011 relocated, not fixed.
 *  R2-B (0012) — buildMessages' budget trim walks backwards and `break`s on the first
 *         over-budget message with no dangling-tool guard, so it can keep a `tool` message
 *         while dropping the assistant that declared its tool_call_id. compactChat() has
 *         exactly that guard; buildMessages does not.
 *  R2-C (0015) — compactToolCalls REBINDS acc.toolCalls to a filtered array, but the pump
 *         assigned the OLD reference onto the message. The message therefore keeps a SPARSE
 *         array, and ctxTokens' `for...of` yields undefined for holes -> tok(t.args) throws,
 *         out of renderMessages, breaking message rendering until reload.
 *  R2-D (0011) — msgAction('del') is the per-message purge and is NOT guarded by
 *         `generating`. Deleting the streaming message mid-turn makes the bound target
 *         vanish, refreshLast silently falls back to messages[length-1], and CR-0011 returns.
 *  R2-E (0014) — `generating=true; abortCtl=...; const sig=streamSignal();` still sits ABOVE
 *         the try. A throw in streamSignal() leaves generating stuck true forever — verbatim
 *         the CR-0014 state, one line above the fix that claims to close it.
 *
 * Note on array holes: `Array.prototype.some/map/filter` SKIP holes, so a sparse array can
 * look clean to every assertion built on them. `for...of` and `for` do NOT skip them — which
 * is exactly why R2-C can hide from a shape check and still crash the renderer. Where a test
 * here needs to see a hole, it uses for...of, never some().
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
  /* ============ R2-C: the de-compacted sparse array must not reach ctxTokens ============ */
  console.log('--- R2-C: hand-off compaction must not leave a sparse array on the message ---');
  {
    const app = await boot();

    const res = app.window.eval(`(function(){
      /* Drive the SHIPPED functions, not reimplementations of them.
         THE DISTINGUISHING INPUT: a hole that SURVIVES to hand-off. If every index eventually
         arrives, the array ends up dense and the defect is invisible — the first draft sent
         {2,0,1} and passed vacuously. A real provider that skips or truncates an index leaves
         a PERMANENT hole, and that is the only input on which "compact at hand-off" and "the
         message holds a usable array" disagree. */
      const acc = newAcc();
      mergeToolDelta(acc, { index: 2, id: 'c2', function: { name: 'grep', arguments: '{"pattern":"b"}' } });
      mergeToolDelta(acc, { index: 0, id: 'c0', function: { name: 'read_file', arguments: '{"path":"a"}' } });
      /* index 1 NEVER arrives — a truncated/skipped fragment. */

      const preHoles = (function(){ let n = 0; for (const t of acc.toolCalls) if (t === undefined) n++; return n; })();

      /* The pump assigns by reference onto the message (index.html ~970). */
      const m = { role: 'assistant', content: '', toolCalls: acc.toolCalls };

      /* Hand-off compaction REBINDS acc.toolCalls in the shipped code. */
      compactToolCalls(acc);

      /* Count holes with for...of, NOT some(): some() skips holes and would report 0 and
         let this exact defect through. */
      let holes = 0;
      for (const t of m.toolCalls) { if (t === undefined) holes++; }

      let threw = null;
      try { ctxTokens({ messages: [m] }); } catch (e) { threw = e.constructor.name + ': ' + e.message; }

      /* Positive control: the same shape WITHOUT holes must token-count fine, so a throw
         below is attributable to the holes and not to a broken fixture. */
      let controlThrew = null;
      try { ctxTokens({ messages: [{ role: 'assistant', content: '',
        toolCalls: [{ id: 'c0', name: 'grep', args: '{}' }] }] }); }
      catch (e) { controlThrew = String(e.message); }

      return { preHoles: preHoles, holes: holes, threw: threw, controlThrew: controlThrew,
               msgCalls: m.toolCalls.length, accCalls: acc.toolCalls.length };
    })()`);

    check(res.preHoles > 0,
      'R2-C: the accumulator really did hold a hole before compaction (got ' + res.preHoles +
      ') — the fixture is not vacuous');
    check(res.controlThrew === null,
      'R2-C: the dense-array control token-counts fine (the fixture is not the problem)');
    check(res.holes === 0,
      'R2-C: the message holds NO sparse holes after hand-off compaction (holes: ' +
      res.holes + ')');
    check(res.threw === null,
      'R2-C: ctxTokens does not throw on a post-compaction message (got: ' + res.threw + ')');

    teardownApp(app);
  }

  /* ============ R2-A: the stream target must follow the agent loop's own message ============ */
  console.log('--- R2-A: a multi-iteration agent turn binds each iteration\'s own message ---');
  {
    const app = await boot();

    /* Two iterations: the first returns a tool_call, the second plain content. The hazard is
       that iteration 2's prose lands on iteration 1's (now stale) assistant bubble. */
    let call = 0;
    app.window.fetch = ((orig) => (input, init) => {
      let key = typeof input === 'string' ? input : String(input);
      let p = key;
      try { p = new URL(key, 'http://localhost:8080').pathname; } catch (e) { /* keep */ }
      if (p === '/v1/chat/completions') {
        call++;
        const n = call;
        const evs = n === 1
          ? [{ choices: [{ delta: { tool_calls: [{ index: 0, id: 'call_1',
                function: { name: 'read_file', arguments: '{"path":"a.py"}' } }] } }] },
             { choices: [{ delta: {} }] }]
          : [{ choices: [{ delta: { content: 'ITER2_PROSE' } }] }];
        return Promise.resolve({ ok: true, status: 200, body: sseBody(evs),
          json: async () => ({}) });
      }
      return orig(input, init);
    })(app.window.fetch);

    $(app, 'ta').value = 'do a two step thing';
    $(app, 'send-btn').click();
    await tick(1400);

    const state = app.window.eval(`(function(){
      const c = active();
      const asst = c.messages.filter(function(m){ return m.role === 'assistant'; });
      return {
        asstCount: asst.length,
        first: (asst[0] && asst[0].content) || '',
        last: (asst[asst.length - 1] && asst[asst.length - 1].content) || '',
        bubbleLast: (document.getElementById('body-' + (c.messages.length - 1)) || {}).innerHTML || ''
      };
    })()`);

    check(state.asstCount >= 2,
      'R2-A: the turn really produced TWO assistant messages (got ' + state.asstCount + ')');
    check(state.last.indexOf('ITER2_PROSE') !== -1,
      'R2-A: iteration 2 prose landed on the LAST assistant message (got "' + state.last + '")');
    check(state.first.indexOf('ITER2_PROSE') === -1,
      'R2-A: iteration 2 prose did NOT land on the FIRST assistant bubble (got "' +
      state.first + '")');

    teardownApp(app);
  }

  /* ============ R2-B: the budget trim must not orphan a tool_call_id ============ */
  console.log('--- R2-B: buildMessages budget trim keeps the assistant that declared a tool ---');
  {
    const app = await boot();

    /* buildMessages is ASYNC (it may await a workdir listing) and reads `active()`, so the
       oversized turn has to be the ACTIVE chat and the result awaited. The first draft called
       it synchronously with a chat argument it does not take, which returned a Promise and
       blew up on .forEach — a broken probe, not a product defect. */
    /* `active()` is undefined until a chat exists, so send one real turn first to create it,
       then REPLACE its messages with the oversized fixture. (The first draft assumed a chat
       was already there and threw on `c.messages = ...` — again a broken probe.) */
    $(app, 'ta').value = 'seed a chat';
    $(app, 'send-btn').click();
    await tick(500);

    const res = await app.window.eval(`(async function(){
      const c = active();
      if (!c) return { error: 'no active chat after seeding' };
      c.messages = [
        { role: 'assistant', content: 'I will read it.', toolCalls: [], ts: 1 },
        { role: 'assistant', content: '', thinking: '',
          toolCalls: [{ id: 'call_Z', name: 'read_file', args: '{"path":"a.py"}' }], ts: 2 },
        { role: 'tool', name: 'read_file', toolCallId: 'call_Z',
          content: 'X'.repeat(90000), ts: 3 }
      ];
      /* A tiny ctxLimit forces the budget trim to bite, which is the code under test. */
      settings.ctxLimit = 400;
      const out = await buildMessages({ excludeLastAssistant: false });
      if (!Array.isArray(out)) return { error: 'buildMessages did not return an array' };
      const declared = {};
      out.forEach(function(m){
        (m.tool_calls || []).forEach(function(tc){ declared[tc.id] = true; });
      });
      const toolMsgs = out.filter(function(m){ return m.role === 'tool'; });
      const orphans = toolMsgs
        .filter(function(m){ return !declared[m.tool_call_id]; })
        .map(function(m){ return m.tool_call_id; });
      return { total: out.length, toolCount: toolMsgs.length, orphans: orphans };
    })()`);

    check(!res.error, 'R2-B: buildMessages returned an array (' + (res.error || 'ok') + ')');
    check(res.orphans.length === 0,
      'R2-B: no outbound tool message has an undeclared tool_call_id (' +
      res.toolCount + ' tool msgs, orphans: ' + JSON.stringify(res.orphans) + ')');

    teardownApp(app);
  }

  /* ============ R2-D: the per-message purge must refuse mid-stream ============ */
  console.log('--- R2-D: msgAction(\'del\') refuses to delete the streaming message ---');
  {
    const app = await boot();
    app.window.fetch = ((orig) => (input, init) => {
      let key = typeof input === 'string' ? input : String(input);
      let p = key;
      try { p = new URL(key, 'http://localhost:8080').pathname; } catch (e) { /* keep */ }
      if (p === '/v1/chat/completions') {
        return new Promise((res) => setTimeout(() => res(
          { ok: true, status: 200, body: sseBody([{ choices: [{ delta: { content: 'LATE' } }] }]),
            json: async () => ({}) }), 300));
      }
      return orig(input, init);
    })(app.window.fetch);

    $(app, 'ta').value = 'stream then be purged';
    $(app, 'send-btn').click();
    await tick(120);

    const res = app.window.eval(`(function(){
      const c = active();
      const asst = c.messages.filter(function(m){ return m.role === 'assistant'; }).pop();
      const idx = c.messages.indexOf(asst);
      const before = c.messages.length;
      msgAction('del', idx);
      return { before: before, after: c.messages.length, busy: generating,
               targetStillThere: streamTarget ? c.messages.indexOf(streamTarget.msg) !== -1 : null };
    })()`);

    check(res.busy === true, 'R2-D: the turn really was mid-stream when del was called');
    check(res.after === res.before,
      'R2-D: msgAction(\'del\') REFUSED to delete the streaming message (' +
      res.before + ' -> ' + res.after + ')');
    check(res.targetStillThere === true,
      'R2-D: the bound stream target still exists after the refused delete');

    await tick(600);
    teardownApp(app);
  }

  /* ============ R2-E: nothing above the try can strand the app ============ */
  console.log('--- R2-E: a fault in streamSignal() (above the try) still tears the app down ---');
  {
    const app = await boot();

    const armed = app.window.eval(`(function(){
      let fired = false;
      const real = window.streamSignal;
      window.streamSignal = function(){
        if (!fired && generating) { fired = true; throw new Error('injected signal fault'); }
        return real.apply(this, arguments);
      };
      window.__restoreSignal = function(){ window.streamSignal = real; };
      return true;
    })()`);
    check(armed === true, 'R2-E: armed a throwing streamSignal for the above-try window');

    const unhandled = [];
    const onUnhandled = (e) => { unhandled.push(String((e && e.message) || e)); };
    process.on('unhandledRejection', onUnhandled);

    $(app, 'ta').value = 'fault the signal';
    $(app, 'send-btn').click();
    await tick(700);

    const state = app.window.eval(`(function(){
      const b = document.getElementById('send-btn');
      const s = document.getElementById('stop-btn');
      return { busy: generating, sendDisabled: !!(b && b.disabled),
               sendShown: b ? b.style.display : null, stopShown: s ? s.style.display : null };
    })()`);

    check(state.busy === false,
      'R2-E: generating is FALSE after a fault ABOVE the try (got ' + state.busy + ')');
    check(state.sendDisabled === false && state.sendShown !== 'none',
      'R2-E: the send button is restored after an above-try fault (disabled=' +
      state.sendDisabled + ' display=' + state.sendShown + ')');
    check(state.stopShown === 'none',
      'R2-E: the stop button is hidden again after an above-try fault (got ' +
      state.stopShown + ')');
    check(unhandled.length === 0,
      'R2-E: the above-try fault does not escape as an unhandled rejection (got ' +
      JSON.stringify(unhandled) + ')');

    process.removeListener('unhandledRejection', onUnhandled);
    teardownApp(app);
  }

  process.exit(summary('PHASE 17 R2: consequences of the R1 fixes'));
})().catch((e) => {
  console.error('R2 harness threw:', e && e.stack ? e.stack : e);
  process.exit(1);
});
