'use strict';
/*
 * PHASE 20 E2E — the compaction rite must never lose the transcript, and must never
 * summarise an attachment array as "[object Object]".
 *
 * Ticket: TICKET-2026-10-01-compaction-data-loss
 *         (CR-Nanites-harness-0003 high, 0026 medium)
 *
 *  0003 HIGH — `compactChat` assigns `c.summary = ... + res.trim()` and THEN walks the
 *        budget and slices `c.messages`. When the summary call returns an empty or
 *        whitespace-only string the rite has enshrined NOTHING, yet it still throws the
 *        front of the conversation away. The summary note then renders
 *        "CONTEXT COMPACTED — 1 TOK ENSHRINED." over a transcript that is now missing its
 *        front, so the loss is invisible AND unrecoverable. The fix must bail BEFORE
 *        touching `c.messages` — and before touching `c.summary`, which the current
 *        assignment has already clobbered — and say plainly that nothing was compacted.
 *
 *        This is reachable with NO operator action: `maybeAutoCompact()` fires
 *        `compactChat(true)` at `compactAt`% of the context limit. A dead/overloaded
 *        endpoint that returns 200 with an empty body is enough.
 *
 *  0026 MEDIUM — the compression history is built from `m.content` raw. An attachment
 *        message's `content` is an ARRAY (`CogCore.buildAttachmentContent`), so
 *        `'['+m.role.toUpperCase()+'] '+m.content` renders `[object Object]` into the
 *        summariser's prompt: the model is asked to compress a transcript whose
 *        attachments are literally gone. `msgHTML` has routed through
 *        `CogCore.contentText()` since Phase 3/9; the compaction path never did.
 *
 *  Third clause (same root cause, the render seam): `refreshLastFor` renders with
 *        `renderMd(m.content)` unconditionally, while `msgHTML` guards user/tool with
 *        `esc(CogCore.contentText(m.content))`. So a streaming refresh of an attachment
 *        message REPLACES the correctly-rendered bubble with the stringified array —
 *        the view degrades the moment a delta lands. The two renderers must agree.
 *
 * Before this file `compactChat` was called by NO test — the only match anywhere in
 * tests/frontend was a comment. Nothing could see any of this.
 *
 * NOTE for the next agent: the budget walk and the dangling-tool-result boundary at the
 * retained edge are part of the SAME function and are deliberately left alone here.
 * Cases below pin them so a future compaction change cannot quietly break them.
 */
const path = require('path');
const { check, summary, fails, launchApp, teardownApp } = require('./helpers');

const tick = (ms = 30) => new Promise((r) => setTimeout(r, ms));
const failsSnapshot = () => fails.length;

const SEED = {
  endpoint: 'http://x', model: 'test-model', backend: 'openai', system: '',
  ctxLimit: 131072, compactAt: 70, autoCompact: false,
  workdir: '', agent: false, autoBridge: false, profiles: [], activeProfile: ''
};

/* A budget small enough that the retention walk actually has to drop messages.
   ctxLimit drives it: `budget = ceil(ctxLimit*0.35) - tok(summary)`. At the shipped
   131072 default even 60 short messages fit, so a "did it trim?" case would pass for
   the wrong reason — nothing was over budget, not nothing was retained. */
const TIGHT = Object.assign({}, SEED, { ctxLimit: 900 });

const ROUTES = {
  '/v1/models': { status: 200, json: { data: [{ id: 'test-model' }] } }
};

/* `/v1/chat/completions` returns this. `reasoning_content` + `content` are joined by
   callOpenAI's non-stream path, so an empty `content` is an empty string — exactly
   what a 200-with-empty-body endpoint produces. */
function chatCompletions(content) {
  return { status: 200, json: { choices: [{ message: { role: 'assistant', content } }] } };
}

function seedBlob(messages, summary_, settingsOverride) {
  const chats = [{ id: 'c1', title: 'COMPACT TEST', created: 1, updated: 2, messages, summary: summary_ }];
  return {
    'cogitator.settings': JSON.stringify(Object.assign({}, SEED, settingsOverride || {})),
    'cogitator.chats': JSON.stringify(chats),
    'cogitator.active': 'c1'
  };
}

function plainMessages(n) {
  const out = [];
  for (let i = 0; i < n; i++) {
    out.push(i % 2 === 0
      ? { role: 'user', content: 'operator transmission ' + i }
      : { role: 'assistant', content: 'machine-spirit reply ' + i });
  }
  return out;
}

/* Persisted blob — what a reload would see. Asserting the in-memory array alone would
   miss a rite that mutates `chats` and never `save()`s it. */
function persisted(app) {
  try { return JSON.parse(app.storage.getItem('cogitator.chats')); } catch (e) { return null; }
}

function lastChatRequest(app) {
  const hits = app.events.filter((e) => /\/v1\/chat\/completions/.test(e.url));
  if (!hits.length) return null;
  try { return JSON.parse(hits[hits.length - 1].body); } catch (e) { return null; }
}

/* Alerts are not stubbed by helpers.js (jsdom's alert is a no-op that logs a
   "not implemented" error). Capture them so the operator-reporting clauses can assert. */
function captureAlerts(app) {
  const seen = [];
  app.window.alert = (msg) => { seen.push(String(msg)); };
  return seen;
}

function flashNotes(app) {
  return Array.from(app.document.querySelectorAll('.flash-compact-note')).map((n) => n.textContent);
}

/* ================================================================== *
 *  0003 — an empty summary must not cost the operator their transcript
 * ================================================================== */
async function emptySummaryKeepsEverything() {
  console.log('\n--- e2e: 0003 empty summary must NOT slice the transcript ---');
  const msgs = plainMessages(8);
  const app = await launchApp({ routes: { ...ROUTES, '/v1/chat/completions': chatCompletions('') }, seed: seedBlob(msgs, '') });
  const alerts = captureAlerts(app);
  const before = JSON.parse(JSON.stringify(msgs));
  const chat = app.window.active();

  check(chat && chat.messages.length === 8, 'seeded chat is active with 8 messages (got ' + (chat && chat.messages.length) + ')');

  let ret;
  try { ret = await app.window.compactChat(false); } catch (e) { ret = 'THREW: ' + e.message; }
  await tick();

  console.log('  compactChat returned: ' + JSON.stringify(ret));
  console.log('  messages after: ' + (chat.messages ? chat.messages.length : 'n/a'));
  console.log('  c.summary after: ' + JSON.stringify(chat.summary));
  console.log('  alerts: ' + JSON.stringify(alerts));

  /* 1. The transcript is intact, in memory. */
  check(chat.messages.length === before.length,
    'c.messages still holds all 8 messages (got ' + chat.messages.length + ')');
  check(JSON.stringify(chat.messages) === JSON.stringify(before),
    'c.messages is byte-identical to what it was before the rite');

  /* 2. The transcript is intact on disk — not just in the array a reload would discard. */
  const disk = persisted(app);
  check(!!disk && disk[0].messages.length === before.length,
    'the persisted blob still holds all 8 messages (reload would not lose them)');

  /* 3. No empty summary was enshrined. */
  check(chat.summary === '',
    'c.summary is still empty — nothing was enshrinened (got ' + JSON.stringify(chat.summary) + ')');

  /* 4. The operator was told, in plain terms, on BOTH paths.
        silent===false is the button; silent===true is maybeAutoCompact.
        Assert the notice says the rite did NOT complete — the current code flashes
        "COMPACTION RITE COMPLETE" here, which matches a loose /compact/i check while
        reporting the exact opposite of what happened. */
  const told = alerts.join(' ') + ' ' + flashNotes(app).join(' ');
  console.log('  operator told: ' + JSON.stringify(told));
  check(told.trim().length > 0, 'the operator was told something about the compaction rite');
  check(!/RITE COMPLETE/i.test(told),
    'the notice does NOT claim the rite completed (got ' + JSON.stringify(told) + ')');
  check(/did not|empty|no summary|returned nothing|unchanged/i.test(told),
    'the notice says the rite did not complete and the log is intact (got ' + JSON.stringify(told) + ')');

  /* 5. A failed rite must not report success. */
  check(ret === false, 'compactChat returned false (got ' + JSON.stringify(ret) + ')');

  /* 6. The button is re-enabled — the rite is retryable, not wedged. */
  check(app.document.getElementById('compact-btn').disabled === false,
    'the COMPACT button is re-enabled after the failed rite');

  teardownApp(app);
}

