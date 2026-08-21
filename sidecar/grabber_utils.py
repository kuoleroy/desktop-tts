# -*- coding: utf-8 -*-
"""抓取进程工具模块：剪贴板操作、窗口信息、SendInput 模拟 Ctrl+C、单实例管理等。

所有函数纯工具性质，无全局状态依赖。
"""
import ctypes
import ctypes.wintypes
import json
import os
import sys
import threading
import time

# ---- 常量 ----
# 单次抓取最大字符数
MAX_GRAB_CHARS = 50000
# UIA
TextPatternId = 10014
SelectionPatternId = 10001
UIA_Text_TextSelectionChangedEventId = 20014
TreeScope_Subtree = 4
# 窗口消息
WM_TIMER = 0x0113
WM_CLIPBOARDUPDATE = 0x031D
DEBOUNCE_MS = 350
DEBOUNCE_ID = 1
# 自动复制最小间隔
AUTOCOPY_COOLDOWN = 1.0

# ---- 单实例 ----
ERROR_ALREADY_EXISTS = 183
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
WAIT_ABANDONED = 0x80
SINGLE_MUTEX = "Local\\DesktopTTS_Grabber_Mutex"
SHUTDOWN_EVENT = "Local\\DesktopTTS_Grabber_Shutdown"
SHUTDOWN_ID = 2

COINIT_APARTMENTTHREADED = 0x2

# ---- 跳过注入配置 ----
SKIP_CONFIG = {"skip_window_classes": [], "skip_exe_names": []}
GRAB_SKIP_CONFIG = {"grab_skip_window_classes": [], "grab_skip_exe_names": []}

_OUT_LOCK = threading.Lock()


def dbg(msg):
    try:
        sys.stderr.write("[grabber] " + msg + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def out(obj):
    with _OUT_LOCK:
        try:
            sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        except OSError as e:
            if e.errno == 22:
                dbg("stdout pipe broken, exiting for restart")
                sys.exit(1)
            dbg("out failed: %r" % e)
        except Exception as e:
            dbg("out failed: %r" % e)


# ---- 跳过配置加载 ----
def _load_skip_config():
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skip_apps.json")
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        SKIP_CONFIG["skip_window_classes"] = data.get("skip_window_classes", [])
        SKIP_CONFIG["skip_exe_names"] = data.get("skip_exe_names", [])
        dbg("skip config loaded: classes=%s exes=%s" % (
            SKIP_CONFIG["skip_window_classes"], SKIP_CONFIG["skip_exe_names"]))
    except Exception as e:
        dbg("skip config load failed: %r, using defaults" % e)
        SKIP_CONFIG["skip_window_classes"] = ["ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS"]
        SKIP_CONFIG["skip_exe_names"] = []


def _load_grab_skip_config():
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "grab_skip_apps.json")
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        GRAB_SKIP_CONFIG["grab_skip_window_classes"] = data.get("grab_skip_window_classes", [])
        GRAB_SKIP_CONFIG["grab_skip_exe_names"] = data.get("grab_skip_exe_names", [])
        dbg("grab skip config loaded: classes=%s exes=%s" % (
            GRAB_SKIP_CONFIG["grab_skip_window_classes"], GRAB_SKIP_CONFIG["grab_skip_exe_names"]))
    except Exception as e:
        dbg("grab skip config load failed: %r, using defaults" % e)
        GRAB_SKIP_CONFIG["grab_skip_window_classes"] = []
        GRAB_SKIP_CONFIG["grab_skip_exe_names"] = []


