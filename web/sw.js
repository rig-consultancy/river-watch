// River Watch as an installable app: pages, scripts and the last data are kept so the dashboard
// still opens without a connection. Network first, so a new deploy or a new image shows at once;
// the saved copy is only used when the network fails. Map tiles are not kept.
const CACHE = "river-watch-v1";
const SHELL = ["./", "index.html", "app.js", "i18n.js", "style.css", "report.html", "report.js", "report.css",
  "ward.html", "ward.js", "manifest.webmanifest", "icons/icon-192.png", "data/index.json"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  const leaflet = url.hostname === "cdnjs.cloudflare.com";
  if (e.request.method !== "GET" || (url.origin !== location.origin && !leaflet)) return;
  // The page asks for fresh data with cache: "no-cache"; keep the answer without the query string.
  const key = url.origin === location.origin ? url.origin + url.pathname : e.request.url;
  e.respondWith(fetch(e.request).then((res) => {
    if (res.ok) { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(key, copy)); }
    return res;
  }).catch(() => caches.match(key).then((hit) => hit || caches.match("index.html"))));
});
