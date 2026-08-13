# -*- coding: utf-8 -*-
"""全局选区抓取独立进程 —— 纯事件驱动版（无鼠标钩子，零全局监控）。

不再监控鼠标，改用两条系统主动通知：
1. UIA 文本选择变化事件（UIA_Text_TextSelectionChangedEventId=20014）：
   支持 UIA 的应用（记事本/浏览器/Word/VS Code 等）里拖选文字时，系统主动通知，
   直接读事件发送者的 TextPattern 选区，不遍历、不拦截鼠标。
2. 剪贴板变化监听（WM_CLIPBOARDUPDATE）：
   不支持 UIA 的应用（如部分 IDE），用户手动 Ctrl+C 复制时系统通知，读取剪贴板文本。
   绝不模拟按键，不干预系统输入。

- 进程默认待命（armed=False），收到 {"cmd":"arm"} 后才响应；收到 {"cmd":"disarm"} 停止响应。
- 读到的文字以 NDJSON 上报 Rust（移动悬浮框 + 填充文本）。

输入(每行): {"cmd": "arm"|"disarm"}
输出(每行): {"grab": true, "text": "...", "x": 123, "y": 456}
"""
import ctypes
import ctypes.wintypes
import json
import sys
import threading
import time

# ---- 常量 ----
# 单次抓取最大字符数（5万，约整章/多章；分块按 2000 字切后顺序朗读）
MAX_GRAB_CHARS = 50000
# UIA
TextPatternId = 10014
UIA_Text_TextSelectionChangedEventId = 20014
TreeScope_Subtree = 4
# 窗口消息
WM_CLIPBOARDUPDATE = 0x031D
WM_TIMER = 0x0113
DEBOUNCE_MS = 350  # 选择事件高频触发，防抖后只读一次
DEBOUNCE_ID = 1

COINIT_APARTMENTTHREADED = 0x2


