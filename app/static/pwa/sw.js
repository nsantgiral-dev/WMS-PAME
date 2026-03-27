const CACHE_NAME = 'wms-pwa-v1';
const OFFLINE_QUEUE_KEY = 'wms_sync_queue';

// Archivos a cachear para funcionar offline
const CACHE_FILES = [
  '/static/pwa/index.html',
  '/static/pwa/app.js',
  '/static/pwa/manifest.json'
];

// Instalar Service Worker
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => {
      return cache.addAll(CACHE_FILES);
    })
  );
  self.skipWaiting();
});

// Activar y limpiar caches viejos
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(
        keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k))
      )
    )
  );
  self.clients.claim();
});

// Interceptar requests
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);

  // API calls — intentar red, si falla guardar en cola offline
  if (url.pathname.startsWith('/api/mobile/confirmar')) {
    event.respondWith(
      fetch(event.request.clone()).catch(() => {
        // Sin conexión — guardar en cola para sincronizar después
        return event.request.json().then(body => {
          guardarEnCola(body);
          return new Response(
            JSON.stringify({
              offline: true,
              mensaje: 'Sin conexión — guardado para sincronizar cuando vuelva el WiFi'
            }),
            { headers: { 'Content-Type': 'application/json' } }
          );
        });
      })
    );
    return;
  }

  // Archivos estáticos — cache first
  event.respondWith(
    caches.match(event.request).then(cached => {
      return cached || fetch(event.request);
    })
  );
});

// Sincronizar cuando vuelve la conexión
self.addEventListener('sync', event => {
  if (event.tag === 'sync-confirmaciones') {
    event.waitUntil(sincronizarCola());
  }
});

// Cuando vuelve online
self.addEventListener('message', event => {
  if (event.data === 'SYNC_NOW') {
    sincronizarCola();
  }
});

async function guardarEnCola(item) {
  const clients = await self.clients.matchAll();
  clients.forEach(client => {
    client.postMessage({ tipo: 'GUARDADO_OFFLINE', item });
  });
}

async function sincronizarCola() {
  const clients = await self.clients.matchAll();
  clients.forEach(client => {
    client.postMessage({ tipo: 'SINCRONIZAR' });
  });
}