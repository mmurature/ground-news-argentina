// Puente — service worker
// Estrategia "network-first": siempre intenta traer la versión más nueva
// de internet; si no hay conexión, sirve la última que quedó guardada.
// Así, cuando el sitio se actualiza (cada 3hs), quien ya lo instaló ve
// el cambio en la próxima carga en vez de quedarse pegado a una vieja.

const CACHE = "puente-v2";
const ARCHIVOS = ["./", "./index.html", "./manifest.json", "./icono.svg"];

self.addEventListener("install", (event) => {
  self.skipWaiting();
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(ARCHIVOS)));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((nombres) =>
        Promise.all(nombres.filter((n) => n !== CACHE).map((n) => caches.delete(n)))
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  event.respondWith(
    fetch(event.request)
      .then((resp) => {
        const copia = resp.clone();
        caches.open(CACHE).then((cache) => cache.put(event.request, copia));
        return resp;
      })
      .catch(() => caches.match(event.request))
  );
});
