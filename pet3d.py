# -*- coding: utf-8 -*-
"""3D 桌宠 Web 端：本地静态服务 + pywebview 透明窗口骨架"""
import os
import sys
import time
import threading
import functools
import http.server
import socketserver

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "assets", "web")
MODELS_DIR = os.path.join(BASE_DIR, "models")
TTS_DIR = os.path.join(BASE_DIR, "tts_cache")


class _Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, root=None, **kwargs):
        super().__init__(*args, directory=root or WEB_DIR, **kwargs)

    def do_POST(self):
        if self.path == "/diag":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8", "replace")
            with open(os.path.join(BASE_DIR, "diag.json"), "w", encoding="utf-8") as f:
                f.write(body)
            self.send_response(200)
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def translate_path(self, path):
        if path.startswith("/models/"):
            return os.path.join(MODELS_DIR, path[len("/models/"):].replace("/", os.sep))
        if path.startswith("/tts/"):
            return os.path.join(TTS_DIR, path[len("/tts/"):].replace("/", os.sep))
        return super().translate_path(path)

    def log_message(self, fmt, *args):
        try:
            with open(os.path.join(BASE_DIR, "web_req.log"), "a", encoding="utf-8") as f:
                f.write(f"{self.address_string()} {fmt % args}\n")
        except OSError:
            pass


def start_server(port=8877):
    handler = functools.partial(_Handler, root=WEB_DIR)
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


class Api:
    def __init__(self):
        self.dragging = False
        self.voice = "zh-CN-XiaoxiaoNeural"
        self.rate = 0
        self.pitch = "medium"
        self._lock = threading.Lock()
        os.makedirs(TTS_DIR, exist_ok=True)

    def list_models(self):
        try:
            return sorted(f for f in os.listdir(MODELS_DIR) if f.lower().endswith((".vrm", ".glb")))
        except OSError:
            return []

    def list_voices(self):
        try:
            import edge_tts
            return [v["ShortName"] for v in edge_tts.list_voices().get("zh-CN", [])]
        except Exception:
            return ["zh-CN-XiaoxiaoNeural"]

    def _tts_sync(self, text):
        """edge-tts 合成 mp3 到 tts_cache，返回可播放 URL；失败则回退 SAPI wav"""
        name = f"t{int(time.time() * 1000)}"
        fname = os.path.join(TTS_DIR, name + ".mp3")
        try:
            import edge_tts
            rate_s = f"{self.rate * 10:+d}%" if self.rate else "+0%"
            pitch_s = {"low": "-10Hz", "medium": "+0Hz", "high": "+10Hz"}.get(self.pitch, "+0Hz")
            coro = edge_tts.Communicate(text, self.voice, rate=rate_s, pitch=pitch_s).save(fname)
            import asyncio
            asyncio.run(coro)
            return f"/tts/{name}.mp3"
        except Exception as e:
            print("[tts] edge fail:", repr(e))
            return self._tts_sapi(text)

    def _tts_sapi(self, text):
        """回退：win32com 直调 SAPI 合成 wav"""
        try:
            import win32com.client
            name = f"t{int(time.time() * 1000)}"
            fname = os.path.join(TTS_DIR, name + ".wav")
            voice = win32com.client.Dispatch("SAPI.SpVoice")
            stream = win32com.client.Dispatch("SAPI.SpFileStream")
            stream.Format.Type = 39  # 48kHz 16bit mono
            stream.Open(fname, 3)  # SSFMCreateForWrite
            voice.AudioOutputStream = stream
            rate = int(self.rate * 5)
            voice.Rate = rate
            try:
                voice.Speak(text)
            finally:
                stream.Close()
            return f"/tts/{name}.wav"
        except Exception as e:
            print("[tts] sapi fail:", repr(e))
            return None

    def read_text(self, text):
        def worker():
            with self._lock:
                name = f"t{int(time.time() * 1000)}"
                fname = os.path.join(TTS_DIR, name + ".mp3")
                url = self._tts_sync(text)
                if not url:
                    return
                import webview
                try:
                    webview.windows[0].evaluate_js(f"playAudioFrom('http://127.0.0.1:8877{url}?v={int(time.time()*1000)}')")
                except Exception as e:
                    print("[tts] eval fail:", repr(e))
        threading.Thread(target=worker, daemon=True).start()
        return "ok"

    def stop_read(self):
        print("[api] stop")
        try:
            import webview
            webview.windows[0].evaluate_js("stopAudio()")
        except Exception as e:
            print("[tts] stop eval fail:", repr(e))

    def export_mp3(self):
        print("[api] export")

    def grab(self):
        print("[api] grab")

    def home(self):
        print("[api] home")

    def quit(self):
        print("[api] quit")
        os._exit(0)

    def toggle(self):
        print("[api] toggle")

    def drag_start(self, x, y):
        self.dragging = True
        print("[api] drag start", x, y)

    def drag_end(self):
        self.dragging = False
        print("[api] drag end")

    def set_voice(self, name):
        self.voice = name
        print("[api] voice:", name)

    def set_rate(self, rate):
        self.rate = int(rate)
        print("[api] rate:", rate)

    def set_pitch(self, pitch):
        self.pitch = pitch
        print("[api] pitch:", pitch)

    def resize_window(self, w):
        print("[api] resize:", w)

    def set_click_through(self, on):
        import os as _os
        return set_click_through(_os.getpid(), bool(on))

    def toggle_mode(self):
        """观赏/交互模式切换：穿透切换 + JS 显隐面板"""
        import os as _os
        cur = get_click_through(_os.getpid())
        on = not cur if cur is not None else True
        set_click_through(_os.getpid(), on)
        mode = "watch" if on else "interact"
        try:
            import webview
            if webview.windows:
                webview.windows[0].evaluate_js(f"setMode('{mode}')")
        except Exception as e:
            print("[mode] eval fail:", repr(e))
        return mode

    def get_click_through(self):
        import os as _os
        return get_click_through(_os.getpid())


