# -*- coding: utf-8 -*-
"""开发用静态服务器：服务 assets/web，并捕获前端 POST /diag 的诊断日志。

用法: python dev_server.py           # 默认 8877
用法: python dev_server.py 8899      # 指定端口
"""
import http.server
import json
import os
import socket
import sys

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8877
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "web")


class DualStackServer(http.server.ThreadingHTTPServer):
    """同时监听 IPv4 与 IPv6（WebView 常走 ::1）。"""
    address_family = socket.AF_INET6

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


if __name__ == "__main__":
    print(f"dev server on http://localhost:{PORT}  (root={ROOT})", flush=True)
    DualStackServer(("::", PORT), Handler).serve_forever()