/*
 * phase21_service_worker.test.js — CR-Nanites-harness-0018 (high) + 0007 (medium).
 *
 * Why this file drives the REAL sw.js instead of grepping it
 * -----------------------------------------------------------
 * The phase gate as written says "static assertions only — jsdom has no
 * ServiceWorker/container APIs". That is true of jsdom, but it is NOT a reason to
 * accept a text guard: `sw.js` is plain ES5-ish JavaScript that only touches
 * `self.addEventListener`, `caches`, `location.origin`, `URL` and `fetch`, every one
 * of which can be supplied by a Node `vm` sandbox. So this suite EXECUTES the shipped
 * service worker and drives its real `fetch` listener with real event objects.
 *
 * Why that matters, concretely. The bug is a `.catch(() => caches.match('./index.html'))`
 * that is not scoped to navigations. A text guard could pin the *spelling* of the fix
 * ("the file mentions 'navigate'") while the handler still answered a script request with
 * the HTML document — the exact defect, re-spelled. Driving the listener is the only
 * assertion here that cannot pass that way: it asks what the handler RETURNS for a
 * script request, and what it returns for a navigation.
 *
 * The two directions are the load-bearing half. The positive case (a navigation offline
 * load still gets index.html) must be asserted, or a handler that simply rejects
 * everything would satisfy every negative below — and "the app shows a network error
 * even when online" is not the property we want either.
 *
 * Offline support currently makes the app STRICTLY WORSE than no offline support: it
 * converts a recoverable "you are offline" into a white screen, because `CogCore` ends up
 * undefined and index.html's boot path throws. That is the "done" line of the phase.
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..', '..');
const SW_PATH = path.join(ROOT, 'sw.js');
const ORIGIN = 'https://cogitator.local';

let fails = [];
const check = (cond, msg) => {
  console.log((cond ? 'ok  - ' : 'FAIL- ') + msg);
  if (!cond) fails.push(msg);
};

/* ------------------------------------------------------------------ *
 * A service-worker sandbox: real sw.js, faked platform.
 * ------------------------------------------------------------------ */

function fakeResponse(body, opts) {
  opts = opts || {};
  return {
    ok: opts.ok !== false,
    status: opts.status === undefined ? 200 : opts.status,
    type: opts.type || 'basic',
    body: body === undefined ? '' : body,
    clone() { return fakeResponse(this.body, this); }
  };
}

/**
 * Boot sw.js in a vm with a fake CacheStorage + fetch.
 * @param opts.fetchImpl  async (url) => fakeResponse ; throw to simulate a network failure
 * @param opts.seed       {cacheName: {url: response}} pre-existing cache entries
 */
function bootSW(opts) {
  opts = opts || {};
  const listeners = {};
  const store = new Map();                       // cacheName -> Map(url -> response)

  const norm = (req) => (typeof req === 'string' ? new URL(req, ORIGIN + '/').href
                                                : new URL(req.url, ORIGIN + '/').href);
  const cacheFor = (name) => {
    if (!store.has(name)) store.set(name, new Map());
    const m = store.get(name);
    return {
      put: async (req, res) => { m.set(norm(req), res); },
      match: async (req) => m.get(norm(req)),
      addAll: async (urls) => {
        for (const u of urls) {
          const href = norm(u);
          if (opts.fetchImpl) {
            const res = await opts.fetchImpl(href);   // a real precache really fetches
            m.set(href, res);
          } else {
            m.set(href, fakeResponse('cached:' + u));
          }
        }
      },
      keys: async () => [...m.keys()]
    };
  };
  if (opts.seed) {
    for (const name of Object.keys(opts.seed)) {
      const m = store.get(name) || new Map();
      for (const url of Object.keys(opts.seed[name])) m.set(url, opts.seed[name][url]);
      store.set(name, m);
    }
  }

  const caches = {
    open: async (name) => cacheFor(name),
    match: async (req, o) => {
      for (const name of store.keys()) {
        const hit = store.get(name).get(norm(req));
        if (hit) return hit;
      }
      return undefined;
    },
    keys: async () => [...store.keys()],
    delete: async (name) => store.delete(name)
  };

  const calls = { fetched: [], cached: [] };
  const fetchImpl = opts.fetchImpl || (async (url) => {
    calls.fetched.push(url);
    if (opts.offline) throw new Error('offline');
    return fakeResponse('net:' + url);
  });

  const sandbox = {
    self: {
      addEventListener: (type, fn) => { listeners[type] = fn; },
      skipWaiting: async () => {},
      clients: { claim: async () => {} }
    },
    caches, location: { origin: ORIGIN }, fetch: fetchImpl,
    URL, Promise, console, setTimeout, clearTimeout
  };
  sandbox.self.location = sandbox.location;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(SW_PATH, 'utf8'), sandbox, { filename: 'sw.js' });

  // Top-level `const` in a vm script is NOT a property of the sandbox object — it
  // lives in the context's lexical scope. So CACHE/SHELL/deriveCacheName must be read
  // by evaluating in the context, not by property access. (Reading sandbox.CACHE
  // silently yields undefined and every downstream assertion fails for the wrong
  // reason — which is exactly how this harness first misreported itself.)
  const get = (expr) => vm.runInContext(expr, sandbox);
  const cacheName = get('CACHE');
  const shell = get('SHELL');
  const derive = get('typeof deriveCacheName === "function" ? deriveCacheName : null');
  const sandboxCaches = sandbox.caches;
  return {
    sandbox, listeners, store, calls, cacheName, get,
    SHELL: shell, deriveCacheName: derive, cachesApi: sandboxCaches,
    /* Drive the real fetch listener and await whatever it responds with. */
    async fetch(req) {
      let out = null;
      const e = {
        request: req,
        respondWith(p) { out = Promise.resolve(p); }
      };
      listeners.fetch(e);
      if (!out) return { responded: false };
      try { return { responded: true, ok: true, value: await out }; }
      catch (err) { return { responded: true, ok: false, error: err }; }
    }
  };
}

