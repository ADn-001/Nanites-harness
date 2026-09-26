'use strict';
/*
 * PHASE 9 (security + correctness) — live context pricing + COPY on attachments.
 *
 * Two operator-visible defects from codereview.md pass 3 (both reproduced in the
 * real app BEFORE the fix — this file is written RED-first):
 *
 *  #16 The shipped token estimator is array-blind. `const tok=s=>Math.max(1,
 *      Math.ceil((s||'').length/4))` (index.html:430) receives a multimodal
 *      `content` ARRAY for attachment messages, where `s.length` is the NUMBER OF
 *      PARTS, not characters: a 20 000-char inline attachment was priced at 1 token
 *      (gauge `1/131.1k`, tooltip `~1 TOKENS`), under-reporting `#ctx-pct`/
 *      `#ctx-bar` by orders of magnitude, treating attachments as free in the
 *      `buildMessages()` budget walk, and corrupting maybeAutoCompact/compactChat
 *      retention. An array-aware `CogCore.tok` already existed in appcore.js and
 *      was NEVER called by any shipped path.
 *      Behavior under test: the live `tok` delegates to CogCore.tok — array content
 *      sums text parts (+85 per image_url part); plain strings keep the old
 *      `max(1, ceil(len/4))` math; object args never yield NaN.
 *
 *  #17 COPY on an attachment message wrote `c.messages[i].content` (an array)
 *      straight into `navigator.clipboard.writeText`, which stringifies it to
 *      `[object Object],[object Object]`. It must go through CogCore.contentText
 *      (the same helper msgHTML/autoTitle already use).
 */
const path = require('path');
const { check, summary, clearFails, launchApp, teardownApp } = require('./helpers');
const CogCore = require(path.join(__dirname, '..', '..', 'appcore.js'));

const tick = (ms = 30) => new Promise((r) => setTimeout(r, ms));

const ROUTES = {
  '/v1/models': { status: 200, json: { data: [{ id: 'test-model' }] } },
  '/v1/chat/completions': { status: 200, json: { ok: true } }
};
const SEED = {
  endpoint: 'http://x', model: 'test-model', backend: 'openai', system: '',
  ctxLimit: 131072, compactAt: 70, autoCompact: false,
  workdir: '', agent: false, autoBridge: false, profiles: [], activeProfile: ''
};

const BIG = 'x'.repeat(20000);                                   // ~5000 tokens
const attachContent = () => [{ type: 'text', text: BIG }];

/* Seed exactly one chat (id 'c1') as the active log, with the given messages. */
function seedBlob(messages) {
  const chats = [{ id: 'c1', title: 'TOK TEST', created: 1, updated: 2, messages: messages, summary: '' }];
  return {
    'cogitator.settings': JSON.stringify(SEED),
    'cogitator.chats': JSON.stringify(chats),
    'cogitator.active': 'c1'
  };
}

/* '5.0k' -> 5000, '100' -> 100, otherwise NaN. */
function numFrom(s) {
  const m = /^\s*([\d.]+)\s*(k?)\s*$/.exec(String(s == null ? '' : s));
  if (!m) return NaN;
  const n = parseFloat(m[1]);
  return m[2] === 'k' ? n * 1000 : n;
}

