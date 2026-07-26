// Service Worker — update detection con banner de notificación al usuario.
// Flask inyecta "// v{mtime}" al inicio para que el browser detecte cambios en cada deploy.
const CACHE_NAME = 'wms-shell-v2';
const SHELL = ['/pwa', '/static/pwa/app.js', '/static/pwa/picking.js', '/static/pwa/packing.js', '/static/pwa/recepcion.js', '/static/pwa/rutas.js', '/static/pwa/traslados.js', '/static/pwa/conteo.js', '/static/pwa/reposicion.js', '/static/pwa/liquidacion.js', '/static/pwa/layout.js', '/static/pwa/tienda.js', '/static/pwa/etiquetas.js', '/static/pwa/vigia.js', '/static/pwa/compras_ia.js', '/static/pwa/kardex.js', '/static/pwa/temporada.js'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(c => c.addAll(SHELL)).catch(() => {})
  );
});

self.addEventListener('activate', (event) => {
  self.clients.claim();
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    )
  );
});

// Network-first con fallback a caché — solo para assets del shell (no API)
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET') return;
  if (url.pathname.startsWith('/api/')) return;
  event.respondWith(
    fetch(event.request)
      .then(res => {
        const clone = res.clone();
        caches.open(CACHE_NAME).then(c => c.put(event.request, clone));
        return res;
      })
      .catch(() => caches.match(event.request))
  );
});

self.addEventListener('message', (event) => {
  if (event.data === 'SKIP_WAITING') self.skipWaiting();
});
