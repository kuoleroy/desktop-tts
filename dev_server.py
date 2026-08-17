# -*- coding: utf-8 -*-
"""开发用静态服务器：服务 assets/web，并捕获前端 POST /diag 的诊断日志。

特性：
- 前端热重载：监听 assets/web 下 .html/.js/.css/.json 变化，通过 SSE(/hotreload)
  通知已打开的窗口自动刷新，改动保存即生效，无需重启。
- 为 .html 响应注入热重载客户端脚本（EventSource）。

用法: python dev_server.py           # 默认 8877
用法: python dev_server.py 8899      # 指定端口
"""
import http.server
import json
import os
import socket
import sys
import threading
import time

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8877
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "web")

# 当前连接的 SSE 客户端 socket 集合
CLIENTS = set()
_CLIENTS_LOCK = threading.Lock()

# 热重载客户端脚本：连 SSE，收到 reload 事件即刷新本地页面（带自动重连）。
_HOTRELOAD_CLIENT = r"""
<script>
(function () {
  var es = null;
  function connect() {
    try {
      es = new EventSource('/hotreload');
      es.onmessage = function (e) {
        try { var d = JSON.parse(e.data); if (d && d.event === 'reload') { location.reload(); } } catch (_) {}
      };
      es.onerror = function () { if (es) { es.close(); es = null; } setTimeout(connect, 1000); };
    } catch (_) { setTimeout(connect, 2000); }
  }
  connect();
})();
</script>"""


class DualStackServer(http.server.ThreadingHTTPServer):
    """同时监听 IPv4 与 IPv6（WebView 常走 ::1）。"""
    address_family = socket.AF_INET6
    allow_reuse_address = True

    def server_bind(self):
        # 兼容 IPv6/IPv4 双栈
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except OSError:
            pass
        super().server_bind()


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    # 关闭默认日志，避免刷屏
    def log_message(self, fmt, *args):
        print("  [req] " + (fmt % args), flush=True)

    # 禁止缓存：WebView 会缓存 .js/.html，导致前端改动不生效
    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def do_GET(self):
        if self.path == "/hotreload":
            return self.serve_sse()
        path = self.path.split("?")[0]
        if path == "/":
            p = os.path.join(ROOT, "index.html")
        elif path.endswith(".html"):
            p = os.path.join(ROOT, path.lstrip("/"))
        else:
            p = None
        if p:
            p = os.path.normpath(p)
            root = os.path.normpath(ROOT)
            if (p == root or p.startswith(root + os.sep)) and os.path.isfile(p):
                return self.serve_injected_html(p)
        super().do_GET()

    def serve_injected_html(self, abs_path):
        body = b""
        try:
            with open(abs_path, "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(404, "File not found")
            return
        text = body.decode("utf-8", "replace")
        text = text.replace("</body>", _HOTRELOAD_CLIENT + "\n</body>", 1) \
            if "</body>" in text else text + _HOTRELOAD_CLIENT
        data = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def serve_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        conn = self.connection
        with _CLIENTS_LOCK:
            CLIENTS.add(conn)
        try:
            while True:
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
                time.sleep(10)
                conn.settimeout(8)
        except Exception:
            pass
        finally:
            self.wfile.close()
            conn.close()
            with _CLIENTS_LOCK:
                CLIENTS.discard(conn)

    def do_POST(self):
        if self.path == "/diag":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8", "replace")
            try:
                d = json.loads(body)
            except Exception:
                d = {"raw": body[:2000]}
            errs = d.get("errs") or []
            vrm = d.get("vrm")
            gl = d.get("gl")
            canvas = d.get("canvas")
            line = f"[diag] vrm={vrm} gl={gl} canvas={canvas}"
            if errs:
                line += " | ERRORS: " + " || ".join(errs)
            print(line, flush=True)
            self.send_response(204)
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()


def _snapshot():
    out = {}
    for root, _dirs, files in os.walk(ROOT):
        for f in files:
            if f.endswith((".html", ".js", ".css")) or f.endswith(".json"):
                path = os.path.join(root, f)
                try:
                    out[path] = os.path.getmtime(path)
                except OSError:
                    pass
    return out


def _broadcast_reload():
    payload = ("data: " + json.dumps({"event": "reload"}) + "\n\n").encode("utf-8")
    with _CLIENTS_LOCK:
        dead = []
        for conn in list(CLIENTS):
            try:
                conn.sendall(payload)
            except Exception:
                dead.append(conn)
        for c in dead:
            CLIENTS.discard(c)


def _watch_loop():
    snap = _snapshot()
    while True:
        time.sleep(1)
        cur = _snapshot()
        if cur != snap:
            snap = cur
            _broadcast_reload()


if __name__ == "__main__":
    print(f"dev server on http://localhost:{PORT}  (root={ROOT})  [hot-reload ON]", flush=True)
    threading.Thread(target=_watch_loop, daemon=True).start()
    DualStackServer(("::", PORT), Handler).serve_forever()