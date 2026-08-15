// Service Worker — caché del shell, versionada por deploy.
//
// Flask antepone `const SW_VERSION = "<mtime mas nuevo de la carpeta>";`. Ese
// sello nombra la caché, así que un deploy crea una caché nueva y el `activate`
// borra la anterior. Antes el nombre era fijo: el borrado nunca borraba nada y
// la versión viajaba en un comentario que nadie leía.
const CACHE_NAME = 'wms-shell-' + (typeof SW_VERSION === 'string' ? SW_VERSION : 'dev');
const SHELL = ['/pwa', '/static/vendor/JsBarcode.all.min.js', '/static/pwa/app.js', '/static/pwa/picking.js', '/static/pwa/packing.js', '/static/pwa/recepcion.js', '/static/pwa/rutas.js', '/static/pwa/traslados.js', '/static/pwa/conteo.js', '/static/pwa/reposicion.js', '/static/pwa/liquidacion.js', '/static/pwa/layout.js', '/static/pwa/tienda.js', '/static/pwa/etiquetas.js', '/static/pwa/vigia.js', '/static/pwa/compras_ia.js', '/static/pwa/kardex.js', '/static/pwa/temporada.js', '/static/pwa/flota.js'];

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

// Cache-first sobre el shell. La caché lleva el sello del deploy en el nombre,
// así que "primero la caché" no puede servir código viejo: si hay versión nueva
// la caché es otra y está vacía.
//
// Era network-first, que es lo mismo que no tener caché salvo cuando no hay red:
// 915 KB de JS por cada carga de cada pantalla, y con `no-store` en el servidor
// ni el navegador los reusaba. Es la mayor parte de los ~390 GB de red del mes.
//
// SE CACHEA POR LISTA BLANCA, NO POR EXCLUSIÓN.
//
// La primera versión excluía `/api/` y cacheaba todo lo demás. El módulo de
// flota no cuelga de `/api/` sino de `/flota/`, así que sus GET quedaron
// cacheados: la ficha técnica se guardaba bien, se volvía a pedir, y el
// navegador devolvía la respuesta VIEJA —la de antes de guardar—. En pantalla:
// «Ficha guardada y completa ✓» e inmediatamente el formulario en blanco.
// Yesid lo reportó en tres vehículos el 2026-08-05.
//
// La lección no es que faltaba `/flota/` en la lista de exclusiones. Es que una
// lista de exclusiones es la forma equivocada de escribir esto: cada módulo
// nuevo montado en un prefijo nuevo entra al caché **por omisión**, y el fallo
// no es un error sino datos viejos presentados como actuales — la peor clase de
// dato en un WMS, y la más difícil de notar.
//
// Con lista blanca, lo que no está declarado va a la red. Un módulo nuevo no
// puede quedar cacheado por olvido.
function esDelShell(url) {
  // Los archivos estáticos del PWA: JS, CSS, imágenes, manifest.
  if (url.pathname.startsWith('/static/')) return true;
  // La página en sí.
  if (url.pathname === '/pwa' || url.pathname === '/pwa/') return true;
  return false;
}

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET') return;
  if (url.origin !== self.location.origin) return;
  if (!esDelShell(url)) return;
  // El propio sw es el canal de actualización: nunca de caché.
  if (url.pathname.endsWith('/sw.js')) return;

  event.respondWith(
    caches.match(event.request).then(hit => {
      if (hit) return hit;
      return fetch(event.request).then(res => {
        // Solo se guarda lo que llegó bien. Cachear un 404 o un 500 lo vuelve
        // permanente hasta el próximo deploy.
        if (res && res.ok && res.type === 'basic') {
          const clone = res.clone();
          caches.open(CACHE_NAME).then(c => c.put(event.request, clone));
        }
        return res;
      });
    })
  );
});

self.addEventListener('message', (event) => {
  if (event.data === 'SKIP_WAITING') self.skipWaiting();
});
