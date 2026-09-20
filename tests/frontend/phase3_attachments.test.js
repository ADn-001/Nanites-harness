'use strict';
/*
 * PHASE 3 E2E — Attachments (files / folders / images / from workdir).
 *
 * Behavior under test (written BEFORE implementation — TDD RED):
 *  - CogCore.isImageName / looksBinary / capText helpers exist.
 *  - CogCore.buildAttachmentContent: no files -> plain string; with files ->
 *    multimodal array [ {type:'text',text:prompt}, {type:'text',text:<fenced block>}, ...,
 *    {type:'image_url',image_url:{url:dataURL}} ]; binary text files skipped;
 *    oversized text capped.
 *  - Composer ATTACH adds chips above the textarea; a chip can be removed (x).
 *  - Sending with attachments POSTs /v1/chat/completions whose user message
 *    content is the multimodal array (fenced text block / image_url part).
 *  - From-workdir: picker lists bridge list_dir entries; picking one reads it via
 *    read_file and attaches; sent payload carries its content.
 */
const path = require('path');
const { check, summary, clearFails, launchApp, teardownApp } = require('./helpers');
const CogCore = require(path.join(__dirname, '..', '..', 'appcore.js'));

const tick = (ms = 30) => new Promise((r) => setTimeout(r, ms));

/* /tools/execute dispatches by tool name so the from-workdir flow works.
   NOTE: function routes must return a Response-LIKE object (json is a method),
   unlike object routes which helpers.js wraps for us. */
const WDLIST = 'f hello.txt\nf notes.md';
function toolRoute() {
  return ({ body }) => {
    let b = {};
    try { b = JSON.parse(String(body || '{}')); } catch (e) { b = {}; }
    let payload = { ok: false, error: 'unknown tool' };
    if (b.name === 'list_dir') payload = { ok: true, result: WDLIST };
    if (b.name === 'read_file') payload = { ok: true, result: 'WD FILE CONTENT hello.txt' };
    return { ok: payload.ok, status: payload.ok ? 200 : 404, json: async () => payload };
  };
}
const ROUTES = {
  '/v1/models': { status: 200, json: { data: [{ id: 'test-model' }] } },
  '/tools/execute': toolRoute(),
  '/v1/chat/completions': { status: 200, json: { ok: true } }
};
const SEED = {
  endpoint: 'http://x', model: 'test-model', backend: 'openai', system: '',
  workdir: '/proj/alpha', agent: false, autoBridge: false, profiles: [], activeProfile: ''
};

