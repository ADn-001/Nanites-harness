'use strict';
/*
 * PHASE 10 (workstream B) — LOCAL CORTEX frontend config + `CogCore.localModels` client.
 *
 * The whole machinery must exist and be INERT. Written RED-first: nothing in this
 * file passes until (a) `appcore.js` grows the never-throwing injected-fetch client and
 * (b) `index.html` grows the `settings.localModels` block and the LOCAL CORTEX section.
 *
 * The two gates that matter most:
 *   - with `localModels.enabled=false` NO request is ever made to :8932 (the sidecar),
 *   - with the sidecar DOWN every client call resolves `{ok:false,degraded:true}` — never throws,
 *     never rejects.
 *
 * Harness facts used here (see tests/frontend/helpers.js):
 *   - the inline script declares `const DEF_SETTINGS` / `let settings`, so they are NOT window
 *     properties: read them back with `app.window.eval('JSON.stringify(settings)')`.
 *   - `launchApp` records every fetch in `app.events` ({url, method, body, headers}); unmocked
 *     URLs reject with a TypeError, exactly like a dead endpoint.
 *   - jsdom has no `AbortSignal.timeout` — the client must guard it, so we DELETE the static and
 *     assert the call still resolves rather than stubbing the guard away.
 */
const path = require('path');
const { check, summary, clearFails, launchApp, teardownApp } = require('./helpers');
const CogCore = require(path.join(__dirname, '..', '..', 'appcore.js'));

const tick = (ms = 40) => new Promise((r) => setTimeout(r, ms));

const $ = (app, id) => app.document.getElementById(id);
const live = (app) => JSON.parse(app.window.eval('JSON.stringify(settings)'));
const setVal = (app, id, v) => { const el = $(app, id); if (el) el.value = v; };
const setChk = (app, id, v) => { const el = $(app, id); if (el) el.checked = !!v; };
const ev8932 = (app) => app.events.filter((e) => String(e.url).indexOf(':8932') !== -1);

/* Exact URL of the sidecar's health route for a given port. */
const healthUrl = (port) => 'http://127.0.0.1:' + port + '/health';

/* A tiny fetch spy: records every init, then defers to `handler`. */
function fakeFetch(handler) {
  const calls = [];
  const f = (url, init) => {
    calls.push({
      url: String(url),
      method: String((init && init.method) || 'GET').toUpperCase(),
      body: init && init.body === undefined ? null : init.body,
      headers: Object.assign({}, init && init.headers)
    });
    return handler(url, init);
  };
  f.calls = calls;
  return f;
}
const okJson = (json) => fakeFetch(() => Promise.resolve({
  ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(json))
}));
/* Header keys are case-insensitive; the client must never send an Authorization header at all. */
const hasAuthHeader = (headers) =>
  Object.keys(headers || {}).some((k) => String(k).toLowerCase() === 'authorization');