/* The same loss reachable with NO operator action. This is the clause that makes the
   gate about the shipped system rather than about the button. */
async function emptySummaryUnderAutoCompactKeepsEverything() {
  console.log('\n--- e2e: 0003 the AUTO rite (no operator action) must not lose the transcript ---');
  const msgs = plainMessages(8);
  const seed = seedBlob(msgs, '');
  /* autoCompact on, compactAt low, ctxLimit tiny => ctxTokens() breaches on boot's
     own maybeAutoCompact() call at the end of send(). Drive maybeAutoCompact directly,
     which is the exact function the threshold calls. */
  const autoSeed = Object.assign({}, seed, {
    'cogitator.settings': JSON.stringify(Object.assign({}, SEED, {
      autoCompact: true, compactAt: 1, ctxLimit: 400
    }))
  });
  const app = await launchApp({ routes: { ...ROUTES, '/v1/chat/completions': chatCompletions('') }, seed: autoSeed });
  captureAlerts(app);
  const chat = app.window.active();
  const before = JSON.parse(JSON.stringify(msgs));

  app.window.maybeAutoCompact();          // flashes, then setTimeout(compactChat(true), 600)
  await tick(900);                       // past the 600ms deferral + the fetch round trip

  console.log('  messages after auto-compact: ' + chat.messages.length);
  check(chat.messages.length === before.length,
    'the auto rite kept all 8 messages (got ' + chat.messages.length + ')');
  check(JSON.stringify(chat.messages) === JSON.stringify(before),
    'the auto rite left the transcript byte-identical');
  check(chat.summary === '',
    'the auto rite enshrinened nothing (c.summary still empty)');
  check(flashNotes(app).some((t) => /compact/i.test(t)),
    'the auto rite surfaced a note naming the compaction rite (got ' + JSON.stringify(flashNotes(app)) + ')');

  teardownApp(app);
}

/* THE DATA LOSS ITSELF. At the shipped 131072 default the retention walk keeps
   everything, so the case above shows the lying notice and the corrupted summary but
   not the loss. Drive it with a tight budget — exactly what a real over-budget chat
   has — and the front of the transcript goes. This is the CR-0003 headline. */
async function emptySummaryOverBudgetDestroysNothing() {
  console.log('\n--- e2e: 0003 an empty summary over a TIGHT budget must not drop the front ---');
  const msgs = toolChain(40, 120);
  const app = await launchApp({ routes: { ...ROUTES, '/v1/chat/completions': chatCompletions('') }, seed: seedBlob(msgs, '', TIGHT) });
  captureAlerts(app);
  const chat = app.window.active();
  const before = JSON.parse(JSON.stringify(msgs));

  console.log('  before: ' + msgs.length + ' messages; budget = ceil(' + TIGHT.ctxLimit + '*0.35) = ' + Math.ceil(TIGHT.ctxLimit * 0.35) + ' tokens');
  const ret = await app.window.compactChat(false);
  await tick();

  console.log('  after: ' + chat.messages.length + ' messages, c.summary=' + JSON.stringify(chat.summary));
  check(chat.messages.length === before.length,
    'all ' + before.length + ' messages survived an empty summary over a tight budget (got ' + chat.messages.length + ')');
  check(JSON.stringify(chat.messages) === JSON.stringify(before),
    'the transcript is byte-identical — the front was NOT discarded');
  check(chat.summary === '', 'nothing was enshrinened (c.summary still empty)');
  check(ret === false, 'compactChat returned false (got ' + JSON.stringify(ret) + ')');

  teardownApp(app);
}

/* Whitespace-only is the same defect wearing a different value: `''.trim()` is `''`, but
   so is `'   \n  '.trim()`, and a model that emits a blank line is not exotic. */
async function whitespaceSummaryKeepsEverything() {
  console.log('\n--- e2e: 0003 whitespace-only summary is also an empty summary ---');
  const msgs = plainMessages(8);
  const app = await launchApp({ routes: { ...ROUTES, '/v1/chat/completions': chatCompletions('   \n\n  ') }, seed: seedBlob(msgs, '') });
  captureAlerts(app);
  const chat = app.window.active();

  const ret = await app.window.compactChat(false);
  await tick();

  check(chat.messages.length === 8, 'all 8 messages survived a whitespace-only summary (got ' + chat.messages.length + ')');
  check(chat.summary === '', 'c.summary is still empty (got ' + JSON.stringify(chat.summary) + ')');
  check(ret === false, 'compactChat reported false (got ' + JSON.stringify(ret) + ')');

  teardownApp(app);
}

/* An empty summary must not be allowed to APPEND onto an existing one either — the
   current code writes `'[DEEPER PAST] '` before the emptiness is noticed. */
async function emptySummaryDoesNotCorruptAnExistingSummary() {
  console.log('\n--- e2e: 0003 an empty summary must not append a DEEPER PAST marker ---');
  const msgs = plainMessages(8);
  const app = await launchApp({ routes: { ...ROUTES, '/v1/chat/completions': chatCompletions('') }, seed: seedBlob(msgs, 'THE ORIGINAL ENSHRINED SUMMARY.') });
  captureAlerts(app);
  const chat = app.window.active();

  await app.window.compactChat(false);
  await tick();

  console.log('  c.summary after: ' + JSON.stringify(chat.summary));
  check(chat.summary === 'THE ORIGINAL ENSHRINED SUMMARY.',
    'the existing summary is untouched (got ' + JSON.stringify(chat.summary) + ')');
  check(!/DEEPER PAST/.test(String(chat.summary)),
    'no "[DEEPER PAST]" marker was appended for a rite that enshrined nothing');
  check(chat.messages.length === 8, 'all 8 messages survived (got ' + chat.messages.length + ')');

  teardownApp(app);
}

/* ================================================================== *
 *  0026 — the summariser prompt must contain the attachment's TEXT
 * ================================================================== */
async function compactionPromptCarriesAttachmentText() {
  console.log('\n--- e2e: 0026 the compression prompt carries attachment TEXT ---');
  const ATT = 'ATTACHMENT-PAYLOAD-ALPHA the operator pinned this exact string';
  const msgs = [
    { role: 'user', content: 'read the file please' },
    { role: 'assistant', content: 'reading it now' },
    { role: 'user', content: [{ type: 'text', text: ATT }, { type: 'image_url', image_url: { url: 'data:image/png;base64,AAAA' } }] },
    { role: 'assistant', content: 'the file says alpha' },
    { role: 'user', content: 'and the second one?' }
  ];
  const app = await launchApp({ routes: { ...ROUTES, '/v1/chat/completions': chatCompletions('A SUMMARY.') }, seed: seedBlob(msgs, '') });
  captureAlerts(app);

  await app.window.compactChat(false);
  await tick();

  const req = lastChatRequest(app);
  check(!!req, 'the compaction rite issued a chat/completions request');
  const prompt = req ? String(req.messages.map((m) => m.content).join('\n')) : '';

  console.log('  prompt length: ' + prompt.length);
  check(prompt.indexOf(ATT) !== -1, 'the attachment TEXT appears in the summariser prompt');
  check(prompt.indexOf('[IMAGE]') !== -1,
    'the image part is summarised as an [IMAGE] marker (via contentText)');
  check(prompt.indexOf('[object Object]') === -1,
    'the prompt does NOT contain "[object Object]" (got ' + (prompt.indexOf('[object Object]') !== -1 ? 'PRESENT' : 'absent') + ')');
  check(prompt.indexOf('read the file please') !== -1, 'the other messages are still in the prompt');

  teardownApp(app);
}

/* A tool result carrying an attachment array must be summarised too — the tool branch
   builds its own string and never went through contentText either. */
async function toolResultAttachmentTextReachesPrompt() {
  console.log('\n--- e2e: 0026 a tool RESULT with array content reaches the prompt as text ---');
  const TOOLTXT = 'TOOL-PAYLOAD-BETA read_file returned exactly this';
  const msgs = [
    { role: 'user', content: 'read it' },
    { role: 'assistant', content: '', toolCalls: [{ id: 'c1', name: 'read_file', args: '{"path":"a.txt"}' }] },
    { role: 'tool', name: 'read_file', toolCallId: 'c1', content: [{ type: 'text', text: TOOLTXT }] },
    { role: 'assistant', content: 'it says beta' }
  ];
  const app = await launchApp({ routes: { ...ROUTES, '/v1/chat/completions': chatCompletions('A SUMMARY.') }, seed: seedBlob(msgs, '') });
  captureAlerts(app);

  await app.window.compactChat(false);
  await tick();

  const req = lastChatRequest(app);
  const prompt = req ? String(req.messages.map((m) => m.content).join('\n')) : '';
  check(prompt.indexOf(TOOLTXT) !== -1, 'the tool result TEXT reaches the summariser prompt');
  check(prompt.indexOf('[object Object]') === -1,
    'no "[object Object]" in the prompt (got ' + (prompt.indexOf('[object Object]') !== -1 ? 'PRESENT' : 'absent') + ')');
  check(prompt.indexOf('[TOOL RESULT read_file c1]') !== -1,
    'the tool-result marker is still in the prompt (the branch was not dropped)');

  teardownApp(app);
}

