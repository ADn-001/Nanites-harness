'use strict';
/*
 * PHASE 5 E2E — Dynamic per-endpoint API key (bearer auth).
 *
 * Behavior under test (written BEFORE implementation — TDD RED):
 *  - Profile shape carries apiKey: {id,name,backend,endpoint,model,apiKey}.
 *  - apiKey is optional and backward-compatible: saving a profile without one
 *    defaults it to ''.
 *  - The settings modal exposes an API KEY input (#set-apikey); save persists it
 *    in the profile, load restores it, delete clears nothing global.
 *  - When settings.apiKey is non-empty,requests to the model endpoint carry an
 *    `Authorization: Bearer <key>` header.
 *  - When settings.apiKey is empty, no Authorization header is sent (local
 *    no-auth backends keep working).
 *  - The key persists across a simulated reload (settings blob round-trip).
 */
const path = require('path');
const { check, summary, clearFails, launchApp, teardownApp } = require('./helpers');
const CogCore = require(path.join(__dirname, '..', '..', 'appcore.js'));

const tick = (ms = 30) => new Promise((r) => setTimeout(r, ms));
const fakeAdapter = (seed = {}) => {
  let s = Object.assign({}, seed);
  return {
    getItem: (k) => (k in s ? s[k] : null),
    setItem: (k, v) => { s[k] = String(v); },
    removeItem: (k) => { delete s[k]; },
    dump: () => s
  };
};

const ROUTES = {
  '/v1/models': { status: 200, json: { data: [{ id: 'test-model' }] } },
  '/v1/chat/completions': { status: 200, json: { ok: true } }
};
const PROFKEY = 'cogitator.profiles';

