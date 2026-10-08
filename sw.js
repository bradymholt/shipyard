// Shipyard service worker: makes the page installable as an app and keeps the
// last-loaded page available offline. Only same-origin page loads are handled;
// GitHub API calls and companion endpoints always go straight to the network.
const CACHE = "shipyard-shell-v1";

self.addEventListener("install", () => self.skipWaiting());

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) if (key !== CACHE) await caches.delete(key);
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.mode !== "navigate" || new URL(req.url).origin !== self.location.origin) return;
  event.respondWith((async () => {
    const cache = await caches.open(CACHE);
    try {
      const res = await fetch(req);
      if (res.ok) await cache.put("./", res.clone());
      return res;
    } catch (err) {
      const cached = await cache.match("./");
      if (cached) return cached;
      throw err;
    }
  })());
});