_hotkey_thread = None


def start_hotkey(api):
    """全局热键：Ctrl+Shift+T 切换点击穿透（RegisterHotKey，可靠）"""
    import threading as _th
    import ctypes
    from ctypes import wintypes

    MOD_CONTROL = 0x0002
    MOD_SHIFT = 0x0004
    MOD_NOREPEAT = 0x4000
    VK_T = 0x54
    WM_HOTKEY = 0x0312
    HOTKEY_ID = 1

    def toggle_from_hotkey(api):
        try:
            mode = api.toggle_mode()
        except Exception as e:
            print("[hotkey] fail:", repr(e))

    def _listen():
        user32 = ctypes.windll.user32
        if not user32.RegisterHotKey(None, HOTKEY_ID, MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT, VK_T):
            print("[hotkey] RegisterHotKey failed")
            return
        print("[hotkey] registered Ctrl+Shift+T")
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                toggle_from_hotkey(api)
            else:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))

    global _hotkey_thread
    if _hotkey_thread is None or not _hotkey_thread.is_alive():
        _hotkey_thread = _th.Thread(target=_listen, daemon=True)
        _hotkey_thread.start()


def make_transparent(hwnd):
    """WebView2 透明共用方案：WS_EX_NOREDIRECTIONBITMAP + 页面透明背景"""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    GWL_EXSTYLE = -20
    WS_EX_NOREDIRECTIONBITMAP = 0x00200000
    ex = user32.GetWindowLongW(wintypes.HWND(hwnd), GWL_EXSTYLE)
    ret = user32.SetWindowLongW(wintypes.HWND(hwnd), GWL_EXSTYLE, ex | WS_EX_NOREDIRECTIONBITMAP)
    ex2 = user32.GetWindowLongW(wintypes.HWND(hwnd), GWL_EXSTYLE)
    try:
        with open(os.path.join(BASE_DIR, "web_req.log"), "a", encoding="utf-8") as f:
            f.write(f"[transparent] hwnd={hwnd} before={ex:#x} setret={ret} after={ex2:#x} NORE={bool(ex2 & WS_EX_NOREDIRECTIONBITMAP)}\n")
    except OSError:
        pass
    return bool(ex2 & WS_EX_NOREDIRECTIONBITMAP)


def find_top_hwnd(pid):
    """枚举进程窗口，返回 WinForms 主窗口（BrowserForm），否则 None"""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    result = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _):
        wpid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(wpid))
        if wpid.value == pid and user32.IsWindowVisible(wintypes.HWND(hwnd)):
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(wintypes.HWND(hwnd), cls, 256)
            if cls.value.startswith("WindowsForms"):
                result.append(int(hwnd))
        return True

    user32.EnumWindows(_cb, 0)
    return result[0] if result else None


_MAIN_HWND = [None]


def set_main_hwnd(hwnd):
    _MAIN_HWND[0] = hwnd


def _hwnd():
    if _MAIN_HWND[0]:
        return _MAIN_HWND[0]
    import ctypes
    return find_top_hwnd(ctypes.windll.kernel32.GetCurrentProcessId())


def _refresh_window(hwnd):
    """改完窗口样式后强制 DWM 重新合成（否则视觉停留在玻璃模式/窗口消失）"""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    SWP_NOSIZE = 0x0001
    SWP_NOMOVE = 0x0002
    SWP_NOZORDER = 0x0004
    SWP_NOACTIVATE = 0x0010
    SWP_FRAMECHANGED = 0x0020
    user32.SetWindowPos(
        wintypes.HWND(hwnd), None, 0, 0, 0, 0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
    )
    RDW_INVALIDATE = 0x0001
    RDW_ALLCHILDREN = 0x0080
    RDW_FRAME = 0x0400
    RDW_UPDATENOW = 0x0100
    user32.RedrawWindow(wintypes.HWND(hwnd), None, None, RDW_INVALIDATE | RDW_ALLCHILDREN | RDW_FRAME | RDW_UPDATENOW)


