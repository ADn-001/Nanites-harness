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
    },

    /*
     * providerProfileStore — persisted list of provider endpoint profiles.
     * DOM-free. Operates over an injected storage adapter that satisfies
     * {getItem,setItem,removeItem} (localStorage in the browser, fake in tests).
     * Profile shape: {id, name, backend, endpoint, model}.
     * A profile is a saved snapshot of {backend, endpoint, model}; "applying" it
     * returns that slice so the caller can rewrite the live settings.
     */
    profileStore: {
      key: 'cogitator.profiles',

      _read: function (storage) {
        try {
          var v = storage.getItem(this.key);
          var a = v ? JSON.parse(v) : [];
          return Array.isArray(a) ? a : [];
        } catch (e) { return []; }
      },
      _write: function (storage, arr) {
        try { storage.setItem(this.key, JSON.stringify(arr)); return true; }
        catch (e) { return false; }
      },

      /** Sorted copy of all profiles (newest first). */
      list: function (storage) {
        return this._read(storage).slice().reverse();
      },

      /** Look up a profile by id. */
      get: function (storage, id) {
        return this._read(storage).find(function (p) { return p.id === id; }) || null;
      },

      /** Insert (no id) or update in place (with id). Returns the saved profile. */
      save: function (storage, profile) {
        var a = this._read(storage);
        var rec = {
          id: profile && profile.id ? profile.id : CogCore.uid(),
          name: String((profile && profile.name) || '').trim() || 'UNNAMED PROFILE',
          backend: (profile && profile.backend) || 'auto',
          endpoint: (profile && profile.endpoint) || '',
          model: (profile && profile.model) || ''
        };
        var i = a.findIndex(function (p) { return p.id === rec.id; });
        if (i >= 0) a[i] = rec; else a.push(rec);
        this._write(storage, a);
        return this.get(storage, rec.id);
      },

      /** Delete a profile by id (or by name as a convenience). Returns the remaining list. */
      remove: function (storage, id) {
        var a = this._read(storage).filter(function (p) { return p.id !== id && p.name !== id; });
        this._write(storage, a);
        return a;
      },

      /** The slice of settings a profile applies. */
      apply: function (storage, id) {
        return this.get(storage, id);
      }
    }
  };

  return CogCore;
});