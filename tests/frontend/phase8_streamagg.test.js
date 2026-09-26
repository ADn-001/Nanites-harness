'use strict';
/*
 * PHASE 8 E2E — `chat.end` aggregate stream shape: no content duplication.
 *
 * Written BEFORE the fix (TDD RED). codereview.md issue #1:
 *   index.html processStreamObject(), the aggregated `j.type==='chat.end'` branch,
 *   did  `if(o.type==='message') acc.content += o.content || acc.content;`
 * A `message` output object with EMPTY content therefore evaluated the RHS to the
 * accumulator itself and appended the accumulated buffer to itself, duplicating the
 * whole response so far. Correct: `o.content || ''`.
 *
 * This suite drives the REAL send path end-to-end in jsdom (send() ->
 * stream() -> callModel() -> callOpenAI() -> pumpSSE() -> processStreamObject())
 * with a scripted SSE body carrying the aggregate shape, then asserts the
 * user-visible rendered body AND the stored message content.
 */
const { check, summary, clearFails, launchApp, teardownApp } = require('./helpers');

const tick = (ms = 30) => new Promise((r) => setTimeout(r, ms));

/* Response-like object whose `body` is a fake reader yielding SSE frames. */
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

/* One non-streaming-capable route returning the scripted SSE frames. */
function chatRoute(events) {
  return () => ({ ok: true, status: 200, json: async () => ({}), text: async () => '', body: sseBody(events) });
}

/* Aggregate shape emitted by llama.cpp-class servers / some LM-Studio modes. */
function aggregate(output) { return { type: 'chat.end', result: { output } }; }
const msg = (content) => ({ type: 'message', content });
const reasoning = (content) => ({ type: 'reasoning', content });
const toolCall = (name, args) => ({ type: 'message', tool_calls: [{ index: 0, id: 'call_1', function: { name, arguments: args } }] });

const ROUTES = (events) => ({
  '/v1/models': { status: 200, json: { data: [{ id: 'test-model' }] } },
  '/v1/chat/completions': chatRoute(events)
});

/* Non-agent mode -> plain callModel path (still goes through processStreamObject). */
const SEED = {
  endpoint: 'http://x', model: 'test-model', backend: 'openai', system: '',
  workdir: '', agent: false, autoBridge: false, autoApproveRead: true,
  profiles: [], activeProfile: ''
};

async function runTurn(events, text = 'hail the machine-spirit') {
  const app = await launchApp({
    routes: ROUTES(events),
    seed: { 'cogitator.settings': JSON.stringify(SEED) }
  });
  await tick(60);
  app.window.TextDecoder = class { constructor() {} decode(v) { return v == null ? '' : String(v); } };
  const $a = (id) => app.document.getElementById(id);
  $a('ta').value = text;
  $a('send-btn').click();
  await tick(220);
  return app;
}

const lastAssistant = (app) => {
  const c = app.window.active();
  const m = (c && c.messages) || [];
  for (let i = m.length - 1; i >= 0; i--) if (m[i].role === 'assistant') return m[i];
  return null;
};
const renderedBody = (app) => {
  const c = app.window.active();
  const i = ((c && c.messages) || []).length - 1;
  const el = app.document.getElementById('body-' + i);
  return el ? el.textContent : null;
};

(async () => {
  clearFails();

  /* ===== A. THE REPORTED DEFECT: content-less message self-appends the buffer ===== */
  console.log('--- A: content-less message in chat.end must NOT duplicate content ---');
  let app = await runTurn([aggregate([msg('Hello world'), msg('')])]);
  try {
    const stored = lastAssistant(app);
    check(!!stored, 'aggregate turn produced an assistant message');
    check(stored && stored.content === 'Hello world',
      'stored content is exactly "Hello world" (got ' + JSON.stringify(stored && stored.content) + ')');
    check(stored && stored.content.indexOf('Hello worldHello world') === -1,
      'PROOF: content was not duplicated by the empty message');
    check(renderedBody(app) === 'Hello world',
      'rendered transcript body is exactly "Hello world" (got ' + JSON.stringify(renderedBody(app)) + ')');
    check(app.bootErrors.length === 0, 'no uncaught boot errors in case A');
  } finally {
    teardownApp(app);
  }

  /* ===== B. Content-less message BEFORE the content arrives ===== */
  console.log('--- B: leading content-less message must not poison the aggregate ---');
  app = await runTurn([aggregate([msg(''), msg('Only answer')])]);
  try {
    const stored = lastAssistant(app);
    check(stored && stored.content === 'Only answer',
      'leading empty message leaves content exactly "Only answer" (got ' + JSON.stringify(stored && stored.content) + ')');
    check(renderedBody(app) === 'Only answer',
      'rendered body is exactly "Only answer" (got ' + JSON.stringify(renderedBody(app)) + ')');
    check(app.bootErrors.length === 0, 'no uncaught boot errors in case B');
  } finally {
    teardownApp(app);
  }

  /* ===== C. Repeated content-less messages: no compounding duplication ===== */
  console.log('--- C: several content-less messages scale linearly, not exponentially ---');
  app = await runTurn([aggregate([msg('Alpha'), msg(''), msg(''), msg('')])]);
  try {
    const stored = lastAssistant(app);
    check(stored && stored.content === 'Alpha',
      'three empty messages leave content exactly "Alpha" (got ' + JSON.stringify(stored && stored.content) + ')');
    check(app.bootErrors.length === 0, 'no uncaught boot errors in case C');
  } finally {
    teardownApp(app);
  }

  /* ===== D. Reasoning-only / tool-call-only messages carry no content ===== */
  console.log('--- D: reasoning + tool-call objects must not inject content ---');
  app = await runTurn([aggregate([
    reasoning('pondering'),
    reasoning(''),
    toolCall('list_dir', '{"path":"."}'),
    msg('Done.')
  ])]);
  try {
    const stored = lastAssistant(app);
    check(stored && stored.content === 'Done.',
      'reasoning/tool-call objects leave content exactly "Done." (got ' + JSON.stringify(stored && stored.content) + ')');
    check(stored && stored.thinking === 'pondering',
      'reasoning still accumulates into the cogitation record');
    check(app.bootErrors.length === 0, 'no uncaught boot errors in case D');
  } finally {
    teardownApp(app);
  }

  /* ===== E. Regression guard: the per-delta SSE path is unaffected ===== */
  console.log('--- E: per-delta streaming path still concatenates normally ---');
  app = await runTurn([
    { choices: [{ delta: { content: 'Lord ' } }] },
    { choices: [{ delta: { content: 'of ' } }] },
    { choices: [{ delta: { content: 'Machines' } }] }
  ]);
  try {
    const stored = lastAssistant(app);
    check(stored && stored.content === 'Lord of Machines',
      'delta stream concatenates in order (got ' + JSON.stringify(stored && stored.content) + ')');
    check(renderedBody(app) === 'Lord of Machines',
      'rendered body reflects the delta stream');
    check(app.bootErrors.length === 0, 'no uncaught boot errors in case E');
  } finally {
    teardownApp(app);
  }

  process.exit(summary('PHASE 8 CHAT.END AGGREGATE'));
})().catch((e) => { console.error(e); process.exit(1); });