/* ================================================================== *
 *  0026 (render seam) — refreshLast must render like msgHTML
 * ================================================================== */
async function refreshLastMatchesMsgHTMLForAttachment() {
  console.log('\n--- e2e: 0026 refreshLast must render an attachment like msgHTML does ---');
  const ATT = 'REFRESHLAST-PAYLOAD-GAMMA the visible text of the attachment';
  /* Attachment message LAST, so refreshLast()'s non-streaming fallback targets it. */
  const msgs = [
    { role: 'assistant', content: 'earlier reply' },
    { role: 'user', content: [{ type: 'text', text: ATT }, { type: 'image_url', image_url: { url: 'data:image/png;base64,BBBB' } }] }
  ];
  const app = await launchApp({ routes: ROUTES, seed: seedBlob(msgs, '') });
  const chat = app.window.active();
  const idx = chat.messages.length - 1;

  const before = app.document.getElementById('body-' + idx).innerHTML;
  console.log('  msgHTML render: ' + JSON.stringify(before.slice(0, 160)));

  app.window.refreshLast();
  const after = app.document.getElementById('body-' + idx).innerHTML;
  console.log('  after refresh:  ' + JSON.stringify(after.slice(0, 160)));

  check(before.indexOf(ATT) !== -1, 'msgHTML shows the attachment TEXT (not the stringified array)');
  check(before.indexOf('[object Object]') === -1, 'msgHTML output has no "[object Object]"');
  check(after === before,
    'refreshLast produced byte-identical output to msgHTML — the two renderers agree');
  check(after.indexOf('[object Object]') === -1,
    'refreshLast did not degrade the bubble to "[object Object]"');

  teardownApp(app);
}

/* The tool bubble too: msgHTML guards isTool with contentText as well. */
async function refreshLastMatchesMsgHTMLForToolResult() {
  console.log('\n--- e2e: 0026 refreshLast matches msgHTML for a tool RESULT bubble ---');
  const TT = 'TOOLBUBBLE-DELTA the streamed tool text';
  const msgs = [
    { role: 'user', content: 'go' },
    { role: 'tool', name: 'read_file', toolCallId: 'c1', content: [{ type: 'text', text: TT }] }
  ];
  const app = await launchApp({ routes: ROUTES, seed: seedBlob(msgs, '') });
  const chat = app.window.active();
  const idx = chat.messages.length - 1;

  const before = app.document.getElementById('body-' + idx).innerHTML;
  app.window.refreshLast();
  const after = app.document.getElementById('body-' + idx).innerHTML;

  check(before.indexOf(TT) !== -1, 'msgHTML shows the tool result TEXT');
  check(after === before, 'refreshLast produced byte-identical output for a tool bubble');
  check(after.indexOf('[object Object]') === -1, 'no "[object Object]" after refreshLast');

  teardownApp(app);
}

/* ================================================================== *
 *  Positive control — the rite must still WORK when the model answers.
 *  Every clause above is a refusal; without this, "always bail" passes the whole file.
 * ================================================================== */
async function aRealSummaryStillCompacts() {
  console.log('\n--- e2e: POSITIVE CONTROL a real summary still compacts and enshrines ---');
  const msgs = plainMessages(60);
  const app = await launchApp({ routes: { ...ROUTES, '/v1/chat/completions': chatCompletions('DENSE SUMMARY OF THE LOG.') }, seed: seedBlob(msgs, '', TIGHT) });
  captureAlerts(app);
  const chat = app.window.active();

  const ret = await app.window.compactChat(false);
  await tick();

  console.log('  messages: ' + msgs.length + ' -> ' + chat.messages.length + ', summary ' + JSON.stringify(chat.summary));
  check(ret === true, 'compactChat returned true (got ' + JSON.stringify(ret) + ')');
  check(chat.summary === 'DENSE SUMMARY OF THE LOG.', 'the summary was enshrinened (got ' + JSON.stringify(chat.summary) + ')');
  check(chat.messages.length < msgs.length,
    'the transcript was actually trimmed (got ' + chat.messages.length + ' from ' + msgs.length + ')');
  check(chat.messages.length > 0, 'the transcript was not emptied (got ' + chat.messages.length + ')');
  check(!!app.document.getElementById('compact-note'), 'the COMPACTED note is rendered');
  check(app.storage.getItem('cogitator.chats').indexOf('DENSE SUMMARY') !== -1,
    'the new summary was persisted');

  teardownApp(app);
}

/* A second compaction appends under a DEEPER PAST marker — the rule the empty-summary
   case must NOT trip. Pinned so the bail is about emptiness, not about "no prior summary". */
async function secondCompactionAppendsDeeperPast() {
  console.log('\n--- e2e: POSITIVE CONTROL a second compaction appends under [DEEPER PAST] ---');
  const msgs = plainMessages(60);
  const app = await launchApp({ routes: { ...ROUTES, '/v1/chat/completions': chatCompletions('THE SECOND SUMMARY.') }, seed: seedBlob(msgs, 'THE FIRST SUMMARY.', TIGHT) });
  captureAlerts(app);
  const chat = app.window.active();

  const ret = await app.window.compactChat(false);
  await tick();

  console.log('  c.summary after: ' + JSON.stringify(chat.summary));
  check(ret === true, 'compactChat returned true (got ' + JSON.stringify(ret) + ')');
  check(chat.summary === 'THE FIRST SUMMARY.\n\n[DEEPER PAST] THE SECOND SUMMARY.',
    'the second summary was appended under the marker (got ' + JSON.stringify(chat.summary) + ')');

  teardownApp(app);
}

/* ================================================================== *
 *  The budget walk + dangling-tool boundary — SAME function, deliberately untouched.
 *  Pinned so a future compaction change cannot quietly break the retention rule.
 * ================================================================== */
/* Each assistant message MUST declare the tool call its tool message answers. Without
   that the transcript is already protocol-illegal BEFORE compaction, and the orphan
   count would measure this fixture rather than the retention walk. */
function toolChain(turns, filler) {
  const msgs = [{ role: 'user', content: 'BEGIN TRANSMISSION ' + 'x'.repeat(40) }];
  for (let i = 0; i < turns; i++) {
    msgs.push({
      role: 'assistant', content: 'reply ' + i + ' ' + 'y'.repeat(filler),
      toolCalls: [{ id: 'tc' + i, name: 'read_file', args: '{"path":"f' + i + '.txt"}' }]
    });
    msgs.push({ role: 'tool', name: 'read_file', toolCallId: 'tc' + i, content: 'tool result ' + i + ' ' + 'z'.repeat(filler) });
  }
  msgs.push({ role: 'user', content: 'TAIL MESSAGE ' + 'w'.repeat(200) });
  return msgs;
}