def dbg(msg):
    try:
        sys.stderr.write("[grabber] " + msg + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def out(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


# ---- 剪贴板读取（纯 ctypes，不模拟任何按键）----
def _read_clipboard_text():
    CF_UNICODETEXT = 13
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.OpenClipboard.restype = ctypes.c_int
    user32.OpenClipboard.argtypes = [ctypes.wintypes.HWND]
    user32.GetClipboardData.restype = ctypes.wintypes.HANDLE
    user32.GetClipboardData.argtypes = [ctypes.c_uint]
    user32.CloseClipboard.restype = ctypes.c_int
    user32.CloseClipboard.argtypes = []
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = ctypes.c_int
    kernel32.GlobalUnlock.argtypes = [ctypes.wintypes.HGLOBAL]
    try:
        if not user32.OpenClipboard(None):
            return ''
        try:
            h = user32.GetClipboardData(CF_UNICODETEXT)
            if not h:
                return ''
            p = kernel32.GlobalLock(h)
            if not p:
                return ''
            try:
                return ctypes.wstring_at(p)
            finally:
                kernel32.GlobalUnlock(h)
        finally:
            user32.CloseClipboard()
    except Exception:
        return ''


def _cursor_pos():
    try:
        p = ctypes.wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(p))
        return (p.x, p.y)
    except Exception:
        return (0, 0)


def _element_anchor(el, fallback):
    """取选区包围盒下方作为悬浮框位置；失败用 fallback（鼠标位置）。"""
    try:
        r = el.CurrentBoundingRectangle
        if isinstance(r, (tuple, list)) and len(r) >= 4:
            return (int(r[0]) + 8, int(r[3]) + 12)
        if hasattr(r, "left"):
            return (int(r.left) + 8, int(r.bottom) + 12)
    except Exception:
        pass
    return fallback


def main():
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="strict")
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    except Exception:
        pass

    # 是否处于「已武装」状态：只有 arm 后才响应事件
    armed = {"on": False}
    # 待处理抓取（防抖后消费）
    pending = {"element": None, "dirty": False}
    # 上次上报文本（去重，避免 Ctrl+C 悬浮框复制引发的重复上报）
    last_text = [""]
    # 隐藏窗口句柄：UIA 回调线程经它投递防抖定时器到主线程消息泵
    hwnd_box = {"hwnd": None}
    # 首次收到 UIA 20014 事件的诊断标记（避免刷屏）
    seen_uia = {"hit": False}

    # ---- STA 主线程 + UIA 事件注册（必须在主线程，事件依赖其消息泵）----
    ctypes.windll.ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)

    import comtypes
    import comtypes.client
    comtypes.client.GetModule(r"C:\Windows\System32\UIAutomationCore.dll")
    from comtypes.gen import UIAutomationClient as UIA
    from comtypes import COMMETHOD, GUID, HRESULT, IUnknown
    from ctypes import POINTER, c_int

    uia = comtypes.client.CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation)
    root = uia.GetRootElement()

    class IUIAutomationEventHandler(IUnknown):
        _iid_ = GUID("{146c3c17-f12e-4e22-8c27-f894b9b79c69}")
        _methods_ = [
            COMMETHOD([], HRESULT, 'HandleAutomationEvent',
                      (['in'], POINTER(IUnknown), 'sender'),
                      (['in'], c_int, 'eventId')),
        ]

    class MyHandler(comtypes.COMObject):
        _com_interfaces_ = [IUIAutomationEventHandler]

        def HandleAutomationEvent(self, sender, eventId):
            if eventId != UIA_Text_TextSelectionChangedEventId:
                return
            if not armed["on"]:
                return
            if not seen_uia["hit"]:
                seen_uia["hit"] = True
                dbg("UIA 20014 selection event received")
            try:
                el = sender.QueryInterface(UIA.IUIAutomationElement)
            except Exception:
                return
            pending["element"] = el
            pending["dirty"] = True
            # 重启防抖定时器：连续事件只读一次。
            # 注意：UIA 回调在系统线程执行，SetTimer 必须绑定主线程隐藏窗口
            # (hwnd)，否则 WM_TIMER 投递到回调线程队列，主消息泵永远收不到。
            user32 = ctypes.windll.user32
            user32.SetTimer.restype = ctypes.c_void_p  # UINT_PTR
            user32.SetTimer.argtypes = [
                ctypes.wintypes.HWND, ctypes.wintypes.UINT,
                ctypes.c_uint, ctypes.c_void_p]
            user32.KillTimer.restype = ctypes.c_int
            user32.KillTimer.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.UINT]
            hw = hwnd_box["hwnd"]
            if hw:
                user32.KillTimer(hw, DEBOUNCE_ID)
                user32.SetTimer(hw, DEBOUNCE_ID, DEBOUNCE_MS, None)
            else:
                # 隐藏窗口尚未创建（极早期）：直接消费本次选区
                threading.Thread(target=_do_grab, args=("uia",), daemon=True).start()

    handler = MyHandler()  # 强引用，防 GC
    try:
        uia.AddAutomationEventHandler(
            UIA_Text_TextSelectionChangedEventId, root, TreeScope_Subtree, None, handler)
        dbg("UIA selection event registered")
    except Exception as e:
        dbg("AddAutomationEventHandler failed: %r" % e)

    # ---- 从 UIA 元素读取选区文字 ----
    def _read_from_element(el):
        try:
            pat = el.GetCurrentPattern(TextPatternId)
            if not pat:
                return ''
            pat = pat.QueryInterface(UIA.IUIAutomationTextPattern)
            ranges = pat.GetSelection()
            parts = []
            n = ranges.Length
            for i in range(n):
                r = ranges.GetElement(i)
                t = r.GetText(-1) or ''
                if t:
                    parts.append(t)
            return ''.join(parts)
        except Exception:
            return ''

    # ---- 执行一次抓取（防抖定时器触发 / 剪贴板变化触发）----
    def _do_grab(source):
        anchor = _cursor_pos()
        text = ''
        if source == "uia" and pending["element"] is not None:
            el = pending["element"]
            pending["element"] = None
            pending["dirty"] = False
            try:
                text = _read_from_element(el)
            except Exception:
                text = ''
            dbg("uia consumed, read len=%d" % len(text))
            anchor = _element_anchor(el, anchor)
        elif source == "clip":
            text = _read_clipboard_text()
        text = ' '.join(text.split())[:MAX_GRAB_CHARS]
        if len(text) < 2 or text == last_text[0]:
            return
        last_text[0] = text
        dbg("grab hit (%s) len=%d" % (source, len(text)))
        out({"id": 0, "ok": True, "grab": True, "text": text,
             "x": anchor[0], "y": anchor[1]})

    # ---- stdin 监听线程：arm/disarm ----
    def _stdin_reader():
        for line in sys.stdin:
            line = line.strip().lstrip("\ufeff")
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            cmd = msg.get("cmd")
            if cmd == "arm":
                armed["on"] = True
                dbg("received arm, now armed")
                out({"id": 0, "ok": True, "grab": False, "armed": True})
            elif cmd == "disarm":
                armed["on"] = False
                pending["element"] = None
                pending["dirty"] = False
                dbg("received disarm")
                out({"id": 0, "ok": True, "grab": False, "armed": False})

    threading.Thread(target=_stdin_reader, daemon=True).start()

    # ---- 隐藏窗口（用于 WM_CLIPBOARDUPDATE 剪贴板通知）----
    WndProc = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t,           # LRESULT
        ctypes.wintypes.HWND,       # hWnd
        ctypes.c_uint,              # uMsg
        ctypes.wintypes.WPARAM,     # wParam
        ctypes.wintypes.LPARAM,     # lParam
    )

    @WndProc
    def _wndproc(hwnd, uMsg, wParam, lParam):
        if uMsg == WM_CLIPBOARDUPDATE:
            if armed["on"]:
                threading.Thread(target=_do_grab, args=("clip",), daemon=True).start()
            return 0
        if uMsg == WM_TIMER and wParam == DEBOUNCE_ID:
            user32 = ctypes.windll.user32
            user32.KillTimer(hwnd_box["hwnd"], DEBOUNCE_ID)
            if armed["on"] and pending["dirty"]:
                _do_grab("uia")
            return 0
        user32 = ctypes.windll.user32
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.DefWindowProcW.argtypes = [
            ctypes.wintypes.HWND, ctypes.c_uint,
            ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]
        return user32.DefWindowProcW(hwnd, uMsg, wParam, lParam)

    # WNDCLASSW（ctypes.wintypes 不含此结构，按 64 位布局手写）
    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", ctypes.c_uint),
            ("lpfnWndProc", ctypes.c_void_p),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", ctypes.wintypes.HINSTANCE),
            ("hIcon", ctypes.wintypes.HICON),
            ("hCursor", ctypes.wintypes.HANDLE),
            ("hbrBackground", ctypes.wintypes.HANDLE),
            ("lpszMenuName", ctypes.wintypes.LPCWSTR),
            ("lpszClassName", ctypes.wintypes.LPCWSTR),
        ]

    user32 = ctypes.windll.user32
    user32.RegisterClassW.restype = ctypes.c_ushort  # ATOM
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    user32.CreateWindowExW.restype = ctypes.wintypes.HWND
    user32.CreateWindowExW.argtypes = [
        ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.wintypes.HWND, ctypes.wintypes.HMENU, ctypes.wintypes.HINSTANCE,
        ctypes.c_void_p]
    user32.AddClipboardFormatListener.restype = ctypes.c_int
    user32.AddClipboardFormatListener.argtypes = [ctypes.wintypes.HWND]

    wc = WNDCLASSW()
    wc.lpfnWndProc = ctypes.cast(_wndproc, ctypes.c_void_p)
    wc.lpszClassName = "ttsGrabHidden"
    user32.RegisterClassW(ctypes.byref(wc))
    hwnd = user32.CreateWindowExW(
        0, "ttsGrabHidden", "ttsGrabHidden", 0,
        0, 0, 0, 0, None, None, None, None)
    if hwnd:
        hwnd_box["hwnd"] = hwnd
        user32.AddClipboardFormatListener(hwnd)
        dbg("clipboard listener installed (hwnd=%s)" % hwnd)
    else:
        dbg("hidden window create failed; clipboard fallback off")

    out({"id": 0, "ok": True, "grab": False, "text": "", "x": None, "y": None,
         "note": "grabber started (event mode, no mouse hook)"})
    dbg("grabber ready: UIA selection events + clipboard listener")

    # ---- 主消息泵（驱动 UIA 事件与剪贴板通知，无任何鼠标钩子）----
    msg = ctypes.wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


if __name__ == "__main__":
    main()