(async () => {
  clearFails();

  /* =============== UNIT: the live `tok` is the array-aware estimator =============== */
  console.log('--- unit: live tok delegates to array-aware CogCore.tok ---');
  const appU = await launchApp({ routes: ROUTES, seed: { 'cogitator.settings': JSON.stringify(SEED) } });
  await tick(60);
  try {
    check(appU.window.eval('typeof tok') === 'function', 'live `tok` helper is reachable in the page scope');
    const T = (expr) => appU.window.eval('tok(' + expr + ')');

    /* --- regression guards: plain strings keep the shipped math --- */
    check(T("'abcd'") === 1, 'GUARD tok("abcd") === 1 (plain-string math unchanged)');
    check(T("'x'.repeat(400)") === 100, 'GUARD tok(400-char string) === 100 (plain-string math unchanged)');
    check(T("''") === 1, 'GUARD tok("") === 1');

    /* --- #16a: multimodal content array is priced by characters, not by part count --- */
    const bigTok = T("[{type:'text',text:'x'.repeat(20000)}]");
    check(typeof bigTok === 'number' && !Number.isNaN(bigTok) && bigTok >= 4000,
      'tok(multimodal: one 20000-char text part) >= 4000 (got ' + bigTok + ')');
    check(bigTok === CogCore.tok(attachContent()),
      'live tok matches CogCore.tok on a 20000-char attachment (got ' + bigTok + ' / ' + CogCore.tok(attachContent()) + ')');

    const imgTok = T("[{type:'text',text:'abcd'},{type:'image_url',image_url:{url:'data:image/png;base64,eHg='}}]");
    check(imgTok === CogCore.tok([{ type: 'text', text: 'abcd' }, { type: 'image_url', image_url: { url: 'data:image/png;base64,eHg=' } }]),
      'live tok prices an image_url part like CogCore.tok (+85) (got ' + imgTok + ')');

    /* --- #16b: never NaN — parsed tool-call args objects, undefined, null --- */
    const cases = ["{path:'a',limit:10}", '[1,2,3,4,5,6,7,8]', 'undefined', 'null', '42'];
    for (const expr of cases) {
      const v = T(expr);
      check(typeof v === 'number' && !Number.isNaN(v) && v >= 1,
        'tok(' + expr + ') is a finite number >= 1, not NaN (got ' + v + ')');
    }
    /* through the real consumer: ctxTokens prices m.toolCalls[].args, which can be
       an already-parsed OBJECT (the old helper returned NaN there). */
    const withObjArgs = appU.window.ctxTokens({
      messages: [{ role: 'assistant', content: 'abcd', toolCalls: [{ name: 'read_file', args: { path: 'a' } }] }]
    });
    check(typeof withObjArgs === 'number' && !Number.isNaN(withObjArgs) && withObjArgs === 5,
      'ctxTokens is a finite number for a message with OBJECT tool args (got ' + withObjArgs + ', want 5)');
  } finally { teardownApp(appU); }

  /* =============== E2E: the context gauge prices the attachment =============== */
  console.log('--- e2e: #ctx-pct / #ctx-bar price a 20 000-char attachment ---');
  const appG = await launchApp({
    routes: ROUTES,
    seed: seedBlob([{ role: 'user', content: attachContent(), ts: Date.now() }])
  });
  await tick(80);
  try {
    check(!!appG.window.active() && appG.window.active().messages.length === 1,
      'seeded chat with one attachment message is active after boot');

    const used = appG.window.ctxUsage();
    check(typeof used === 'number' && !Number.isNaN(used) && used >= 4000,
      'ctxUsage() for a 20 000-char attachment >= 4000 (got ' + used + ')');
    check(used === CogCore.tok(attachContent()),
      'numeric context usage equals the array-aware value ' + CogCore.tok(attachContent()) + ' (got ' + used + ')');

    const pct = appG.document.getElementById('ctx-pct').textContent;
    check(/\/131\.1k$/.test(pct), '#ctx-pct still quotes the 131.1k limit (got "' + pct + '")');
    check(numFrom(String(pct).split('/')[0]) >= 4000,
      '#ctx-pct numerator is in the right order of magnitude (>= 4000) (got "' + pct + '")');
    check(!/^1\//.test(pct), '#ctx-pct is NOT "1/131.1k" — the attachment is not priced at ~1 token (got "' + pct + '")');

    const title = appG.document.getElementById('ctx-bar').title;
    const tm = /~\s*([\d.]+)\s*TOKENS/i.exec(String(title));
    check(!!tm && parseFloat(tm[1]) >= 4000,
      '#ctx-bar tooltip carries the same order of magnitude (got "' + title + '")');

    const fillPct = parseFloat(appG.document.getElementById('ctx-fill').style.width);
    check(!Number.isNaN(fillPct) && fillPct > 3,
      '#ctx-fill width reflects ~5000/131072 tokens (>3%) (got ' + appG.document.getElementById('ctx-fill').style.width + ')');
  } finally { teardownApp(appG); }

  /* =============== REGRESSION: a plain-string chat's gauge is unchanged =============== */
  console.log('--- regression: plain-string chat gauge untouched by the refactor ---');
  const appS = await launchApp({
    routes: ROUTES,
    seed: seedBlob([{ role: 'user', content: 'y'.repeat(400), ts: Date.now() }])
  });
  await tick(80);
  try {
    const used = appS.window.ctxUsage();
    check(used === 100, 'plain-string chat: ctxUsage() === 100 (400 chars / 4) (got ' + used + ')');
    check(appS.document.getElementById('ctx-pct').textContent === '100/131.1k',
      'plain-string chat: #ctx-pct reads "100/131.1k" (got "' + appS.document.getElementById('ctx-pct').textContent + '")');
  } finally { teardownApp(appS); }

  /* =============== E2E: COPY on an attachment message copies its TEXT =============== */
  console.log('--- e2e: COPY on a multimodal message copies text, not "[object Object]" ---');
  const COPY_CONTENT = [{ type: 'text', text: 'aaaa' }, { type: 'text', text: 'bbbb' }];
  const appC = await launchApp({
    routes: ROUTES,
    seed: seedBlob([{ role: 'user', content: [{ type: 'text', text: 'aaaa' }, { type: 'text', text: 'bbbb' }], ts: Date.now() }])
  });
  await tick(80);
  try {
    const cap = { writes: [], writeText(t) { this.writes.push(String(t)); } };
    Object.defineProperty(appC.window.navigator, 'clipboard', { value: cap, configurable: true });

    const ctrl = appC.document.querySelector('.acts span[data-a="copy"]');
    check(!!ctrl, 'the rendered attachment message exposes a COPY control');
    if (ctrl) { ctrl.click(); await tick(20); }

    check(cap.writes.length === 1, 'clicking COPY reached navigator.clipboard.writeText exactly once (got ' + cap.writes.length + ')');
    const got = cap.writes.length ? cap.writes[0] : null;
    check(got !== null && got.indexOf('[object Object]') === -1,
      'COPY did not write "[object Object]" (got ' + JSON.stringify(got) + ')');
    check(got !== null && got.includes('aaaa') && got.includes('bbbb'),
      'COPY wrote the message TEXT (contains "aaaa" and "bbbb") (got ' + JSON.stringify(got) + ')');
    check(got === CogCore.contentText(COPY_CONTENT),
      'COPY output equals CogCore.contentText(content) (got ' + JSON.stringify(got) + ')');

    /* the real path, invoked directly (what the rendered control calls) */
    cap.writes.length = 0;
    check(typeof appC.window.msgAction === 'function', 'msgAction is reachable from the harness');
    appC.window.msgAction('copy', 0);
    await tick(10);
    check(cap.writes.length === 1 && cap.writes[0].indexOf('[object Object]') === -1 && cap.writes[0].includes('aaaa'),
      'msgAction("copy", 0) writes the attachment TEXT (got ' + JSON.stringify(cap.writes[0]) + ')');
  } finally { teardownApp(appC); }

  process.exit(summary('PHASE 9 TOK + COPY'));
})().catch((e) => { console.error('PHASE9 CATCH:', e); process.exit(1); });
