'use strict';
/*
 * PHASE 7 E2E — Structured output validator (deterministic protection layer).
 *
 * Written BEFORE implementation (TDD RED). Behaviour under test:
 *  - `CogCore.validateStructuredOutput(result, allowedTools)` is a PURE, deterministic
 *    gate between the model endpoint and the bridge: it parses `tool_calls`, rejects
 *    non-JSON / non-object `arguments`, rejects unknown tool names (allow-list derived
 *    from the LIVE TOOL_SCHEMAS), rejects a call missing an id/name, and enforces the
 *    per-tool required-field / declared-type checks. Returns {ok, errors, sanitized}.
 *  - `ok` is true ONLY when every call passes; `sanitized` carries only the calls that
 *    may be dispatched, with `args` already parsed to a plain object.
 *  - END-TO-END: a rejected call is NEVER dispatched to the bridge
 *    (`POST 127.0.0.1:8931/tools/execute`) — a `role:'tool'` error message is fed back
 *    into the model loop instead so the model can correct, and a valid sibling call in
 *    the SAME turn still reaches the bridge.
 */
const path = require('path');
const { check, summary, clearFails, launchApp, teardownApp } = require('./helpers');
const CogCore = require(path.join(__dirname, '..', '..', 'appcore.js'));

const tick = (ms = 30) => new Promise((r) => setTimeout(r, ms));

const BRIDGE = 'http://127.0.0.1:8931/tools/execute';

/* ---- SSE helpers: a Response-like object whose `body` is a fake reader ---- */
function sseBody(events) {
  const chunks = events.map((e) => 'data: ' + JSON.stringify(e) + '\n\n');
  chunks.push('data: [DONE]\n\n');
  let i = 0;
  return {
    getReader() {
      return {
        read: async () => {
          if (i >= chunks.length) return { done: true, value: undefined };
          const value = chunks[i++];
          return { done: false, value };
        }
      };
    }
  };
}

/* A deltas stream carrying one or more tool calls in OpenAI chunk shape. */
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