def _is_grab_skip_hwnd(hwnd):
    if not hwnd:
        return True
    try:
        user32 = ctypes.windll.user32
        user32.GetClassNameW.restype = ctypes.c_int
        user32.GetClassNameW.argtypes = [ctypes.wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if cls.value in GRAB_SKIP_CONFIG.get("grab_skip_window_classes", []):
            return True
        exe = _fg_process_exe(hwnd)
        if exe and exe in GRAB_SKIP_CONFIG.get("grab_skip_exe_names", []):
            return True
        return False
    except Exception:
        return True


def _is_skip_hwnd(hwnd):
    if not hwnd:
        return True
    try:
        user32 = ctypes.windll.user32
        user32.GetClassNameW.restype = ctypes.c_int
        user32.GetClassNameW.argtypes = [ctypes.wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        c = cls.value
        if c in SKIP_CONFIG.get("skip_window_classes", []):
            return True
        exe = _fg_process_exe(hwnd)
        if exe and exe in SKIP_CONFIG.get("skip_exe_names", []):
            return True
        return False
    except Exception:
        return True


def _is_console_foreground():
    try:
        h = ctypes.windll.user32.GetForegroundWindow()
        return _is_skip_hwnd(h)
    except Exception:
        return True


# ---- 单实例 ----
def _acquire_single_instance():
    k32 = ctypes.windll.kernel32
    k32.CreateMutexW.restype = ctypes.wintypes.HANDLE
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    k32.CreateEventW.restype = ctypes.wintypes.HANDLE
    k32.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p]
    k32.SetEvent.restype = ctypes.c_int
    k32.SetEvent.argtypes = [ctypes.wintypes.HANDLE]
    k32.WaitForSingleObject.restype = ctypes.c_uint
    k32.WaitForSingleObject.argtypes = [ctypes.wintypes.HANDLE, ctypes.c_uint]
    k32.ResetEvent.restype = ctypes.c_int
    k32.ResetEvent.argtypes = [ctypes.wintypes.HANDLE]
    k32.CloseHandle.restype = ctypes.c_int
    k32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
    k32.GetLastError.restype = ctypes.c_uint

    mutex = k32.CreateMutexW(None, True, SINGLE_MUTEX)
    already = k32.GetLastError() == ERROR_ALREADY_EXISTS
    evt = k32.CreateEventW(None, False, False, SHUTDOWN_EVENT)
    if already:
        dbg("single-instance: old grabber alive, requesting exit & waiting takeover")
        k32.SetEvent(evt)
        r = k32.WaitForSingleObject(mutex, 5000)
        if r == WAIT_TIMEOUT:
            dbg("single-instance: old grabber did not exit in 5s, taking over anyway")
        k32.ResetEvent(evt)
    else:
        dbg("single-instance: no other grabber, I am the only instance")
    return mutex, evt


# ---- 剪贴板 ----
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


def _backup_clipboard():
    GMEM_MOVEABLE = 0x0002
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
                if not user32.SetClipboardData(fmt, h):
                    kernel32.GlobalFree(h)
        finally:
            user32.CloseClipboard()
    except Exception:
        pass


# ---- 窗口信息 ----
def _cursor_pos():
    try:
        p = ctypes.wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(p))
        return (p.x, p.y)
    except Exception:
        return (0, 0)


def _fg_window_info():
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


def _fg_class():
    try:
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = ctypes.wintypes.HWND
        user32.GetForegroundWindow.argtypes = []
        user32.GetClassNameW.restype = ctypes.c_int
        user32.GetClassNameW.argtypes = [ctypes.wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
        h = user32.GetForegroundWindow()
        if not h:
            return ''
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(h, cls, 256)
        return cls.value
    except Exception:
        return ''


def _fg_hwnd():
    try:
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = ctypes.wintypes.HWND
        user32.GetForegroundWindow.argtypes = []
        h = user32.GetForegroundWindow()
        return int(h) if h else 0
    except Exception:
        return 0


def _fg_process_exe(hwnd):
    try:
        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        h = kernel32.OpenProcess(0x1000, False, pid.value)
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            sz = ctypes.c_ulong(1024)
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(sz)):
                path = buf.value
                base = path.rsplit("\\", 1)[-1] if "\\" in path else path
                return base.lower()
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        pass
    return ""


def _elevation_info():
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
                    if not advapi32.GetTokenInformation(th, TokenIntegrityLevel, buf, sz.value, ctypes.byref(sz)):
                        return "?"
                    sid_ptr = int.from_bytes(bytes(buf.raw[:8]), "little")
                    if not sid_ptr:
                        return "?"
                    sid = ctypes.cast(sid_ptr, ctypes.POINTER(ctypes.c_ubyte))
                    sub_count = sid[1] & 0xFF
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
        user32.GetWindowThreadProcessId.argtypes = [ctypes.wintypes.HWND, ctypes.POINTER(ctypes.c_ulong)]
        me = int(ctypes.windll.kernel32.GetCurrentProcessId())
        h = user32.GetForegroundWindow()
        fgp = ctypes.c_ulong(0)
        if h:
            user32.GetWindowThreadProcessId(h, ctypes.byref(fgp))
        return "elev me=%s fg=%s" % (pid_sid(me), pid_sid(int(fgp.value)))
    except Exception:
        return "elev=?"


def _root_window_at(x, y):
    try:
        user32 = ctypes.windll.user32
        user32.WindowFromPoint.restype = ctypes.wintypes.HWND
        user32.WindowFromPoint.argtypes = [ctypes.wintypes.POINT]
        user32.GetAncestor.restype = ctypes.wintypes.HWND
        user32.GetAncestor.argtypes = [ctypes.wintypes.HWND, ctypes.c_uint]
        p = ctypes.wintypes.POINT()
        p.x, p.y = int(x), int(y)
        h = user32.WindowFromPoint(p)
        if not h:
            return 0
        return user32.GetAncestor(h, 2)
    except Exception:
        return 0


