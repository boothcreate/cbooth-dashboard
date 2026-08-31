/*
 * service-worker.js — app-shell caching for offline use on the phone.
 *
 * Hand-authored, static (unlike index.html, which build_dashboard.py
 * regenerates every run). Requires a secure context (HTTPS, or localhost) —
 * see BUILD.md, "Offline support (PWA)" for why phone access moved from
 * plain http://<tailscale-ip>:8787 to https://<device>.<tailnet>.ts.net via
 * `tailscale serve`.
 *
 * Scope, deliberately narrow:
 *   - Caches the page shell (index.html, manifest, icons) so the app opens
 *     at all with zero signal, network-first so an online visit always gets
 *     the freshest generated dashboard (today's events/deadlines baked in
 *     at build time) and only falls back to the cached copy on failure.
 *   - NEVER intercepts /api/* — to-do data has its own online/offline
 *     handling in the page's own JS (localStorage cache + a pending-change
 *     queue for offline checkbox toggles, synced back through the real
 *     /api/todos endpoint once reachable again). A cached API response here
 *     would silently race that logic and violate BUILD.md's "never invent
 *     or silently show stale data" rule for to-dos.
 *
 * Bump CACHE_NAME whenever SHELL_URLS changes, so an old cache doesn't
 * linger — the activate handler below deletes anything not matching the
 * current name.
 *
 * NETWORK_TIMEOUT_MS exists because of a real failure mode found
 * 2026-08-29 on iOS: reopening the Home Screen app right after force-
 * quitting it (PC/Tailscale unreachable) worked, but waiting even a few
 * seconds longer before reopening did not. The network attempt below
 * doesn't hang forever when unreachable, but how LONG it takes to fail
 * varies (DNS state, Tailscale's own connection/DERP-relay negotiation)
 * -- and iOS races its own "can't connect" interstitial against page JS
 * on a fresh top-level navigation. If our fetch() takes too long to
 * reject, iOS's native offline screen wins that race and this fetch
 * handler's cache fallback never gets to run at all. Forcing our own
 * fetch to give up on a short, fixed timer keeps us ahead of that race
 * regardless of how slow the real failure would otherwise be.
 */

const CACHE_NAME = "uvm-planner-shell-v1";
const SHELL_URLS = [
  "/",
  "/manifest.webmanifest",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
];
const NETWORK_TIMEOUT_MS = 3000;

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(SHELL_URLS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);
  if (url.pathname.startsWith("/api/")) return; // see module docstring

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), NETWORK_TIMEOUT_MS);

  event.respondWith(
    fetch(req, { cache: "no-store", signal: controller.signal })
      .then((resp) => {
        clearTimeout(timeoutId);
        // Only cache real, complete responses -- an opaque/error response
        // cached here would serve a broken page next time offline.
        if (resp && resp.ok) {
          const copy = resp.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(req, copy));
        }
        return resp;
      })
      .catch(() => {
        clearTimeout(timeoutId);
        return caches.match(req).then((cached) => cached || caches.match("/"));
      })
  );
});
