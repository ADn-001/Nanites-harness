'use strict';
/*
 * PHASE 2 E2E — Agent system prompt + WORKDIR CONTEXT injection.
 *
 * Behavior under test (written BEFORE implementation — TDD RED):
 *  - CogCore exposes buildAgentSystemPrompt(opts) and buildSystemMessages(opts).
 *  - buildAgentSystemPrompt is short (small-ctx friendly), names the bound workdir,
 *    contains a project-RELATIVE path rule, and refuses host-absolute paths.
 *  - buildSystemMessages injects a [WORKDIR CONTEXT] block carrying the workdir path
 *    and its listing, AND a prior summary becomes a [PRIOR COMMUNION] block.
 *  - In agent mode, an agent send() POSTs /v1/chat/completions whose `messages` array
 *    contains the WORKDIR CONTEXT block + the orient prompt, and advertises `tools`.
 *  - In non-agent mode, the user canticle is preserved verbatim, no WORKDIR CONTEXT block,
 *    no orient prompt, and no `tools` are sent.
 */
const path = require('path');
const { check, summary, clearFails, launchApp, teardownApp } = require('./helpers');
const CogCore = require(path.join(__dirname, '..', '..', 'appcore.js'));

const tick = (ms = 30) => new Promise((r) => setTimeout(r, ms));

const BRIDGE_LISTING = 'd src\nf hello.py\nf main.py';

const ROUTES = {
  '/v1/models': { status: 200, json: { data: [{ id: 'test-model' }] } },
  '/tools/execute': { status: 200, json: { ok: true, result: BRIDGE_LISTING } },
  '/v1/chat/completions': { status: 200, json: { ok: true } } // body:null -> NO BODY (logged before throw)
};