async function retentionNeverLeavesADanglingToolResult() {
  console.log('\n--- e2e: the retained boundary never orphans a tool result ---');
  const msgs = toolChain(40, 120);
  /* Sanity: the fixture is protocol-legal to begin with. If this ever fails, the
     orphan assertion below is measuring the fixture, not the product. */
  const preDeclared = new Set();
  for (const m of msgs) if (m.role === 'assistant') for (const t of (m.toolCalls || [])) preDeclared.add(t.id);
  const preOrphans = msgs.filter((m) => m.role === 'tool' && !preDeclared.has(m.toolCallId)).length;

  const app = await launchApp({ routes: { ...ROUTES, '/v1/chat/completions': chatCompletions('RETENTION SUMMARY.') }, seed: seedBlob(msgs, '', TIGHT) });
  captureAlerts(app);
  const chat = app.window.active();

  await app.window.compactChat(false);
  await tick();

  console.log('  ' + msgs.length + ' messages -> ' + chat.messages.length + ' retained (orphans in fixture: ' + preOrphans + ')');
  check(preOrphans === 0, 'the fixture starts protocol-legal (pre-existing orphans: ' + preOrphans + ')');
  check(chat.messages.length > 0 && chat.messages.length < msgs.length,
    'the transcript was trimmed but not emptied (' + chat.messages.length + ' retained)');
  check(chat.messages[0].role !== 'tool',
    'the retained transcript does NOT start with an orphaned tool result (starts ' + chat.messages[0].role + ')');

  /* The load-bearing shape: every retained tool message must be ANSWERED by a retained
     assistant message that declared it. An orphan is what phase 17 (CR-0012) fixed for
     buildMessages; compaction slices the array directly and can reintroduce it. */
  const declared = new Set();
  for (const m of chat.messages) {
    if (m.role === 'assistant') for (const t of (m.toolCalls || [])) declared.add(t.id);
  }
  const orphans = chat.messages.filter((m) => m.role === 'tool' && !declared.has(m.toolCallId));
  console.log('  orphan tool messages retained: ' + orphans.length);
  check(orphans.length === 0,
    'no retained tool message lacks its declaring assistant tool_call (' + orphans.length + ' orphan(s))');

  teardownApp(app);
}

/* Fewer than four messages is refused before any model call — the guard must stay ahead
   of the new emptiness check so a short chat costs nothing. */
async function shortChatIsRefusedWithoutAModelCall() {
  console.log('\n--- e2e: a chat under 4 messages is refused with no model call ---');
  const msgs = plainMessages(3);
  const app = await launchApp({ routes: { ...ROUTES, '/v1/chat/completions': chatCompletions('SHOULD NEVER BE CALLED.') }, seed: seedBlob(msgs, '') });
  const alerts = captureAlerts(app);

  const ret = await app.window.compactChat(false);
  await tick();

  check(ret === false, 'compactChat returned false (got ' + JSON.stringify(ret) + ')');
  check(app.events.filter((e) => /\/v1\/chat\/completions/.test(e.url)).length === 0,
    'no model call was issued for a chat with nothing worth compacting');
  check(alerts.length === 1, 'the operator was told why (got ' + JSON.stringify(alerts) + ')');
  check(app.window.active().messages.length === 3, 'the 3 messages are untouched');

  teardownApp(app);
}

/* ================================================================== *
 *  0003 (review round 1) — `.trim()` is not the same as "has content".
 *
 *  The first fix tested `if(!summaryText)` where `summaryText=String(res).trim()`, and
 *  the suite seeded only `''` and `'   \n  '`. Both are inside `String.prototype.trim`'s
 *  set (WhiteSpace + LineTerminator). The Unicode FORMAT characters are NOT: a lone
 *  ZWNJ (U+200C), ZWJ (U+200D), word joiner (U+2060) or soft hyphen (U+00AD) survives
 *  `.trim()` at length 1 — as does a bare NUL.
 *
 *  So the destructive path was still fully reachable: a 200 response whose content is a
 *  single ZWJ drops the front of the transcript and reports "RITE COMPLETE". Measured
 *  before the fix: 82 messages -> 9, `ret=true`, summary "\u200d".
 *
 *  This is the same defect CR-0003 named, wearing a value the first fix did not cover —
 *  which is why the rule is asserted here ("a summary must contain at least one
 *  non-whitespace, non-zero-width character") and not as a list of the strings we
 *  happened to think of.
 * ================================================================== */
const INVISIBLE = [
  ['ZWNJ U+200C', '‌'],
  ['ZWJ U+200D', '‍'],
  ['word joiner U+2060', '⁠'],
  ['soft hyphen U+00AD', '­'],
  ['bare NUL', ' '],
  ['ZWJ repeated', '‍‍‍'],
  ['newlines + ZWJ + spaces', '\n\n  ‍  \n']
];

async function invisibleOnlySummaryKeepsEverything() {
  console.log('\n--- e2e: 0003 a summary with no VISIBLE characters must not slice the transcript ---');
  for (const [label, value] of INVISIBLE) {
    const msgs = toolChain(40, 120);
    const app = await launchApp({ routes: { ...ROUTES, '/v1/chat/completions': chatCompletions(value) }, seed: seedBlob(msgs, '', TIGHT) });
    captureAlerts(app);
    const chat = app.window.active();
    const before = JSON.parse(JSON.stringify(msgs));

    console.log('  [' + label + '] JS .trim().length = ' + value.trim().length);

    const ret = await app.window.compactChat(false);
    await tick();

    const told = captureAlerts(app).length ? '' : flashNotes(app).join(' ');
    console.log('    msgs ' + msgs.length + ' -> ' + chat.messages.length +
      ', ret=' + JSON.stringify(ret) + ', summary=' + JSON.stringify(chat.summary));

    check(chat.messages.length === before.length,
      '[' + label + '] all ' + before.length + ' messages survived (got ' + chat.messages.length + ')');
    check(JSON.stringify(chat.messages) === JSON.stringify(before),
      '[' + label + '] the transcript is byte-identical');
    check(chat.summary === '',
      '[' + label + '] nothing was enshrinened (c.summary still empty)');
    check(ret === false, '[' + label + '] compactChat returned false (got ' + JSON.stringify(ret) + ')');
    check(!/RITE COMPLETE/i.test(told),
      '[' + label + '] the notice does NOT claim the rite completed');

    teardownApp(app);
  }
}

/* The complement: a summary that is invisible chars PLUS real text is a legitimate
   summary and must still go through. Without this, "reject anything with a format char"
   would pass the whole file above. */
async function summaryWithRealTextPlusFormatCharsStillCompacts() {
  console.log('\n--- e2e: a summary with real text AND a format char is legitimate ---');
  const msgs = toolChain(40, 120);
  const app = await launchApp({
    routes: { ...ROUTES, '/v1/chat/completions': chatCompletions('REAL SUMMARY TEXT‍') },
    seed: seedBlob(msgs, '', TIGHT)
  });
  captureAlerts(app);
  const chat = app.window.active();

  const ret = await app.window.compactChat(false);
  await tick();

  console.log('  summary: ' + JSON.stringify(chat.summary) + ', msgs ' + msgs.length + ' -> ' + chat.messages.length);
  check(ret === true, 'compactChat returned true (got ' + JSON.stringify(ret) + ')');
  check(/REAL SUMMARY TEXT/.test(String(chat.summary)),
    'the real text was enshrinened (got ' + JSON.stringify(chat.summary) + ')');
  check(chat.messages.length > 0 && chat.messages.length < msgs.length,
    'the transcript was trimmed (got ' + chat.messages.length + ')');

  teardownApp(app);
}

/* The escaped mutation (review round 1, M5): making `refreshLastFor` a no-op left this
   whole file GREEN, because `after === before` compares a DOM node to itself across a
   refresh — it cannot distinguish "re-rendered identically" from "rendered nothing".
   The fix: plant a KNOWN-WRONG value, refresh, and assert the node actually changed to
   the right thing. If a future refactor stops writing to the node at all, this goes red. */
async function refreshLastActuallyRewritesTheNode() {
  console.log('\n--- e2e: refreshLast MUST write to the node (kills the no-op mutation) ---');
  const NEW = 'REWRITTEN-BY-REFRESHLAST the node really was updated';
  const msgs = [{ role: 'assistant', content: 'original assistant text' }];
  const app = await launchApp({ routes: ROUTES, seed: seedBlob(msgs, '') });
  const chat = app.window.active();
  const idx = 0;
  const node = app.document.getElementById('body-' + idx);
  const orig = node.innerHTML;

  /* Change the model behind the renderer's back, then refresh. A renderer that does
     nothing leaves `orig` on screen; a working one re-renders the new content. */
  chat.messages[idx].content = NEW;
  app.window.refreshLast();

  const after = node.innerHTML;
  console.log('  before refresh: ' + JSON.stringify(orig.slice(0, 60)));
  console.log('  after refresh:  ' + JSON.stringify(after.slice(0, 60)));
  check(after !== orig, 'refreshLast changed the node — it is not a no-op');
  check(after.indexOf(NEW) !== -1, 'the node now shows the NEW content');

  /* And the reverse direction proves it tracks the model, not a constant. */
  chat.messages[idx].content = 'a second, different value';
  app.window.refreshLast();
  check(app.document.getElementById('body-' + idx).innerHTML.indexOf('a second, different value') !== -1,
    'a second refresh tracks the second value — the renderer reads the model, not a constant');

  teardownApp(app);
}

