// Lets the app open with no internet. It only ever stores the page and its icons.
//
// Never add the status check or outage data here. A cached answer would show
// green during an outage, which is the worst thing this site could do.

const CACHE = 'updog-v1';
const PAGE = '/';
const ICONS = ['/manifest.webmanifest', '/icon-192.png', '/icon-512.png', '/apple-touch-icon.png'];

self.addEventListener('install', event => {
    event.waitUntil(
        caches.open(CACHE)
            .then(cache => cache.addAll([PAGE, ...ICONS]))
            .then(() => self.skipWaiting()),
    );
});

self.addEventListener('activate', event => {
    event.waitUntil(
        caches.keys()
            .then(names => Promise.all(names.filter(name => name !== CACHE).map(name => caches.delete(name))))
            .then(() => self.clients.claim()),
    );
});

async function pageFromNetworkFirst(request) {
    const cache = await caches.open(CACHE);
    try {
        const response = await fetch(request);
        if (response.ok) await cache.put(PAGE, response.clone());
        return response;
    } catch (error) {
        const saved = await cache.match(PAGE);
        if (saved) return saved;
        throw error;
    }
}

self.addEventListener('fetch', event => {
    const { request } = event;
    if (request.method !== 'GET') return;

    const url = new URL(request.url);
    if (url.origin !== self.location.origin) return;

    if (request.mode === 'navigate') {
        event.respondWith(pageFromNetworkFirst(request));
    } else if (ICONS.includes(url.pathname)) {
        event.respondWith(caches.match(url.pathname).then(saved => saved || fetch(request)));
    }
    // Everything else, including /api and the status check, goes straight to the network.
});
