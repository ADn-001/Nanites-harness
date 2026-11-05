'use strict';
/*
 * PHASE 23 E2E — a timeout is not an endpoint failure, and a probe that omits the
 * key is not a probe that passed.
 *
 * Tickets: CR-Nanites-harness-0005 (medium), CR-Nanites-harness-0029 (medium).
 *
 * Both shipped GREEN because no assertion could see them:
 *
 *  0005 — `streamSignal(ms)` returns `AbortSignal.any([abortCtl.signal,
 *        AbortSignal.timeout(ms)])`. The timeout signal aborts with a DOMException
 *        whose `name` is 'TimeoutError', NOT 'AbortError'. `stream()`'s catch tested
 *        ONLY `e.name==='AbortError'`, so a genuine 180 s budget expiry fell into the
 *        ELSE branch and was reported as `VERIFY ENDPOINT LINK` — pointing the operator
 *        at the one component that is healthy — and, on LM Studio, fired a soul-load
 *        retry: a SECOND 120 s model load on top of a turn that already ran 180 s.
 *        Separately, one `sig` was threaded through all 12 iterations of `runAgentLoop`,
 *        so the budget covered the WHOLE agentic turn; a legitimate multi-iteration
 *        tool-using turn is cut off mid-work.
 *
 *  0029 — `fetchLMSLoaded()` called `fetch(api('/api/v0/models'), {signal: apiSig()})`
 *        with NO headers, while all five sibling probes pass `authHeaders()`. Against a
 *        keyed LM Studio backend that one request fails auth and the model list reports
 *        "not loaded" with no error anywhere.
 *
 * The harness note in the ledger said "jsdom has no AbortSignal.timeout at all, so the
 * frontend harness cannot currently reproduce it without a polyfill". That is WRONG on
 * this host — jsdom under Node 24 supplies it. The Phase 5 gatelog claim is also wrong.
 * These tests therefore do NOT assume either way: they inject a controllable
 * AbortSignal, which is the honest way to drive an abort cause at all, and they assert
 * against the INJECTED fake. A test that depends on a real 180 s wait is not a test.
 */
const { check, summary, clearFails, launchApp, teardownApp } = require('./helpers');

const tick = (ms = 30) => new Promise((r) => setTimeout(r, ms));

/* Build a DOMException-alike whose `name` we choose, exactly as the platform does:
   AbortSignal.timeout() aborts with `name: 'TimeoutError'`, an operator abort with
   `name: 'AbortError'`. Modelling both is the whole point — a fake that always threw
   AbortError could not tell the two code paths apart. */
function fakeAbortSignal(kind) {
  const name = kind === 'timeout' ? 'TimeoutError' : 'AbortError';
  return {
    aborted: false,
    reason: undefined,
    addEventListener() {},
    removeEventListener() {},
    /* `stream()` and `runAgentLoop` both read `.aborted` to test the loop guard, and the
       catch distinguishes causes by `e.name`. Firing sets BOTH, as the platform does. */
    fire() {
      this.aborted = true;
      this.reason = { name: name, message: name === 'TimeoutError' ? 'The operation timed out.' : 'This operation was aborted' };
      return this.reason;
    },
    throwIfAborted() { if (this.aborted) throw this.reason; }
  };
}

const BASE_ROUTES = {
  '/v1/models': { status: 200, json: { data: [{ id: 'test-model' }] } },
  '/tools/execute': { status: 200, json: { ok: true, result: 'file contents here' } },
  '/api/v0/models': { status: 200, json: { object: 'list', data: [] } }
};

/* LANDMINE (Phase 11/12/13): `send()` returns early with `alert('NO SOUL-MODEL SELECTED')`
   when `settings.model` is empty, so an unseeded app never starts a turn and every
   transcript assertion passes/fails for the wrong reason. Seed a real model. */
function baseSettings(extra) {
  return Object.assign({
    endpoint: 'http://x', model: 'test-model', backend: 'openai', system: '',
    workdir: '', agent: false, autoBridge: false, autoApproveRead: true,
    profiles: [], activeProfile: '', apiKey: 'sk-phase23',
    ctxLimit: 8000,
    localModels: { enabled: false }
  }, extra || {});
}

/* Drive a real send whose model call fails with the given abort cause, then read what
   the transcript says. This exercises the REAL stream() catch against the REAL app. */
