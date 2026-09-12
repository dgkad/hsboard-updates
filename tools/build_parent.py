# -*- coding: utf-8 -*-
"""
家长端打包脚本：把 parent/ 组装成可直接部署的静态站点（EdgeOne Pages 等静态托管）。

输出：
    dist/parent-web/          站点根目录（把整个文件夹上传即可）
    dist/parent-web.zip       同内容的 zip 包

做了什么：
    1. 名单路径改写：fetch("../config/students.json") -> fetch("./config/students.json")
       （源站里 parent/ 是项目子目录所以用 ..；独立部署后站点根就是包根）
    2. 注入 PWA：manifest.json + 图标 + Service Worker 注册
       （手机浏览器"添加到主屏幕"后可当 App 打开；壳资源离线可用）
    3. 标题去掉 PoC 字样（只改产物，源文件 parent/index.html 不动）

用法：
    python tools/build_parent.py
"""
import os
import shutil

from PIL import Image, ImageDraw

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_HTML = os.path.join(ROOT_DIR, "parent", "index.html")
SRC_MQTT = os.path.join(ROOT_DIR, "parent", "mqtt.min.js")
SRC_CLASSES = os.path.join(ROOT_DIR, "config", "classes.json")
SRC_STU_DIR = os.path.join(ROOT_DIR, "config", "students")
SRC_GUIDE = os.path.join(ROOT_DIR, "docs", "家长老师使用指南.html")
OUT_DIR = os.path.join(ROOT_DIR, "dist", "parent-web")

THEME = "#07c160"

HEAD_INJECT = """<meta name="theme-color" content="{theme}">
<link rel="manifest" href="./manifest.json">
<link rel="apple-touch-icon" href="./icons/icon-192.png">"""

SW_SNIPPET = """<script>
if ("serviceWorker" in navigator) addEventListener("load", function () {
  navigator.serviceWorker.register("./sw.js");
});
</script>
</body>"""


# ---------- PWA 图标：绿色圆角方块 + 白色气泡（三圆点），4x 超采样抗锯齿 ----------
def make_icon(size, maskable, path):
    S = 4
    big = size * S
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    g = (7, 193, 96, 255)
    if maskable:
        d.rectangle([0, 0, big - 1, big - 1], fill=g)          # 全出血纯色底（安全区由系统裁）
        pad = 0.12                                             # 内容缩进到中间 ~76%
        radius = 0
    else:
        d.rounded_rectangle([0, 0, big - 1, big - 1], radius=int(big * 0.22), fill=g)
        pad = 0.0
        radius = 0.05

    def R(f):  # 百分比 -> 像素
        return int(big * f)

    # 气泡主体
    d.rounded_rectangle(
        [R(0.24 + pad), R(0.28 + pad), R(0.76 - pad), R(0.60 - pad)],
        radius=int(big * (0.16 - pad)), fill=(255, 255, 255, 255))
    # 气泡尾巴
    d.polygon([
        (R(0.36 + pad), R(0.58 - pad)),
        (R(0.30 + pad), R(0.72 - pad)),
        (R(0.50 - pad), R(0.58 - pad)),
    ], fill=(255, 255, 255, 255))
    # 三个圆点
    cy = R(0.44 + pad * 0.5)
    for fx in (0.385, 0.50, 0.615):
        x = R(fx + pad * (0.5 - fx) * 2)
        rr = R(0.032)
        d.ellipse([x - rr, cy - rr, x + rr, cy + rr], fill=g)

    img = img.resize((size, size), Image.LANCZOS)
    img.save(path)


MANIFEST = """{
  "name": "班级留言板",
  "short_name": "留言板",
  "description": "家校留言板：家长给孩子留言，老师发普通/紧急通知",
  "lang": "zh-CN",
  "start_url": "./",
  "scope": "./",
  "display": "standalone",
  "orientation": "portrait",
  "background_color": "#ededed",
  "theme_color": "%s",
  "icons": [
    { "src": "icons/icon-192.png", "sizes": "192x192", "type": "image/png" },
    { "src": "icons/icon-512.png", "sizes": "512x512", "type": "image/png" },
    { "src": "icons/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable" }
  ]
}
""" % THEME