/* A summary call that RETURNS NOTHING (undefined/null — a malformed completion body,
   not an empty string) must be treated the same way. `String(undefined)` is the 9-char
   truthy string "undefined", so a naive `String(res)` coercion would enshrine the word
   "undefined" and compact. */
async function nonStringSummaryKeepsEverything() {
  console.log('\n--- e2e: 0003 a null/undefined completion body must not compact ---');
  const SHAPES = [
    ['null content', { choices: [{ message: { role: 'assistant', content: null } }] }],
    ['undefined content', { choices: [{ message: { role: 'assistant' } }] }],
    ['no choices', { object: 'list' }]
  ];
  for (const [label, json] of SHAPES) {
    const msgs = toolChain(40, 120);
    const app = await launchApp({
      routes: { ...ROUTES, '/v1/chat/completions': { status: 200, json } },
      seed: seedBlob(msgs, '', TIGHT)
    });
    captureAlerts(app);
    const chat = app.window.active();

    const ret = await app.window.compactChat(false);
    await tick();

    console.log('  [' + label + '] msgs ' + msgs.length + ' -> ' + chat.messages.length +
      ', ret=' + JSON.stringify(ret) + ', summary=' + JSON.stringify(chat.summary));
    check(chat.messages.length === msgs.length,
      '[' + label + '] all ' + msgs.length + ' messages survived (got ' + chat.messages.length + ')');
    check(ret === false, '[' + label + '] compactChat returned false (got ' + JSON.stringify(ret) + ')');
    check(!/undefined/.test(String(chat.summary)),
      '[' + label + '] the literal string "undefined" was not enshrinened');

    teardownApp(app);
  }
}

/* Positive control for the whitespace/invisible rules: ordinary prose must still compact. */
async function ordinaryProseStillCompacts() {
  console.log('\n--- e2e: POSITIVE CONTROL ordinary prose still compacts ---');
  const msgs = toolChain(40, 120);
  const app = await launchApp({
    routes: { ...ROUTES, '/v1/chat/completions': chatCompletions('The operator asked about the bridge jail and the git policy.') },
    seed: seedBlob(msgs, '', TIGHT)
  });
  captureAlerts(app);
  const chat = app.window.active();
  const ret = await app.window.compactChat(false);
  await tick();
  check(ret === true, 'compactChat returned true for real prose (got ' + JSON.stringify(ret) + ')');
  check(chat.messages.length > 0 && chat.messages.length < msgs.length,
    'real prose compacted the transcript (' + chat.messages.length + ' retained)');
  teardownApp(app);
}

/* Repeated automatic rites (review round 1, defect 3): maybeAutoCompact() defers by 600ms
   with no in-flight guard, so a chat sitting over the threshold fires one per call. The
   bail means nothing is LOST — this pins that the transcript and the summary ledger stay
   intact across a burst, and that a real summary still lands afterwards. */
async function repeatedAutoCompactionLosesNothing() {
  console.log('\n--- e2e: a burst of automatic compactions loses nothing ---');
  const msgs = toolChain(40, 120);
  const app = await launchApp({
    routes: { ...ROUTES, '/v1/chat/completions': chatCompletions('') },
    seed: seedBlob(msgs, '', TIGHT)
  });
  captureAlerts(app);
  const chat = app.window.active();
  const before = JSON.parse(JSON.stringify(msgs));

  for (let i = 0; i < 5; i++) app.window.maybeAutoCompact();
  await tick(1200);

  console.log('  after 5 automatic rites: msgs ' + msgs.length + ' -> ' + chat.messages.length +
    ', summary=' + JSON.stringify(chat.summary));
  check(chat.messages.length === before.length,
    'the transcript survived a burst of 5 automatic rites (got ' + chat.messages.length + ')');
  check(JSON.stringify(chat.messages) === JSON.stringify(before), 'still byte-identical');
  check(chat.summary === '', 'no summary ledger growth from failed rites');

  /* ...and the rite still works once the endpoint recovers. */
  const ok = await launchApp({
    routes: { ...ROUTES, '/v1/chat/completions': chatCompletions('RECOVERED SUMMARY.') },
    seed: seedBlob(msgs, '', TIGHT)
  });
  captureAlerts(ok);
  const chat2 = ok.window.active();
  const r = await ok.window.compactChat(false);
  await tick();
  check(r === true, 'the rite compacts normally once the endpoint answers (got ' + JSON.stringify(r) + ')');
  check(chat2.summary === 'RECOVERED SUMMARY.', 'the recovered summary was enshrinened');
  check(chat2.messages.length < msgs.length, 'and the transcript was trimmed (' + chat2.messages.length + ')');
  teardownApp(ok);

  teardownApp(app);
}

/* ================================================================== *
 *  0003 (review round 2, F1) — assert the RULE, not the instances.
 *
 *  Round 1's fix enumerated the categories the round-1 fixtures happened to use
 *  (`\p{Cf}`, `\p{Cc}`). Review round 2 proved that incomplete: combining marks
 *  (`\p{Mn}`), variation selectors (`\p{Mn}` supplement) and lone surrogates
 *  (`\p{Cs}`) are glyphless too, and each one still destroyed 73 of 82 messages while
 *  the rite flashed "RITE COMPLETE". The rule is now stated as its intent — a summary
 *  must contain at least one character that CARRIES MEANING ON ITS OWN — and asserted
 *  DIRECTLY here.
 *
 *  This section is the important one. Round 2's mutation M8 replaced the regex with a
 *  strictly CORRECT version and the suite stayed green, which proved the suite was
 *  pinning a list of strings rather than a rule. A test that only drives `compactChat`
 *  cannot tell a correct rule from a broken one; only calling the function directly with
 *  a table of values it has never seen can.
 * ================================================================== */
const CogCore = require(path.join(__dirname, '..', '..', 'appcore.js'));

function summaryUsableRule() {
  console.log('\n--- e2e: CogCore.summaryUsable — the RULE itself, called directly ---');

  /* No glyph. These are the round-2 holes: they are invisible, so enshrining one and
     slicing the transcript loses content with nothing readable in its place. */
  const NO_GLYPH = [
    ['empty string', ''],
    ['spaces only', '   '],
    ['newlines only', '\n\n\n'],
    ['tabs', '\t\t'],
    ['ZWSP U+200B', ''],
    ['ZWNJ U+200C', '‌'],
    ['ZWJ U+200D', '‍'],
    ['word joiner U+2060', '⁠'],
    ['soft hyphen U+00AD', '­'],
    ['BOM U+FEFF', '﻿'],
    ['NUL', ' '],
    ['CGJ U+034F', '͏'],
    ['combining acute U+0301', '́'],
    ['combining grave U+0300', '̀'],
    ['VS16 U+FE0F', '️'],
    ['VS1 U+FE00', '󠀀'],
    ['VS17 U+E0100', '\u{E0100}'],
    ['variation selectors x8', '󠀀󠀁󠀂󠀃󠀄󠀅󠀆󠀇'],
    ['lone high surrogate U+D800', '\uD800'],
    ['lone low surrogate U+DC00', '\uDC00'],
    ['marks + ZWJ + NUL mixed', '́‍ '],
    ['format+control+marks', '᠎⁣ ́'],
    ['ideographic space U+3000', '　'],
    ['ogham space U+1680', ' ']
  ];
  for (const [label, v] of NO_GLYPH) {
    check(CogCore.summaryUsable(v) === false,
      'no-glyph: ' + label + ' must be rejected');
  }

  /* Carries meaning on its own. These MUST be accepted — including scripts whose
     letters legitimately rely on combining marks. Rejecting Devanagari or Thai here
     would be a new data-loss bug wearing the fix's clothes. */
  const MEANINGFUL = [
    ['ordinary prose', 'The operator asked about the git policy.'],
    ['single letter', 'a'],
    ['digit', '7'],
    ['all digits', '1234567890'],
    ['prose + ZWJ', 'REAL TEXT‍'],
    ['prose + NUL', 'REAL TEXT '],
    ['prose + combining mark', 'Résumé'],
    ['accents + punctuation', 'Résumé — Ünïcödé, §4.2 (ok)'],
    ['cjk', '上下文压缩例程'],
    ['thai', 'สรุปการสนทนา'],
    ['hangul', '대화 요약'],
    ['russian', 'Краткое содержание беседы'],
    ['hebrew rtl', 'סיכום השיחה'],
    ['devanagari WITH marks', 'सारांश'],
    ['arabic', 'ملخص المحادثة'],
    ['emoji zwj sequence', '✅‍'],
    ['markdown', '- **bold**\n```js\ncode\n```'],
    ['punctuation only', '...'],
    ['url', 'https://example.invalid/x'],
    ['json', '{"a":1,"b":[2,3]}']
  ];
  for (const [label, v] of MEANINGFUL) {
    check(CogCore.summaryUsable(v) === true,
      'meaningful: ' + label + ' must be accepted (got ' + CogCore.summaryUsable(v) + ')');
  }

  /* Not a string. `String(undefined)` is the truthy 9-char string "undefined", so a
     coercion here would enshrine the word "undefined" and compact. */
  for (const [label, v] of [['undefined', undefined], ['null', null], ['number', 0],
    ['number 42', 42], ['false', false], ['true-ish object', {}], ['empty array', []],
    ['array of text', ['a real summary']], ['NaN', NaN]]) {
    check(CogCore.summaryUsable(v) === false,
      'non-string: ' + label + ' must be rejected');
  }

  /* A REJECT must never be silent: the whole point is that the caller can tell the
     operator. Assert the rule has a cheap complement rather than leaving callers to
     re-derive the whole thing. */
  check(typeof CogCore.summaryUsable('x') === 'boolean',
    'summaryUsable returns a boolean, not a truthy/falsy of another type');
}

