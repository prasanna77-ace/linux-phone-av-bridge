self.addEventListener('install', (e) => {
    self.skipWaiting();
});

self.addEventListener('activate', (e) => {
    e.waitUntil(clients.claim());
});

self.addEventListener('fetch', (e) => {
    // Dynamic network-first pass-through for real-time WebSockets & WebRTC
    e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
});