SW_JS = """/* 家长端 Service Worker：壳资源缓存；名单/首页网络优先（改名单能及时生效） */
var CACHE = "hs-board-v2";
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
                 /index\\.html$/.test(url.pathname) ||
                 /guide\\.html$/.test(url.pathname) ||
                 /config\\/(classes|students\\/[^/]+)\\.json$/.test(url.pathname);
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
"""


def main():
    # 输出目录结构：站点根 = 包根
    if os.path.isdir(OUT_DIR):
        shutil.rmtree(OUT_DIR)
    os.makedirs(os.path.join(OUT_DIR, "config"))
    os.makedirs(os.path.join(OUT_DIR, "icons"))

    shutil.copy2(SRC_MQTT, os.path.join(OUT_DIR, "mqtt.min.js"))
    shutil.copy2(SRC_CLASSES, os.path.join(OUT_DIR, "config", "classes.json"))
    if os.path.isdir(SRC_STU_DIR):
        for fn in sorted(os.listdir(SRC_STU_DIR)):
            if fn.endswith(".json"):
                os.makedirs(os.path.join(OUT_DIR, "config", "students"), exist_ok=True)
                shutil.copy2(os.path.join(SRC_STU_DIR, fn),
                             os.path.join(OUT_DIR, "config", "students", fn))
    shutil.copy2(SRC_GUIDE, os.path.join(OUT_DIR, "guide.html"))

    with open(SRC_HTML, "r", encoding="utf-8") as f:
        html = f.read()

    # 1) 配置路径改写（../config/ -> ./config/，覆盖 classes.json 与 students/{id}.json）
    assert 'fetch("../config/classes.json")' in html, "源 index.html 未找到班级清单 fetch 语句"
    assert 'fetch("../config/students/"' in html, "源 index.html 未找到名单 fetch 语句"
    html = html.replace('"../config/', '"./config/')

    # 2) 标题去掉 PoC 字样
    html = html.replace("<title>班级留言板（家长/老师端 PoC）</title>",
                        "<title>班级留言板</title>")

    # 3) 注入 PWA（str.replace，模板含 CSS 花括号禁用 format）
    html = html.replace("</title>", "</title>\n" + HEAD_INJECT.format(theme=THEME), 1)
    html = html.replace("</body>", SW_SNIPPET, 1)

    with open(os.path.join(OUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)

    # 4) 图标 + manifest + sw
    make_icon(192, False, os.path.join(OUT_DIR, "icons", "icon-192.png"))
    make_icon(512, False, os.path.join(OUT_DIR, "icons", "icon-512.png"))
    make_icon(512, True, os.path.join(OUT_DIR, "icons", "icon-maskable-512.png"))

    with open(os.path.join(OUT_DIR, "manifest.json"), "w", encoding="utf-8") as f:
        f.write(MANIFEST)
    with open(os.path.join(OUT_DIR, "sw.js"), "w", encoding="utf-8") as f:
        f.write(SW_JS)

    # 5) 自检
    checks = {
        "配置路径已改写": html.count('fetch("./config/classes.json")') == 1
                          and html.count('fetch("./config/students/"') == 1,
        "旧路径已清除": "../config/" not in html,
        "manifest 注入": html.count('rel="manifest"') == 1,
        "SW 注册注入": html.count('serviceWorker.register') == 1,
        "标题已更新": "<title>班级留言板</title>" in html,
        "指南按钮(登录卡)": html.count('id="btnGuide"') == 1,
        "指南入口(聊天页)": html.count('id="hdrGuide"') == 1,
        "指南已嵌入": os.path.isfile(os.path.join(OUT_DIR, "guide.html")),
    }
    for name, ok in checks.items():
        print(("  [OK] " if ok else "  [FAIL] ") + name)
    if not all(checks.values()):
        raise SystemExit("打包自检未通过")

    # 6) zip
    zip_path = shutil.make_archive(
        os.path.join(ROOT_DIR, "dist", "parent-web"), "zip",
        root_dir=os.path.join(ROOT_DIR, "dist"), base_dir="parent-web")

    print("\n打包完成：")
    for dirpath, _, files in os.walk(OUT_DIR):
        for fn in sorted(files):
            p = os.path.join(dirpath, fn)
            print("  %6.1f KB  %s" % (os.path.getsize(p) / 1024,
                                      os.path.relpath(p, OUT_DIR)))
    print("\n站点目录: %s" % OUT_DIR)
    print("压缩包  : %s" % zip_path)


if __name__ == "__main__":
    main()
