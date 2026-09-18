/*
 * appcore.js — COGITATOR shared pure logic.
 * UMD: exposed as `window.CogCore` in the browser (loaded by index.html BEFORE the
 * inline script) and as `module.exports` under Node so the frontend test suite can
 * unit-test the same code that ships. Keep everything in here DOM-free and side-effect
 * free (no globals, no localStorage touches) so it is deterministic to test.
 */
(function (root, factory) {
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = factory();
  } else {
    root.CogCore = factory();
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var CogCore = {
    VERSION: '0.1.0',

    /** Collision-tolerant id (ms base-36 + random tail). */
    uid: function () {
      return Date.now().toString(36) + Math.random().toString(36).slice(2, 5);
    },

    /** HTML-escape a value for safe interpolation. */
    esc: function (s) {
      return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
    },

    /** Rough token estimate used by the context gauge (4 chars ~ 1 token). */
    tok: function (s) {
      return Math.max(1, Math.ceil((s == null ? '' : String(s)).length / 4));
    }
  };

  return CogCore;
});