(async () => {
  clearFails();

  /* ==================================================================== *
   * 1. DEFAULTS ARE OFF — a plain boot changes nothing                    *
   * ==================================================================== */
  console.log('--- defaults: LOCAL CORTEX ships OFF and inert ---');
  const appD = await launchApp({ routes: { '/v1/models': { status: 200, json: { data: [{ id: 'test-model' }] } } } });
  await tick(60);
  try {
    const s = live(appD);
    check(!!s.localModels, 'settings.localModels exists after boot');
    const lm = s.localModels || {};
    check(lm.enabled === false, 'GUARD settings.localModels.enabled === false (got ' + JSON.stringify(lm.enabled) + ')');
    check(!!lm.needle && lm.needle.enabled === false, 'GUARD localModels.needle.enabled === false');
    check(!!lm.laya && lm.laya.enabled === false, 'GUARD localModels.laya.enabled === false');
    check(!!lm.dispatcher && lm.dispatcher.enabled === false, 'GUARD localModels.dispatcher.enabled === false');
    check(!!lm.sanitizer && lm.sanitizer.enabled === true, 'GUARD localModels.sanitizer.enabled === true (deterministic pass is free)');
    check(lm.port === 8932, 'GUARD localModels.port === 8932 (got ' + JSON.stringify(lm.port) + ')');
    check(lm.ledger === 'var/local-models.jsonl', 'localModels.ledger === "var/local-models.jsonl" (got ' + JSON.stringify(lm.ledger) + ')');
    /* the nested shapes the later phases rely on must be present, not undefined */
    check(!!lm.needle && lm.needle.minConfidence === 0.75 && Array.isArray(lm.needle.confirmBand)
      && lm.needle.confirmBand[0] === 0.5 && lm.needle.confirmBand[1] === 0.75 && lm.needle.timeoutMs === 800,
      'localModels.needle is fully shaped (minConfidence 0.75, confirmBand [0.5,0.75], timeoutMs 800)');
    check(!!lm.laya && lm.laya.minConfidence === 0.70 && lm.laya.timeoutMs === 500
      && lm.laya.preflight === true && lm.laya.anomaly === true,
      'localModels.laya is fully shaped (minConfidence 0.70, timeoutMs 500, preflight/anomaly true)');
    check(!!lm.sanitizer && lm.sanitizer.mode === 'auto' && lm.sanitizer.deterministicPass === true,
      'localModels.sanitizer is fully shaped (mode "auto", deterministicPass true)');
    check(!!lm.dispatcher && lm.dispatcher.autoReadOnly === true,
      'localModels.dispatcher is fully shaped (autoReadOnly true)');

    /* Defaults are byte-comparable with the plan §4 block. */
    check(JSON.stringify(lm) === JSON.stringify(CogCore.localModels.DEFAULTS),
      'live settings.localModels deep-equals CogCore.localModels.DEFAULTS');
  } finally { teardownApp(appD); }

  /* ==================================================================== *
   * 2. ZERO REQUESTS TO :8932 WITH DEFAULTS (the phase's hard gate)        *
   * ==================================================================== */
  console.log('--- gate: enabled=false => not one request ever reaches :8932 ---');
  const appZ = await launchApp({ routes: { '/v1/models': { status: 200, json: { data: [{ id: 'test-model' }] } } } });
  await tick(80);
  try {
    check(ev8932(appZ).length === 0, 'no request to :8932 during a full boot with defaults (got ' + ev8932(appZ).length + ')');
    $(appZ, 'open-settings').click();
    await tick(80);
    check(ev8932(appZ).length === 0, 'no request to :8932 after opening the settings modal (got ' + ev8932(appZ).length + ')');
    check($(appZ, 'lm-status') !== null, 'the settings modal carries a #lm-status line');
    check($(appZ, 'test-cortex') !== null, 'the settings modal carries a #test-cortex button');
    check($(appZ, 'set-lm-enabled') !== null && $(appZ, 'set-lm-port') !== null,
      'the settings modal carries the LOCAL CORTEX controls (set-lm-enabled / set-lm-port)');
  } finally { teardownApp(appZ); }

  /* ==================================================================== *
   * 3. NORMALISER — a partial/legacy stored blob cannot blank the shape   *
   * ==================================================================== */
  console.log('--- normaliser: partial stored blob merges over the defaults ---');
  const appP = await launchApp({
    routes: { '/v1/models': { status: 200, json: { data: [{ id: 'test-model' }] } } },
    seed: { 'cogitator.settings': JSON.stringify({ localModels: { enabled: true, needle: { enabled: true }, port: 9001 } }) }
  });
  await tick(60);
  try {
    const lm = (live(appP).localModels) || {};
    check(lm.enabled === true, 'stored localModels.enabled:true survives the reload');
    check(!!lm.needle && lm.needle.enabled === true, 'stored localModels.needle.enabled:true survives the reload');
    check(lm.port === 9001, 'stored localModels.port:9001 survives the reload (got ' + JSON.stringify(lm.port) + ')');
    check(!!lm.laya && lm.laya.timeoutMs === 500, 'UNTOUCHED nested default kept: localModels.laya.timeoutMs === 500');
    check(!!lm.laya && lm.laya.enabled === false, 'UNTOUCHED nested default kept: localModels.laya.enabled === false');
    check(!!lm.sanitizer && lm.sanitizer.mode === 'auto', 'UNTOUCHED nested default kept: localModels.sanitizer.mode === "auto"');
    check(!!lm.needle && lm.needle.minConfidence === 0.75, 'UNTOUCHED nested default kept: localModels.needle.minConfidence === 0.75');
    check(!!lm.dispatcher && lm.dispatcher.autoReadOnly === true, 'UNTOUCHED nested default kept: localModels.dispatcher.autoReadOnly === true');
    check(lm.ledger === 'var/local-models.jsonl', 'UNTOUCHED top-level default kept: localModels.ledger');

    /* a legacy blob with NO localModels key at all must still be fully shaped */
    const appL = await launchApp({
      routes: { '/v1/models': { status: 200, json: { data: [{ id: 'test-model' }] } } },
      seed: { 'cogitator.settings': JSON.stringify({ endpoint: 'http://x', model: 'm', backend: 'openai' }) }
    });
    await tick(60);
    try {
      const lml = (live(appL).localModels) || {};
      check(lml.enabled === false && !!lml.needle && !!lml.laya && !!lml.sanitizer && !!lml.dispatcher,
        'a legacy settings blob with no localModels key still gets the full default shape');
    } finally { teardownApp(appL); }

    /* a hostile blob (wrong types) must not throw and must not leave undefined sub-objects */
    const appH = await launchApp({
      routes: { '/v1/models': { status: 200, json: { data: [{ id: 'test-model' }] } } },
      seed: { 'cogitator.settings': JSON.stringify({ localModels: 'corrupt' }) }
    });
    await tick(60);
    try {
      const lmh = (live(appH).localModels) || {};
      check(!!lmh && lmh.enabled === false && !!lmh.needle && !!lmh.laya && !!lmh.sanitizer && !!lmh.dispatcher,
        'a corrupt (non-object) localModels blob falls back to the full defaults without throwing');
      check(appH.bootErrors.length === 0, 'boot with a corrupt localModels blob produced no uncaught errors');
    } finally { teardownApp(appH); }
  } finally { teardownApp(appP); }

  /* ==================================================================== *
   * 4. MODAL ROUND-TRIP (write -> save -> reopen -> restore)              *
   * ==================================================================== */
  console.log('--- modal: every set-lm-* field round-trips through INSCRIBE ---');
  const appM = await launchApp({ routes: { '/v1/models': { status: 200, json: { data: [{ id: 'test-model' }] } } } });
  await tick(60);
  try {
    $(appM, 'open-settings').click();
    await tick(40);

    check($(appM, 'set-lm-needle') !== null && $(appM, 'set-lm-needle-conf') !== null
      && $(appM, 'set-lm-needle-timeout') !== null && $(appM, 'set-lm-laya') !== null
      && $(appM, 'set-lm-laya-conf') !== null && $(appM, 'set-lm-laya-timeout') !== null
      && $(appM, 'set-lm-laya-preflight') !== null && $(appM, 'set-lm-laya-anomaly') !== null
      && $(appM, 'set-lm-sanitizer') !== null && $(appM, 'set-lm-dispatcher') !== null
      && $(appM, 'set-lm-dispatcher-autoread') !== null && $(appM, 'set-lm-ledger') !== null,
      'all LOCAL CORTEX inputs exist in the settings modal');

    check($(appM, 'set-lm-needle-conf').getAttribute('step') === '.05',
      'set-lm-needle-conf carries step=".05" (got ' + $(appM, 'set-lm-needle-conf').getAttribute('step') + ')');
    check($(appM, 'set-lm-laya-conf').getAttribute('step') === '.05',
      'set-lm-laya-conf carries step=".05" (got ' + $(appM, 'set-lm-laya-conf').getAttribute('step') + ')');

    /* the section must sit after AUTO-AUTHORIZE READ-ONLY RITES and before the CANTICLE textarea */
    const lmBody = appM.document.body.innerHTML;
    const iAutoRead = lmBody.indexOf('set-auto-read');
    const iCortex = lmBody.indexOf('set-lm-enabled');
    const iCanticle = lmBody.indexOf('set-sys');
    check(iCortex !== -1 && iAutoRead !== -1 && iCanticle !== -1 && iAutoRead < iCortex && iCortex < iCanticle,
      'LOCAL CORTEX section is placed after set-auto-read and before the CANTICLE textarea');

    /* the copy must state OPT-IN / OFF BY DEFAULT / LOCAL (legible, not just grim) */
    const cortexBlock = lmBody.slice(iCortex, iCanticle).toUpperCase();
    check(/OPT-IN/.test(cortexBlock), 'LOCAL CORTEX copy states these models are OPT-IN');
    check(/OFF BY DEFAULT/.test(cortexBlock), 'LOCAL CORTEX copy states OFF BY DEFAULT');
    check(/LOCAL/.test(cortexBlock), 'LOCAL CORTEX copy states they run locally');

    /* defaults are restored into the inputs */
    check($(appM, 'set-lm-enabled').checked === false, 'set-lm-enabled is unchecked by default');
    check($(appM, 'set-lm-sanitizer').checked === true, 'set-lm-sanitizer is checked by default');
    check(String($(appM, 'set-lm-port').value) === '8932', 'set-lm-port shows 8932 by default (got ' + $(appM, 'set-lm-port').value + ')');
    check(String($(appM, 'set-lm-ledger').value) === 'var/local-models.jsonl', 'set-lm-ledger shows the default ledger path');
    check(String($(appM, 'set-lm-needle-conf').value) === '0.75', 'set-lm-needle-conf shows 0.75 by default (got ' + $(appM, 'set-lm-needle-conf').value + ')');

    /* write every field, then INSCRIBE */
    setChk(appM, 'set-lm-enabled', true);
    setChk(appM, 'set-lm-needle', true);
    setVal(appM, 'set-lm-needle-conf', '0.85');
    setVal(appM, 'set-lm-needle-timeout', '950');
    setChk(appM, 'set-lm-laya', true);
    setVal(appM, 'set-lm-laya-conf', '0.65');
    setVal(appM, 'set-lm-laya-timeout', '650');
    setChk(appM, 'set-lm-laya-preflight', false);
    setChk(appM, 'set-lm-laya-anomaly', false);
    setChk(appM, 'set-lm-sanitizer', false);
    setChk(appM, 'set-lm-dispatcher', true);
    setChk(appM, 'set-lm-dispatcher-autoread', false);
    setVal(appM, 'set-lm-port', '9100');
    setVal(appM, 'set-lm-ledger', 'var/test-ledger.jsonl');
    $(appM, 'save-settings').click();
    await tick(50);

    const saved = (live(appM).localModels) || {};
    check(saved.enabled === true, 'INSCRIBE wrote enabled:true');
    check(!!saved.needle && saved.needle.enabled === true, 'INSCRIBE wrote needle.enabled:true');
    check(!!saved.needle && saved.needle.minConfidence === 0.85, 'INSCRIBE wrote needle.minConfidence:0.85 (got ' + (saved.needle || {}).minConfidence + ')');
    check(!!saved.needle && saved.needle.timeoutMs === 950, 'INSCRIBE wrote needle.timeoutMs:950 (got ' + (saved.needle || {}).timeoutMs + ')');
    check(!!saved.laya && saved.laya.enabled === true, 'INSCRIBE wrote laya.enabled:true');
    check(!!saved.laya && saved.laya.minConfidence === 0.65, 'INSCRIBE wrote laya.minConfidence:0.65 (got ' + (saved.laya || {}).minConfidence + ')');
    check(!!saved.laya && saved.laya.timeoutMs === 650, 'INSCRIBE wrote laya.timeoutMs:650 (got ' + (saved.laya || {}).timeoutMs + ')');
    check(!!saved.laya && saved.laya.preflight === false, 'INSCRIBE wrote laya.preflight:false');
    check(!!saved.laya && saved.laya.anomaly === false, 'INSCRIBE wrote laya.anomaly:false');
    check(!!saved.sanitizer && saved.sanitizer.enabled === false, 'INSCRIBE wrote sanitizer.enabled:false');
    check(!!saved.dispatcher && saved.dispatcher.enabled === true, 'INSCRIBE wrote dispatcher.enabled:true');
    check(!!saved.dispatcher && saved.dispatcher.autoReadOnly === false, 'INSCRIBE wrote dispatcher.autoReadOnly:false');
    check(saved.port === 9100, 'INSCRIBE wrote port:9100 as a NUMBER (got ' + JSON.stringify(saved.port) + ')');
    check(saved.ledger === 'var/test-ledger.jsonl', 'INSCRIBE wrote ledger:"var/test-ledger.jsonl"');
    /* fields the modal does not expose must not be destroyed by a save */
    check(!!saved.needle && Array.isArray(saved.needle.confirmBand) && saved.needle.confirmBand.length === 2,
      'INSCRIBE did not destroy needle.confirmBand (still a 2-tuple)');
    check(!!saved.sanitizer && saved.sanitizer.mode === 'auto' && saved.sanitizer.deterministicPass === true,
      'INSCRIBE did not destroy sanitizer.mode/deterministicPass');

    /* persistence: the blob really reached localStorage */
    const persisted = JSON.parse(appM.storage.getItem('cogitator.settings') || '{}');
    check(!!persisted.localModels && persisted.localModels.port === 9100 && persisted.localModels.needle.minConfidence === 0.85,
      'settings.localModels was persisted to localStorage by saveSet()');

    /* reopen -> inputs restored */
    $(appM, 'open-settings').click();
    await tick(40);
    check($(appM, 'set-lm-enabled').checked === true, 'reopened modal restores set-lm-enabled');
    check($(appM, 'set-lm-needle').checked === true, 'reopened modal restores set-lm-needle');
    check(String($(appM, 'set-lm-needle-conf').value) === '0.85', 'reopened modal restores set-lm-needle-conf (got ' + $(appM, 'set-lm-needle-conf').value + ')');
    check(String($(appM, 'set-lm-needle-timeout').value) === '950', 'reopened modal restores set-lm-needle-timeout');
    check(String($(appM, 'set-lm-laya-timeout').value) === '650', 'reopened modal restores set-lm-laya-timeout');
    check($(appM, 'set-lm-laya-preflight').checked === false, 'reopened modal restores set-lm-laya-preflight');
    check($(appM, 'set-lm-dispatcher').checked === true, 'reopened modal restores set-lm-dispatcher');
    check($(appM, 'set-lm-dispatcher-autoread').checked === false, 'reopened modal restores set-lm-dispatcher-autoread');
    check(String($(appM, 'set-lm-port').value) === '9100', 'reopened modal restores set-lm-port (got ' + $(appM, 'set-lm-port').value + ')');
    check(String($(appM, 'set-lm-ledger').value) === 'var/test-ledger.jsonl', 'reopened modal restores set-lm-ledger');
  } finally { teardownApp(appM); }

  /* ==================================================================== *
   * 5. UNIT: CogCore.localModels.client — never throws, never rejects     *
   * ==================================================================== */
  console.log('--- unit: CogCore.localModels.client ---');
  check(!!CogCore.localModels && typeof CogCore.localModels.client === 'function',
    'CogCore.localModels.client is a function');
  check(!!CogCore.localModels.DEFAULTS && CogCore.localModels.DEFAULTS.port === 8932,
    'CogCore.localModels.DEFAULTS carries the settings block');

  const BASE = 'http://127.0.0.1:8932';
  const C = CogCore.localModels.client;

  /* success — and the request must be a bare GET with no Authorization header */
  const ffOk = okJson({ ok: true, needle: { loaded: false }, version: '0.0.1' });
  const hOk = await C(BASE, ffOk).health();
  check(hOk.ok === true, 'health() on a healthy sidecar resolves ok:true');
  check(!!hOk.needle && hOk.needle.loaded === false, 'health() result carries the payload (needle.loaded === false)');
  check(ffOk.calls.length === 1, 'health() made exactly one request');
  check(ffOk.calls[0].method === 'GET', 'health() uses method GET (got ' + ffOk.calls[0].method + ')');
  check(/\/health$/.test(ffOk.calls[0].url), 'health() hits /health (got ' + ffOk.calls[0].url + ')');
  check(!hasAuthHeader(ffOk.calls[0].headers), 'health() sends NO Authorization header ever (got ' + JSON.stringify(ffOk.calls[0].headers) + ')');
  check(typeof hOk.latency_ms === 'number' && !Number.isNaN(hOk.latency_ms), 'health() success carries a numeric latency_ms (got ' + hOk.latency_ms + ')');
  check(hOk.degraded !== true, 'health() success is not marked degraded');

  /* trailing slashes in `base` are tolerated (no '//health') */
  const ffSlash = okJson({ ok: true });
  await C(BASE + '///', ffSlash).health();
  check(ffSlash.calls.length === 1 && ffSlash.calls[0].url === healthUrl(8932),
    'trailing slashes in base are tolerated (got ' + (ffSlash.calls[0] || {}).url + ')');

  /* a JSON body with no `ok` field is forced to ok:true */
  const ffNoOk = okJson({ needle: { loaded: true } });
  const rNoOk = await C(BASE, ffNoOk).health();
  check(rNoOk.ok === true, 'a response body with no `ok` field is normalised to ok:true');
  /* ...but the server's explicit ok:false is kept */
  const ffSaysNo = okJson({ ok: false, degraded: true, reason: 'needle down' });
  const rSaysNo = await C(BASE, ffSaysNo).health();
  check(rSaysNo.ok === false, "the server's own ok:false is preserved, not overwritten");

  /* network failure -> degraded, never a rejection */
  const ffNet = fakeFetch(() => Promise.reject(new TypeError('NetworkError: connection refused')));
  let threw = false, hNet = null;
  try { hNet = await C(BASE, ffNet).health(); } catch (e) { threw = true; }
  check(!threw, 'a rejecting fetch does NOT reject health() (never throws)');
  check(!!hNet && hNet.ok === false && hNet.degraded === true, 'network failure resolves {ok:false, degraded:true}');
  check(!!hNet && typeof hNet.error === 'string' && hNet.error.length > 0, 'network failure carries a short error string (got ' + JSON.stringify(hNet && hNet.error) + ')');

  /* a synchronously-throwing fetch must be caught too */
  const ffThrow = fakeFetch(() => { throw new Error('exploded before the promise'); });
  let threwSync = false, hThrow = null;
  try { hThrow = await C(BASE, ffThrow).health(); } catch (e) { threwSync = true; }
  check(!threwSync && !!hThrow && hThrow.ok === false && hThrow.degraded === true,
    'a fetch that throws synchronously still resolves {ok:false, degraded:true}');

  /* non-2xx */
  const ff503 = fakeFetch(() => Promise.resolve({ ok: false, status: 503, json: async () => ({}) }));
  const h503 = await C(BASE, ff503).health();
  check(h503.ok === false && h503.degraded === true, 'a non-2xx (503) response resolves {ok:false, degraded:true}');
  check(typeof h503.error === 'string' && h503.error.indexOf('503') !== -1, 'the 503 error string names the status (got ' + JSON.stringify(h503.error) + ')');

  /* unparseable JSON */
  const ffBad = fakeFetch(() => Promise.resolve({ ok: true, status: 200, json: async () => { throw new SyntaxError('Unexpected token <'); } }));
  let threwBad = false, hBad = null;
  try { hBad = await C(BASE, ffBad).health(); } catch (e) { threwBad = true; }
  check(!threwBad && !!hBad && hBad.ok === false && hBad.degraded === true, 'unparseable JSON resolves {ok:false, degraded:true}');

  /* a response object with no json() at all */
  const ffNoJson = fakeFetch(() => Promise.resolve({ ok: true, status: 200 }));
  const hNoJson = await C(BASE, ffNoJson).health();
  check(hNoJson.ok === false && hNoJson.degraded === true, 'a response with no json() resolves {ok:false, degraded:true}');

  /* route mapping + POST body shape for the four POST routes */
  const routes = [
    { method: 'repair', route: '/repair', payload: { suspect: { name: 'read_file', arguments: '{}' }, candidates: [] }, tmo: undefined },
    { method: 'decide', route: '/decide', payload: { state: 'x', questions: {} }, tmo: 500 },
    { method: 'selectTool', route: '/select', payload: { input: 'hi', candidates: [] }, tmo: 800 },
    { method: 'outcome', route: '/ledger', payload: { trace_id: 't1', action: 'accepted' }, tmo: 1500 }
  ];
  for (const r of routes) {
    const ff = okJson({ ok: true });
    await C(BASE, ff)[r.method](r.payload, r.tmo ? { timeoutMs: r.tmo } : undefined);
    const call = ff.calls[0] || {};
    check(ff.calls.length === 1, r.method + '() made exactly one request');
    check(call.method === 'POST', r.method + '() uses method POST (got ' + call.method + ')');
    check(call.url === BASE + r.route, r.method + '() hits ' + r.route + ' (got ' + call.url + ')');
    check(typeof call.body === 'string' && call.body === JSON.stringify(r.payload),
      r.method + '() body is JSON.stringify(payload) (got ' + call.body + ')');
    const ct = Object.keys(call.headers || {}).reduce((acc, k) => (String(k).toLowerCase() === 'content-type' ? call.headers[k] : acc), '');
    check(String(ct).indexOf('application/json') !== -1, r.method + '() sends Content-Type: application/json (got ' + JSON.stringify(call.headers) + ')');
    check(!hasAuthHeader(call.headers), r.method + '() sends NO Authorization header ever');
  }
  /* a missing/undefined payload still produces a valid JSON object body */
  const ffEmpty = okJson({ ok: true });
  await C(BASE, ffEmpty).repair();
  check(ffEmpty.calls.length === 1 && ffEmpty.calls[0].body === '{}',
    'repair() with no payload POSTs "{}" (got ' + JSON.stringify((ffEmpty.calls[0] || {}).body) + ')');

  /* no fetchImpl and no usable global fetch -> degraded, never a rejection */
  const Cbare = require(path.join(__dirname, '..', '..', 'appcore.js')).localModels.client;
  let threwBare = false, hBare = null;
  try { hBare = await Cbare(BASE).health(); }
  catch (e) { threwBare = true; }
  check(!threwBare && !!hBare && hBare.ok === false && hBare.degraded === true,
    'no fetchImpl and no global fetch resolves {ok:false, degraded:true} (never throws)');

  /* injected clock is honoured (elapsed ms), and its absence is fine */
  let t = 1000;
  const clock = { now: () => { const v = t; t += 25; return v; } };
  const ffClock = okJson({ ok: true });
  const rClock = await C(BASE, ffClock, clock).health();
  check(typeof rClock.latency_ms === 'number' && rClock.latency_ms >= 0,
    'latency_ms comes from the injected clock (got ' + rClock.latency_ms + ')');
  const rNoClock = await C(BASE, okJson({ ok: true })).health();
  check(typeof rNoClock.latency_ms === 'number', 'omitting the clock still yields a numeric latency_ms');

  /* ==================================================================== *
   * 6a. AbortSignal.timeout is ABSENT in jsdom — the guard must hold       *
   * ==================================================================== */
  console.log('--- guard: no AbortSignal.timeout (jsdom) => the call still resolves ---');
  const appA = await launchApp({
    routes: { '/health': { status: 200, json: { ok: true, needle: { loaded: true } } } },
    seed: { 'cogitator.settings': JSON.stringify({ localModels: { enabled: true } }) }
  });
  await tick(60);
  try {
    const w = appA.window;
    let hadTimeout = false;
    try { hadTimeout = !!(w.AbortSignal && w.AbortSignal.timeout); } catch (e) { hadTimeout = false; }
    try { if (w.AbortSignal) delete w.AbortSignal.timeout; } catch (e) { /* read-only in some realms */ }
    check(!w.AbortSignal || !w.AbortSignal.timeout, 'AbortSignal.timeout is genuinely absent in the jsdom realm');

    const inPage = w.CogCore.localModels.client;
    let threwA = false, rA = null;
    try { rA = await inPage('http://127.0.0.1:8932', undefined, undefined).health(); }
    catch (e) { threwA = true; }
    check(!threwA, 'health() with no AbortSignal.timeout does not throw (hadTimeout=' + hadTimeout + ')');
    check(!!rA && rA.ok === true && !!rA.needle && rA.needle.loaded === true,
      'health() still resolves the parsed payload with no AbortSignal.timeout');
    check(appA.bootErrors.length === 0, 'no uncaught js error was raised by the guard path');
  } finally { teardownApp(appA); }

  /* ==================================================================== *
   * 6b. #lm-status — DISABLED, healthy, and sidecar-DOWN                 *
   * ==================================================================== */
  console.log('--- status line: DISABLED / healthy / sidecar down ---');
  const appS = await launchApp({
    routes: { '/v1/models': { status: 200, json: { data: [{ id: 'test-model' }] } } },
    seed: { 'cogitator.settings': JSON.stringify({ localModels: { enabled: false } }) }
  });
  await tick(60);
  try {
    $(appS, 'open-settings').click();
    await tick(40);
    $(appS, 'test-cortex').click();
    await tick(60);
    const txt = String($(appS, 'lm-status').textContent || '');
    check(/DISABLED/.test(txt.toUpperCase()), '#lm-status renders a DISABLED line when enabled=false (got "' + txt + '")');
    check(ev8932(appS).length === 0, 'clicking TEST CORTEX with enabled=false still makes ZERO :8932 requests (got ' + ev8932(appS).length + ')');
  } finally { teardownApp(appS); }

  /* healthy sidecar */
  const HEALTHY = {
    ok: true,
    version: '0.0.1',
    needle: { enabled: true, loaded: false, weights: 'missing', generation: 3, lib: 'linux-arm64' },
    laya: { enabled: true, loaded: false, child_pid: null, cache: '~/.cache/receptron-laya' },
    ledger: { path: 'var/local-models.jsonl', writable: true },
    degraded: ['needle weights missing']
  };
  const appH2 = await launchApp({
    routes: { '/health': { status: 200, json: HEALTHY }, '/v1/models': { status: 200, json: { data: [{ id: 'test-model' }] } } },
    seed: { 'cogitator.settings': JSON.stringify({ localModels: { enabled: true, port: 8932 } }) }
  });
  await tick(60);
  try {
    $(appH2, 'test-cortex').click();
    await tick(80);
    const txt = String($(appH2, 'lm-status').textContent || '');
    check(/needle/i.test(txt), '#lm-status on a healthy sidecar names needle (got "' + txt + '")');
    check(/laya/i.test(txt), '#lm-status on a healthy sidecar names laya (got "' + txt + '")');
    check(txt.indexOf('var/local-models.jsonl') !== -1, '#lm-status on a healthy sidecar reports the ledger path (got "' + txt + '")');
    check(txt.indexOf('missing') !== -1, '#lm-status surfaces the degraded reason (weights missing) (got "' + txt + '")');
    check(!/OFFLINE/i.test(txt), '#lm-status does NOT say OFFLINE when the sidecar answered (got "' + txt + '")');
    check(ev8932(appH2).length >= 1, 'a healthy poll really did reach :8932 (got ' + ev8932(appH2).length + ')');
    check(appH2.bootErrors.length === 0, 'the healthy poll produced no uncaught js error');
  } finally { teardownApp(appH2); }

  /* sidecar DOWN: enabled:true, no route mocked -> fetch rejects */
  const appDown = await launchApp({
    routes: { '/v1/models': { status: 200, json: { data: [{ id: 'test-model' }] } } },
    seed: { 'cogitator.settings': JSON.stringify({ localModels: { enabled: true, port: 8932 } }) }
  });
  await tick(60);
  try {
    let threwDown = false;
    try { $(appDown, 'test-cortex').click(); } catch (e) { threwDown = true; }
    await tick(80);
    check(!threwDown, 'clicking TEST CORTEX with the sidecar DOWN does not throw');
    const txt = String($(appDown, 'lm-status').textContent || '');
    check(/OFFLINE/i.test(txt) && /DEGRADED/i.test(txt), '#lm-status with the sidecar down renders the OFFLINE (degraded) line (got "' + txt + '")');
    check(appDown.bootErrors.length === 0, 'a DOWN sidecar produced no uncaught boot errors');
    /* and the app is still usable: the settings modal closes normally */
    $(appDown, 'save-settings').click();
    await tick(40);
    check(!$(appDown, 'settings-modal').classList.contains('open'),
      'the settings modal still closes cleanly with a DOWN sidecar');
  } finally { teardownApp(appDown); }

  /* refreshCortexStatus must be safe when #lm-status is absent from the document */
  const appNoEl = await launchApp({ seed: { 'cogitator.settings': JSON.stringify({ localModels: { enabled: true } }) } });
  await tick(60);
  try {
    const el = $(appNoEl, 'lm-status');
    if (el && el.parentNode) el.parentNode.removeChild(el);
    let threwAbsent = false;
    try { await appNoEl.window.refreshCortexStatus(); } catch (e) { threwAbsent = true; }
    check(!threwAbsent, 'refreshCortexStatus() is safe when #lm-status is absent');
  } finally { teardownApp(appNoEl); }

  process.exit(summary('PHASE 10 LOCAL CORTEX CONFIG'));
})().catch((e) => { console.error('PHASE10 CATCH:', e); process.exit(1); });
