/*
 * phase0_smoke.test.js — Phase 0 gate: the app boots headlessly in jsdom with
 * mocked network, the shared appcore.js module is loaded, and the settings modal
 * opens when RITES/CONFIG is clicked. No uncaught script errors during boot.
 */
'use strict';
const { check, summary, launchApp, teardownApp } = require('./helpers');

(async () => {
  const app = await launchApp();
  const { window, document, bootErrors } = app;

  try {
    // shared module loaded
    check(!!window.CogCore, 'appcore.js exposes window.CogCore');
    check(window.CogCore && window.CogCore.uid && typeof window.CogCore.uid === 'function', 'CogCore.uid is a function');

    // boot clean
    check(bootErrors.length === 0, 'no uncaught script errors at boot: ' + (bootErrors.length ? bootErrors[0] : ''));

    // core UI present
    check(!!document.getElementById('settings-modal'), 'settings modal present');
    check(!!document.getElementById('ta'), 'composer textarea present');
    check(!!document.getElementById('chat-list'), 'chat list present');

    // settings modal opens via the RITES/CONFIG button
    const modal = document.getElementById('settings-modal');
    check(!modal.classList.contains('open'), 'settings modal closed initially');
    document.getElementById('open-settings').click();
    check(modal.classList.contains('open'), 'RITES/CONFIG opens the settings modal');
    check($value(document, 'set-endpoint') !== null, 'endpoint field exists');

    // a new chat can be created (app functional)
    const before = document.querySelectorAll('.chat-item').length;
    document.getElementById('new-chat').click();
    check(document.querySelectorAll('.chat-item').length === before + 1, 'NEW COMMUNION creates a chat item');

  } finally {
    teardownApp(app);
  }

  process.exit(summary('PHASE 0 SMOKE'));
})();

function $value(doc, id) {
  return doc.getElementById(id);
}