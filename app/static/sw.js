// Service worker minimal : coquille hors-ligne pour l'installation PWA.
// Pas de cache agressif (les données doivent rester fraîches) — on sert le
// réseau d'abord, sans mise en cache des réponses API.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