def _focus_window(hwnd):
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
        user32.GetWindowThreadProcessId.argtypes = [ctypes.wintypes.HWND, ctypes.POINTER(ctypes.c_ulong)]
        user32.AttachThreadInput.restype = ctypes.c_int
        user32.AttachThreadInput.argtypes = [ctypes.c_ulong, ctypes.c_ulong, ctypes.c_int]
        user32.SetForegroundWindow.restype = ctypes.c_int
        user32.SetForegroundWindow.argtypes = [ctypes.wintypes.HWND]
        user32.BringWindowToTop.restype = ctypes.c_int
        user32.BringWindowToTop.argtypes = [ctypes.wintypes.HWND]
        user32.IsWindow.restype = ctypes.c_int
        user32.IsWindow.argtypes = [ctypes.wintypes.HWND]
        user32.IsIconic.restype = ctypes.c_int
        user32.IsIconic.argtypes = [ctypes.wintypes.HWND]
        user32.ShowWindow.restype = ctypes.c_int
        user32.ShowWindow.argtypes = [ctypes.wintypes.HWND, ctypes.c_int]
        if not user32.IsWindow(hwnd):
            return False
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)
        cur = int(kernel32.GetCurrentThreadId())
        tpid = ctypes.c_ulong(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(tpid))
        tid = int(tpid.value)
        attached = False
        if tid and tid != cur:
            attached = bool(user32.AttachThreadInput(cur, tid, True))
        try:
            ok = user32.SetForegroundWindow(hwnd)
            user32.BringWindowToTop(hwnd)
            return bool(ok)
        finally:
            if attached:
                user32.AttachThreadInput(cur, tid, False)
    except Exception:
        return False


# ---- 模拟 Ctrl+C ----
def _send_ctrl_c(mode="vk", target_hwnd=0):
    VK_CONTROL = 0x11
    VK_C = 0x43
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_SCANCODE = 0x0008
    INPUT_KEYBOARD = 1

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
            ("dwFlags", ctypes.c_uint), ("time", ctypes.c_uint),
            ("dwExtraInfo", ctypes.c_void_p),
        ]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", ctypes.c_long), ("dy", ctypes.c_long),
            ("mouseData", ctypes.c_uint), ("dwFlags", ctypes.c_uint),
            ("time", ctypes.c_uint), ("dwExtraInfo", ctypes.c_void_p),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", ctypes.c_uint), ("wParamL", ctypes.c_ushort), ("wParamH", ctypes.c_ushort)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", ctypes.c_uint), ("u", _INPUTUNION)]

    user32 = ctypes.windll.user32
    if _is_console_foreground():
        dbg("  [send] aborted: console foreground (avoid Ctrl+C/SIGINT)")
        return False
    if target_hwnd and not _is_console_foreground():
        if _focus_window(target_hwnd):
            time.sleep(0.05)
            if _is_console_foreground():
                dbg("  [send] aborted: target is console after focus (avoid Ctrl+C/SIGINT)")
                return False
    dbg("  [send] %s | %s" % (_fg_window_info(), _elevation_info()))

    try:
        ctrl_scan = user32.MapVirtualKeyW(VK_CONTROL, 0)
        c_scan = user32.MapVirtualKeyW(VK_C, 0)
    except Exception:
        ctrl_scan, c_scan = 0x1D, 0x2E

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

    if mode == "keybd":
        try:
            user32.keybd_event.restype = ctypes.c_void_p
            user32.keybd_event.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte, ctypes.c_uint, ctypes.c_void_p]
            user32.keybd_event(VK_CONTROL, 0, 0, 0)
            time.sleep(0.01)
            user32.keybd_event(VK_C, 0, 0, 0)
            time.sleep(0.01)
            user32.keybd_event(VK_C, 0, KEYEVENTF_KEYUP, 0)
            time.sleep(0.01)
            user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
            dbg("  [send] keybd_event Ctrl+C sent")
            return True
        except Exception as e:
            dbg("  [send] keybd_event raised: %r" % e)
            return False

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
        try:
            user32.SendInput.restype = ctypes.c_uint
            user32.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int]
            force = [_in(VK_C, c_scan, KEYEVENTF_KEYUP), _in(VK_CONTROL, ctrl_scan, KEYEVENTF_KEYUP)]
            arr = (INPUT * 2)(force[0], force[1])
            user32.SendInput(2, arr, ctypes.sizeof(INPUT))
        except Exception:
            pass

    if sent != 4:
        dbg("  [send] SendInput(%s) failed (%d/4), fallback to keybd_event" % (mode, sent))
        try:
            user32.keybd_event.restype = ctypes.c_void_p
            user32.keybd_event.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte, ctypes.c_uint, ctypes.c_void_p]
            user32.keybd_event(VK_CONTROL, 0, 0, 0)
            time.sleep(0.01)
            user32.keybd_event(VK_C, 0, 0, 0)
            time.sleep(0.01)
            user32.keybd_event(VK_C, 0, KEYEVENTF_KEYUP, 0)
            user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
        except Exception as e:
            dbg("  [send] keybd_event fallback raised: %r" % e)
    return True