(async () => {
  clearFails();

  /* ========== UNIT: profileStore apiKey field ========== */
  console.log('--- unit: profileStore carries apiKey ---');
  const ad = fakeAdapter();
  const p1 = CogCore.profileStore.save(ad, { name: 'keyed', backend: 'openai', endpoint: 'http://k:1', model: 'm', apiKey: 'sk-secret-abc' });
  check(!!p1 && p1.apiKey === 'sk-secret-abc', 'save persists apiKey');
  check(CogCore.profileStore.apply(ad, p1.id).apiKey === 'sk-secret-abc', 'apply returns apiKey in the slice');
  const p2 = CogCore.profileStore.save(ad, { name: 'nokey', backend: 'ollama', endpoint: 'http://o:2', model: 'q' });
  check(p2 && p2.apiKey === '', 'profile saved without apiKey defaults to empty string');
  const p1b = CogCore.profileStore.save(ad, { id: p1.id, name: 'keyed', backend: 'openai', endpoint: 'http://k:1', model: 'm' });
  check(p1b.apiKey === 'sk-secret-abc', 'upsert without apiKey keeps the existing key');

  /* ========== E2E: form field + save/load roundtrip ========== */
  console.log('--- e2e: #set-apikey field, save/load roundtrip ---');
  const seedSettings = { endpoint: 'http://k:1', model: 'm', backend: 'openai', system: '', profiles: [], activeProfile: '' };
  let app = await launchApp({ routes: ROUTES, seed: { 'cogitator.settings': JSON.stringify(seedSettings) } });
  await tick(40);
  const $a = (id) => app.document.getElementById(id);
  try {
    $a('open-settings').click();
    await tick(30);
    check($a('set-apikey') !== null, 'settings modal exposes an #set-apikey input');
    $a('set-apikey').value = 'sk-secret-abc';
    $a('prof-name').value = 'keyed';
    $a('set-backend').value = 'openai';
    $a('set-endpoint').value = 'http://k:1';
    $a('set-model').value = 'm';
    $a('prof-save').click();
    await tick(30);
    const stored = JSON.parse(app.storage.getItem(PROFKEY) || '[]');
    check(Array.isArray(stored) && stored.length === 1, 'SAVE persists one profile');
    check(stored[0].apiKey === 'sk-secret-abc', 'persisted profile carries the apiKey');
    const savedSettings = JSON.parse(app.storage.getItem('cogitator.settings') || '{}');
    check(savedSettings.apiKey === 'sk-secret-abc', 'saving a profile also records apiKey in the settings blob');

    // switch the key away; LOAD should restore it
    $a('set-apikey').value = '';
    $a('prof-list').value = stored[0].id;
    $a('prof-load').click();
    await tick(30);
    check($a('set-apikey').value === 'sk-secret-abc', 'LOAD restores apiKey into the input');
    const loadedSet = JSON.parse(app.storage.getItem('cogitator.settings') || '{}');
    check(loadedSet.apiKey === 'sk-secret-abc', 'LOAD sets apiKey in the settings blob');
  } finally {
    teardownApp(app);
  }

  /* ========== E2E: Authorization header present when key set ========== */
  console.log('--- e2e: /v1/chat/completions carries Authorization when keyed ---');
  const seedKeyed = { endpoint: 'http://x', model: 'test-model', backend: 'openai', system: '', apiKey: 'sk-bearer-test', profiles: [], activeProfile: '' };
  app = await launchApp({ routes: ROUTES, seed: { 'cogitator.settings': JSON.stringify(seedKeyed) } });
  await tick(60);
  const $b = (id) => app.document.getElementById(id);
  try {
    $b('ta').value = 'hello';
    $b('send-btn').click();
    await tick(140);
    const llm = app.events.filter((e) => e.method === 'POST' && e.url.includes('chat/completions'));
    check(llm.length >= 1, 'a /v1/chat/completions POST was emitted');
    if (llm.length) {
      const h = llm[0].headers || {};
      check(h.Authorization === 'Bearer sk-bearer-test', 'Authorization: Bearer <key> header is sent');
    }
  } finally {
    teardownApp(app);
  }

  /* ========== E2E: no Authorization header when key empty (local backends) ========== */
  console.log('--- e2e: no Authorization header when key empty ---');
  const seedNoKey = { endpoint: 'http://x', model: 'test-model', backend: 'openai', system: '', apiKey: '', profiles: [], activeProfile: '' };
  app = await launchApp({ routes: ROUTES, seed: { 'cogitator.settings': JSON.stringify(seedNoKey) } });
  await tick(60);
  const $c = (id) => app.document.getElementById(id);
  try {
    $c('ta').value = 'hello';
    $c('send-btn').click();
    await tick(140);
    const llm = app.events.filter((e) => e.method === 'POST' && e.url.includes('chat/completions'));
    check(llm.length >= 1, 'a /v1/chat/completions POST was emitted (no-key)');
    if (llm.length) {
      const h = llm[0].headers || {};
      check(!h.Authorization, 'no Authorization header sent when apiKey is empty');
    }
  } finally {
    teardownApp(app);
  }

  /* ========== E2E: reload persistence of the key ========== */
  console.log('--- e2e: apiKey survives a simulated reload ---');
  const seedProfiles = [{ id: 'kp', name: 'keyed-again', backend: 'openai', endpoint: 'http://k:9', model: 'm', apiKey: 'sk-persist' }];
  app = await launchApp({
    routes: ROUTES,
    seed: {
      'cogitator.profiles': JSON.stringify(seedProfiles),
      'cogitator.settings': JSON.stringify({ endpoint: '', model: '', backend: 'auto', system: '', apiKey: 'sk-persist', profiles: [], activeProfile: 'kp' })
    }
  });
  await tick(40);
  try {
    app.document.getElementById('open-settings').click();
    await tick(30);
    const opts = Array.from(app.document.getElementById('prof-list').options).map((o) => o.value);
    check(opts.includes('kp'), 'reloaded profile appears in the dropdown after restart');
    check(app.document.getElementById('set-apikey').value === 'sk-persist', 'reloaded apiKey is shown in the input after restart');
  } finally {
    teardownApp(app);
  }

  process.exit(summary('PHASE 5 API KEY'));
})().catch((e) => { console.error(e); process.exit(1); });