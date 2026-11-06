/* Wiki Mobile service worker
 * - 앱 화면(셸)은 오프라인에서도 열리도록 캐시
 * - 위키 이미지는 한 번 본 것을 캐시 (오프라인 열람용)
 * - GraphQL(POST)은 건드리지 않음 — 문서 데이터 캐시는 앱(index.html)이 처리
 */
const VER = '1.0.2';
const SHELL = 'wiki-m-shell-' + VER;
const IMG = 'wiki-m-img';
const IMG_MAX = 400;
const BASE = new URL(self.registration.scope).pathname; // 예: /m/
const ASSETS = ['', 'index.html', 'manifest.json', 'icons/icon-180.png', 'icons/icon-192.png', 'icons/icon-512.png'].map(p => BASE + p);

self.addEventListener('install', e => {
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil((async () => {
    for (const k of await caches.keys()) if (k.startsWith('wiki-m-shell-') && k !== SHELL) await caches.delete(k);
    await self.clients.claim();
  })());
});

async function trimImages() {
  const c = await caches.open(IMG); const keys = await c.keys();
  for (let i = 0; i < keys.length - IMG_MAX; i++) await c.delete(keys[i]);
}

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;

  if (url.pathname.startsWith(BASE)) {
    if (req.mode === 'navigate') {
      // 앱 화면: 네트워크 우선, 실패 시 캐시
      e.respondWith((async () => {
        try {
          const res = await fetch(req);
          if (res.ok) { const c = await caches.open(SHELL); c.put(BASE, res.clone()); }
          return res;
        } catch (err) {
          return (await caches.match(BASE)) || (await caches.match(BASE + 'index.html')) || Response.error();
        }
      })());
      return;
    }
    // 앱 정적 파일: 캐시 즉시 응답 + 백그라운드 갱신
    e.respondWith((async () => {
      const c = await caches.open(SHELL);
      const hit = await c.match(req);
      const net = fetch(req).then(res => { if (res.ok) c.put(req, res.clone()); return res; }).catch(() => null);
      if (hit) { e.waitUntil(net); return hit; }
      return (await net) || Response.error();
    })());
    return;
  }

  if (req.destination === 'image') {
    // 위키 이미지: 네트워크 우선, 실패 시 캐시
    e.respondWith((async () => {
      const c = await caches.open(IMG);
      try {
        const res = await fetch(req);
        if (res.ok) { c.put(req, res.clone()).then(trimImages); }
        return res;
      } catch (err) {
        return (await c.match(req)) || Response.error();
      }
    })());
  }
});