/* The same values driven through the REAL compactChat, so the rule and its only caller
   are known to agree. If someone changes one without the other, this goes red. */
async function glyphlessSummariesAreRefusedByTheRealRite() {
  console.log('\n--- e2e: 0003 glyphless summaries through the real rite lose nothing ---');
  const GLYPHLESS = [
    ['CGJ U+034F', '͏'],
    ['combining acute', '́'],
    ['VS16 U+FE0F', '️'],
    ['VS17 U+E0100', '\u{E0100}'],
    ['lone surrogate', '\uD800'],
    ['combining marks x4', '̀́̂̃']
  ];
  for (const [label, value] of GLYPHLESS) {
    const msgs = toolChain(40, 120);
    const app = await launchApp({ routes: { ...ROUTES, '/v1/chat/completions': chatCompletions(value) }, seed: seedBlob(msgs, '', TIGHT) });
    captureAlerts(app);
    const chat = app.window.active();

    const ret = await app.window.compactChat(false);
    await tick();

    const note = flashNotes(app).join(' ');
    console.log('  [' + label + '] msgs ' + msgs.length + ' -> ' + chat.messages.length +
      ', summary=' + JSON.stringify(chat.summary));
    check(chat.messages.length === msgs.length,
      '[' + label + '] no messages lost (got ' + chat.messages.length + ')');
    check(chat.summary === '',
      '[' + label + '] nothing enshrinened (got ' + JSON.stringify(chat.summary) + ')');
    check(ret === false, '[' + label + '] returned false (got ' + JSON.stringify(ret) + ')');
    check(!/RITE COMPLETE/i.test(note), '[' + label + '] no success claim');
    teardownApp(app);
  }
}

/* F2 — CR-0026 on the RESPONSE side. Round 2 found the fix covered the prompt side
   (`hist`) but left `callOpenAI`'s non-stream return doing
   `[msg.reasoning_content||'', msg.content||''].join('\n')`. A server answering with the
   multimodal parts ARRAY — the shape this app itself SENDS, and what OpenAI-compatible
   proxies echo — gets `Array.prototype.join` to produce "[object Object]", which is a
   perfectly READABLE string, so no emptiness guard downstream can ever catch it.
   Measured before the fix: 82 -> 9 messages lost, summary "[object Object]", "RITE COMPLETE". */
async function responseSideArrayContentIsNotStringified() {
  console.log('\n--- e2e: F2 a RESPONSE with array/object content is not "[object Object]" ---');
  const SHAPES = [
    ['array of parts', [{ type: 'text', text: 'A REAL SUMMARY FROM PARTS.' }]],
    ['object content', { text: 'A REAL SUMMARY FROM AN OBJECT.' }],
    ['array w/ empty first', ['', { type: 'text', text: 'REAL TEXT' }]],
    ['array of plain strings', ['A REAL SUMMARY.']],
    ['array with an image part', [{ type: 'text', text: 'SUMMARY PLUS IMAGE.' }, { type: 'image_url', image_url: { url: 'data:image/png;base64,AA' } }]]
  ];
  for (const [label, content] of SHAPES) {
    const msgs = toolChain(40, 120);
    const app = await launchApp({
      routes: { ...ROUTES, '/v1/chat/completions': { status: 200, json: { choices: [{ message: { role: 'assistant', content } }] } } },
      seed: seedBlob(msgs, '', TIGHT)
    });
    captureAlerts(app);
    const chat = app.window.active();

    const ret = await app.window.compactChat(false);
    await tick();

    const summary = String(chat.summary);
    console.log('  [' + label + '] ret=' + JSON.stringify(ret) + ' summary=' + JSON.stringify(summary) +
      ' msgs ' + msgs.length + '->' + chat.messages.length);
    check(summary.indexOf('[object Object]') === -1,
      '[' + label + '] the enshrinened summary is not "[object Object]" (got ' + JSON.stringify(summary) + ')');
    check(/REAL|SUMMARY/.test(summary),
      '[' + label + '] the real response text was enshrinened (got ' + JSON.stringify(summary) + ')');
    check(ret === true && chat.messages.length < msgs.length,
      '[' + label + '] a real array-bodied summary compacts normally (ret=' + JSON.stringify(ret) + ')');
    teardownApp(app);
  }
}

/* F3 (round-2 finding, reachable): `c.summary` was assigned BEFORE the budget walk, so
   anything that throws between the assignment and the slice reports "COMPACTION RITE
   FAILED" AFTER the transcript was already trimmed and persisted — and in the
   `toolCalls`-not-an-array case the ledger is truncated mid-write. The mutation order is
   now compute -> slice -> save -> commit summary, so no throw lands between a mutation
   and its persistence. */
async function aMidRiteThrowLeavesBothSummaryAndTranscriptIntact() {
  console.log('\n--- e2e: F3 a throw mid-rite leaves the summary AND the transcript intact ---');
  /* `toolCalls:'not-an-array'` makes the budget walk's `(m.toolCalls||[]).reduce` throw
     AFTER the summary was assigned but BEFORE the slice. */
  const msgs = toolChain(6, 60);
  msgs[2].toolCalls = 'not-an-array';
  const app = await launchApp({
    routes: { ...ROUTES, '/v1/chat/completions': chatCompletions('THE NEW SUMMARY.') },
    seed: seedBlob(msgs, 'THE ORIGINAL SUMMARY.', TIGHT)
  });
  captureAlerts(app);
  const chat = app.window.active();
  const before = JSON.parse(JSON.stringify(msgs));

  const ret = await app.window.compactChat(false);
  await tick();

  console.log('  ret=' + JSON.stringify(ret) + ' msgs ' + msgs.length + '->' + chat.messages.length +
    ' summary=' + JSON.stringify(chat.summary));
  check(ret === false, 'the rite reported failure (got ' + JSON.stringify(ret) + ')');
  check(chat.messages.length === before.length,
    'the transcript was NOT trimmed by a rite that failed (got ' + chat.messages.length + ')');
  check(chat.summary === 'THE ORIGINAL SUMMARY.',
    'the summary was NOT overwritten by a rite that failed (got ' + JSON.stringify(chat.summary) + ')');
  const disk = persisted(app);
  check(!!disk && JSON.stringify(disk[0].messages) === JSON.stringify(before),
    'the persisted transcript is intact too');

  teardownApp(app);
}

/* F5 (round-2, low): the SUCCESS flash was gated on `!silent`, so the automatic rite —
   the one that fires with no operator action, and the one this phase's gate is about —
   dropped 73 messages and said nothing, while the abort notice is loud on both paths.
   The asymmetry is the same one that made CR-0003 invisible. */
async function automaticSuccessIsAlsoAnnounced() {
  console.log('\n--- e2e: an AUTOMATIC success is announced too ---');
  const msgs = toolChain(40, 120);
  const app = await launchApp({
    routes: { ...ROUTES, '/v1/chat/completions': chatCompletions('AN AUTOMATIC SUMMARY.') },
    seed: seedBlob(msgs, '', TIGHT)
  });
  captureAlerts(app);
  const chat = app.window.active();

  /* silent===true is the maybeAutoCompact path. */
  const ret = await app.window.compactChat(true);
  await tick();

  const notes = flashNotes(app).join(' ');
  console.log('  notes: ' + JSON.stringify(notes) + ' msgs ' + msgs.length + '->' + chat.messages.length);
  check(ret === true, 'the automatic rite succeeded (got ' + JSON.stringify(ret) + ')');
  check(chat.messages.length < msgs.length, 'it compacted (' + chat.messages.length + ' retained)');
  check(/compact/i.test(notes),
    'the silent path announced the compaction rather than doing it quietly (got ' + JSON.stringify(notes) + ')');

  teardownApp(app);
}

