// Service worker minimo: no cachea nada (la app necesita internet para
// hablar con Supabase igual), pero su sola presencia es lo que Chrome
// exige para permitir instalar la app en la pantalla de inicio.

self.addEventListener("install", (event) => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  // Dejamos pasar todas las peticiones directo a la red (sin cache).
  event.respondWith(fetch(event.request));
});
