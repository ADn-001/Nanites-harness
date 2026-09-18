/* COGITATOR SERVICE RITE - cache-first machine spirit */
const CACHE = 'cogitator-v10';
const SHELL = ['./index.html','./manifest.webmanifest','./sw.js','./icon.svg','./icon.png','./icon-192.png','./WH40KIDLE.gif','./WH40KSpinAnimation.gif'];
self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return;
  try {
    const url = new URL(e.request.url);
    if (url.origin !== location.origin) return;
  } catch (_) { return; }
  e.respondWith(
    caches.match(e.request, {ignoreSearch: true}).then(hit => hit || fetch(e.request).then(res => {
      const copy = res.clone();
      caches.open(CACHE).then(c => c.put(e.request, copy));
      return res;
    }).catch(() => caches.match('./index.html')))
  );
});