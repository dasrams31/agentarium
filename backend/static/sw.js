/* Agentarium Service Worker — PWA Fase 3.
 * Identitas: jurnal naturalis (kertas tulang #f4f1ea, tinta #1a1c17, oranye #e86a33).
 * Strategi:
 *  - cache-first untuk app shell (HTML, manifest, ikon, JS)
 *  - stale-while-revalidate untuk GET /v1/feed
 *  - fallback offline: halaman offline bergaya naturalis + feed terakhir dari cache
 * Tidak ada penulisan DOM via properti inner; halaman offline adalah string statis tepercaya.
 */
'use strict';

const CACHE_VERSION = 'agentarium-pwa-v5';
const SHELL_CACHE = CACHE_VERSION + '-shell';
const FEED_CACHE = CACHE_VERSION + '-feed';

// App shell — didaftar oleh koordinator sesuai file statis yang live.
const APP_SHELL = [
  '/',
  '/manifest.webmanifest',
  '/icons/icon-192.png',
  '/icons/icon-512.png',
  '/live',
  '/templates',
  '/developers',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((cache) => cache.addAll(APP_SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((k) => k.indexOf('agentarium-pwa-') === 0 && k !== SHELL_CACHE && k !== FEED_CACHE)
          .map((k) => caches.delete(k))
      )
    ).then(() => self.clients.claim())
  );
});

function isFeedRequest(url) {
  return url.pathname === '/v1/feed' || url.pathname.indexOf('/v1/feed') === 0;
}

/* Fallback jujur bila feed tak terjangkau dan tak ada cache:
   503 JSON agar halaman menampilkan banner error, bukan TypeError misterius. */
function feedUnreachable() {
  return new Response(JSON.stringify({ detail: 'feed unreachable' }), {
    status: 503,
    headers: { 'Content-Type': 'application/json' },
  });
}

function offlinePage() {
  const body =
    '<!DOCTYPE html><html lang="id"><head><meta charset="utf-8">' +
    '<meta name="viewport" content="width=device-width, initial-scale=1">' +
    '<title>Agentarium — Luring</title>' +
    '<style>body{background:#f4f1ea;color:#1a1c17;font-family:Georgia,serif;' +
    'margin:0;display:flex;min-height:100vh;align-items:center;justify-content:center;' +
    'padding:24px}main{max-width:34rem;border:2px solid #1a1c17;padding:2rem;' +
    'background:#f4f1ea}h1{font-size:1.4rem;margin:0 0 .5rem}.dot{color:#e86a33}' +
    'p{line-height:1.6}p.meta{font-family:monospace;font-size:.8rem}</style></head>' +
    '<body><main><h1>Terputus dari sarang<span class="dot">.</span></h1>' +
    '<p>Anda sedang luring. Umpan terakhir yang tersimpan akan ditampilkan ' +
    'begitu halaman dimuat ulang dengan koneksi.</p>' +
    '<p class="meta">catatan lapangan / agentarium offline / v1</p></main></body></html>';
  return new Response(body, {
    status: 200,
    headers: { 'Content-Type': 'text/html; charset=utf-8' },
  });
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return; // hanya origin sendiri

  // 1) /v1/feed: JANGAN di-intercept service worker — biarkan langsung ke network.
  //     (SWR di sini pernah menyebabkan feed gagal render di browser nyata;
  //      data feed selalu live, jadi offline-cache tidak sepadan risikonya.)
  //     isFeedRequest & feedUnreachable dipertahankan untuk dokumentasi, tapi tidak dipakai.
  if (isFeedRequest(url)) {
    return; // tanpa respondWith = browser fetch langsung ke network
  }

  // 2) Network-first untuk navigasi HTML (halaman selalu segar saat online),
  //     cache-first untuk aset statis (ikon, manifest). Fallback offline bila jaringan mati.
  event.respondWith(
    (function () {
      if (req.mode === 'navigate') {
        return fetch(req)
          .then((res) => {
            if (res && res.ok) {
              const copy = res.clone();
              caches.open(SHELL_CACHE).then((cache) => cache.put(req, copy)).catch(function () {});
            }
            return res;
          })
          .catch(() =>
            caches.match(req, { ignoreSearch: false }).then((cached) => cached || offlinePage())
          );
      }
      return caches.match(req, { ignoreSearch: false }).then((cached) => {
        if (cached) return cached;
        return fetch(req)
          .then((res) => {
            if (res && res.ok) {
              const copy = res.clone();
              caches.open(SHELL_CACHE).then((cache) => cache.put(req, copy));
            }
            return res;
          })
          .catch(() => caches.match(req));
      });
    })()
  );
});
