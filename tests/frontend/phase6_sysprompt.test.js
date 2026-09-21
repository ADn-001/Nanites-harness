'use strict';
/*
 * PHASE 6 E2E — Agentic system prompt redesign + structured-output contract.
 *
 * Behavior under test (written BEFORE implementation — TDD RED):
 *  - The agent orient prompt derives its tool roster from the REAL tool set
 *    (read_file, write_file, list_dir, grep, git, run_command). It must name ALL
 *    six real tools and name NONE of the phantom ones the old prompt advertised
 *    (shell_exec, clipboard access).
 *  - The prompt carries an explicit structured tool-call contract: the exact JSON
 *    function-call shape with inline escaped-JSON `arguments`, a "one tool call per
 *    turn, observe the result, then continue or stop" rule.
 *  - The workdir-jail / project-relative / header-absolute-refusal rules are kept,
 *    and the prompt remains short enough for small-context models.
 *  - run_command is flagged as requiring the bridge --allow-exec flag.
 *  - End-to-end: an agent-mode send() POSTs /v1/chat/completions whose system orient
 *    prompt reflects the live TOOL_SCHEMAS, and whose body advertises body.tools
 *    (six schemas) + tool_choice:'auto'.
 */
const path = require('path');
const { check, summary, clearFails, launchApp, teardownApp } = require('./helpers');
const CogCore = require(path.join(__dirname, '..', '..', 'appcore.js'));

const tick = (ms = 30) => new Promise((r) => setTimeout(r, ms));

const REAL_TOOLS = ['read_file', 'write_file', 'list_dir', 'grep', 'git', 'run_command'];
const PHANTOM_TOOLS = ['shell_exec', 'clipboard'];

const ROUTES = {
  '/v1/models': { status: 200, json: { data: [{ id: 'test-model' }] } },
  '/tools/execute': { status: 200, json: { ok: true, result: 'd src\nf hello.py\nf main.py' } },
  '/v1/chat/completions': { status: 200, json: { ok: true } }
};

(async () => {
  clearFails();

  /* ============ UNIT: buildAgentSystemPrompt reflects real tool roster ============ */
  console.log('--- unit: buildAgentSystemPrompt tool roster + contract ---');
  const buildAgent = typeof CogCore.buildAgentSystemPrompt === 'function';
  check(buildAgent, 'CogCore.buildAgentSystemPrompt exists');
  if (buildAgent) {
    // Pass a tool roster evocative of the live TOOL_SCHEMAS shape.
    const toolsArg = REAL_TOOLS.map((name) => ({ type: 'function', function: { name: name } }));
    const ap = CogCore.buildAgentSystemPrompt({ workdir: '/proj/omega', tools: toolsArg });
    check(typeof ap === 'string' && ap.length > 0, 'agent prompt is a non-empty string');
    check(ap.length < 2200, 'agent prompt stays short (small-ctx friendly)');
    for (const t of REAL_TOOLS) check(ap.includes(t), 'prompt names real tool "' + t + '"');
    for (const p of PHANTOM_TOOLS) check(!ap.includes(p), 'prompt does NOT name phantom tool "' + p + '"');
    check(ap.includes('run_command') && /allow-exec|--allow_exec|allow_exec/i.test(ap),
      'prompt notes run_command requires bridge --allow-exec');
    // structured tool-call contract
    check(ap.includes('"type":"function"') || ap.includes("'type':'function'") || /type.:.{1,3}function/i.test(ap),
      'prompt shows the function-call JSON shape');
    check(ap.includes('arguments'), 'prompt references the inline JSON `arguments` field');
    check(/"one tool call per turn|one call per turn|exactly one tool call/i.test(ap),
      'prompt enforces exactly one tool call per turn');
    check(/observe|await|wait for|watch/i.test(ap) && /result/i.test(ap),
      'prompt instructs to observe the returned result');
    check(/continue|keep going|\. stop/i.test(ap), 'prompt says to continue or stop after observing');
    // workdir-jail + relative-path rules preserved
    check(/relative|project-relative/i.test(ap), 'prompt still enforces project-RELATIVE paths');
    check(/absolute|refus|never/i.test(ap), 'prompt still refuses host-absolute paths');
    check(ap.includes('/proj/omega'), 'prompt names the bound working directory');
    // backward-compat: still works with no `tools` argument
    const apDefault = CogCore.buildAgentSystemPrompt({ workdir: '/x' });
    check(typeof apDefault === 'string' && apDefault.length > 0, 'buildAgentSystemPrompt works without a tools arg');
  }

  /* ============ E2E: agent send -> orient prompt + body.tools match live schema ============ */
  console.log('--- e2e: agent payload orient prompt + tools reflect TOOL_SCHEMAS ---');
  const seedAgent = {
    endpoint: 'http://x', model: 'test-model', backend: 'openai', system: '',
    workdir: '/proj/omega', agent: true, autoBridge: false, profiles: [], activeProfile: ''
  };
  const app = await launchApp({ routes: ROUTES, seed: { 'cogitator.settings': JSON.stringify(seedAgent) } });
  await tick(60);
  const $a = (id) => app.document.getElementById(id);
  try {
    $a('ta').value = 'summarize the workdir';
    $a('send-btn').click();
    await tick(160);
    const llm = app.events.filter((e) => e.method === 'POST' && e.url.includes('chat/completions'));
    check(llm.length >= 1, 'an agent /v1/chat/completions POST was emitted');
    if (llm.length) {
      const body = JSON.parse(llm[0].body || '{}');
      // body.tools must be the live six-schema list
      check(Array.isArray(body.tools) && body.tools.length >= 6, 'agent payload advertises >= 6 tool schemas');
      if (Array.isArray(body.tools) && body.tools.length) {
        for (const t of REAL_TOOLS) check(body.tools.some((s) => s.function && s.function.name === t),
          'TOOL_SCHEMAS includes "' + t + '"');
      }
      check(body.tool_choice === 'auto', 'agent payload sets tool_choice:"auto"');
      const msgs = (body && body.messages) || [];
      const orient = msgs.find((m) => m.role === 'system' && m.content && /You are COGITATOR|autonomous coding agent/i.test(m.content));
      check(!!orient, 'agent payload includes the orient system prompt');
      if (orient) {
        for (const t of REAL_TOOLS) check(orient.content.includes(t), 'orient prompt names real tool "' + t + '"');
        for (const p of PHANTOM_TOOLS) check(!orient.content.includes(p),
          'orient prompt does NOT name phantom tool "' + p + '"');
        check(/type.:.{1,3}function|"type":"function"/i.test(orient.content), 'orient prompt has the call JSON shape');
        check(/one tool call per turn|exactly one tool call/i.test(orient.content),
          'orient prompt enforces one tool call per turn');
      }
      const ctxMsg = msgs.find((m) => m.content && m.content.includes('[WORKDIR CONTEXT]'));
      check(!!ctxMsg, 'agent payload still includes [WORKDIR CONTEXT] block');
      const relMsg = msgs.find((m) => m.role === 'system' && m.content && /relative|project-relative/i.test(m.content));
      check(!!relMsg, 'agent payload keeps the relative-path rule');
    }
  } finally {
    teardownApp(app);
  }

  process.exit(summary('PHASE 6 SYS PROMPT REDESIGN + STRUCTURED OUTPUT CONTRACT'));
})().catch((e) => { console.error(e); process.exit(1); });