/* Round-1 finding #3 / round-2 note: `maybeAutoCompact` had no in-flight guard, so a
   chat sitting over the threshold fired one (billed) model call per invocation, forever.
   A one-line latch. Pinned here because the cost half of that finding was never
   covered by any test. */
async function autoCompactionDoesNotStackModelCalls() {
  console.log('\n--- e2e: repeated threshold breaches do not stack model calls ---');
  const msgs = toolChain(40, 120);
  /* autoCompact MUST be on, or maybeAutoCompact returns at its first line and the case
     passes vacuously — asserting "one call" when zero were even possible. */
  const app = await launchApp({
    routes: { ...ROUTES, '/v1/chat/completions': chatCompletions('ONE SUMMARY ONLY.') },
    seed: seedBlob(msgs, '', Object.assign({}, TIGHT, { autoCompact: true, compactAt: 1 }))
  });
  captureAlerts(app);

  for (let i = 0; i < 6; i++) app.window.maybeAutoCompact();
  await tick(1200);

  const calls = app.events.filter((e) => /\/v1\/chat\/completions/.test(e.url)).length;
  console.log('  6 threshold breaches -> ' + calls + ' model call(s)');
  check(calls === 1,
    'six breaches produced exactly ONE summarisation call, not six (got ' + calls + ')');

  /* And the latch must not wedge the rite: a later manual compaction still works. The
     FIRST automatic run already enshrinened the summary, so the second appends under the
     [DEEPER PAST] marker — assert the ledger advanced, not that it equals the first text. */
  const ret = await app.window.compactChat(false);
  await tick();
  check(ret === true, 'the rite still works after the latch released (got ' + JSON.stringify(ret) + ')');
  check(/ONE SUMMARY ONLY\./.test(String(app.window.active().summary)),
    'and still advances the summary ledger (got ' + JSON.stringify(app.window.active().summary) + ')');

  teardownApp(app);
}

/* ================================================================== *
 *  Review round 3 — five more findings, each of which stopped one line short of
 *  the shape it was fixing. Written RED-first against the round-2 code.
 * ================================================================== */

/* F-A HIGH: the response-side fix covered only ONE operand of a two-operand join.
   `msg.content` went through contentText; `msg.reasoning_content` / `msg.thinking` did
   not. An array or bare object in EITHER produces "[object Object]" via
   Array.prototype.join — a readable string, so summaryUsable accepts it. Measured against
   the round-2 code: 82 -> 9 messages lost, ret=true, "RITE COMPLETE". */
async function bothOperandsOfTheResponseJoinAreRendered() {
  console.log('\n--- e2e: F-A BOTH operands of the response join go through contentText ---');
  const SHAPES = [
    ['reasoning_content = array', { reasoning_content: [{ type: 'text', text: 'REASONING' }], content: '' }],
    ['reasoning_content = object', { reasoning_content: { text: 'REASONING' }, content: '' }],
    ['reasoning array + real content', { reasoning_content: [{ type: 'text', text: 'REASONING' }], content: 'REAL CONTENT.' }],
    ['both operands arrays', { reasoning_content: [{ type: 'text', text: 'R' }], content: [{ type: 'text', text: 'C' }] }],
    ['plain reasoning string', { reasoning_content: 'PLAIN REASONING', content: 'THE SUMMARY.' }]
  ];
  for (const [label, extra] of SHAPES) {
    const msgs = toolChain(40, 120);
    const app = await launchApp({
      routes: { ...ROUTES, '/v1/chat/completions': { status: 200, json: { choices: [{ message: Object.assign({ role: 'assistant' }, extra) }] } } },
      seed: seedBlob(msgs, '', TIGHT)
    });
    captureAlerts(app);
    const chat = app.window.active();
    const ret = await app.window.compactChat(false);
    await tick();
    const s = String(chat.summary);
    console.log('  [' + label + '] ret=' + JSON.stringify(ret) + ' summary=' + JSON.stringify(s) +
      ' msgs ' + msgs.length + '->' + chat.messages.length);
    check(s.indexOf('[object Object]') === -1,
      '[' + label + '] not "[object Object]" (got ' + JSON.stringify(s) + ')');
    check(/REASONING|THE SUMMARY|REAL CONTENT|\bR\b|\bC\b/.test(s),
      '[' + label + '] real text enshrinened (got ' + JSON.stringify(s) + ')');
    check(ret === true && chat.messages.length < msgs.length,
      '[' + label + '] compacts normally (ret=' + JSON.stringify(ret) + ')');
    teardownApp(app);
  }
}

/* F-B HIGH: renderAll() sits after the commit point but INSIDE the try, and it throws on
   any message whose `content` is not a string (renderMd does t.slice). The operator was
   told "COMPACTION RITE FAILED" on a transcript whose front had already been trimmed AND
   persisted — inviting a retry that appends a second [DEEPER PAST] over an already
   truncated log. Post-commit, a render failure must not read as a compaction failure. */
async function aPostCommitRenderFailureIsNotReportedAsCompactionFailure() {
  console.log('\n--- e2e: F-B a post-commit render failure does not claim compaction FAILED ---');
  const msgs = toolChain(40, 120);
  /* Two constraints the fixture must satisfy or the case proves nothing:
     (a) the bad message must be one the budget walk RETAINS — the walk keeps the tail, so
         a message early in the list is trimmed away and renderAll never sees it;
     (b) it must be an ASSISTANT message, because that is the role `msgBody` routes
         through `renderMd` (which does `t.slice`). A tool or user message takes the
         `esc(contentText(...))` path and cannot throw — a fixture that put the numeric
         content on a tool message passed for the wrong reason. */
  const badIdx = msgs.length - 3;
  check(msgs[badIdx].role === 'assistant',
    'the message chosen to break renderMd is an assistant message (index ' + badIdx +
    ', role ' + msgs[badIdx].role + ')');
  msgs[badIdx].content = 42;
  const app = await launchApp({
    routes: { ...ROUTES, '/v1/chat/completions': chatCompletions('A REAL SUMMARY.') },
    seed: seedBlob(msgs, 'OLD SUMMARY.', TIGHT)
  });
  const alerts = captureAlerts(app);
  const chat = app.window.active();

  const ret = await app.window.compactChat(false);
  await tick();

  console.log('  ret=' + JSON.stringify(ret) + ' msgs ' + msgs.length + '->' + chat.messages.length +
    ' summary=' + JSON.stringify(chat.summary));
  console.log('  alerts: ' + JSON.stringify(alerts));
  check(!alerts.some((a) => /COMPACTION RITE FAILED/i.test(a)),
    'no "COMPACTION RITE FAILED" alert (got ' + JSON.stringify(alerts) + ')');
  check(ret === true, 'the rite reports success — it did commit (got ' + JSON.stringify(ret) + ')');
  check(chat.summary === 'OLD SUMMARY.\n\n[DEEPER PAST] A REAL SUMMARY.',
    'the summary was committed exactly once (got ' + JSON.stringify(chat.summary) + ')');
  check(chat.messages.length < msgs.length, 'the transcript was trimmed (' + chat.messages.length + ')');
  /* And the operator is told something went wrong, rather than a silent partial view. */
  const notes = flashNotes(app).join(' ');
  check(/refresh|view|render/i.test(notes),
    'a rendering problem is surfaced distinctly (got ' + JSON.stringify(notes) + ')');

  teardownApp(app);
}

/* F-C MEDIUM: the latch is claimed at SCHEDULE time, so any throw between claiming and
   scheduling leaves it set forever — silently killing automatic compaction for the
   session with no error anywhere. Simulated by claiming and then failing to schedule. */
