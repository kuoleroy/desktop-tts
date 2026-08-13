# -*- coding: utf-8 -*-
"""全局选区抓取独立进程 —— 纯事件驱动版（无鼠标钩子，零全局监控）。

不再监控鼠标，采用行业标准的「选区变化事件触发 + 模拟复制」方案：
1. UIA 文本选择变化事件（UIA_Text_TextSelectionChangedEventId=20014）：
   支持 UIA 的应用（记事本/浏览器/Word/VS Code 等）里拖选文字时，系统主动通知。
   UIA 事件仅作「选区变化」触发源；能直接读到 TextPattern 选区就直读（不碰剪贴板），
   读不到则自动模拟一次 Ctrl+C（SendInput 完整按下/释放，finally 强制释放防锁键），
   读取剪贴板文本后完整还原剪贴板所有格式（文本/图片/文件等，接近无损）。
2. 不再监听剪贴板变化：用户手动 Ctrl+C 是其主动复制行为，不触发悬浮框。

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
WM_TIMER = 0x0113
DEBOUNCE_MS = 350  # 选择事件高频触发，防抖后只读一次
DEBOUNCE_ID = 1
# 自动复制最小间隔：终端/后台输出刷新会持续触发 20014，冷却打断「Ctrl+C 风暴」
AUTOCOPY_COOLDOWN = 1.0

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


def _clipboard_format_count():
    """当前剪贴板格式数；-1 表示读取失败。用于判断注入 Ctrl+C 后目标是否真的动了剪贴板。"""
    user32 = ctypes.windll.user32
    user32.OpenClipboard.restype = ctypes.c_int
    user32.OpenClipboard.argtypes = [ctypes.wintypes.HWND]
    user32.EnumClipboardFormats.restype = ctypes.c_uint
    user32.EnumClipboardFormats.argtypes = [ctypes.c_uint]
    user32.CloseClipboard.restype = ctypes.c_int
    user32.CloseClipboard.argtypes = []
    try:
        if not user32.OpenClipboard(None):
            return -1
        try:
            n = 0
            fmt = 0
            while True:
                fmt = user32.EnumClipboardFormats(fmt)
                if fmt == 0:
                    break
                n += 1
            return n
        finally:
            user32.CloseClipboard()
    except Exception:
        return -1


def _cursor_pos():
    try:
        p = ctypes.wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(p))
        return (p.x, p.y)
    except Exception:
        return (0, 0)


def _fg_window_info():
    """当前前台窗口信息（hwnd/类名/标题），诊断模拟复制是否发到了目标应用。"""
    try:
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = ctypes.wintypes.HWND
        user32.GetForegroundWindow.argtypes = []
        h = user32.GetForegroundWindow()
        if not h:
            return "fg=0"
        user32.GetClassNameW.restype = ctypes.c_int
        user32.GetClassNameW.argtypes = [ctypes.wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [ctypes.wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(h, cls, 256)
        title = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(h, title, 256)
        return "fg=0x%X cls=%s title=%s" % (h, cls.value, title.value)
    except Exception:
        return "fg=?"


def _is_console_foreground():
    """前台是否为终端/控制台窗口。终端里 Ctrl+C 是 SIGINT（中断运行中的程序），
    且终端输出刷新会持续触发 20014 → 必须跳过注入，避免「键盘一直 Ctrl+C」。"""
    try:
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = ctypes.wintypes.HWND
        user32.GetForegroundWindow.argtypes = []
        user32.GetClassNameW.restype = ctypes.c_int
        user32.GetClassNameW.argtypes = [ctypes.wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
        h = user32.GetForegroundWindow()
        if not h:
            return True
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(h, cls, 256)
        c = cls.value
        return c in ("CASCADIA_HOSTING_WINDOW_CLASS", "ConsoleWindowClass") \
            or "Console" in c
    except Exception:
        return True  # 取不到前台信息时保守跳过注入


def _elevation_info():
    """当前进程与前台窗口进程的完整性 SID（S-1-16-xxxx：4096=Low/8192=Medium/
    12288=High/16384=System）。SendInput 无法向更高级别窗口注入（UIPI 静默丢弃
    但 SendInput 仍返回成功），用此判断「浏览器抓不到」是不是权限隔离导致。"""
    def pid_sid(pid):
        try:
            kernel32 = ctypes.windll.kernel32
            advapi32 = ctypes.windll.advapi32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            TOKEN_QUERY = 0x0008
            TokenIntegrityLevel = 25
            h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return "?"
            try:
                th = ctypes.wintypes.HANDLE()
                if not advapi32.OpenProcessToken(h, TOKEN_QUERY, ctypes.byref(th)):
                    return "?"
                try:
                    sz = ctypes.c_uint(0)
                    advapi32.GetTokenInformation(th, TokenIntegrityLevel, None, 0, ctypes.byref(sz))
                    if sz.value <= 0:
                        return "?"
                    buf = ctypes.create_string_buffer(sz.value)
                    if not advapi32.GetTokenInformation(
                            th, TokenIntegrityLevel, buf, sz.value, ctypes.byref(sz)):
                        return "?"
                    # TOKEN_MANDATORY_LABEL.Label 是 SID_AND_ATTRIBUTES，
                    # 其 Sid 为指针（x64 缓冲区前 8 字节），SID 实体在缓冲区末尾
                    sid_ptr = int.from_bytes(bytes(buf.raw[:8]), "little")
                    if not sid_ptr:
                        return "?"
                    sid = ctypes.cast(sid_ptr, ctypes.POINTER(ctypes.c_ubyte))
                    sub_count = sid[1] & 0xFF
                    # SID 布局：[0]=Revision [1]=SubAuthorityCount [2..7]=Authority(6B)
                    #          之后每 4B 一个 SubAuthority；最后一个即完整性级别
                    off = 8 + 6
                    last = int.from_bytes(bytes(sid[off:off + 4 * sub_count]), "little")
                    if last >= 16384:
                        return "S-1-16-%d(System)" % last
                    if last >= 12288:
                        return "S-1-16-%d(High)" % last
                    if last >= 8192:
                        return "S-1-16-%d(Medium)" % last
                    if last >= 4096:
                        return "S-1-16-%d(Low)" % last
                    return "S-1-16-%d" % last
                finally:
                    kernel32.CloseHandle(th)
            finally:
                kernel32.CloseHandle(h)
        except Exception:
            return "?"
    try:
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = ctypes.wintypes.HWND
        user32.GetForegroundWindow.argtypes = []
        user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
        user32.GetWindowThreadProcessId.argtypes = [
            ctypes.wintypes.HWND, ctypes.POINTER(ctypes.c_ulong)]
        me = int(ctypes.windll.kernel32.GetCurrentProcessId())
        h = user32.GetForegroundWindow()
        fgp = ctypes.c_ulong(0)
        if h:
            user32.GetWindowThreadProcessId(h, ctypes.byref(fgp))
        return "elev me=%s fg=%s" % (pid_sid(me), pid_sid(int(fgp.value)))
    except Exception:
        return "elev=?"


# ---- 模拟 Ctrl+C（SendInput 完整按键序列 + keybd_event 兜底，finally 强制释放，绝不锁 Ctrl）----
def _send_ctrl_c(mode="vk"):
    """向前台窗口注入一次 Ctrl+C。

    mode="vk"  ：批处理 VK 虚拟键（记事本已验证有效）。
    mode="scan"：逐个发送 + 扫描码（KEYEVENTF_SCANCODE）＋20ms 间隔，
                 更接近真实硬件键入，部分 Chromium 类应用只认扫描码事件。
    """
    VK_CONTROL = 0x11
    VK_C = 0x43
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_SCANCODE = 0x0008
    INPUT_KEYBOARD = 1

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", ctypes.c_ushort),
            ("wScan", ctypes.c_ushort),
            ("dwFlags", ctypes.c_uint),
            ("time", ctypes.c_uint),
            ("dwExtraInfo", ctypes.c_void_p),
        ]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", ctypes.c_long),
            ("dy", ctypes.c_long),
            ("mouseData", ctypes.c_uint),
            ("dwFlags", ctypes.c_uint),
            ("time", ctypes.c_uint),
            ("dwExtraInfo", ctypes.c_void_p),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", ctypes.c_uint),
            ("wParamL", ctypes.c_ushort),
            ("wParamH", ctypes.c_ushort),
        ]

    # 必须与原生 x64 INPUT 完全一致（union 取最大 MOUSEINPUT 32B，
    # 总 sizeof=40B）；此前 union 只有 KEYBDINPUT(24B) 导致 sizeof=32，
    # cbSize 传错使 SendInput 恒返回 0（事件全部被拒收）。
    class _INPUTUNION(ctypes.Union):
        _fields_ = [
            ("mi", MOUSEINPUT),
            ("ki", KEYBDINPUT),
            ("hi", HARDWAREINPUT),
        ]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", ctypes.c_uint), ("u", _INPUTUNION)]

    user32 = ctypes.windll.user32
    # 注入前再查一次前台：焦点可能在 _auto_copy_fallback 的检查与这里之间跳动，
    # 若此刻前台是终端，Ctrl+C=SIGINT 会杀掉跑 cargo tauri dev 的进程 →「模型不见了」。
    if _is_console_foreground():
        dbg("  [send] aborted: console foreground (avoid Ctrl+C/SIGINT)")
        return False
    # 诊断：注入前记录前台窗口 + 双方完整性级别（UIPI 会静默拦截向更高级别窗口的注入）
    dbg("  [send] %s | %s" % (_fg_window_info(), _elevation_info()))

    # 扫描码（模拟真实硬件按键；部分 Chromium 应用对 VK 注入不敏感）
    try:
        user32.MapVirtualKeyW.restype = ctypes.c_uint
        user32.MapVirtualKeyW.argtypes = [ctypes.c_uint, ctypes.c_uint]
        ctrl_scan = user32.MapVirtualKeyW(VK_CONTROL, 0)
        c_scan = user32.MapVirtualKeyW(VK_C, 0)
    except Exception:
        ctrl_scan, c_scan = 0x1D, 0x2E  # 兜底：左 Ctrl / C 标准扫描码

    def _in(vk, scan, flags):
        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        if mode == "scan":
            inp.u.ki.wVk = 0
            inp.u.ki.wScan = scan
            inp.u.ki.dwFlags = flags | KEYEVENTF_SCANCODE
        else:
            inp.u.ki.wVk = vk
            inp.u.ki.wScan = 0
            inp.u.ki.dwFlags = flags
        inp.u.ki.time = 0
        inp.u.ki.dwExtraInfo = None
        return inp

    sent = 0
    try:
        user32.SendInput.restype = ctypes.c_uint
        user32.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int]
        seq = [
            _in(VK_CONTROL, ctrl_scan, 0),
            _in(VK_C, c_scan, 0),
            _in(VK_C, c_scan, KEYEVENTF_KEYUP),
            _in(VK_CONTROL, ctrl_scan, KEYEVENTF_KEYUP),
        ]
        if mode == "scan":
            # 逐个发送 + 间隔：更接近真实键入，Chromium 类应用处理更可靠
            for ev in seq:
                arr = (INPUT * 1)(ev)
                sent += user32.SendInput(1, arr, ctypes.sizeof(INPUT))
                time.sleep(0.02)
        else:
            arr = (INPUT * 4)(seq[0], seq[1], seq[2], seq[3])
            sent = user32.SendInput(4, arr, ctypes.sizeof(INPUT))
        dbg("  [send] SendInput(%s) returned %d/4" % (mode, sent))
    except Exception as e:
        dbg("  [send] SendInput raised: %r" % e)
        sent = 0
    finally:
        # 异常/被拦截也强制释放 Ctrl 与 C，避免系统级按键卡死
        try:
            user32.SendInput.restype = ctypes.c_uint
            user32.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int]
            force = [
                _in(VK_C, c_scan, KEYEVENTF_KEYUP),
                _in(VK_CONTROL, ctrl_scan, KEYEVENTF_KEYUP),
            ]
            arr = (INPUT * 2)(force[0], force[1])
            user32.SendInput(2, arr, ctypes.sizeof(INPUT))
        except Exception:
            pass

    if sent != 4:
        # 回退：keybd_event（部分环境对 SendInput 有权限/驱动拦截，keybd_event 更宽松）
        dbg("  [send] SendInput(%s) failed (%d/4), fallback to keybd_event" % (mode, sent))
        try:
            user32.keybd_event.restype = ctypes.c_void_p
            user32.keybd_event.argtypes = [
                ctypes.c_ubyte, ctypes.c_ubyte, ctypes.c_uint, ctypes.c_void_p]
            user32.keybd_event(VK_CONTROL, 0, 0, 0)
            time.sleep(0.01)
            user32.keybd_event(VK_C, 0, 0, 0)
            time.sleep(0.01)
            user32.keybd_event(VK_C, 0, KEYEVENTF_KEYUP, 0)
            user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
        except Exception as e:
            dbg("  [send] keybd_event fallback raised: %r" % e)
    return True


# ---- 剪贴板完整备份/还原（自动 Ctrl+C 后按行业标准还原所有格式，接近无损）----
def _backup_clipboard():
    """枚举剪贴板全部格式并拷贝原始字节，返回 [(format, bytes), ...]。

    对 GlobalLock 失败的 GDI 句柄格式（如旧式 CF_BITMAP）自动跳过；
    现代应用复制图片多用 CF_DIB/CF_HTML/私有格式，均为全局内存，可完整备份。
    """
    GMEM_MOVEABLE = 0x0002
    GMEM_ZEROINIT = 0x0040
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.OpenClipboard.restype = ctypes.c_int
    user32.OpenClipboard.argtypes = [ctypes.wintypes.HWND]
    user32.EnumClipboardFormats.restype = ctypes.c_uint
    user32.EnumClipboardFormats.argtypes = [ctypes.c_uint]
    user32.GetClipboardData.restype = ctypes.wintypes.HANDLE
    user32.GetClipboardData.argtypes = [ctypes.c_uint]
    user32.CloseClipboard.restype = ctypes.c_int
    user32.CloseClipboard.argtypes = []
    kernel32.GlobalSize.restype = ctypes.c_size_t
    kernel32.GlobalSize.argtypes = [ctypes.wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = ctypes.c_int
    kernel32.GlobalUnlock.argtypes = [ctypes.wintypes.HGLOBAL]
    items = []
    try:
        if not user32.OpenClipboard(None):
            return items
        try:
            fmt = 0
            while True:
                fmt = user32.EnumClipboardFormats(fmt)
                if fmt == 0:
                    break
                h = user32.GetClipboardData(fmt)
                if not h:
                    continue
                size = kernel32.GlobalSize(h)
                if not size:
                    continue
                p = kernel32.GlobalLock(h)
                if not p:
                    continue
                try:
                    data = ctypes.string_at(p, size)
                finally:
                    kernel32.GlobalUnlock(h)
                items.append((fmt, data))
        finally:
            user32.CloseClipboard()
    except Exception:
        pass
    return items


def _restore_clipboard(items):
    """将 _backup_clipboard 的结果重放回剪贴板（重建所有格式）。"""
    if not items:
        return
    GMEM_MOVEABLE = 0x0002
    GMEM_ZEROINIT = 0x0040
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.OpenClipboard.restype = ctypes.c_int
    user32.OpenClipboard.argtypes = [ctypes.wintypes.HWND]
    user32.EmptyClipboard.restype = ctypes.c_int
    user32.EmptyClipboard.argtypes = []
    user32.SetClipboardData.restype = ctypes.wintypes.HANDLE
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.wintypes.HANDLE]
    user32.CloseClipboard.restype = ctypes.c_int
    user32.CloseClipboard.argtypes = []
    kernel32.GlobalAlloc.restype = ctypes.wintypes.HGLOBAL
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = ctypes.c_int
    kernel32.GlobalUnlock.argtypes = [ctypes.wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = ctypes.wintypes.HGLOBAL
    kernel32.GlobalFree.argtypes = [ctypes.wintypes.HGLOBAL]
    try:
        if not user32.OpenClipboard(None):
            return
        try:
            user32.EmptyClipboard()
            for fmt, data in items:
                h = kernel32.GlobalAlloc(GMEM_MOVEABLE | GMEM_ZEROINIT, len(data))
                if not h:
                    continue
                p = kernel32.GlobalLock(h)
                if p:
                    try:
                        ctypes.memmove(p, data, len(data))
                    finally:
                        kernel32.GlobalUnlock(h)
                # SetClipboardData 成功后句柄归剪贴板所有；失败需释放防泄漏
                if not user32.SetClipboardData(fmt, h):
                    kernel32.GlobalFree(h)
        finally:
            user32.CloseClipboard()
    except Exception:
        pass


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
    # 上次上报文本（去重，避免连续选区事件对同一文本重复上报）
    last_text = [""]
    # 隐藏窗口句柄：UIA 回调线程经它投递防抖定时器到主线程消息泵
    hwnd_box = {"hwnd": None}
    # 首次收到 UIA 20014 事件的诊断标记（避免刷屏）
    seen_uia = {"hit": False}
    # 自动 Ctrl+C 兜底：busy 防并发/递归（用户手动 Ctrl+C 不触发抓取，无剪贴板监听）
    auto_copy = {"busy": False, "last": 0.0}

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
                # 诊断：区分「事件没触发」与「事件触发但抓取被关闭」
                dbg("UIA 20014 received but disarmed, ignored")
                return
            if not seen_uia["hit"]:
                seen_uia["hit"] = True
                try:
                    el0 = sender.QueryInterface(UIA.IUIAutomationElement)
                    scls = getattr(el0, "CurrentClassName", None) or "?"
                except Exception:
                    scls = "?"
                dbg("UIA 20014 selection event received (sender cls=%s)" % scls)
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

    # ---- 从 UIA 元素读取选区文字（分步诊断，避免静默吞异常）----
    def _read_from_element(el, tag):
        # 1) 取 TextPattern
        try:
            pat = el.GetCurrentPattern(TextPatternId)
        except Exception as e:
            dbg("  [%s] GetCurrentPattern failed: %r" % (tag, e))
            return ''
        if not pat:
            dbg("  [%s] GetCurrentPattern -> None (element has no TextPattern)" % tag)
            return ''
        # 2) QueryInterface
        try:
            tp = pat.QueryInterface(UIA.IUIAutomationTextPattern)
        except Exception as e:
            dbg("  [%s] QueryInterface TextPattern failed: %r" % (tag, e))
            return ''
        # 3) GetSelection（comtypes 可能返回 COMArray 或普通 list，两者都兼容）
        try:
            ranges = tp.GetSelection()
        except Exception as e:
            dbg("  [%s] GetSelection failed: %r" % (tag, e))
            return ''
        if ranges is None:
            dbg("  [%s] GetSelection -> None" % tag)
            return ''
        try:
            n = ranges.Length
        except Exception:
            try:
                n = len(ranges)
            except Exception as e:
                dbg("  [%s] ranges.Length/len failed: %r" % (tag, e))
                return ''
        if n <= 0:
            dbg("  [%s] GetSelection -> %d ranges (empty)" % (tag, n))
            return ''
        parts = []
        for i in range(n):
            try:
                r = ranges.GetElement(i)
            except Exception:
                try:
                    r = ranges[i]
                except Exception as e:
                    dbg("  [%s] ranges[%d] failed: %r" % (tag, i, e))
                    continue
            try:
                # 部分 UIA 实现不接受 -1，先用 -1 再用大数兜底
                t = r.GetText(-1) or r.GetText(1000000) or ''
            except Exception as e:
                dbg("  [%s] range[%d] GetText failed: %r" % (tag, i, e))
                continue
            if t:
                parts.append(t)
        dbg("  [%s] ranges=%d text_len=%d" % (tag, n, sum(len(p) for p in parts)))
        return ''.join(parts)

    # ---- 自动 Ctrl+C 兜底：UIA 读不到选区时模拟复制，读取后完整还原剪贴板 ----
    def _auto_copy_fallback():
        if auto_copy["busy"]:
            return ''
        now = time.time()
        # 冷却：打断终端/后台输出刷新造成的 20014 风暴，避免反复注入 Ctrl+C
        if now - auto_copy["last"] < AUTOCOPY_COOLDOWN:
            return ''
        # 终端/控制台前台跳过注入：Ctrl+C 在终端是 SIGINT，会中断用户运行中的程序
        if _is_console_foreground():
            dbg("  [autocopy] skipped (console foreground, avoid Ctrl+C/SIGINT)")
            return ''
        auto_copy["busy"] = True
        auto_copy["last"] = now
        try:
            before = _read_clipboard_text()
            backup = _backup_clipboard()  # 完整备份所有格式（文本/图片/文件等）
            text = ''
            # 第一轮：VK 批处理（记事本已验证有效）；无结果时第二轮：扫描码+间隔
            for mode in ("vk", "scan"):
                if text:
                    break
                if not _send_ctrl_c(mode):
                    break  # 注入被中止（前台为终端/控制台），无需轮询等待复制
                # 轮询等待目标应用完成复制（剪贴板内容变化且非原内容）
                deadline = time.time() + 0.5
                while time.time() < deadline:
                    t = _read_clipboard_text()
                    if t and t != before:
                        text = t
                        break
                    time.sleep(0.03)
            # 诊断：注入后剪贴板格式数变了但没读到文本 → 目标确实收到了 Ctrl+C，
            # 只是当时没有可复制的选区（区分 UIPI 拦截与「无选区」两种失败）
            if not text and _clipboard_format_count() not in (-1, len(backup)):
                dbg("  [autocopy] clipboard mutated but no text "
                    "(target processed Ctrl+C, empty selection?)")
            # 完整还原剪贴板（不打扰用户内容，接近无损；无论成败都还原，
            # 避免注入把用户剪贴板清空/破坏）
            if backup:
                _restore_clipboard(backup)
            dbg("  [autocopy] before_len=%d got_len=%d formats=%d"
                % (len(before), len(text), len(backup)))
            return text
        except Exception as e:
            dbg("  [autocopy] failed: %r" % e)
            return ''
        finally:
            auto_copy["busy"] = False

    # ---- 执行一次抓取（UIA 选区变化事件触发）----
    def _do_grab(source):
        anchor = _cursor_pos()
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
                # 兜底：GetSelection 只对当前拥有焦点的文本控件有效，
                # sender 元素读不到时改从当前焦点元素读选区。
                try:
                    foc = uia.GetFocusedElement()
                    text = _read_from_element(foc, "focus")
                except Exception as e:
                    dbg("  [focus] GetFocusedElement failed: %r" % e)
            if not text:
                # 最终兜底：自动模拟 Ctrl+C 读剪贴板（读取后完整还原，不打扰用户）
                text = _auto_copy_fallback()
            dbg("uia consumed, read len=%d" % len(text))
            anchor = _element_anchor(el, anchor)
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

    wc = WNDCLASSW()
    wc.lpfnWndProc = ctypes.cast(_wndproc, ctypes.c_void_p)
    wc.lpszClassName = "ttsGrabHidden"
    user32.RegisterClassW(ctypes.byref(wc))
    hwnd = user32.CreateWindowExW(
        0, "ttsGrabHidden", "ttsGrabHidden", 0,
        0, 0, 0, 0, None, None, None, None)
    if hwnd:
        hwnd_box["hwnd"] = hwnd
    else:
        dbg("hidden window create failed; UIA debounce timer off")

    out({"id": 0, "ok": True, "grab": False, "text": "", "x": None, "y": None,
         "note": "grabber started (event mode, no mouse hook)"})
    dbg("grabber ready: UIA selection events + auto-copy fallback")

    # ---- 主消息泵（驱动 UIA 事件与防抖定时器，无任何鼠标钩子/剪贴板监听）----
    msg = ctypes.wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


if __name__ == "__main__":
    main()
