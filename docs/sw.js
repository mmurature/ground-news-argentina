// Ground News Argentina — service worker mínimo (etapa 4)
// Guarda en caché lo último que se vio, para que la página instalada
// abra algo aunque no haya internet en ese momento.
// Nota: esto solo se activa cuando la página se sirve por http(s)
// (localhost o el sitio ya publicado), no al abrir index.html directo
// desde el explorador de archivos.

const CACHE = "gn-ar-v1";
const ARCHIVOS = ["./", "./index.html", "./manifest.json", "./icono.svg"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(ARCHIVOS)));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((nombres) =>
      Promise.all(nombres.filter((n) => n !== CACHE).map((n) => caches.delete(n)))
    )
  );
});

self.addEventListener("fetch", (event) => {
  event.respondWith(
    caches.match(event.request).then((resp) => resp || fetch(event.request))
  );
});
