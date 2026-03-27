// Service Worker desactivado en desarrollo
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", () => self.clients.claim());