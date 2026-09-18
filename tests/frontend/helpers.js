/*
 * helpers.js — shared harness for the COGITATOR frontend test suite.
 *
 * Loads the real index.html into jsdom, stubs the browser APIs the inline script needs
 * (fetch, sendBeacon) so the app boots headlessly, then lets tests drive the real DOM.
 * Exposes assert helpers mirroring the style of the Python test_e2e.py suite:
 *   check(cond, msg)  appends to `fails` if cond is false; prints ok  - / FAIL- prefix.
 */
'use strict';
const fs = require('fs');
const path = require('path');
const { JSDOM, ResourceLoader } = require('jsdom');

const ROOT = path.resolve(__dirname, '..', '..');
const INDEX_HTML = path.join(ROOT, 'index.html');
const APPLITORY = '[GATELOG]';

/*
 * jsdom v30 removed the `resources`/`ResourceLoader` fetch pipeline entirely
 * (external <script src> are never fetched). To keep the real module-loading
 * order (appcore.js UMD runs BEFORE the inline main script), inline appcore.js
 * into the html in place of its <script src> tag. jsdom will not re-fetch the
 * tag (external resources are not loaded by default), so running it inline once
 * is faithful and side-effect-free.
 */
function inlineAppcore(html) {
  const tag = '<script src="appcore.js"></script>';
  const code = fs.readFileSync(path.join(ROOT, 'appcore.js'), 'utf8');
  const first = html.indexOf(tag);
  if (first === -1) return html;
  return html.slice(0, first) + '<script>' + code + '</script>' + html.slice(first + tag.length);
}

let fails = [];

const check = (cond, msg) => {
  console.log((cond ? 'ok  - ' : 'FAIL- ') + msg);
  if (!cond) fails.push(msg);
};

const summary = (label) => {
  console.log('\n' + label + ': ' + fails.length + ' FAILURES');
  return fails.length ? 1 : 0;
};

const clearFails = () => { fails = []; };

/* ---- fake localStorage (jsdom's is fine but explicit is deterministic) ---- */
function fakeStorage() {
  let store = {};
  return {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
    clear: () => { store = {}; },
    _dump: () => JSON.parse(JSON.stringify(store)),
    _seed: (obj) => { store = Object.assign({}, obj); }
  };
}

/*
 * Launch the app in jsdom with the browser APIs stubbed.
 * routes: map of exact URL->serializable JSON response (or a function url=>{status,json}|undefined).
 * If a URL is not in routes, fetch rejects with a TypeError (network-failure behaviour) so the
 * app's error handling exercises the real code path.
 */
function launchApp({ url = 'http://localhost:8080/index.html', routes = {} } = {}) {
  const html = inlineAppcore(fs.readFileSync(INDEX_HTML, 'utf8'));
  const events = [];
  const storage = fakeStorage();
  const bootErrors = [];

  const dom = new JSDOM(html, {
    url,
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    beforeParse(window) {
      window.addEventListener('error', (e) => {
        bootErrors.push((e && (e.message || e.error)) ? (e.message || String(e.error)) : 'uncaught js error');
      });
      // Core polyfill: jsdom has no real fetch. Route or reject.
      window.fetch = (input, init) => {
        const key = typeof input === 'string' ? input : (input && input.url) || String(input);
        const method = ((init && init.method) || 'GET').toUpperCase();
        events.push({ url: key, method: method, body: init && init.body ? String(init.body) : null });
        const hit = routes[key];
        if (typeof hit === 'function') return hit({ url: key, method, body: init && init.body });
        if (hit) return Promise.resolve({
          ok: hit.status ? hit.status < 400 : true,
          status: hit.status || 200,
          json: async () => (hit.json !== undefined ? JSON.parse(JSON.stringify(hit.json)) : {}),
          text: async () => JSON.stringify(hit.json !== undefined ? hit.json : {}),
          body: null
        });
        // unmocked -> network error (TypeError), same as a dead endpoint
        return Promise.reject(new TypeError('NetworkError: jsdom fetch (unmocked ' + key + ')'));
      };
      window.navigator.sendBeacon = () => true;

      // Install our own deterministic storage over jsdom's (jsdom needs a real
      // origin; ours is explicit and resettable between tests).
      try {
        Object.defineProperty(window, 'localStorage', { value: storage, configurable: true });
      } catch (e) {
        window.localStorage = storage;
      }
    }
  });

  // Let the inline script's synchronous boot + first microtasks run.
  return new Promise((resolve) => {
    setTimeout(() => resolve({ window: dom.window, document: dom.window.document, storage, events, bootErrors }), 60);
  });
}

function teardownApp(app) {
  if (!app) return;
  try { app.window.close(); } catch (e) { /* ignore */ }
}

module.exports = {
  ROOT,
  INDEX_HTML,
  check, summary, clearFails,
  launchApp, teardownApp,
  get fails() { return fails; }
};