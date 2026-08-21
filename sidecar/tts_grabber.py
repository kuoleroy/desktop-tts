# -*- coding: utf-8 -*-
"""全局选区抓取独立进程主入口 —— 纯事件驱动版（无鼠标钩子，零全局监控）。

模块构成：
  - grabber_utils.py  — 工具函数（剪贴板、窗口信息、SendInput、单实例等）
  - grabber_ocr.py    — OCR 识别（RapidOCR 截图文字识别）

不再监控鼠标，采用行业标准的「选区变化事件触发 + 模拟复制」方案。
"""
import ctypes
import ctypes.wintypes
import json
import os
import signal
import sys
import threading
import time

import grabber_utils as gu
import grabber_ocr as ocr

# 后台工作进程：生命周期由 Rust 管理（stdin 指令 + 看门狗），无需响应 Ctrl+C。
signal.signal(signal.SIGINT, signal.SIG_IGN)

# 启动时加载跳过配置
gu._load_skip_config()
gu._load_grab_skip_config()

# 朗读锁定
GRAB_LOCK_HWND = None


def main():
    # ---- 单实例强制 ----
    _mutex_h, _shutdown_h = gu._acquire_single_instance()
    single = {"mutex": _mutex_h, "evt": _shutdown_h}
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="strict")
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    except Exception:
        pass

    armed = {"on": False}
    pending = {"element": None, "dirty": False}
    last_text = [""]
    hwnd_box = {"hwnd": None}
    seen_uia = {"hit": False}
    auto_copy = {"busy": False, "last": 0.0}
    clipwatch = {"until": 0.0, "last_clip": ""}

    # ---- 父进程监控 ----
    _ppid = None
    for i, a in enumerate(sys.argv):
        if a == "--ppid" and i + 1 < len(sys.argv):
            _ppid = int(sys.argv[i + 1])
            break
    if _ppid:
        _ph = ctypes.windll.kernel32.OpenProcess(0x00100000 | 0x100000, False, _ppid)
        if _ph:
            def _parent_watchdog(ph, ppid):
                k32 = ctypes.windll.kernel32
                k32.WaitForSingleObject.restype = ctypes.c_uint
                k32.WaitForSingleObject.argtypes = [ctypes.wintypes.HANDLE, ctypes.c_uint]
                while True:
                    if k32.WaitForSingleObject(ph, 2000) == 0:
                        gu.dbg("parent process (pid=%d) died, exiting grabber" % ppid)
                        k32.CloseHandle(ph)
                        ctypes.windll.user32.PostQuitMessage(0)
                        return
            threading.Thread(target=_parent_watchdog, args=(_ph, _ppid), daemon=True).start()
            gu.dbg("parent watchdog started (ppid=%d)" % _ppid)
        else:
            gu.dbg("parent watchdog: OpenProcess failed for pid=%d" % (_ppid or 0))

    # ---- STA 主线程 + UIA 事件注册 ----
    ctypes.windll.ole32.CoInitializeEx(None, gu.COINIT_APARTMENTTHREADED)

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
            if eventId != gu.UIA_Text_TextSelectionChangedEventId:
                return
            try:
                e0 = sender.QueryInterface(UIA.IUIAutomationElement)
            except Exception:
                return
            pending["element"] = e0
            pending["dirty"] = True
            if hwnd_box["hwnd"]:
                ctypes.windll.user32.SetTimer(hwnd_box["hwnd"], gu.DEBOUNCE_ID, gu.DEBOUNCE_MS, None)
            return

    handler = MyHandler()
    try:
        uia.AddAutomationEventHandler(
            gu.UIA_Text_TextSelectionChangedEventId, root, gu.TreeScope_Subtree, None, handler)
        gu.dbg("UIA selection event registered")
    except Exception as e:
        gu.dbg("AddAutomationEventHandler failed: %r" % e)

    # ---- UIA 选区读取 ----
    def _read_from_element(el, tag):
        try:
            pat = el.GetCurrentPattern(gu.TextPatternId)
        except Exception as e:
            gu.dbg("  [%s] GetCurrentPattern failed: %r" % (tag, e))
            return ''
        if not pat:
            gu.dbg("  [%s] GetCurrentPattern -> None" % tag)
            return ''
        try:
            tp = pat.QueryInterface(UIA.IUIAutomationTextPattern)
        except Exception as e:
            gu.dbg("  [%s] QueryInterface TextPattern failed: %r" % (tag, e))
            return ''
        try:
            ranges = tp.GetSelection()
        except Exception as e:
            gu.dbg("  [%s] GetSelection failed: %r" % (tag, e))
            return ''
        if ranges is None:
            gu.dbg("  [%s] GetSelection -> None" % tag)
            return ''
        try:
            n = ranges.Length
        except Exception:
            try:
                n = len(ranges)
            except Exception as e:
                gu.dbg("  [%s] ranges.Length/len failed: %r" % (tag, e))
                return ''
        if n <= 0:
            gu.dbg("  [%s] GetSelection -> %d ranges (empty)" % (tag, n))
            return ''
        parts = []
        for i in range(n):
            try:
                r = ranges.GetElement(i)
            except Exception:
                try:
                    r = ranges[i]
                except Exception as e:
                    gu.dbg("  [%s] ranges[%d] failed: %r" % (tag, i, e))
                    continue
            try:
                t = r.GetText(-1) or r.GetText(1000000) or ''
            except Exception as e:
                gu.dbg("  [%s] range[%d] GetText failed: %r" % (tag, i, e))
                continue
            if t:
                parts.append(t)
        gu.dbg("  [%s] ranges=%d text_len=%d" % (tag, n, sum(len(p) for p in parts)))
        return ''.join(parts)

    # ---- UIA 主动扫描 ----
    def _uia_scan_selection():
        try:
            user32 = ctypes.windll.user32
            user32.GetForegroundWindow.restype = ctypes.wintypes.HWND
            user32.GetForegroundWindow.argtypes = []
            hwnd = user32.GetForegroundWindow()
            cx, cy = gu._cursor_pos()
            mwin = gu._root_window_at(cx, cy)
            try:
                user32.GetClassNameW.restype = ctypes.c_int
                user32.GetClassNameW.argtypes = [ctypes.wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
                cb = ctypes.create_unicode_buffer(128)
                if mwin:
                    user32.GetClassNameW(mwin, cb, 128)
                    cls = cb.value
                    if mwin and cls not in ("Tauri Window", "ttsGrabHidden", "Floater", "Crop"):
                        hwnd = mwin
            except Exception:
                pass
            if not hwnd:
                return '', None
            win = uia.ElementFromHandle(hwnd)
        except Exception as e:
            gu.dbg("  [scan] ElementFromHandle failed: %r" % e)
            return '', None
        best = ''
        rect = None
        try:
            cond = uia.CreateTrueCondition()
            nodes = win.FindAll(0x4, cond)
            n = getattr(nodes, "Length", 0)
            if not n:
                try:
                    n = len(nodes)
                except Exception:
                    n = 0
            for i in range(n):
                try:
                    el = nodes.GetElement(i)
                except Exception:
                    continue
                try:
                    sp = el.GetCurrentPattern(gu.SelectionPatternId)
                    if sp:
                        sel = sp.GetCurrentSelection()
                        cnt = getattr(sel, "Length", 0) or len(sel)
                        for k in range(cnt):
                            try:
                                it = sel.GetElement(k)
                                t = getattr(it, "CurrentName", "") or ""
                                if len(t) > len(best):
                                    best = t
                                    try:
                                        r = it.CurrentBoundingRectangle
                                        rect = (int(r.left), int(r.top), int(r.right), int(r.bottom))
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                except Exception:
                    pass
                try:
                    tp = el.GetCurrentPattern(gu.TextPatternId)
                    if tp:
                        ranges = tp.GetSelection()
                        cnt = getattr(ranges, "Length", 0) or len(ranges)
                        for k in range(cnt):
                            try:
                                r = ranges.GetElement(k)
                                t = r.GetText(-1) or r.GetText(1000000) or ''
                                if len(t) > len(best):
                                    best = t
                                    try:
                                        rr = r.GetBoundingRectangles()
                                        if rr and getattr(rr, "Length", 0) or (rr and len(rr)):
                                            b0 = rr.GetElement(0) if hasattr(rr, "GetElement") else rr[0]
                                            rect = (int(b0[0]), int(b0[1]), int(b0[0] + b0[2]), int(b0[1] + b0[3]))
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                except Exception:
                    pass
        except Exception as e:
            gu.dbg("  [scan] FindAll failed: %r" % e)
        return best, rect

    # ---- 自动 Ctrl+C 兜底 ----
    def _auto_copy_fallback(el=None):
        if auto_copy["busy"]:
            return ''
        now = time.time()
        if now - auto_copy["last"] < gu.AUTOCOPY_COOLDOWN:
            return ''
        if gu._is_console_foreground():
            gu.dbg("  [autocopy] skipped (console foreground, avoid Ctrl+C/SIGINT)")
            return ''
        tx, ty = gu._cursor_pos()
        if el is not None:
            r = ocr._element_rect(el)
            if r:
                tx, ty = (r[0] + r[2]) // 2, (r[1] + r[3]) // 2
        target = gu._root_window_at(tx, ty) if (tx or ty) else 0
        if target:
            gu.dbg("  [autocopy] target hwnd=0x%X (fg=%s)" % (target, gu._fg_window_info()))
            if gu._is_skip_hwnd(target):
                gu.dbg("  [autocopy] skipped (target is skip window)")
                return ''
        auto_copy["busy"] = True
        auto_copy["last"] = now
        try:
            before = gu._read_clipboard_text()
            backup = gu._backup_clipboard()
            text = ''
            for mode in ("vk", "scan", "keybd"):
                if text:
                    break
                if not gu._send_ctrl_c(mode, target_hwnd=target):
                    break
                deadline = time.time() + 0.5
                while time.time() < deadline:
                    t = gu._read_clipboard_text()
                    if t and t != before:
                        text = t
                        break
                    time.sleep(0.03)
            if not text and gu._clipboard_format_count() not in (-1, len(backup)):
                gu.dbg("  [autocopy] clipboard mutated but no text")
            if backup:
                gu._restore_clipboard(backup)
            gu.dbg("  [autocopy] before_len=%d got_len=%d formats=%d" % (len(before), len(text), len(backup)))
            return text
        except BaseException as e:
            gu.dbg("  [autocopy] failed: %r" % e)
            return ''
        finally:
            auto_copy["busy"] = False

    # ---- 抓取逻辑 ----
    def _is_own_hwnd(hwnd):
        if not hwnd or not _ppid:
            return False
        pid = ctypes.wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value == _ppid

    def _report(source, text, anchor):
        text = ' '.join(text.split())[:gu.MAX_GRAB_CHARS]
        try:
            if _is_own_hwnd(gu._fg_hwnd()):
                gu.dbg("  [grab] skipped (own window)")
                return
        except Exception:
            pass
        if len(text) < 2 or text == last_text[0]:
            return
        fgcls = gu._fg_class()
        if fgcls.startswith("Chrome_WidgetWin"):
            if len(text) < 30:
                gu.dbg("noise filtered (chromium len=%d < 30)" % len(text))
                return
        last_text[0] = text
        gu.dbg("grab hit (%s) len=%d" % (source, len(text)))
        gu.out({"id": 0, "ok": True, "grab": True, "text": text,
                "x": anchor[0], "y": anchor[1], "hwnd": gu._fg_hwnd()})

    def _do_grab(source):
        global GRAB_LOCK_HWND
        try:
            fg = ctypes.windll.user32.GetForegroundWindow()
            if gu._is_grab_skip_hwnd(fg):
                gu.dbg("  [grab] skipped (fg is grab-skip window)")
                pending["element"] = None
                pending["dirty"] = False
                return
            if GRAB_LOCK_HWND is not None and fg != GRAB_LOCK_HWND:
                gu.dbg("  [grab] skipped (locked to other window)")
                pending["element"] = None
                pending["dirty"] = False
                return
        except Exception:
            pass
        anchor = gu._cursor_pos()
        text = ''
        if pending["element"] is not None:
            el = pending["element"]
            pending["element"] = None
            pending["dirty"] = False
            try:
                text = _read_from_element(el, "uia")
            except Exception:
                text = ''
            if not text:
                try:
                    foc = uia.GetFocusedElement()
                    text = _read_from_element(foc, "focus")
                except Exception as e:
                    gu.dbg("  [focus] GetFocusedElement failed: %r" % e)
            if not text and source != "uia":
                text = _auto_copy_fallback(el)
            if not text and source != "uia":
                rect = ocr._element_rect(el)
                rect = ocr._focus_ocr_rect(rect)
                if rect:
                    gu.dbg("  [ocr] spawn background OCR rect=%s" % (rect,))
                    threading.Thread(target=_ocr_job, args=(rect, anchor, source), daemon=True).start()
                    return
                gu.dbg("  [ocr] no element rect, skip OCR")
            gu.dbg("uia consumed, read len=%d" % len(text))
        _report(source, text, anchor)

    def _ocr_job(rect, anchor, source):
        try:
            text = ocr._ocr_capture(rect)
        except BaseException as e:
            gu.dbg("  [ocr] job failed: %r" % e)
            text = ''
        gu.dbg("uia consumed (ocr), read len=%d" % len(text))
        _report(source, text, anchor)

    def _ocr_cmd_job(rect):
        try:
            text = ocr._ocr_capture(rect)
        except BaseException as e:
            gu.dbg("  [ocr-cmd] job failed: %r" % e)
            gu.out({"id": 0, "ok": False, "grab": False, "error": repr(e)})
            return
        anchor = ((rect[0] + rect[2]) // 2, rect[3] + 12)
        gu.dbg("ocr-cmd consumed, read len=%d" % len(text))
        _report("ocr", text, anchor)

    def _selread_job():
        global GRAB_LOCK_HWND
        try:
            fg = ctypes.windll.user32.GetForegroundWindow()
            if gu._is_grab_skip_hwnd(fg):
                gu.dbg("  [selread] skipped (fg is grab-skip window)")
                gu.out({"id": 0, "ok": False, "grab": False, "error": "grab skip window"})
                return
            if GRAB_LOCK_HWND is not None and fg != GRAB_LOCK_HWND:
                gu.dbg("  [selread] skipped (locked to other window)")
                gu.out({"id": 0, "ok": False, "grab": False, "error": "grab locked"})
                return
        except Exception:
            pass
        text, rect = _uia_scan_selection()
        source = "selread"
        anchor = None
        if rect:
            anchor = ((rect[0] + rect[2]) // 2, rect[3] + 12)
        if not text:
            gu.dbg("  [selread] UIA empty, trying auto-copy")
            text = _auto_copy_fallback(None)
            if text:
                source = "selread-cc"
        if not text:
            gu.dbg("  [selread] no selection found")
            gu.out({"id": 0, "ok": False, "grab": False, "error": "no selection"})
            return
        gu.dbg("selread consumed, read len=%d" % len(text))
        _report(source, text, anchor or gu._cursor_pos())

    def _on_clipboard_changed():
        try:
            if time.time() > clipwatch["until"]:
                return
            if auto_copy["busy"]:
                return
            text = gu._read_clipboard_text()
            if not text or len(text) < 2:
                return
            if text == clipwatch["last_clip"] or text == last_text[0]:
                return
            clipwatch["last_clip"] = text
            _report("clipwatch", text, gu._cursor_pos())
        except BaseException as e:
            gu.dbg("  [clipwatch] failed: %r" % e)

    def _clipwatch_cmd_job():
        clipwatch["until"] = time.time() + 6.0
        clipwatch["last_clip"] = gu._read_clipboard_text()
        gu.dbg("  [clipwatch] watching clipboard for 6s (copy text to read)")
        gu.out({"id": 0, "ok": True, "grab": False, "clipwatch": True})

    # ---- stdin 监听线程 ----
    def _stdin_reader():
        global GRAB_LOCK_HWND
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
                gu.dbg("received arm, now armed")
                gu.out({"id": 0, "ok": True, "grab": False, "armed": True})
            elif cmd == "disarm":
                armed["on"] = False
                pending["element"] = None
                pending["dirty"] = False
                gu.dbg("received disarm")
                gu.out({"id": 0, "ok": True, "grab": False, "armed": False})
            elif cmd == "ocr":
                gu.dbg("received ocr cmd")
                raw = msg.get("text") or ""
                try:
                    rect = json.loads(raw)
                    if len(rect) != 4:
                        raise ValueError("rect must have 4 numbers")
                    rect = [int(v) for v in rect]
                except Exception as e:
                    gu.dbg("  [ocr-cmd] bad rect: %r" % e)
                    gu.out({"id": 0, "ok": False, "grab": False, "error": "bad rect"})
                    continue
                threading.Thread(target=_ocr_cmd_job, args=(rect,), daemon=True).start()
            elif cmd == "selread":
                gu.dbg("received selread cmd")
                threading.Thread(target=_selread_job, daemon=True).start()
            elif cmd == "clipwatch":
                gu.dbg("received clipwatch cmd")
                _clipwatch_cmd_job()
            elif cmd == "reload_skip":
                gu.dbg("received reload_skip cmd")
                gu._load_skip_config()
                gu._load_grab_skip_config()
            elif cmd == "grab_lock":
                try:
                    h = (msg.get("text") or "").strip()
                    GRAB_LOCK_HWND = int(h) if h and h.lower() != "none" else None
                except Exception:
                    GRAB_LOCK_HWND = None
                gu.dbg("grab lock set to %r" % GRAB_LOCK_HWND)

    threading.Thread(target=_stdin_reader, daemon=True).start()

    # ---- pynput 钩子 ----
    def _start_hooks():
        try:
            from pynput import mouse, keyboard
        except Exception as e:
            gu.dbg("  [hooks] pynput import failed: %r" % e)
            return
        _last_click = [0.0, 0, 0]
        _drag_start = [None]
        _ctrl_down = [False]
        _dbl_pending = [False]

        def _do_trigger():
            threading.Thread(target=_selread_job, daemon=True).start()

        def _on_click(x, y, button, pressed):
            try:
                if getattr(button, "name", "") != "left":
                    return
                now = time.time()
                if pressed:
                    lx, ly, lt = _last_click
                    if now - lt < 0.3 and abs(x - lx) < 6 and abs(y - ly) < 6:
                        _last_click[0] = 0.0
                        _dbl_pending[0] = True
                    else:
                        _last_click[0], _last_click[1], _last_click[2] = now, x, y
                    _drag_start[0] = (x, y)
                else:
                    if _dbl_pending[0]:
                        _dbl_pending[0] = False
                        _do_trigger()
                        return
                    if _drag_start[0]:
                        sx, sy = _drag_start[0]
                        _drag_start[0] = None
                        if max(abs(x - sx), abs(y - sy)) >= 8:
                            _do_trigger()
            except Exception as e:
                gu.dbg("  [hooks] on_click err: %r" % e)

        def _on_key(key):
            try:
                from pynput import keyboard as kb
                if key in (kb.Key.ctrl_l, kb.Key.ctrl_r):
                    _ctrl_down[0] = True
                elif getattr(key, "vk", None) == 65 and _ctrl_down[0]:
                    _do_trigger()
            except Exception:
                pass

        def _on_key_up(key):
            try:
                from pynput import keyboard as kb
                if key in (kb.Key.ctrl_l, kb.Key.ctrl_r):
                    _ctrl_down[0] = False
            except Exception:
                pass

        try:
            m = mouse.Listener(on_click=_on_click)
            m.daemon = True
            m.start()
            k = keyboard.Listener(on_press=_on_key, on_release=_on_key_up)
            k.daemon = True
            k.start()
            gu.dbg("  [hooks] pynput mouse/keyboard listener started")
        except Exception as e:
            gu.dbg("  [hooks] listener start failed: %r" % e)

    _start_hooks()

    # ---- 隐藏窗口 ----
    WndProc = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t, ctypes.wintypes.HWND, ctypes.c_uint,
        ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM,
    )

    @WndProc
    def _wndproc(hwnd, uMsg, wParam, lParam):
        if uMsg == gu.WM_TIMER and wParam == gu.SHUTDOWN_ID:
            k32 = ctypes.windll.kernel32
            k32.WaitForSingleObject.restype = ctypes.c_uint
            k32.WaitForSingleObject.argtypes = [ctypes.wintypes.HANDLE, ctypes.c_uint]
            if k32.WaitForSingleObject(single["evt"], 0) == gu.WAIT_OBJECT_0:
                gu.dbg("single-instance: takeover requested, exiting")
                ctypes.windll.user32.PostQuitMessage(0)
            return 0
        if uMsg == gu.WM_TIMER and wParam == gu.DEBOUNCE_ID:
            user32 = ctypes.windll.user32
            user32.KillTimer(hwnd_box["hwnd"], gu.DEBOUNCE_ID)
            if armed["on"] and pending["dirty"]:
                _do_grab("uia")
            return 0
        if uMsg == gu.WM_CLIPBOARDUPDATE:
            _on_clipboard_changed()
            return 0
        user32 = ctypes.windll.user32
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.DefWindowProcW.argtypes = [ctypes.wintypes.HWND, ctypes.c_uint, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]
        return user32.DefWindowProcW(hwnd, uMsg, wParam, lParam)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", ctypes.c_uint), ("lpfnWndProc", ctypes.c_void_p),
            ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
            ("hInstance", ctypes.wintypes.HINSTANCE), ("hIcon", ctypes.wintypes.HICON),
            ("hCursor", ctypes.wintypes.HANDLE), ("hbrBackground", ctypes.wintypes.HANDLE),
            ("lpszMenuName", ctypes.wintypes.LPCWSTR), ("lpszClassName", ctypes.wintypes.LPCWSTR),
        ]

    user32 = ctypes.windll.user32
    user32.RegisterClassW.restype = ctypes.c_ushort
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    user32.CreateWindowExW.restype = ctypes.wintypes.HWND
    user32.CreateWindowExW.argtypes = [
        ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.wintypes.HWND, ctypes.wintypes.HMENU, ctypes.wintypes.HINSTANCE, ctypes.c_void_p]

    wc = WNDCLASSW()
    wc.lpfnWndProc = ctypes.cast(_wndproc, ctypes.c_void_p)
    wc.lpszClassName = "ttsGrabHidden"
    user32.RegisterClassW(ctypes.byref(wc))
    hwnd = user32.CreateWindowExW(0, "ttsGrabHidden", "ttsGrabHidden", 0, 0, 0, 0, 0, None, None, None, None)
    if hwnd:
        hwnd_box["hwnd"] = hwnd
        user32.SetTimer(hwnd, gu.SHUTDOWN_ID, 500, None)
        user32.AddClipboardFormatListener.restype = ctypes.c_int
        user32.AddClipboardFormatListener.argtypes = [ctypes.wintypes.HWND]
        user32.AddClipboardFormatListener(hwnd)
    else:
        gu.dbg("hidden window create failed; UIA debounce timer off")

    gu.out({"id": 0, "ok": True, "grab": False, "text": "", "x": None, "y": None,
            "note": "grabber started (event mode, no mouse hook)"})
    gu.dbg("grabber ready: UIA selection events + auto-copy fallback + OCR")

    # ---- 主消息泵 ----
    msg = ctypes.wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


if __name__ == "__main__":
    main()