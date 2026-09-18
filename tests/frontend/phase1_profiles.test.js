/*
 * phase1_profiles.test.js — Phase 1 gate: provider endpoint profiles.
 * Covers unit profileStore CRUD plus e2e UI roundtrip (save / load-apply /
 * delete-clears-active / persistence across a simulated reload).
 */
'use strict';
const path = require('path');
const { check, summary, clearFails, launchApp, teardownApp } = require('./helpers');
const CogCore = require(path.join(__dirname, '..', '..', 'appcore.js'));

const tick = (ms = 25) => new Promise((r) => setTimeout(r, ms));
const fakeAdapter = (seed = {}) => {
  let s = Object.assign({}, seed);
  return {
    getItem: (k) => (k in s ? s[k] : null),
    setItem: (k, v) => { s[k] = String(v); },
    removeItem: (k) => { delete s[k]; },
    dump: () => s
  };
};

const MODEL = { '/v1/models': { status: 200, json: { data: [{ id: 'deepseek-r1' }] } } };
const PROFKEY = 'cogitator.profiles';

(async () => {
  clearFails();

  /* ============ UNIT: profileStore CRUD ============ */
  console.log('--- profileStore unit ---');
  const ad = fakeAdapter();
  // save new
  const p1 = CogCore.profileStore.save(ad, { name: 'deepseek-srv', backend: 'openai', endpoint: 'http://ds:11434', model: 'deepseek-r1' });
  check(!!p1 && !!p1.id, 'save assigns an id');
  check(p1.name === 'deepseek-srv' && p1.backend === 'openai' && p1.endpoint === 'http://ds:11434' && p1.model === 'deepseek-r1', 'save keeps all fields');
  check(CogCore.profileStore.list(ad).length === 1, 'list shows the saved profile');
  // save upsert by id
  const p1b = CogCore.profileStore.save(ad, { id: p1.id, name: 'renamed', backend: 'ollama', endpoint: 'http://o:11434', model: 'qwen' });
  check(p1b.id === p1.id, 'save with existing id updates in place');
  check(CogCore.profileStore.list(ad).length === 1 && CogCore.profileStore.get(ad, p1.id).name === 'renamed', 'upsert did not duplicate');
  // save second
  CogCore.profileStore.save(ad, { name: 'lm', backend: 'lmstudio', endpoint: 'http://lm:1234', model: 'nous' });
  check(CogCore.profileStore.list(ad).length === 2, 'multiple profiles coexist');
  // apply
  const applied = CogCore.profileStore.apply(ad, p1.id);
  check(applied && applied.backend === 'ollama' && applied.model === 'qwen', 'apply returns the profile slice to use');
  check(CogCore.profileStore.apply(ad, 'nope') === null, 'apply on unknown id returns null');
  // remove
  CogCore.profileStore.remove(ad, p1.id);
  check(CogCore.profileStore.list(ad).length === 1, 'remove deletes only the named profile');
  CogCore.profileStore.remove(ad, 'lm');
  check(CogCore.profileStore.list(ad).length === 0, 'remove last leaves empty');
  // default name when blank
  const pdef = CogCore.profileStore.save(ad, { backend: 'auto', endpoint: '' });
  check(pdef.name && pdef.name.length > 0, 'blank name gets a default');

  /* ============ E2E: save from the settings form ============ */
  console.log('--- e2e: save / load / delete / reload ---');
  const seedSettings = { endpoint: 'http://ds:11434', model: 'deepseek-r1', backend: 'openai', system: '', profiles: [], activeProfile: '' };
  let app = await launchApp({ routes: MODEL, seed: { 'cogitator.settings': JSON.stringify(seedSettings) } });
  await tick(40);
  const { window, document, storage } = app;

  const $ = (id) => document.getElementById(id);
  const setVal = (id, v) => { const el = $(id); el.value = v; };

  try {
    // open settings
    $('open-settings').click();
    await tick(30);
    check($('prof-list') !== null, 'PROVIDER PROFILES section present (prof-list)');
    check($('prof-save') !== null && $('prof-load') !== null && $('prof-del') !== null, 'SAVE/LOAD/DELETE present');

    // fill + save a profile
    setVal('prof-name', 'deepseek-srv');
    setVal('set-backend', 'openai');
    setVal('set-endpoint', 'http://ds:11434');
    setVal('set-model', 'deepseek-r1');
    $('prof-save').click();
    await tick(20);

    const stored = JSON.parse(storage.getItem(PROFKEY) || '[]');
    check(Array.isArray(stored) && stored.length === 1, 'SAVE persists one profile to storage');
    check(stored[0].backend === 'openai' && stored[0].endpoint === 'http://ds:11434' && stored[0].model === 'deepseek-r1', 'persisted profile carries backend+endpoint+model');
    const opts = Array.from($('prof-list').options).map((o) => o.value);
    check(opts.includes(stored[0].id), 'saved profile appears in the dropdown');
    const profId = stored[0].id;

    // switch inputs away, LOAD should restore
    setVal('set-endpoint', 'http://other:9999');
    setVal('set-backend', 'ollama');
    setVal('set-model', 'qwen');
    $('prof-list').value = profId;
    $('prof-load').click();
    await tick(30);
    check($('set-endpoint').value === 'http://ds:11434', 'LOAD restores endpoint');
    check($('set-backend').value === 'openai', 'LOAD restores backend');
    check($('set-model').value === 'deepseek-r1', 'LOAD restores model');
    const savedSettings = JSON.parse(storage.getItem('cogitator.settings') || '{}');
    check(savedSettings.activeProfile === profId, 'LOAD sets activeProfile in settings');

    // DELETE while active clears it
    $('prof-del').click();
    await tick(20);
    const storedAfter = JSON.parse(storage.getItem(PROFKEY) || '[]');
    check(storedAfter.length === 0, 'DELETE removes the profile from storage');
    check($('prof-list').options.length === 0, 'dropdown emptied after delete');
    const savedAfter = JSON.parse(storage.getItem('cogitator.settings') || '{}');
    check(!savedAfter.activeProfile, 'DELETE clears activeProfile when it deleted the active profile');
  } finally {
    teardownApp(app);
  }

  /* ============ E2E: persistence across a simulated reload ============ */
  console.log('--- e2e: reload persistence ---');
  const seedProfiles = [{ id: 'pr1', name: 'saved-again', backend: 'lmstudio', endpoint: 'http://lm:1234', model: 'nous' }];
  const seed2 = {
    'cogitator.profiles': JSON.stringify(seedProfiles),
    'cogitator.settings': JSON.stringify({ endpoint: '', model: '', backend: 'auto', system: '', profiles: [], activeProfile: 'pr1' })
  };
  app = await launchApp({ routes: MODEL, seed: seed2 });
  await tick(30);
  try {
    const d2 = app.document;
    d2.getElementById('open-settings').click();
    await tick(20);
    const opts2 = Array.from(d2.getElementById('prof-list').options).map((o) => o.value);
    check(opts2.includes('pr1'), 'reloaded profile appears in the dropdown after restart');
    check(d2.getElementById('prof-list').options.item(0).text.includes('saved-again'), 'reloaded dropdown shows the profile name');
  } finally {
    teardownApp(app);
  }

  process.exit(summary('PHASE 1 PROFILES'));
})();