const req = (p, extra) => Object.assign({
  method: 'GET', url: new URL(p, ORIGIN + '/').href,
  mode: 'cors', destination: 'script'
}, extra || {});

/* The HTML document as it would sit in the cache. Anything that is not this body is
 * the bug: a script request answered with the document. */
const HTML_BODY = '<!DOCTYPE html><html><body>THE APP DOCUMENT</body></html>';

(async () => {

  // ==================================================================
  // 1. THE HEADLINE: a failed SCRIPT fetch must NOT be answered with HTML
  // ==================================================================
  // This is CR-0018's impact verbatim: offline reload -> appcore.js misses -> gets
  // index.html -> the script fails to parse -> CogCore undefined -> boot throws.
  {
    const sw = bootSW({
      offline: true,
      seed: { 'seed': { [ORIGIN + '/index.html']: fakeResponse(HTML_BODY) } }
    });
    const r = await sw.fetch(req('./appcore.js', { destination: 'script', mode: 'cors' }));
    check(r.responded, 'the fetch handler responds to a same-origin script request');
    const body = r.ok && r.value ? r.value.body : undefined;
    check(body !== HTML_BODY,
          'a failed script fetch is NOT answered with the HTML document (CR-0018 headline)');
    check(body === undefined,
          'a failed script fetch rejects, so the browser surfaces an honest network error');
  }

  // The same request expressed the other way browsers express it: mode 'no-cors'
  // is what a classic <script src> actually carries.
  {
    const sw = bootSW({
      offline: true,
      seed: { 'seed': { [ORIGIN + '/index.html']: fakeResponse(HTML_BODY) } }
    });
    const r = await sw.fetch(req('./appcore.js', { destination: 'script', mode: 'no-cors' }));
    const body = r.ok && r.value ? r.value.body : undefined;
    check(body !== HTML_BODY,
          "a failed classic-script fetch (mode 'no-cors') is not answered with HTML either");
  }

  // And a stylesheet / image / font: same class of non-document request.
  for (const destination of ['style', 'image', 'font', 'worker', 'manifest']) {
    const sw = bootSW({
      offline: true,
      seed: { 'seed': { [ORIGIN + '/index.html']: fakeResponse(HTML_BODY) } }
    });
    const r = await sw.fetch(req('./theme.css', { destination, mode: 'no-cors' }));
    const body = r.ok && r.value ? r.value.body : undefined;
    check(body !== HTML_BODY,
          `a failed '${destination}' request is not answered with the HTML document`);
  }

  // ==================================================================
  // 2. THE POSITIVE DIRECTION: offline NAVIGATION still gets the shell
  // ==================================================================
  // Without this, "always reject" satisfies every negative above — and the app would
  // lose offline loading entirely, which is a different regression, not a fix.
  for (const nav of [{ mode: 'navigate' }, { destination: 'document' }]) {
    const sw = bootSW({
      offline: true,
      seed: { 'seed': { [ORIGIN + '/index.html']: fakeResponse(HTML_BODY) } }
    });
    const r = await sw.fetch(req('./', Object.assign({ destination: '', mode: 'cors' }, nav)));
    check(r.ok && r.value && r.value.body === HTML_BODY,
          'an offline NAVIGATION is still answered with the cached shell ' +
          `(via ${nav.mode ? 'mode=navigate' : 'destination=document'})`);
  }

  // A navigation when the shell is NOT cached must reject rather than resolve with
  // undefined — respondWith(undefined) is itself a TypeError in the browser.
  {
    const sw = bootSW({ offline: true, seed: { 'seed': {} } });
    const r = await sw.fetch(req('./', { mode: 'navigate', destination: 'document' }));
    check(r.responded && !r.ok,
          'an offline navigation with no cached shell rejects instead of resolving undefined');
  }

  // ==================================================================
  // 3. NON-GET and cross-origin are still passed through untouched
  // ==================================================================
  {
    const sw = bootSW({ offline: true });
    const r = await sw.fetch(req('./appcore.js', { method: 'POST' }));
    check(!r.responded, 'a non-GET request is not intercepted at all');
  }
  {
    const sw = bootSW({ offline: true });
    let responded = false;
    sw.listeners.fetch({
      request: { method: 'GET', url: 'https://evil.example/appcore.js', mode: 'cors', destination: 'script' },
      respondWith() { responded = true; }
    });
    check(!responded, 'a cross-origin request is not intercepted (no cache poisoning)');
  }

  // ==================================================================
  // 4. THE PRECACHE IS COMPLETE — every same-origin script index.html loads
  // ==================================================================
  // CR-0018 part (1): appcore.js was reachable only via the runtime c.put, which needs a
  // prior successful ONLINE fetch. So the very first offline visit — the case the whole
  // offline feature exists for — could not get it at all.
  {
    const html = fs.readFileSync(path.join(ROOT, 'index.html'), 'utf8');
    const scripts = [...html.matchAll(/<script[^>]*\ssrc=["']([^"']+)["']/g)].map(m => m[1]);
    const styles = [...html.matchAll(/<link[^>]*\srel=["']stylesheet["'][^>]*\shref=["']([^"']+)["']/g)].map(m => m[1]);
    check(scripts.length > 0, 'index.html references at least one external script to check');
    const sw = bootSW({});
    const shell = sw.SHELL;
    check(Array.isArray(shell), 'sw.js exposes SHELL as an array');
    for (const s of scripts) {
      if (/^(https?:)?\/\//.test(s)) continue;
      check(shell.includes('./' + s.replace(/^\.\//, '')),
            `index.html's <script src="${s}"> is in the precached SHELL`);
    }
    for (const s of styles) {
      if (/^(https?:)?\/\//.test(s)) continue;
      check(shell.includes('./' + s.replace(/^\.\//, '')),
            `index.html's stylesheet "${s}" is in the precached SHELL`);
    }
    // index.html currently inlines its CSS, so `styles` is EMPTY and that loop is
    // vacuous. Say so rather than letting a reader assume the stylesheet rule is
    // exercised — it becomes live the moment a <link rel=stylesheet> appears, which is
    // exactly when someone needs to know the guard is there waiting.
    check(styles.length === 0 || styles.every(s => shell.includes('./' + s)),
          `the stylesheet-in-SHELL rule is currently ${styles.length === 0 ? 'VACUOUS (index.html inlines its CSS; it activates when a <link rel=stylesheet> is added)' : 'exercised'}`);
  }

  // Every SHELL entry must exist on disk: cache.addAll() is all-or-nothing, so one
  // missing file makes the whole install reject and the worker never activates.
  {
    const sw = bootSW({});
    for (const entry of sw.SHELL) {
      check(fs.existsSync(path.join(ROOT, entry.replace(/^\.\//, ''))),
            `SHELL entry "${entry}" exists on disk (a missing one fails cache.addAll wholesale)`);
    }
  }

  // ==================================================================
  // 5. A FAILED RESPONSE MUST NOT BE POISONED INTO THE CACHE
  // ==================================================================
  // Same failure class as the headline: a 404 body served later from cache is a dead
  // app with no error. This is the runtime-put twin of the navigation-fallback bug.
  {
    const sw = bootSW({ fetchImpl: async () => fakeResponse('not found', { ok: false, status: 404 }) });
    const r = await sw.fetch(req('./appcore.js'));
    check(r.ok && r.value.status === 404, 'an error response is still returned to the caller');
    const cached = await sw.cachesApi.match('./appcore.js');
    check(!cached, 'a non-ok response is NOT written into the runtime cache');
  }

  // ...and a genuinely good response IS cached, so offline still works at all.
  {
    const sw = bootSW({});
    await sw.fetch(req('./appcore.js'));
    const cached = await sw.cachesApi.match('./appcore.js');
    check(!!cached, 'a successful response IS written into the runtime cache');
  }

  // ==================================================================
  // 6. CR-0007: the cache name is derived, not a human-remembered literal
  // ==================================================================
  // The gate asks: "the CACHE string changed when any SHELL asset changed". A literal
  // 'cogitator-v10' cannot satisfy that by construction. Assert the SHAPE of the
  // mechanism (derived from the asset set, one bump) rather than pinning a version
  // number, which would be pinning an implementation detail that must change anyway.
  {
    const sw = bootSW({});
    const cache = sw.cacheName;
    check(typeof cache === 'string' && cache.length > 0, 'sw.js exposes a CACHE name');
    check(!/^cogitator-v\d+$/.test(cache),
          `CACHE is not a hand-maintained "cogitator-vNNN" literal (CR-0007): got "${cache}"`);

    // The derive function must actually depend on the SHELL asset set: two different
    // asset sets produce two different cache names.
    const derive = sw.deriveCacheName;
    check(typeof derive === 'function',
          'sw.js exposes a deriveCacheName() used to build CACHE (one bump, not one line)');
    if (typeof derive === 'function') {
      const shellNow = sw.SHELL;
      const a = derive(shellNow, { 'index.html': 'aaa', 'appcore.js': 'bbb' });
      const b = derive(shellNow, { 'index.html': 'aaa', 'appcore.js': 'ccc' });
      const c = derive(shellNow.concat(['./new-asset.js']), { 'index.html': 'aaa', 'appcore.js': 'bbb', 'new-asset.js': 'ddd' });
      check(a !== b, 'changing an asset\'s CONTENT changes the derived cache name');
      check(a !== c, 'adding an ASSET changes the derived cache name');
      check(a === derive(shellNow, { 'index.html': 'aaa', 'appcore.js': 'bbb' }),
            'the derivation is deterministic (same assets -> same name)');

      // CACHE must be the derivation of the SHELL's REAL digests — not of some
      // hypothetical map. Comparing against the shipped ASSET_DIGESTS is what makes
      // this an assertion about the shipped wiring rather than about the pure function.
      const digests = sw.get('ASSET_DIGESTS');
      check(sw.cacheName === derive(shellNow, digests),
            'CACHE is exactly what deriveCacheName(SHELL, ASSET_DIGESTS) returns');

      // A digest of '' for every asset would make CACHE a constant again — the very
      // CR-0007 shape, one indirection further from a bump. So pin that the digests are
      // actually POPULATED, and (in test_e2e.py, which can read the files) that they
      // match the real contents. An empty map here is the tell.
      //
      // The ONE permitted blank is './sw.js': it embeds this map, so hashing its own
      // bytes is a self-reference. Asserting that exemption is exactly './sw.js' (rather
      // than skipping the check) keeps a second exempt asset from quietly joining it.
      const keys = Object.keys(digests || {});
      const blank = keys.filter(k => !String(digests[k]).trim());
      check(keys.length > 0, 'sw.js ships a populated ASSET_DIGESTS map');
      check(blank.length === 1 && blank[0] === './sw.js',
            `every precached asset but the self-referential './sw.js' has a real digest ` +
            `(blank: ${JSON.stringify(blank)})`);
      check(keys.length === sw.SHELL.length,
            'ASSET_DIGESTS covers exactly the SHELL entries (no missing, no extra)');
    }
  }

  // The activate handler must delete every cache that is not the current one — a stale
  // -v10 cache left behind is unbounded growth on a phone.
  //
  // BOTH directions are asserted, and the current cache is SEEDED alongside the stale
  // ones. That seeding is load-bearing: an earlier form seeded only the stale caches, so
  // a handler deleting EVERY cache (including the live one — which silently kills
  // offline loading on the next visit) passed vacuously on an empty result set. The
  // distinguishing input is the current cache being present: "keep exactly the current
  // one" and "delete everything" can only be told apart when there is something to keep.
  {
    const probe = bootSW({});
    const current = probe.cacheName;
    const sw = bootSW({
      seed: {
        'cogitator-v9': { [ORIGIN + '/index.html']: fakeResponse('old') },
        'cogitator-v8': { [ORIGIN + '/index.html']: fakeResponse('older') },
        [current]: { [ORIGIN + '/index.html']: fakeResponse('CURRENT') }
      }
    });
    let done = null;
    sw.listeners.activate({ waitUntil(p) { done = p; } });
    await done;
    const keys = await sw.cachesApi.keys();
    // Assert the RULE, not the arithmetic: the current cache survives, and nothing else does.
    check(keys.length === 1 && keys[0] === current,
          `activate keeps ONLY the current cache (got ${JSON.stringify(keys)})`);
    check(!keys.includes('cogitator-v9') && !keys.includes('cogitator-v8'),
          'activate deletes the seeded stale caches cogitator-v9 and cogitator-v8');
    // And the surviving entry must still hold its CONTENT, not just its name — a
    // delete-and-recreate would keep the key while emptying the cache.
    const still = await sw.cachesApi.match('./index.html');
    check(!!still && still.body === 'CURRENT',
          'the current cache survives activate with its entries INTACT');
  }

  console.log('\nPHASE 21 SERVICE WORKER: ' + fails.length + ' FAILURES');
  process.exit(fails.length ? 1 : 0);
})().catch(e => { console.error(e); process.exit(2); });