(async () => {
  clearFails();

  /* ====================== UNIT: buildAgentSystemPrompt ====================== */
  console.log('--- unit: buildAgentSystemPrompt ---');
  const buildAgent = typeof CogCore.buildAgentSystemPrompt === 'function';
  check(buildAgent, 'CogCore.buildAgentSystemPrompt exists');
  if (buildAgent) {
    const ap = CogCore.buildAgentSystemPrompt({ workdir: '/proj/alpha' });
    check(typeof ap === 'string' && ap.length > 0, 'agent prompt is a non-empty string');
    check(ap.length < 2000, 'agent prompt is short (fits small ctx models)');
    check(ap.includes('WORKDIR') && ap.includes('/proj/alpha'), 'prompt names the bound working directory');
    check(/relative|project-relative/i.test(ap), 'prompt enforces project-RELATIVE tool paths');
    check(/absolute|refus|never/i.test(ap), 'prompt refuses host-absolute paths');
    const apDefault = CogCore.buildAgentSystemPrompt();
    check(typeof apDefault === 'string' && apDefault.length > 0, 'buildAgentSystemPrompt() works with no opts');
  }

  /* ====================== UNIT: buildSystemMessages ====================== */
  console.log('--- unit: buildSystemMessages ---');
  const buildSys = typeof CogCore.buildSystemMessages === 'function';
  check(buildSys, 'CogCore.buildSystemMessages exists');
  if (buildSys) {
    const agentPrompt = buildAgent ? CogCore.buildAgentSystemPrompt({ workdir: '/proj/alpha' }) : 'agent prompt';
    const sys = CogCore.buildSystemMessages({
      system: agentPrompt,
      summary: 'the user wanted a thing',
      workdirCtx: { path: '/proj/alpha', listing: BRIDGE_LISTING }
    });
    check(Array.isArray(sys) && sys.length >= 2, 'agent mode produces multiple system messages');
    const ctx = sys.find((m) => m.content && m.content.includes('[WORKDIR CONTEXT]'));
    check(!!ctx, 'WORKDIR CONTEXT block is injected for agent mode');
    if (ctx) {
      check(ctx.content.includes('/proj/alpha'), 'WORKDIR CONTEXT carries the workdir path');
      check(ctx.content.includes('hello.py'), 'WORKDIR CONTEXT carries the listing entry');
    }
    const sys2 = CogCore.buildSystemMessages({ system: 'MY CANTICLE', summary: null, workdirCtx: null });
    check(sys2.length === 1 && sys2[0].content === 'MY CANTICLE', 'non-agent keeps custom canticle, no workdir block');
    const sys3 = CogCore.buildSystemMessages({ system: 'MY CANTICLE', summary: 'compressed record', workdirCtx: null });
    check(sys3.some((m) => m.content && m.content.includes('MY CANTICLE')), 'non-agent keeps canticle with summary');
    check(sys3.some((m) => m.content && m.content.includes('[PRIOR COMMUNION')), 'summary becomes [PRIOR COMMUNION] block');
    check(!sys3.some((m) => m.content && m.content.includes('[WORKDIR CONTEXT]')), 'non-agent emits no WORKDIR CONTEXT');
    const sys4 = CogCore.buildSystemMessages({ system: agentPrompt, summary: null, workdirCtx: { path: '/proj/alpha', listing: '' } });
    const ctx4 = sys4.find((m) => m.content && m.content.includes('[WORKDIR CONTEXT]'));
    check(!!ctx4 && ctx4.content.includes('/proj/alpha'), 'empty listing still injects WORKDIR CONTEXT with path');
  }

  /* ============== E2E: agent send -> payload carries WORKDIR CONTEXT ============== */
  console.log('--- e2e: agent payload has WORKDIR CONTEXT + tools ---');
  const seedAgent = {
    endpoint: 'http://x', model: 'test-model', backend: 'openai', system: '',
    workdir: '/proj/alpha', agent: true, autoBridge: false, profiles: [], activeProfile: ''
  };
  const app = await launchApp({ routes: ROUTES, seed: { 'cogitator.settings': JSON.stringify(seedAgent) } });
  await tick(60);
  const $a = (id) => app.document.getElementById(id);
  try {
    $a('ta').value = 'list files in the workdir please';
    $a('send-btn').click();
    await tick(140);
    const toks = app.events.filter((e) => e.method === 'POST' && e.url.includes('tools/execute'));
    check(toks.length >= 1, 'getWorkdirListing calls the bridge /tools/execute');
    if (toks.length) {
      const tb = JSON.parse(toks[0].body || '{}');
      check(tb.name === 'list_dir' && tb.arguments && tb.arguments.path === '.', 'listing request uses list_dir on "."');
    }
    const llm = app.events.filter((e) => e.method === 'POST' && e.url.includes('chat/completions'));
    check(llm.length >= 1, 'an agent /v1/chat/completions POST was emitted');
    if (llm.length) {
      const body = JSON.parse(llm[0].body || '{}');
      const msgs = (body && body.messages) || [];
      const ctxMsg = msgs.find((m) => m.role === 'system' && m.content && m.content.includes('[WORKDIR CONTEXT]'));
      check(!!ctxMsg, 'agent payload includes [WORKDIR CONTEXT] system block');
      if (ctxMsg) {
        check(ctxMsg.content.includes('/proj/alpha'), 'WORKDIR CONTEXT in payload carries workdir path');
        check(ctxMsg.content.includes('hello.py'), 'WORKDIR CONTEXT in payload carries listing');
      }
      const agentMsg = msgs.find((m) => m.role === 'system' && m.content && /relative|project-relative/i.test(m.content));
      check(!!agentMsg, 'agent payload includes the orient prompt with relative-path rule');
      check(!!body.tools && body.tools.length > 0, 'agent payload advertises tool schema (agent mode)');
    }
  } finally {
    teardownApp(app);
  }

  /* ============== E2E: non-agent send -> canticle kept, no workdir ============== */
  console.log('--- e2e: non-agent payload keeps canticle, omits workdir block ---');
  const seedPlain = {
    endpoint: 'http://x', model: 'test-model', backend: 'openai', system: 'MY CANTICLE',
    workdir: '/proj/alpha', agent: false, autoBridge: false, profiles: [], activeProfile: ''
  };
  const app2 = await launchApp({ routes: ROUTES, seed: { 'cogitator.settings': JSON.stringify(seedPlain) } });
  await tick(60);
  const $b = (id) => app2.document.getElementById(id);
  try {
    $b('ta').value = 'hello';
    $b('send-btn').click();
    await tick(140);
    const llm = app2.events.filter((e) => e.method === 'POST' && e.url.includes('chat/completions'));
    check(llm.length >= 1, 'a non-agent /v1/chat/completions POST was emitted');
    if (llm.length) {
      const body = JSON.parse(llm[0].body || '{}');
      const msgs = (body && body.messages) || [];
      check(msgs.some((m) => m.role === 'system' && m.content && m.content.includes('MY CANTICLE')), 'non-agent payload keeps custom canticle');
      check(!msgs.some((m) => m.content && m.content.includes('[WORKDIR CONTEXT]')), 'non-agent payload has NO WORKDIR CONTEXT block');
      check(!msgs.some((m) => m.content && /You are an autonomous coding agent/i.test(m.content)), 'non-agent payload does not inject the agent orient-prompt');
      check(!body.tools, 'non-agent payload does not advertise tool schema');
    }
  } finally {
    teardownApp(app2);
  }

  process.exit(summary('PHASE 2 SYS PROMPT + WORKDIR CONTEXT'));
})().catch((e) => { console.error(e); process.exit(1); });
