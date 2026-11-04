// Opens the dashboard at once from a saved copy of the page, and fetches the newest
// copy behind it. It handles only the page itself: data and pictures never go
// through here, so nothing about any computer is kept by this file.
//
// To remove it from every browser, replace this whole file with:
//   self.addEventListener('install', () => self.skipWaiting());
//   self.addEventListener('activate', () => self.registration.unregister());
// /index.html never passes through it, so it always gives the newest page.

const SHELL = 'em-shell-v1';

self.addEventListener('install', (e) => {
  self.skipWaiting();
  // Save the page now, so the very next visit is already instant.
  e.waitUntil(caches.open(SHELL).then((c) => c.add('/')).catch(() => {}));
});

self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    for (const name of await caches.keys()) {
      if (name !== SHELL) await caches.delete(name);
    }
    await self.clients.claim();
  })());
});

async function refreshShell(cache, saved) {
  const fresh = await fetch('/', { cache: 'no-cache', credentials: 'same-origin' });
  if (!fresh.ok) return fresh;
  const before = saved ? saved.headers.get('x-shell') : null;
  const after = fresh.headers.get('x-shell');
  if (before !== after || !after) await cache.put('/', fresh.clone());
  // Only a real change of version asks the page to reload, so this can never loop.
  if (before && after && before !== after) {
    for (const c of await self.clients.matchAll({ type: 'window' })) c.postMessage('shell-updated');
  }
  return fresh;
}

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.mode !== 'navigate' || url.origin !== location.origin || url.pathname !== '/') return;
  e.respondWith((async () => {
    const cache = await caches.open(SHELL);
    const saved = await cache.match('/');
    if (saved) {
      e.waitUntil(refreshShell(cache, saved).catch(() => {}));
      return saved;
    }
    return refreshShell(cache, null);
  })());
});