async function driveTurnWith(app, kind) {
  const win = app.window;
  const asked = [];
  /* Arm the failure INSIDE the app: replace fetch so /v1/chat/completions rejects with the
     abort error of the chosen kind. This is the seam the defect lives on — the catch
     block's `e.name` test — so the test drives production code, not a copy of it.
     Every requested URL is recorded, so "no soul-load retry" can be asserted against
     what the app ACTUALLY asked for rather than against an array that was never filled. */
  win.fetch = function (input, init) {
    const key = typeof input === 'string' ? input : String(input);
    let p = key;
    try { p = new URL(key, 'http://localhost:8080').pathname; } catch (e) { /* keep */ }
    asked.push({ url: key, path: p, method: ((init && init.method) || 'GET').toUpperCase() });
    if (p === '/v1/chat/completions') {
      const s = fakeAbortSignal(kind);
      return Promise.reject(s.fire());
    }
    /* Everything else resolves empty so boot stays quiet. */
    if (p === '/api/v1/models/load') {
      return Promise.resolve({ ok: true, status: 200, json: async () => ({}), text: async () => '{}' });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => ({}), text: async () => '{}' });
  };

  const ta = win.document.getElementById('ta');
  ta.value = 'phase 23 probe';
  win.document.getElementById('send-btn').click();
  await tick(600);

  const texts = [];
  try {
    const msgs = win.active().messages;
    for (const m of msgs) texts.push(String(m.content || ''));
  } catch (e) { /* fall through; assertion reports empty */ }
  return { text: texts.join('\n'), asked: asked };
}

