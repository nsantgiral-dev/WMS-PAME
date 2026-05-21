// Service Worker — update detection con banner de notificación al usuario.
// Flask inyecta "// v{mtime}" al inicio para que el browser detecte cambios en cada deploy.
self.addEventListener('install', () => {
  // No skipWaiting automático — el banner permite al usuario elegir cuándo actualizar.
});

self.addEventListener('activate', () => self.clients.claim());

self.addEventListener('message', (event) => {
  if (event.data === 'SKIP_WAITING') self.skipWaiting();
});