async function aLatchThatCannotBeScheduledIsReleased() {
  console.log('\n--- e2e: F-C a latch that cannot run is released, not stuck ---');
  const msgs = toolChain(40, 120);
  const app = await launchApp({
    routes: { ...ROUTES, '/v1/chat/completions': chatCompletions('ONE SUMMARY.') },
    seed: seedBlob(msgs, '', Object.assign({}, TIGHT, { autoCompact: true, compactAt: 1 }))
  });
  captureAlerts(app);

  /* Make the scheduling path throw by removing document.body (flashCompact appends to it),
     which is the first statement after the claim. */
  const savedBody = app.document.body;
  Object.defineProperty(app.document, 'body', { get() { throw new Error('body is gone'); }, configurable: true });
  let threw = null;
  try { app.window.maybeAutoCompact(); } catch (e) { threw = e.message; }
  Object.defineProperty(app.document, 'body', { get() { return savedBody; }, configurable: true });

  console.log('  maybeAutoCompact threw: ' + JSON.stringify(threw));
  check(threw !== null, 'the scheduling path was genuinely broken (threw: ' + JSON.stringify(threw) + ')');

  /* Now the machine must still be able to compact — a stuck latch would make every
     further breach a no-op forever. */
  for (let i = 0; i < 3; i++) app.window.maybeAutoCompact();
  await tick(1200);
  const calls = app.events.filter((e) => /\/v1\/chat\/completions/.test(e.url)).length;
  console.log('  model calls after recovery: ' + calls);
  check(calls >= 1,
    'automatic compaction still works after a failed claim — the latch was released (got ' + calls + ' call(s))');

  teardownApp(app);
}

/* F-D MEDIUM: the latch is per-page, not per-chat. The operator switches chats inside the
   600ms deferral window and the queued rite compacts the NEW chat — which never breached
   anything. Measured against the round-2 code: chat B went 30 -> 7 with no operator
   action on B. */
async function theAutomaticRiteIsBoundToTheChatThatClaimedIt() {
  console.log('\n--- e2e: F-D the automatic rite only compacts the chat that claimed it ---');
  const A = toolChain(40, 120);                       // over threshold
  const B = plainMessages(30);                         // nowhere near it
  const chats = [
    { id: 'c1', title: 'BREACHING', created: 1, updated: 2, messages: A, summary: '' },
    { id: 'c2', title: 'INNOCENT', created: 1, updated: 2, messages: B, summary: '' }
  ];
  const app = await launchApp({
    routes: { ...ROUTES, '/v1/chat/completions': chatCompletions('A SUMMARY.') },
    seed: {
      'cogitator.settings': JSON.stringify(Object.assign({}, SEED, { autoCompact: true, compactAt: 1, ctxLimit: 900 })),
      'cogitator.chats': JSON.stringify(chats), 'cogitator.active': 'c1'
    }
  });
  captureAlerts(app);

  app.window.maybeAutoCompact();                       // claims for c1, defers 600ms
  app.window.setActive && app.window.setActive('c2');  // operator switches inside the window
  /* If the app has no setActive export, drive localStorage the way its own switcher does. */
  try { app.storage.setItem('cogitator.active', 'c2'); } catch (e) {}
  await tick(1200);

  const after = JSON.parse(app.storage.getItem('cogitator.chats'));
  const b = after.find((x) => x.id === 'c2');
  console.log('  chat B: ' + B.length + ' -> ' + b.messages.length + ' messages, summary=' + JSON.stringify(b.summary));
  check(b.messages.length === B.length,
    'the chat that never breached kept all ' + B.length + ' messages (got ' + b.messages.length + ')');
  check(b.summary === '',
    'and was not summarised behind the operator\'s back (got ' + JSON.stringify(b.summary) + ')');

  teardownApp(app);
}

/* F-F LOW: \p{L} and \p{S} contain code points that render BLANK — the Hangul fillers most
   famously (U+3164 is the well-known invisible-Hangul hazard). The allow-list accepted
   them; each measured destroying 73 of 82 messages through the AUTOMATIC rite. */
async function blankRenderingCodePointsAreRefused() {
  console.log('\n--- e2e: F-F blank-rendering letters and symbols are refused ---');
  const BLANK = [
    ['HANGUL FILLER U+3164 x5', 'ㅤㅤㅤㅤㅤ'],
    ['HANGUL CHOSEONG FILLER U+115F x5', 'ᅟᅟᅟᅟᅟ'],
    ['HANGUL JUNGSEONG FILLER U+1160 x5', 'ᅠᅠᅠᅠᅠ'],
    ['HALFWIDTH HANGUL FILLER U+FFA0 x5', 'ﾠﾠﾠﾠﾠ'],
    ['BRAILLE PATTERN BLANK U+2800 x5', '⠀⠀⠀⠀⠀'],
    ['REPLACEMENT CHARACTER U+FFFD x5', '�����']
  ];
  for (const [label, value] of BLANK) {
    check(CogCore.summaryUsable(value) === false,
      'blank glyph: ' + label + ' must be rejected by the rule');
  }
  /* And REAL Hangul must still be accepted — the deny-list must not eat the script. */
  check(CogCore.summaryUsable('대화 요약') === true, 'real Hangul text must still be accepted');
  check(CogCore.summaryUsable('요약: 사용자가') === true, 'real Hangul prose must still be accepted');
  check(CogCore.summaryUsable('REAL SUMMARY') === true, 'real text unaffected by the deny-list');

  /* And end-to-end through the automatic rite, with no operator action. */
  for (const [label, value] of BLANK.slice(0, 3)) {
    const msgs = toolChain(40, 120);
    const app = await launchApp({
      routes: { ...ROUTES, '/v1/chat/completions': chatCompletions(value) },
      seed: seedBlob(msgs, '', Object.assign({}, TIGHT, { autoCompact: true, compactAt: 1 }))
    });
    captureAlerts(app);
    app.window.maybeAutoCompact();
    await tick(1200);
    let disk = null;
    try { disk = JSON.parse(app.storage.getItem('cogitator.chats'))[0]; } catch (e) {}
    const kept = disk ? disk.messages.length : -1;
    console.log('  [' + label + '] disk msgs ' + msgs.length + '->' + kept + ' summary=' + JSON.stringify(disk && disk.summary));
    check(kept === msgs.length,
      '[' + label + '] the automatic rite kept every message (got ' + kept + ')');
    check(disk && disk.summary === '', '[' + label + '] nothing was enshrinened');
    teardownApp(app);
  }
}

(async () => {
  await emptySummaryKeepsEverything();
  await emptySummaryUnderAutoCompactKeepsEverything();
  await emptySummaryOverBudgetDestroysNothing();
  await whitespaceSummaryKeepsEverything();
  await emptySummaryDoesNotCorruptAnExistingSummary();
  await compactionPromptCarriesAttachmentText();
  await toolResultAttachmentTextReachesPrompt();
  await refreshLastMatchesMsgHTMLForAttachment();
  await refreshLastMatchesMsgHTMLForToolResult();
  await aRealSummaryStillCompacts();
  await secondCompactionAppendsDeeperPast();
  await retentionNeverLeavesADanglingToolResult();
  await shortChatIsRefusedWithoutAModelCall();
  /* Review round 1 additions — the invisible-character hole, the null-body shapes, and
     the no-op refreshLast mutation that the original pair of renderer tests missed. */
  await invisibleOnlySummaryKeepsEverything();
  await summaryWithRealTextPlusFormatCharsStillCompacts();
  await nonStringSummaryKeepsEverything();
  await ordinaryProseStillCompacts();
  await refreshLastActuallyRewritesTheNode();
  await repeatedAutoCompactionLosesNothing();
  /* Review round 2 additions. summaryUsableRule() is synchronous and runs first — it
     asserts the RULE directly, which is the assertion whose absence let round 2's
     mutation M8 (a strictly MORE correct rule) pass the suite untouched. */
  summaryUsableRule();
  await glyphlessSummariesAreRefusedByTheRealRite();
  await responseSideArrayContentIsNotStringified();
  await aMidRiteThrowLeavesBothSummaryAndTranscriptIntact();
  await automaticSuccessIsAlsoAnnounced();
  await autoCompactionDoesNotStackModelCalls();
  /* Review round 3 — five findings, each of which stopped one line short of the shape
     it was fixing. */
  await bothOperandsOfTheResponseJoinAreRendered();
  await aPostCommitRenderFailureIsNotReportedAsCompactionFailure();
  await aLatchThatCannotBeScheduledIsReleased();
  await theAutomaticRiteIsBoundToTheChatThatClaimedIt();
  await blankRenderingCodePointsAreRefused();
  const n = failsSnapshot();
  console.log('\nPHASE 20 COMPACTION: ' + n + ' FAILURES');
  process.exit(n === 0 ? 0 : 1);
})();