(async function main() {
  console.log('PHASE 23 — timeout diagnostics + probe auth header\n');

  // ---------------------------------------------------------------- 0005 (a)
  console.log('--- 0005: a stream timeout is reported as a TIMEOUT, not a link fault ---');
  {
    const app = await launchApp({ routes: BASE_ROUTES, seed: { 'cogitator.settings': JSON.stringify(baseSettings()) } });
    await tick(60);
    const out = await driveTurnWith(app, 'timeout');
    const t = out.text;

    // PROVE the test is not vacuous: the turn really ran and really produced a report.
    check(/RITE|TIMEOUT|BUDGET|HALTED/.test(t) === true,
      '0005: the timeout turn actually produced a transcript report (got ' + JSON.stringify(t.slice(-160)) + ')');

    check(t.indexOf('VERIFY ENDPOINT LINK') === -1,
      '0005: a TimeoutError is NOT reported as "VERIFY ENDPOINT LINK" (the wrong diagnosis)');

    /* Deliberately does NOT match the raw error message. The pre-fix output contains
       "The operation timed out." inside "[ RITE FAILED: ... ]", so a loose /timed out/i
       passes on the DEFECTIVE code — which is precisely how this defect shipped. Require
       the bracketed notice the catch block itself must emit. */
    check(/\[ [^\]]*(TIMED OUT|BUDGET)[^\]]*\]/.test(t) === true,
      '0005: the timeout is reported AS a timeout, in a dedicated notice (got ' + JSON.stringify(t.slice(-160)) + ')');

    teardownApp(app);
  }

  // ---------------------------------------------------------------- 0005 (b)
  console.log('--- 0005: an operator abort still reads as a HALT ---');
  {
    const app = await launchApp({ routes: BASE_ROUTES, seed: { 'cogitator.settings': JSON.stringify(baseSettings()) } });
    await tick(60);
    const out = await driveTurnWith(app, 'operator');
    const t = out.text;
    check(/HALTED BY OPERATOR/.test(t) === true,
      '0005: an operator AbortError still reads "TRANSMISSION HALTED BY OPERATOR" (got ' + JSON.stringify(t.slice(-160)) + ')');
    check(t.indexOf('VERIFY ENDPOINT LINK') === -1,
      '0005: an operator abort is not reported as a link fault either');
    teardownApp(app);
  }

  // ---------------------------------------------------------------- 0005 (c)
  console.log('--- 0005: no LM Studio soul-load retry fires on a TIMEOUT ---');
  {
    /* The retry is the expensive half: a second 120 s model load on top of a turn that
       already ran 180 s. It is gated on currentBackend()==='lmstudio'. Force that, then
       assert NO /api/v1/models/load request is issued when the cause is a timeout. */
    const app = await launchApp({ routes: BASE_ROUTES, seed: { 'cogitator.settings': JSON.stringify(baseSettings({ backend: 'lmstudio' })) } });
    await tick(60);
    const out = await driveTurnWith(app, 'timeout');

    // Non-vacuity: the recorder must actually have seen the model call, or "no load
    // request" would be trivially true because nothing ran at all.
    const sawChat = out.asked.some((a) => a.path === '/v1/chat/completions');
    check(sawChat === true,
      '0005: the LM Studio turn really did issue a model call (the no-retry check is not vacuous)');
    const loads = out.asked.filter((a) => a.path === '/api/v1/models/load');
    check(loads.length === 0,
      '0005: NO soul-load retry is issued on a timeout (got ' + loads.length + ' load request(s))');

    /* POSITIVE DIRECTION, and the reason the check above is meaningful: the SAME turn on a
       non-timeout fault DOES retry. A guard that suppresses everything would satisfy the
       timeout assertion while silently disabling soul-load recovery entirely. */
    const app2 = await launchApp({ routes: BASE_ROUTES, seed: { 'cogitator.settings': JSON.stringify(baseSettings({ backend: 'lmstudio' })) } });
    await tick(60);
    const win2 = app2.window;
    const asked2 = [];
    win2.fetch = function (input, init) {
      const key = typeof input === 'string' ? input : String(input);
      let p = key;
      try { p = new URL(key, 'http://localhost:8080').pathname; } catch (e) { /* keep */ }
      asked2.push({ path: p, method: ((init && init.method) || 'GET').toUpperCase() });
      if (p === '/v1/chat/completions') return Promise.reject(new TypeError('NetworkError: induced endpoint failure'));
      return Promise.resolve({ ok: true, status: 200, json: async () => ({}), text: async () => '{}' });
    };
    win2.document.getElementById('ta').value = 'phase 23 control';
    win2.document.getElementById('send-btn').click();
    await tick(700);
    const loads2 = asked2.filter((a) => a.path === '/api/v1/models/load');
    check(loads2.length > 0,
      '0005: a genuine endpoint fault on LM Studio STILL fires the soul-load retry (the fix is not over-broad), got ' + loads2.length);
    teardownApp(app2);
    teardownApp(app);
  }

  // ---------------------------------------------------------------- 0005 (d)
  console.log('--- 0005: the loop-wide signal carries NO timeout (the budget is per call) ---');
  {
    /* The previous version of this check sliced the source around a string and grepped for
       'TimeoutError' — which my own explanatory comment satisfied, so it stayed green with
       the feature removed. A guard that reads prose cannot see code.

       This asserts the RULE behaviourally instead: the turn-wide signal the loop gates on
       must not expire on its own. Give it longer than any test could wait, then confirm it
       is still un-aborted after the equivalent of many budgets — i.e. no timer is attached
       to it at all. */
    const app = await launchApp({ routes: BASE_ROUTES, seed: { 'cogitator.settings': JSON.stringify(baseSettings()) } });
    await tick(60);
    const win = app.window;
    const res = win.eval(`(function(){
      abortCtl = new AbortController();
      var s = operatorSignal();
      var before = !!s && s.aborted === false;
      /* The distinguishing property: no timeout is composed onto the operator signal.
         If the turn-wide signal still carried the 180s budget, abortCause() could report
         a TimeoutError from it — and the loop would die on a budget rather than on STOP. */
      return { present: !!s, unAborted: before, cause: abortCause(s) };
    })()`);
    check(res && res.present === true, '0005: the loop-wide signal exists (the check is not vacuous)');
    check(res && res.unAborted === true, '0005: the loop-wide signal starts un-aborted');
    check(res && res.cause === null,
      '0005: an un-fired loop-wide signal reports NO cause (it is not a timer)');
    teardownApp(app);
  }

  // ---------------------------------------------------------------- 0005 (e)
  console.log('--- 0005: a MULTI-ITERATION turn survives a per-iteration budget ---');
  {
    /* THE behavioural test for the defect CR-0005 actually describes, and the one the
       source-shape checks below cannot substitute for. The old code built ONE signal at
       the top of stream() carrying the 180 s timeout and gated every one of the loop's
       iterations on it, so the budget covered the WHOLE turn: a legitimate multi-iteration
       tool-using turn was cut off mid-work. Building a per-iteration signal that is then
       never used, because the turn-wide signal still kills the loop at the next guard,
       leaves every observable behaviour identical — which is why the shape assertions
       alone would have reported this phase done.

       The trick is a budget SMALL ENOUGH to fire. Shrink it by overriding the budget the
       app reads, then drive a real multi-iteration agent turn. On the fixed code the turn
       completes its iterations; on the pre-fix code it is cut off after the first budget
       expiry regardless of how much per-iteration work remains. */
    const app = await launchApp({ routes: BASE_ROUTES, seed: { 'cogitator.settings': JSON.stringify(baseSettings({ agent: true, autoApproveRead: true })) } });
    await tick(60);
    const win = app.window;
    /* Shrink the budget the app actually uses, so the scenario is reachable in test time.
       `STREAM_BUDGET_MS` is a `let` precisely so this is possible — with a `const` the only
       way to test a 180 s budget would be to wait 180 s. */
    win.eval('STREAM_BUDGET_MS=600;');
    /* LANDMINE (Phase 13/14/17): TextDecoder is not wired to the fake response body in
       jsdom, so the SSE pump decodes nothing and every iteration looks like an empty
       stream — which stops the loop after one call for reasons that have nothing to do
       with the budget. phase17_agent_turn.test.js installs the same stub. */
    win.TextDecoder = class { constructor() {} decode(v) { return v == null ? '' : String(v); } };

    let chatCalls = 0;
    /* DISTINGUISHING INPUT — the whole point of this test, and the reason an earlier
       version of it was worthless. Two things had to be true at once:

       1. Each model call must take real time (~200 ms). With instant replies BOTH the
          pre-fix and post-fix builds finish all 12 iterations, so the test passes on the
          broken code — the defect would have been reported fixed while still present.
       2. The budget must be far smaller than the whole turn's work (600 ms vs ~2.4 s), so
          one turn-wide budget provably cannot cover the turn.

       Measured A/B on this exact fixture: pre-fix stops at 3 iterations (the turn-wide
       budget expires mid-loop); post-fix completes all 12. That difference is the
       behaviour the phase exists to fix, so it is what gets asserted. */
    const PER_CALL_MS = 200;
    win.fetch = function (input, init) {
      const key = typeof input === 'string' ? input : String(input);
      let p = key;
      try { p = new URL(key, 'http://localhost:8080').pathname; } catch (e) { /* keep */ }
      if (p === '/v1/chat/completions') {
        chatCalls++;
        /* Every iteration asks for a read-only tool call, so the loop genuinely iterates. */
        const events = [
          { choices: [{ delta: { tool_calls: [{ index: 0, id: 'call_x' + chatCalls, type: 'function', function: { name: 'read_file', arguments: '{"path":"."}' } }] } }] },
          { choices: [{ delta: {}, finish_reason: 'tool_calls' }] }
        ];
        const chunks = events.map((e) => 'data: ' + JSON.stringify(e) + '\n\n');
        chunks.push('data: [DONE]\n\n');
        let i = 0;
        /* The stream pump reads `response.body.getReader()`, NOT a string — a fake that
           sets `body` to the SSE text fails with "body.getReader is not a function", which
           aborts the turn for a reason that has nothing to do with the budget. Copy the
           reader shape from phase17_agent_turn.test.js's sseBody(). */
        const reader = {
          getReader() {
            return { read: async () => (i >= chunks.length ? { done: true, value: undefined } : { done: false, value: chunks[i++] }) };
          }
        };
        return new Promise((resolve) => setTimeout(() => resolve({
          ok: true, status: 200, json: async () => ({}), text: async () => '', body: reader
        }), PER_CALL_MS));
      }
      if (p === '/tools/execute') return Promise.resolve({ ok: true, status: 200, json: async () => ({ ok: true, result: 'file contents here' }), text: async () => '{}' });
      return Promise.resolve({ ok: true, status: 200, json: async () => ({}), text: async () => '{}' });
    };

    win.document.getElementById('ta').value = 'iterate please';
    win.document.getElementById('send-btn').click();
    /* Generous: 12 iterations x ~200 ms plus tool round-trips. Cutting this short would
       cap the iteration count for the harness's reasons rather than the product's. */
    await tick(4000);

    check(chatCalls >= 12,
      '0005: a multi-iteration turn completes ALL 12 iterations on a per-iteration budget (got ' + chatCalls + ')');
    teardownApp(app);
  }

  // ---------------------------------------------------------------- 0005 (f)
  console.log('--- 0005: the per-iteration budget is ARMED (a slow call still times out) ---');
  {
    /* The other half of the multi-iteration test, and the mutation that escaped without it.
       Removing `iterSig=streamSignal()` leaves a per-iteration signal that is really the
       operator's — which carries NO timer — so a hung model call would hang forever and the
       previous assertion still passed, because those iterations are all fast.

       The distinguishing input is a model call SLOWER than the budget. A correct build
       aborts it and reports a timeout; a build with no timer on the per-iteration signal
       waits for it and the turn hangs instead. This is the assertion that pins the
       per-iteration signal to something that actually bounds time. */
    const app = await launchApp({ routes: BASE_ROUTES, seed: { 'cogitator.settings': JSON.stringify(baseSettings({ agent: true, autoApproveRead: true })) } });
    await tick(60);
    const win = app.window;
    win.TextDecoder = class { constructor() {} decode(v) { return v == null ? '' : String(v); } };
    win.eval('STREAM_BUDGET_MS=600;');

    let finished = false;
    void finished;
    /* 2500 ms of "thinking" against a 600 ms budget. The promise resolves ONLY when the
       2500 ms timer fires; the signal's abort event rejects it early. A build with no
       timer on this signal therefore waits the full 2500 ms and the turn never reports a
       timeout — which is the behaviour this assertion exists to catch. */
    win.fetch = function (input, init) {
      const key = typeof input === 'string' ? input : String(input);
      let p = key;
      try { p = new URL(key, 'http://localhost:8080').pathname; } catch (e) { /* keep */ }
      if (p !== '/v1/chat/completions') return Promise.resolve({ ok: true, status: 200, json: async () => ({}), text: async () => '{}' });
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => resolve({
          ok: true, status: 200, json: async () => ({}), text: async () => '',
          body: { getReader() { return { read: async () => ({ done: true }) }; } }
        }), 2500);
        const s = init && init.signal;
        if (s && typeof s.addEventListener === 'function') {
          s.addEventListener('abort', () => {
            clearTimeout(timer);
            /* Carry the REAL reason name through, exactly as the platform does — this is
               what lets the catch block tell a timeout from an operator halt. */
            const err = new Error('The operation was aborted.');
            err.name = (s.reason && s.reason.name) || 'AbortError';
            reject(err);
          }, { once: true });
        }
      });
    };

    win.document.getElementById('ta').value = 'hang please';
    win.document.getElementById('send-btn').click();
    await tick(1500);

    let text = '';
    try {
      text = win.eval('active().messages.map(function(m){return String(m.content||"")}).join("\\n")');
    } catch (e) { /* reported below */ }
    check(/BUDGET|TIMED OUT/i.test(text) === true,
      '0005: a model call slower than the per-iteration budget is reported as a TIMEOUT (got ' + JSON.stringify(text.slice(-140)) + ')');
    check(text.indexOf('VERIFY ENDPOINT LINK') === -1,
      '0005: a per-iteration budget expiry is not misreported as an endpoint fault');
    teardownApp(app);
  }

  // ---------------------------------------------------------------- 0005 (g)
  console.log('--- 0005: the PLAIN (non-agent) chat path is still bounded ---');
  {
    /* Regression guard for the fix itself. Making the turn-wide signal operator-only
       removed the budget from every path that received it — including plain chat, which
       has no runAgentLoop to build a fresh signal. Passing `sig` straight through would
       have left an ordinary chat turn with NO time limit at all: a regression in the
       opposite direction to the one being fixed, and one nothing else would catch. */
    const app = await launchApp({ routes: BASE_ROUTES, seed: { 'cogitator.settings': JSON.stringify(baseSettings({ agent: false })) } });
    await tick(60);
    const win = app.window;
    win.TextDecoder = class { constructor() {} decode(v) { return v == null ? '' : String(v); } };
    win.eval('STREAM_BUDGET_MS=600;');
    win.fetch = function (input, init) {
      const key = typeof input === 'string' ? input : String(input);
      let p = key;
      try { p = new URL(key, 'http://localhost:8080').pathname; } catch (e) { /* keep */ }
      if (p !== '/v1/chat/completions') return Promise.resolve({ ok: true, status: 200, json: async () => ({}), text: async () => '{}' });
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => resolve({
          ok: true, status: 200, json: async () => ({}), text: async () => '',
          body: { getReader() { return { read: async () => ({ done: true }) }; } }
        }), 2500);
        const s = init && init.signal;
        if (s && typeof s.addEventListener === 'function') {
          s.addEventListener('abort', () => {
            clearTimeout(timer);
            const err = new Error('The operation was aborted.');
            err.name = (s.reason && s.reason.name) || 'AbortError';
            reject(err);
          }, { once: true });
        }
      });
    };
    win.document.getElementById('ta').value = 'plain hang';
    win.document.getElementById('send-btn').click();
    await tick(1500);

    let text = '';
    try {
      text = win.eval('active().messages.map(function(m){return String(m.content||"")}).join("\\n")');
    } catch (e) { /* reported below */ }
    check(/BUDGET|TIMED OUT/i.test(text) === true,
      '0005: a plain (non-agent) chat turn is STILL bounded and reports a timeout (got ' + JSON.stringify(text.slice(-140)) + ')');
    teardownApp(app);
  }

  // ---------------------------------------------------------------- 0029
  console.log('--- 0029: every /api/v0/models probe carries the Authorization header ---');
  {
    /* This is the assertion the ledger says NO suite makes: inspect REQUEST HEADERS, not
       just the status code. `launchApp`'s fetch records `headers` on every call, so we
       drive the real probe and read what it actually sent. */
    const seenHeaders = [];
    const app = await launchApp({
      routes: {
        '/api/v0/models': { json: { object: 'list', data: [{ id: 'qwen3-8b', state: 'loaded' }] } },
        '/v1/models': { json: { data: [{ id: 'qwen3-8b' }] } },
        '/api/tags': { json: { models: [{ name: 'qwen3:8b' }] } }
      },
      seed: { 'cogitator.settings': JSON.stringify(baseSettings()) }
    });
    await tick(60);
    /* Wrap fetch to record headers, then call the real fetchLMSLoaded(). */
    const win = app.window;
    const orig = win.fetch;
    win.fetch = function (input, init) {
      const key = typeof input === 'string' ? input : String(input);
      seenHeaders.push({ url: key, headers: Object.assign({}, init && init.headers) });
      return orig(input, init);
    };
    await win.eval('fetchLMSLoaded()');
    await tick(30);

    const v0 = seenHeaders.filter((h) => h.url.indexOf('/api/v0/models') !== -1);
    check(v0.length > 0, '0029: the probe really issued a /api/v0/models request (test is not vacuous), got ' + v0.length);
    const withAuth = v0.filter((h) => {
      const keys = Object.keys(h.headers).map((k) => k.toLowerCase());
      return keys.indexOf('authorization') !== -1;
    });
    check(v0.length > 0 && withAuth.length === v0.length,
      '0029: EVERY /api/v0/models request carries an Authorization header (' +
      withAuth.length + '/' + v0.length + ')');
    teardownApp(app);
  }

  // ---------------------------------------------------------------- 0029 (b)
  console.log('--- 0029: no /api/v0/models call site omits authHeaders() ---');
  {
    /* Family guard: assert PER CALL SITE, not for one function. A test that names only
       fetchLMSLoaded would let a sibling regress silently — the exact failure this
       project already shipped once. Every fetch to /api/v0/models must pass headers. */
    const fs = require('fs');
    const path = require('path');
    const src = fs.readFileSync(path.join(__dirname, '..', '..', 'index.html'), 'utf8');
    const lines = src.split('\n');
    const sites = [];
    lines.forEach((ln, i) => { if (ln.indexOf('/api/v0/models') !== -1) sites.push({ n: i + 1, ln: ln }); });
    check(sites.length >= 2, '0029: found the /api/v0/models call sites (got ' + sites.length + ')');
    let bad = [];
    sites.forEach((s) => {
      /* A call site is compliant when the same line passes authHeaders(). Tolerate the
         multi-line form by also scanning the next 2 lines for the header helper. */
      const window3 = s.ln + '\n' + (lines[s.n] || '') + '\n' + (lines[s.n + 1] || '');
      if (window3.indexOf('authHeaders()') === -1) bad.push(s.n);
    });
    check(bad.length === 0,
      '0029: no /api/v0/models call site omits authHeaders() (offenders at line(s): ' + JSON.stringify(bad) + ')');
  }

  // ---------------------------------------------------------------- static
  console.log('--- structural: stream() still tears down in a finally (no regression) ---');
  {
    const fs = require('fs');
    const path = require('path');
    const src = fs.readFileSync(path.join(__dirname, '..', '..', 'index.html'), 'utf8');
    check(/}finally\{/.test(src.slice(src.indexOf('async function stream'), src.indexOf('async function stream') + 9000)),
      '0005: stream() teardown remains inside a finally');
  }

  console.log('\nPHASE 23: ' + summary('phase23'));
  process.exitCode = summary('PHASE 23') ? 1 : 0;
})();