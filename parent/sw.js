/* 家长端 Service Worker：壳资源缓存；名单/首页网络优先（改名单能及时生效） */
var CACHE = "hs-board-v3";
var SHELL = [
  "./", "./index.html", "./mqtt.min.js", "./manifest.json", "./guide.html",
  "./icons/icon-192.png", "./icons/icon-512.png", "./icons/icon-maskable-512.png"
];

self.addEventListener("install", function (e) {
  e.waitUntil(
    caches.open(CACHE).then(function (c) { return c.addAll(SHELL); })
      .then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener("activate", function (e) {
  e.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.filter(function (k) { return k !== CACHE; })
        .map(function (k) { return caches.delete(k); }));
    }).then(function () { return self.clients.claim(); })
  );
});

self.addEventListener("fetch", function (e) {
  var req = e.request;
  if (req.method !== "GET") return;
  var url = new URL(req.url);
  if (url.origin !== location.origin) return;

  var netFirst = url.pathname.endsWith("/") ||
                 /index\.html$/.test(url.pathname) ||
                 /guide\.html$/.test(url.pathname) ||
                 /config\/(classes|students\/[^/]+)\.json$/.test(url.pathname);
  if (netFirst) {
    e.respondWith(
      fetch(req).then(function (r) {
        var cp = r.clone();
        caches.open(CACHE).then(function (c) { c.put(req, cp); });
        return r;
      }).catch(function () { return caches.match(req, { ignoreSearch: true }); })
    );
  } else {
    e.respondWith(
      caches.match(req).then(function (hit) {
        return hit || fetch(req).then(function (r) {
          var cp = r.clone();
          caches.open(CACHE).then(function (c) { c.put(req, cp); });
          return r;
        });
      })
    );
  }
});
