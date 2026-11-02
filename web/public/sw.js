// Caches only the app shell. API responses and audio are never cached here, so
// signing out or losing access leaves no radio data behind on the device.
const CACHE = 'sentrysdr-shell-v2';

self.addEventListener('install', () => self.skipWaiting());

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) {
      if (key !== CACHE) await caches.delete(key);
    }
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', (event) => {
  const { request } = event;
  const url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== self.location.origin || url.pathname.includes('/api/')) {
    return;
  }
  // Network first keeps the app current; the cache only serves when offline.
  event.respondWith((async () => {
    const cache = await caches.open(CACHE);
    try {
      const response = await fetch(request);
      if (response.ok) await cache.put(request, response.clone());
      return response;
    } catch (error) {
      const cached = await cache.match(request);
      if (cached) return cached;
      const shell = request.mode === 'navigate' ? await cache.match('./') : null;
      if (shell) return shell;
      throw error;
    }
  })());
});