/* Route factory: consumes `script` (array of event-lists) one entry per model call. */
function chatRoute(script) {
  let call = 0;
  return () => {
    const events = script[Math.min(call, script.length - 1)];
    call++;
    return {
      ok: true, status: 200,
      json: async () => ({}), text: async () => '',
      body: sseBody(events)
    };
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

const bridgeEvents = (app) => app.events.filter((e) => e.url === BRIDGE || e.url.endsWith('/tools/execute'));
const bridgeBodies = (app) => bridgeEvents(app).map((e) => { try { return JSON.parse(e.body || '{}'); } catch (err) { return {}; } });
const chatPosts = (app) => app.events.filter((e) => e.method === 'POST' && e.url.includes('chat/completions'));
const approvalModalOpen = (app) => {
  const el = app.document.getElementById('agent-modal');
  return !!(el && el.classList.contains('open'));
};

/* Launch the app with a scripted model stream and pump one agent-mode turn. */
async function runAgentTurn(script, text = 'inspect the workdir') {
  const routes = Object.assign({}, BASE_ROUTES, { '/v1/chat/completions': chatRoute(script) });
  const app = await launchApp({ routes, seed: { 'cogitator.settings': JSON.stringify(SEED) } });
  await tick(60);
  // jsdom ships no TextDecoder; the SSE pump needs one. String chunks pass through.
  app.window.TextDecoder = class { constructor() {} decode(v) { return v == null ? '' : String(v); } };
  const $a = (id) => app.document.getElementById(id);
  $a('ta').value = text;
  $a('send-btn').click();
  await tick(220);
  return app;
}

(async () => {
  clearFails();

  /* ============ grab the LIVE tool schemas (single source of truth) ============ */
  console.log('--- unit: validateStructuredOutput (pure) ---');
  const boot = await launchApp({ routes: BASE_ROUTES });
  const LIVE_TOOLS = JSON.parse(String(boot.window.eval('JSON.stringify(TOOL_SCHEMAS)')));
  check(Array.isArray(LIVE_TOOLS) && LIVE_TOOLS.length >= 6,
    'app exposes its live TOOL_SCHEMAS to the harness');
  teardownApp(boot);

  const has = typeof CogCore.validateStructuredOutput === 'function';
  check(has, 'CogCore.validateStructuredOutput exists');
  if (has) {
    const V = (calls, tools) => CogCore.validateStructuredOutput(calls, tools === undefined ? LIVE_TOOLS : tools);

    /* --- valid call passes --- */
    let r = V([{ id: 'call_1', name: 'read_file', args: '{"path":"main.py"}' }]);
    check(r && r.ok === true, 'valid tool call: ok===true');
    check(r && Array.isArray(r.errors) && r.errors.length === 0, 'valid tool call: no errors');
    check(r && r.sanitized.length === 1 && r.sanitized[0].name === 'read_file',
      'valid tool call is sanitized through');
    check(r && r.sanitized[0].args && typeof r.sanitized[0].args === 'object' && !Array.isArray(r.sanitized[0].args),
      'sanitized call carries parsed object args');
    check(r && r.sanitized[0].args.path === 'main.py', 'sanitized args keep the parsed value');

    /* --- arguments must be valid JSON --- */
    r = V([{ id: 'call_2', name: 'read_file', args: '{not json' }]);
    check(r && r.ok === false && r.errors.length >= 1, 'non-JSON arguments -> rejected');
    check(r && /json/i.test(r.errors[0].error || ''), 'non-JSON arguments error names JSON');
    check(r && r.sanitized.length === 0, 'rejected call is NOT in sanitized');

    /* --- arguments must be a plain object --- */
    for (const bad of ['"just a string"', '[]', '42', 'null', 'true']) {
      r = V([{ id: 'call_3', name: 'read_file', args: bad }]);
      check(r && r.ok === false && r.sanitized.length === 0,
        'non-object arguments rejected (' + bad + ')');
    }
    r = V([{ id: 'call_3b', name: 'read_file', args: ['a'] }]);
    check(r && r.ok === false, 'arguments already parsed as an ARRAY are rejected');

    /* --- unknown tool name (allow-list derived from live TOOL_SCHEMAS) --- */
    r = V([{ id: 'call_4', name: 'definitely_not_a_tool', args: '{}' }]);
    check(r && r.ok === false && r.sanitized.length === 0, 'unknown tool name rejected');
    check(r && /definitely_not_a_tool/.test(r.errors[0].error || ''), 'error names the offending tool');
    for (const phantom of ['shell_exec', 'clipboard', 'bash']) {
      r = V([{ id: 'call_4b', name: phantom, args: '{}' }]);
      check(r && r.ok === false, 'phantom tool "' + phantom + '" rejected');
    }
    for (const real of ['read_file', 'write_file', 'list_dir', 'grep', 'git', 'run_command']) {
      r = V([{ id: 'call_4c', name: real, args: real === 'git' ? '{"args":"status"}' : real === 'run_command' ? '{"command":"ls"}' : real === 'read_file' ? '{"path":"a"}' : real === 'write_file' ? '{"path":"a","content":"b"}' : real === 'grep' ? '{"path":".","pattern":"x"}' : '{"path":"."}' }]);
      check(r && r.ok === true, 'every real tool name is allowed ("' + real + '")');
    }

    /* --- identity fields --- */
    r = V([{ id: '', name: 'read_file', args: '{"path":"a"}' }]);
    check(r && r.ok === false, 'call missing an id is rejected');
    r = V([{ id: 'call_5', name: '', args: '{}' }]);
    check(r && r.ok === false, 'call missing a name is rejected');

    /* --- per-tool required field / declared type checks --- */
    r = V([{ id: 'call_6', name: 'read_file', args: '{}' }]);
    check(r && r.ok === false, 'missing required arg rejected (read_file without path)');
    check(r && /path/.test(r.errors[0].error || ''), 'error names the missing required field');
    r = V([{ id: 'call_7', name: 'read_file', args: '{"path":42}' }]);
    check(r && r.ok === false, 'wrong declared type rejected (read_file path:42)');
    r = V([{ id: 'call_8', name: 'write_file', args: '{"path":"a"}' }]);
    check(r && r.ok === false, 'missing required arg rejected (write_file without content)');

    /* --- mixed bag: ok is false, valid siblings survive in sanitized --- */
    r = V([
      { id: 'call_9', name: 'list_dir', args: '{"path":"."}' },
      { id: 'call_10', name: 'evil_tool', args: '{}' }
    ]);
    check(r && r.ok === false, 'mixed valid+invalid turn: ok===false');
    check(r && r.sanitized.length === 1 && r.sanitized[0].name === 'list_dir',
      'mixed turn: only the valid call is sanitized');
    check(r && r.errors.length === 1, 'mixed turn: one error recorded');

    /* --- OpenAI-shaped + finalized-shaped inputs both accepted --- */
    r = CogCore.validateStructuredOutput(
      { tool_calls: [{ id: 'c1', type: 'function', function: { name: 'list_dir', arguments: '{"path":"."}' } }] },
      LIVE_TOOLS);
    check(r && r.ok === true && r.sanitized[0].name === 'list_dir',
      'accepts an OpenAI-shaped assistant message (tool_calls + function.arguments)');
    r = CogCore.validateStructuredOutput(
      { toolCalls: [{ id: 'c2', name: 'list_dir', args: '{"path":"."}' }] }, LIVE_TOOLS);
    check(r && r.ok === true && r.sanitized[0].name === 'list_dir',
      'accepts the finalized {toolCalls:[{name,args}]} shape');

    /* --- garbage input never throws --- */
    for (const junk of [null, undefined, 0, 'nope', {}, { tool_calls: 'x' }]) {
      let threw = false, out = null;
      try { out = CogCore.validateStructuredOutput(junk, LIVE_TOOLS); } catch (e) { threw = true; }
      check(!threw && out && out.ok === false && Array.isArray(out.errors) && Array.isArray(out.sanitized),
        'garbage input returns {ok:false,...} instead of throwing (' + JSON.stringify(junk) + ')');
    }

    /* --- allowedTools may be plain names too --- */
    r = CogCore.validateStructuredOutput([{ id: 'c3', name: 'read_file', args: '{"path":"a"}' }], ['read_file']);
    check(r && r.ok === true, 'allowedTools as a plain name list still validates');
    r = CogCore.validateStructuredOutput([{ id: 'c4', name: 'git', args: '{}' }], ['read_file']);
    check(r && r.ok === false, 'allowedTools as a plain name list still blocks non-members');
  }

  /* ============ E2E A: unknown tool is never dispatched ============ */
  console.log('--- e2e: rejected call never reaches the bridge ---');
  let app = await runAgentTurn([
    [toolDeltaStream([{ id: 'call_bad', name: 'definitely_not_a_tool', args: '{}' }])],
    [contentStream('No such rite exists; answering in prose.')]
  ]);
  try {
    const posts = chatPosts(app);
    check(posts.length >= 2, 'model loop continued after the rejection (' + posts.length + ' model calls)');
    const fed = posts.length >= 2
      ? (JSON.parse(posts[1].body || '{}').messages || []).filter((m) => m.role === 'tool')
      : [];
    check(fed.length >= 1, 'a role:"tool" correction was fed back into the model loop');
    check(fed.some((m) => /reject/i.test(String(m.content || '')) && /definitely_not_a_tool/.test(String(m.content || ''))),
      'fed-back correction names the rejected rite + the reason');
    check(!bridgeBodies(app).some((b) => b.name === 'definitely_not_a_tool'),
      'PROOF: unknown tool was never dispatched to the bridge');
    check(!approvalModalOpen(app),
      'PROOF: unknown tool never even reached the approval/dispatch stage');
    check(app.bootErrors.length === 0, 'no uncaught boot errors in the rejection path');
  } finally {
    teardownApp(app);
  }

  /* ============ E2E B: malformed arguments for a REAL tool are not dispatched ============ */
  app = await runAgentTurn([
    [toolDeltaStream([{ id: 'call_badargs', name: 'read_file', args: '{oops' }])],
    [contentStream('Retrying with valid JSON.')]
  ]);
  try {
    check(!bridgeBodies(app).some((b) => b.name === 'read_file'),
      'PROOF: tool call with non-JSON arguments was never dispatched');
    check(!approvalModalOpen(app),
      'PROOF: malformed call never reached the approval/dispatch stage');
    const posts = chatPosts(app);
    const fed = posts.length >= 2 ? (JSON.parse(posts[1].body || '{}').messages || []).filter((m) => m.role === 'tool') : [];
    check(fed.some((m) => /json/i.test(String(m.content || ''))), 'fed-back correction explains the JSON fault');
    check(app.bootErrors.length === 0, 'no uncaught boot errors in the malformed-args path');
  } finally {
    teardownApp(app);
  }

  /* ============ E2E C: valid call IS dispatched (no over-blocking) ============ */
  app = await runAgentTurn([
    [toolDeltaStream([{ id: 'call_ok', name: 'read_file', args: '{"path":"main.py"}' }])],
    [contentStream('Read it.')]
  ]);
  try {
    const dispatched = bridgeBodies(app).filter((b) => b.name === 'read_file');
    check(dispatched.length === 1, 'PROOF: the valid call WAS dispatched exactly once');
    check(dispatched.length === 1 && dispatched[0].arguments && dispatched[0].arguments.path === 'main.py',
      'validated args reach the bridge parsed (path=main.py)');
    check(app.bootErrors.length === 0, 'no uncaught boot errors in the valid path');
  } finally {
    teardownApp(app);
  }

  /* ============ E2E D: per-call granularity in a mixed turn ============ */
  app = await runAgentTurn([
    [toolDeltaStream([
      { id: 'call_mix_ok', name: 'list_dir', args: '{"path":"."}' },
      { id: 'call_mix_bad', name: 'evil_tool', args: '{}' }
    ])],
    [contentStream('Handled.')]
  ]);
  try {
    const bodies = bridgeBodies(app);
    check(!bodies.some((b) => b.name === 'evil_tool'), 'mixed turn: the bogus sibling is never dispatched');
    check(!approvalModalOpen(app), 'mixed turn: the bogus sibling never reached the approval/dispatch stage');
    check(bodies.some((b) => b.name === 'list_dir'), 'mixed turn: the valid sibling IS dispatched');
    check(app.bootErrors.length === 0, 'no uncaught boot errors in the mixed path');
  } finally {
    teardownApp(app);
  }

  process.exit(summary('PHASE 7 STRUCTURED OUTPUT VALIDATOR'));
})().catch((e) => { console.error(e); process.exit(1); });