def set_click_through(pid, on):
    """鼠标穿透：主窗口 + WebView2 子窗口树 + CoreWebView2Controller.IsBrowserHitTransparent"""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    GWL_EXSTYLE = -20
    WS_EX_TRANSPARENT = 0x00000020
    hwnd = _hwnd()
    if not hwnd:
        return None

    def walk(h, flag):
        ex = user32.GetWindowLongW(wintypes.HWND(h), GWL_EXSTYLE)
        if flag:
            ex |= WS_EX_TRANSPARENT
        else:
            ex &= ~WS_EX_TRANSPARENT
        user32.SetWindowLongW(wintypes.HWND(h), GWL_EXSTYLE, ex)
        kids = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def cb(ch, _):
            kids.append(int(ch))
            return True

        user32.EnumChildWindows(wintypes.HWND(h), cb, 0)
        for k in kids:
            walk(k, flag)

    walk(hwnd, on)
    _refresh_window(hwnd)
    _set_browser_hit_transparent(on)
    return bool(user32.GetWindowLongW(wintypes.HWND(hwnd), GWL_EXSTYLE) & WS_EX_TRANSPARENT)


def _log_trans(s):
    try:
        with open(os.path.join(BASE_DIR, "web_req.log"), "a", encoding="utf-8") as f:
            f.write("[transparent] " + s + "\n")
    except OSError:
        pass


def _set_browser_hit_transparent(on):
    """WebView2 官方 API：让浏览器内容对鼠标点击透明（必须 UI 线程，用 BeginInvoke 调度）"""
    try:
        import webview
        if not webview.windows:
            _log_trans("no windows")
            return False
        native = webview.windows[0].native
        from Microsoft.Web.WebView2.WinForms import WebView2
        for c in native.Controls:
            if isinstance(c, WebView2):
                def set_it(c=c, on=on):
                    try:
                        if c.CoreWebView2 and hasattr(c.CoreWebView2, "Controller") and c.CoreWebView2.Controller:
                            c.CoreWebView2.Controller.IsBrowserHitTransparent = bool(on)
                            _log_trans(f"IsBrowserHitTransparent = {on} (ui thread)")
                            return True
                        _log_trans("IsBrowserHitTransparent unsupported, relying on WS_EX_TRANSPARENT patch")
                    except Exception as e:
                        _log_trans("set_it fail: " + repr(e))
                    return False
                try:
                    from System.Windows.Forms import MethodInvoker
                    invoker = MethodInvoker(lambda: set_it())
                    native.BeginInvoke(invoker)
                    _log_trans(f"BeginInvoke scheduled (target={on})")
                except Exception as e:
                    _log_trans("BeginInvoke fail: " + repr(e))
                return True
        _log_trans("WebView2 control not found")
    except Exception as e:
        _log_trans("set hit transparent fail: " + repr(e))
    return False


def get_click_through(pid):
    """读取当前是否处于点击穿透状态"""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    GWL_EXSTYLE = -20
    WS_EX_TRANSPARENT = 0x00000020
    hwnd = _hwnd()
    if not hwnd:
        return None
    return bool(user32.GetWindowLongW(wintypes.HWND(hwnd), GWL_EXSTYLE) & WS_EX_TRANSPARENT)


def main():
    srv, port = start_server()
    print(f"server on http://127.0.0.1:{port}")

    import webview
    api = Api()
    d = int(time.time())  # 强制刷新缓存
    win = webview.create_window(
        "3D Pet",
        f"http://127.0.0.1:{port}/index.html?v={d}",
        width=240,
        height=300,
        transparent=True,
        frameless=True,
        on_top=True,
        easy_drag=False,
        js_api=api,
    )
    start_hotkey(api)

    def _apply_transparent():
        import os as _os
        def log(s):
            try:
                with open(_os.path.join(BASE_DIR, "web_req.log"), "a", encoding="utf-8") as f:
                    f.write("[transparent] " + s + "\n")
            except OSError:
                pass
        try:
            pid = _os.getpid()
            native = win.native
            hwnd = int(native.Handle.ToInt32())
            set_main_hwnd(hwnd)
            log("native hwnd=" + str(hwnd))
            make_transparent(hwnd)
            log("native=" + repr(native))
            from System.Drawing import Color
            mode = _os.environ.get("EXP_MODE", "C")
            if mode != "A":
                native.BackColor = Color.FromArgb(240, 240, 240)
                native.TransparencyKey = Color.FromArgb(240, 240, 240)
                log("transparencykey set (mode=" + mode + ")")
            else:
                log("transparencykey skipped (mode=A)")
        except Exception as e:
            log("fail: " + repr(e))

    win.events.shown += _apply_transparent

    webview.start(debug=True)


if __name__ == "__main__":
    main()
