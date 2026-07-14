// 주식서포터 서비스워커
// 네트워크 우선(network-first) 전략: 온라인이면 항상 최신 파일을 가져오고,
// 오프라인일 때만 캐시된 마지막 버전을 보여준다.
// -> 컴퓨터에서 update_stock_data.py 로 HTML을 갱신한 뒤 재배포하면,
//    폰 앱은 다음 접속 시 온라인 상태에서 자동으로 최신 내용을 받아온다.

var CACHE_NAME = "stock-supporter-v2";
var ASSETS = ["./주식서포터.html", "./manifest.json", "./icon.svg"];

self.addEventListener("install", function (event) {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME).then(function (cache) { return cache.addAll(ASSETS); })
  );
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.filter(function (k) { return k !== CACHE_NAME; }).map(function (k) { return caches.delete(k); }));
    }).then(function () { return self.clients.claim(); })
  );
});

self.addEventListener("fetch", function (event) {
  if (event.request.method !== "GET") return;
  event.respondWith(
    // GitHub Pages가 HTML/manifest 등에 캐시(max-age)를 걸어두므로,
    // 브라우저 HTTP 캐시를 우회해서 항상 진짜 최신 파일을 받아온다.
    fetch(event.request, { cache: "no-store" })
      .then(function (res) {
        var resClone = res.clone();
        caches.open(CACHE_NAME).then(function (cache) { cache.put(event.request, resClone); });
        return res;
      })
      .catch(function () { return caches.match(event.request); })
  );
});