(async () => {
  clearFails();

  /* ====================== UNIT: attachment helpers ====================== */
  console.log('--- unit: isImageName / looksBinary / capText ---');
  check(typeof CogCore.isImageName === 'function', 'CogCore.isImageName exists');
  if (typeof CogCore.isImageName === 'function') {
    check(CogCore.isImageName('photo.png') === true, 'isImageName true for .png');
    check(CogCore.isImageName('a.JPG') === true, 'isImageName case-insensitive');
    check(CogCore.isImageName('code.js') === false, 'isImageName false for .js');
  }
  check(typeof CogCore.looksBinary === 'function', 'CogCore.looksBinary exists');
  if (typeof CogCore.looksBinary === 'function') {
    check(CogCore.looksBinary('plain text') === false, 'looksBinary false for text');
    check(CogCore.looksBinary('ab\x00cd') === true, 'looksBinary true on NUL byte');
  }
  check(typeof CogCore.capText === 'function', 'CogCore.capText exists');
  if (typeof CogCore.capText === 'function') {
    check(CogCore.capText('hello', 100) === 'hello', 'capText leaves short text intact');
    const capped = CogCore.capText('x'.repeat(50), 10);
    check(capped.length <= 100 && /TRUNCATED/i.test(capped), 'capText truncates and marks');
  }

  /* ====================== UNIT: buildAttachmentContent ====================== */
  console.log('--- unit: buildAttachmentContent ---');
  check(typeof CogCore.buildAttachmentContent === 'function', 'CogCore.buildAttachmentContent exists');
  if (typeof CogCore.buildAttachmentContent === 'function') {
    const plain = CogCore.buildAttachmentContent({ text: 'no files', files: [] });
    check(plain === 'no files', 'no files -> plain string unchanged');

    const arr = CogCore.buildAttachmentContent({
      text: 'look at this',
      files: [
        { name: 'src/app.py', kind: 'text', data: 'print(1)' },
        { name: 'bin.dat', kind: 'text', data: '\x00\x01\x02' },
        { name: 'pic.png', kind: 'image', data: 'data:image/png;base64,eHg=' }
      ]
    });
    check(Array.isArray(arr), 'with files -> multimodal array');
    if (Array.isArray(arr)) {
      const textParts = arr.filter((p) => p.type === 'text');
      const prompt = arr[0];
      check(prompt.type === 'text' && prompt.text === 'look at this', 'first part is the user prompt text');
      const block = textParts.find((p) => p.text && p.text.includes('src/app.py'));
      check(!!block && block.text.includes('print(1)'), 'text file inlined as labelled block');
      check(block.text.includes('```'), 'text block is fenced');
      check(!arr.some((p) => p.text && p.text.includes('bin.dat')), 'binary text file skipped (not inlined)');
      const img = arr.find((p) => p.type === 'image_url');
      check(!!img && img.image_url && img.image_url.url === 'data:image/png;base64,eHg=', 'image becomes image_url part with data URL');
    }
    const capped = CogCore.buildAttachmentContent({
      text: 'big', cap: 8,
      files: [{ name: 'big.js', kind: 'text', data: 'z'.repeat(100) }]
    });
    const bigBlock = Array.isArray(capped) ? capped.find((p) => p.text && p.text.includes('big.js')) : null;
    check(!!bigBlock && /TRUNCATED/i.test(bigBlock.text), 'oversized text attachment is capped');
  }

  /* ====================== E2E: attach text file -> payload ====================== */
  console.log('--- e2e: attach text file -> fenced block in payload ---');
  let app = await launchApp({ routes: ROUTES, seed: { 'cogitator.settings': JSON.stringify(SEED) } });
  await tick(80);
  let $a = (id) => app.document.getElementById(id);
  try {
    check(typeof app.window.attachFiles === 'function', 'attachFiles wiring present (window global)');
    if (typeof app.window.attachFiles === 'function') {
      const ff = new app.window.File(['def hello():\n  pass'], 'greet.py', { type: 'text/python' });
      app.window.attachFiles([ff]);
      await tick(90);
      const chips = app.document.querySelectorAll('#attach-chips .chip');
      check(chips.length === 1, 'one attachment chip rendered after attach');
      check(/greet\.py/.test(chips[0].textContent), 'chip labels the file name');
      // remove chip
      const rm = chips[0].querySelector('.chip-x');
      check(!!rm, 'chip has a remove (x) control');
      if (rm) { rm.click(); await tick(20); }
      check(app.document.querySelectorAll('#attach-chips .chip').length === 0, 'remove empties the chips row');
    }
  } finally { teardownApp(app); }
  if (typeof app.window.attachFiles === 'function') {
    app = await launchApp({ routes: ROUTES, seed: { 'cogitator.settings': JSON.stringify(SEED) } });
    await tick(80);
    $a = (id) => app.document.getElementById(id);
    try {
      const ff = new app.window.File(['def hello():\n  pass'], 'greet.py', { type: 'text/python' });
      app.window.attachFiles([ff]);
      await tick(90);
      $a('ta').value = 'review this file';
      $a('send-btn').click();
      await tick(160);
      const llm = app.events.filter((e) => e.method === 'POST' && e.url.includes('chat/completions'));
      check(llm.length >= 1, 'a /v1/chat/completions POST was emitted with attachment');
      if (llm.length) {
        const body = JSON.parse(llm[0].body || '{}');
        const user = (body.messages || []).find((m) => m.role === 'user');
        check(user && Array.isArray(user.content), 'user message content is an array (multimodal)');
        if (user && Array.isArray(user.content)) {
          const block = user.content.find((p) => p.type === 'text' && p.text && p.text.includes('greet.py'));
          check(!!block && block.text.includes('def hello()'), 'fenced text block with file content in payload');
          check(user.content[0].type === 'text' && /review this file/.test(user.content[0].text), 'prompt text kept as first text part');
        }
      }
    } finally { teardownApp(app); }
  }

  /* ====================== E2E: attach image -> image_url part ====================== */
  console.log('--- e2e: attach image -> image_url data URL in payload ---');
  const appImg = await launchApp({ routes: ROUTES, seed: { 'cogitator.settings': JSON.stringify(SEED) } });
  await tick(80);
  const $ai = (id) => appImg.document.getElementById(id);
  try {
    const img = new appImg.window.File(['11'], 'pic.png', { type: 'image/png' });
    appImg.window.attachFiles([img]);
    await tick(120);
    $ai('ta').value = 'see this image';
    $ai('send-btn').click();
    await tick(180);
    const llm = appImg.events.filter((e) => e.method === 'POST' && e.url.includes('chat/completions'));
    if (llm.length) {
      const body = JSON.parse(llm[0].body || '{}');
      const user = (body.messages || []).find((m) => m.role === 'user');
      const imgPart = user && Array.isArray(user.content) ? user.content.find((p) => p.type === 'image_url') : null;
      check(!!imgPart && /^data:image\/png;base64,/.test(imgPart.image_url.url), 'image sent as image_url data URL part');
    } else {
      check(false, 'image attachment emitted a chat/completions POST');
    }
  } finally { teardownApp(appImg); }

  /* ====================== E2E: from-workdir flow ====================== */
  console.log('--- e2e: from-workdir browse + read + attach ---');
  const appWd = await launchApp({ routes: ROUTES, seed: { 'cogitator.settings': JSON.stringify(SEED) } });
  await tick(80);
  try {
    check(typeof appWd.window.openWorkdirPicker === 'function', 'openWorkdirPicker wiring present');
    if (typeof appWd.window.openWorkdirPicker === 'function') {
      appWd.window.openWorkdirPicker();
      await tick(120);
      const list = appWd.document.getElementById('wd-list');
      check(!!list && list.querySelectorAll('.wd-file').length >= 2, 'workdir picker lists bridge entries');
      const rows = list ? list.querySelectorAll('.wd-file') : [];
      const hello = Array.prototype.find.call(rows, (r) => /hello\.txt/.test(r.textContent));
      check(!!hello, 'hello.txt entry present');
      if (hello) { hello.click(); await tick(120); }
      const chips = appWd.document.querySelectorAll('#attach-chips .chip');
      check(chips.length === 1 && /hello\.txt/.test(chips[0].textContent), 'picked workdir file attached as chip');
      $a = (id) => appWd.document.getElementById(id);
      if ($a('ta')) { $a('ta').value = 'summarize'; $a('send-btn').click(); await tick(180); }
      const llm = appWd.events.filter((e) => e.method === 'POST' && e.url.includes('chat/completions'));
      if (llm.length) {
        const body = JSON.parse(llm[0].body || '{}');
        const user = (body.messages || []).find((m) => m.role === 'user');
        const block = user && Array.isArray(user.content) ? user.content.find((p) => p.text && p.text.includes('hello.txt')) : null;
        check(!!block && block.text.includes('WD FILE CONTENT'), 'workdir file content read via read_file and sent');
      } else {
        check(false, 'from-workdir send emitted a chat/completions POST');
      }
    }
  } finally { teardownApp(appWd); }

  process.exit(summary('PHASE 3 ATTACHMENTS'));
})().catch((e) => { console.error('PHASE3 CATCH:', e); process.exit(1); });