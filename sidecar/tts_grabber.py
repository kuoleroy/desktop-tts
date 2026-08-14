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
import signal
import sys
import threading
import time

# 后台工作进程：生命周期由 Rust 管理（stdin 指令 + 看门狗），无需响应 Ctrl+C。
# 忽略 SIGINT，避免终端 Ctrl+C / 注入 Ctrl+C 把进程杀掉
# （此前 KeyboardInterrupt 反复中断抓取，记事本都不弹了）。
signal.signal(signal.SIGINT, signal.SIG_IGN)

# ---- 常量 ----
# 单次抓取最大字符数（5万，约整章/多章；分块按 2000 字切后顺序朗读）
MAX_GRAB_CHARS = 50000
# UIA
TextPatternId = 10014
SelectionPatternId = 10001
UIA_Text_TextSelectionChangedEventId = 20014
TreeScope_Subtree = 4
# 窗口消息
WM_TIMER = 0x0113
WM_CLIPBOARDUPDATE = 0x031D
DEBOUNCE_MS = 350  # 选择事件高频触发，防抖后只读一次
DEBOUNCE_ID = 1
# 自动复制最小间隔：终端/后台输出刷新会持续触发 20014，冷却打断「Ctrl+C 风暴」
AUTOCOPY_COOLDOWN = 1.0

# ---- 单实例强制（Windows 命名互斥锁）：同一时刻只允许一个抓取进程存活 ----
# 新实例启动时创建/持有互斥锁；若已有旧实例，则置关闭事件请它退出，
# 等它释放后接管。旧实例通过定时器轮询关闭事件，收到信号即干净退出
# （避免 dev 重启后旧孤儿 grabber 残留、stdout 管道断裂仍抢占抓取）。
ERROR_ALREADY_EXISTS = 183
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
WAIT_ABANDONED = 0x80
SINGLE_MUTEX = "Local\\DesktopTTS_Grabber_Mutex"
SHUTDOWN_EVENT = "Local\\DesktopTTS_Grabber_Shutdown"
SHUTDOWN_ID = 2  # 单实例接管轮询定时器 id

COINIT_APARTMENTTHREADED = 0x2


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
                # 管道断裂：Rust 父进程已退出/被重启，上报无意义。
                # 退出由看门狗重启新实例（避免孤儿进程继续抢占抓取）。
                dbg("stdout pipe broken, exiting for restart")
                sys.exit(1)
            dbg("out failed: %r" % e)
        except Exception as e:
            dbg("out failed: %r" % e)


def _acquire_single_instance():
    """单实例入口：若已有旧抓取进程，通知其退出并等其释放后接管。

    返回 (mutex, evt) 句柄对；两者须在进程整个生命周期持有，
    保证互斥锁不被误释放（否则新实例会误以为无旧实例而并存）。
    """
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
        # 有旧实例存活：置关闭事件请它退出，再等它释放互斥锁后接管
        dbg("single-instance: old grabber alive, requesting exit & waiting takeover")
        k32.SetEvent(evt)
        r = k32.WaitForSingleObject(mutex, 5000)
        if r == WAIT_TIMEOUT:
            dbg("single-instance: old grabber did not exit in 5s, taking over anyway")
        # 清掉残留信号，防止自己的 SHUTDOWN_ID 定时器误判为接管请求而自我退出
        k32.ResetEvent(evt)
    else:
        dbg("single-instance: no other grabber, I am the only instance")
    return mutex, evt


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


def _fg_class():
    """当前前台窗口类名（用于按应用类型过滤噪音选区事件）。"""
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

def _is_console_foreground():
    """前台是否真正的「纯控制台」窗口（仅原生 cmd.exe 的 ConsoleWindowClass）。

    终端里 Ctrl+C 是 SIGINT（中断运行中的程序），且输出刷新会持续触发 20014
    → 必须跳过注入，避免「键盘一直 Ctrl+C」。

    注意：此判断刻意「收紧」——只对 Windows 原生控制台窗口（ConsoleWindowClass，
    即旧式 cmd.exe/conhost）返回 True。其余窗口（含 Windows Terminal 的宿主
    CASCADIA_HOSTING_WINDOW_CLASS、各种嵌入 TermControl 的客户端）一律不跳过，
    以扩大 Ctrl+C 注入覆盖面（浏览器/Electron/客户端等 UIA 读不到选区的应用
    都能靠注入复制抓到）。Ctrl+C 注入本身受 _send_ctrl_c 的 SendInput 保护，
    且只在已选文字时触发，不会无限刷键。
    """
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
        # 仅原生控制台才跳过；其余一律放行，扩大抓取覆盖面
        return c == "ConsoleWindowClass"
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


def _root_window_at(x, y):
    """鼠标位置所在顶层窗口（WindowFromPoint → GetAncestor GA_ROOT）。

    悬浮框/面板 topmost 抢焦点时，前台窗口≠用户实际操作的窗口，注入 Ctrl+C
    会发到应用自身窗口而失败；用「选中文字的位置」定位真正目标。
    """
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
        return user32.GetAncestor(h, 2)  # GA_ROOT = 2 取顶层窗口
    except Exception:
        return 0


def _focus_window(hwnd):
    """把 hwnd 置为前台窗口（绕过「前台锁」）。

    悬浮框/面板 topmost 抢焦点后，前台可能不是用户操作的目标窗口；
    注入 Ctrl+C 前先聚焦目标，确保按键发到真正选中文字的应用。
    """
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
        user32.GetWindowThreadProcessId.argtypes = [
            ctypes.wintypes.HWND, ctypes.POINTER(ctypes.c_ulong)]
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
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
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


# ---- 模拟 Ctrl+C（SendInput 完整按键序列 + keybd_event 兜底，finally 强制释放，绝不锁 Ctrl）----
def _send_ctrl_c(mode="vk", target_hwnd=0):
    """向目标/前台窗口注入一次 Ctrl+C。

    mode="vk"  ：批处理 VK 虚拟键（记事本已验证有效）。
    mode="scan"：逐个发送 + 扫描码（KEYEVENTF_SCANCODE）＋20ms 间隔，
                 更接近真实硬件键入，部分 Chromium 类应用只认扫描码事件。
    target_hwnd：可选。给定时先聚焦该窗口再注入（悬浮框/面板抢焦点时，
                 前台≠目标，必须先把目标置前，否则 Ctrl+C 发错窗口）。
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
    # 先把「用户实际选中文字」的目标窗口置为前台：悬浮框/面板 topmost 抢焦点时，
    # 前台是应用自身窗口，Ctrl+C 会发错地方（got_len=0）。聚焦目标后再注入。
    if target_hwnd and not _is_console_foreground():
        if _focus_window(target_hwnd):
            time.sleep(0.05)
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

    # keybd_event 模式：不走 SendInput（SendInput 返回 4/4 但部分 Chromium 应用
    # 仍不响应模拟复制），直接用 keybd_event 模拟真实按键，能穿透 Edge/浏览器。
    # 仅由 _auto_copy_fallback 在 SendInput 无结果时调用，且伴随完整剪贴板备份还原。
    if mode == "keybd":
        try:
            user32.keybd_event.restype = ctypes.c_void_p
            user32.keybd_event.argtypes = [
                ctypes.c_ubyte, ctypes.c_ubyte, ctypes.c_uint, ctypes.c_void_p]
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
    # ---- 单实例强制：新进程接管前，先让旧的孤儿/残留抓取进程退出 ----
    _mutex_h, _shutdown_h = _acquire_single_instance()
    # 句柄持有至进程退出（局部变量被闭包引用，不会被 GC 释放）
    single = {"mutex": _mutex_h, "evt": _shutdown_h}
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
    # 剪贴板监听窗口期（点「朗读」按钮后开启）：期限内用户手动 Ctrl+C 复制文本 → 自动朗读。
    # 仅记录一个截止时间，过期即停；用 last 去重自身备份/还原的触发。
    clipwatch = {"until": 0.0, "last_clip": ""}

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
            # 诊断：记录所有流入的 UIA 事件（不限 20014），排查真实运行时事件是否到达
            try:
                e0 = sender.QueryInterface(UIA.IUIAutomationElement)
                _cls = getattr(e0, "CurrentClassName", None) or "?"
            except Exception:
                _cls = "?"
            # 自动抓取已停用（改为双击/拖选/Ctrl+A 主动触发）：20014 事件不再自动弹，
            # 避免 opencode/Edge/记事本 Edit 控件在光标/聚焦变化时高频触发导致乱跳。
            # 仅保留诊断日志（仅在调试期短暂开启，生产可注释）。
            if False and eventId == UIA_Text_TextSelectionChangedEventId:
                dbg("UIA event id=0x%X cls=%s" % (eventId, _cls))
            return

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

    def _uia_scan_selection():
        """主动遍历前台窗口元素树，收集所有选中文本（取最长）。

        与 desktop_tts.py 旧版一致：不依赖 20014 事件，点击「朗读」时主动扫描
        前台窗口的 SelectionPattern/TextPattern 选中范围。GetText(-1) 能读取
        完整的选中文本范围（含屏幕外/长文本），适合读小说等长段落。
        返回 (text, rect)。
        """
        try:
            user32 = ctypes.windll.user32
            user32.GetForegroundWindow.restype = ctypes.wintypes.HWND
            user32.GetForegroundWindow.argtypes = []
            hwnd = user32.GetForegroundWindow()
            # 优先用鼠标位置定位目标窗口：桌宠/悬浮框 alwaysOnTop 会抢前台，
            # 但用户鼠标停在「选中文字」上，_root_window_at 能拿到真正目标。
            # 仅当鼠标不在本应用窗口上时才用它（避免读到悬浮框/桌宠自身）。
            cx, cy = _cursor_pos()
            mwin = _root_window_at(cx, cy)
            try:
                # 排除桌宠自身窗口（Tauri Window / ttsGrabHidden）
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
            dbg("  [scan] ElementFromHandle failed: %r" % e)
            return '', None
        best = ''
        rect = None
        # 用 FindAll(Subtree) 遍历整个窗口树
        try:
            cond = uia.CreateTrueCondition()
            nodes = win.FindAll(0x4, cond)  # 0x4 = TreeScope_Subtree
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
                # 1) SelectionPattern 选区
                try:
                    sp = el.GetCurrentPattern(SelectionPatternId)
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
                # 2) TextPattern 选中范围（GetText(-1) 读完整长文本）
                try:
                    tp = el.GetCurrentPattern(TextPatternId)
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
            dbg("  [scan] FindAll failed: %r" % e)
        return best, rect

    # ---- 自动 Ctrl+C 兜底：UIA 读不到选区时模拟复制，读取后完整还原剪贴板 ----
    def _auto_copy_fallback(el=None):
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
        # 目标窗口 = 「用户选中文字」的位置所在顶层窗口。悬浮框/面板 topmost
        # 抢焦点时前台≠目标，用选区包围盒中心（失败退回鼠标位置）定位目标窗口，
        # 注入 Ctrl+C 前先聚焦它，避免把复制发到应用自身窗口（got_len=0）。
        tx, ty = _cursor_pos()
        if el is not None:
            r = _element_rect(el)
            if r:
                tx, ty = (r[0] + r[2]) // 2, (r[1] + r[3]) // 2
        target = _root_window_at(tx, ty) if (tx or ty) else 0
        if target:
            dbg("  [autocopy] target hwnd=0x%X (fg=%s)" % (target, _fg_window_info()))
        auto_copy["busy"] = True
        auto_copy["last"] = now
        try:
            before = _read_clipboard_text()
            backup = _backup_clipboard()  # 完整备份所有格式（文本/图片/文件等）
            text = ''
            # 第一轮：VK 批处理（记事本已验证有效）；第二轮：扫描码+间隔；
            # 第三轮：keybd_event（SendInput 对部分 Chromium 应用返回成功却仍不复制，
            # keybd_event 模拟真实按键可穿透 Edge/浏览器）。
            for mode in ("vk", "scan", "keybd"):
                if text:
                    break
                if not _send_ctrl_c(mode, target_hwnd=target):
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
        except BaseException as e:
            dbg("  [autocopy] failed: %r" % e)
            return ''
        finally:
            auto_copy["busy"] = False

    # ---- OCR 兜底：截图选区包围盒 + RapidOCR(onnxruntime/PaddleOCR 模型)识别。
    #     用于浏览器/管理员应用等注入 Ctrl+C 被 UIPI 拦截、UIA 又读不到选区的最终手段。
    #     RapidOCR 首次调用会加载模型（较慢），之后复用全局引擎。----
    _ocr_engine = {"engine": None, "lock": threading.Lock()}

    def _get_ocr_engine():
        if _ocr_engine["engine"] is None:
            with _ocr_engine["lock"]:
                if _ocr_engine["engine"] is None:
                    try:
                        from rapidocr_onnxruntime import RapidOCR
                        _ocr_engine["engine"] = RapidOCR()
                    except BaseException as e:
                        # 依赖缺失/损坏也绝不能让抓取进程崩溃；缓存失败标记避免反复加载
                        dbg("  [ocr] engine load failed: %r" % e)
                        _ocr_engine["engine"] = False
        return _ocr_engine["engine"]

    def _ocr_capture(rect):
        """rect=(l,t,r,b) 屏幕坐标。返回识别文本；依赖缺失/失败返回 ''。"""
        try:
            from PIL import Image, ImageGrab
        except BaseException as e:
            dbg("  [ocr] imports failed: %r" % e)
            return ''
        try:
            l, t, r, b = (int(v) for v in rect)
            if r <= l or b <= t:
                return ''
            img = ImageGrab.grab(bbox=(l, t, r, b))
            if img is None or img.width < 4 or img.height < 4:
                return ''
            # 放大 2 倍提升小字号识别率
            img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
            # 直接以内存 numpy 数组喂 RapidOCR（避免临时文件与 OCR 引擎之间的
            # 磁盘读写竞态 —— 此前临时 PNG 未写完就被读导致 OSError truncated）。
            import numpy as np
            arr = np.array(img.convert("RGB"))
            engine = _get_ocr_engine()
            if not engine:
                return ''
            res, _ = engine(arr)
            if not res:
                dbg("  [ocr] rect=%s recognized nothing" % (rect,))
                return ''
            text = "\n".join(line[1] for line in res)
            text = ' '.join(text.split())
            dbg("  [ocr] rect=%s got_len=%d" % (rect, len(text)))
            return text
        except BaseException as e:
            dbg("  [ocr] failed: %r" % e)
            return ''

    def _element_rect(el):
        """取 UIA 元素的屏幕包围盒 (l,t,r,b)；失败返回 None。"""
        try:
            r = el.CurrentBoundingRectangle
            if hasattr(r, "left"):
                return (int(r.left), int(r.top), int(r.right), int(r.bottom))
            if isinstance(r, (tuple, list)) and len(r) >= 4:
                return (int(r[0]), int(r[1]), int(r[2]), int(r[3]))
        except Exception:
            pass
        return None

    def _focus_ocr_rect(rect):
        """将 OCR 区域聚焦到鼠标位置附近，避免对整窗口/整屏识别不准。

        Chromium/Electron 应用（Chrome/opencode/VS Code 等）不向 UIA 暴露选区
        边界，_element_rect 往往返回整个窗口甚至全屏。此时若直接 OCR 会读到
        大片无关内容、识别质量差。改成以鼠标光标为中心截取一个聚焦矩形：
        - 光标停在选中的文字上（用户选中后鼠标通常在其上），聚焦框能框住它
        - 聚焦框宽高固定，覆盖 1~2 行文字，识别更准
        若 rect 本身较小（说明是精确选区，如 Win32 控件），则原样返回。
        """
        if not rect:
            return None
        l, t, r, b = rect
        w, h = r - l, b - t
        # 合理的精确选区（宽高都足够）：直接使用，OCR 框住的就是选中文字。
        # 过小/过高说明不是精确选区（空元素、单字符行、整窗口），交给聚焦逻辑。
        if 80 <= w <= 900 and 40 <= h <= 350:
            return (l, t, r, b)
        # 整窗口/全屏：改用鼠标位置聚焦。聚焦框保证尺寸，仅受屏幕范围约束，
        # 不受原窗口 rect 约束（否则会被压扁成一行）。
        cx, cy = _cursor_pos()
        FOCUS_W, FOCUS_H = 520, 220
        nl = cx - FOCUS_W // 2
        nt = cy - FOCUS_H // 2
        nr = cx + FOCUS_W // 2
        nb = cy + FOCUS_H // 2
        # 限制在屏幕范围内（多显示器主屏 0,0..W,H）
        try:
            from ctypes import windll
            sw = windll.user32.GetSystemMetrics(0)
            sh = windll.user32.GetSystemMetrics(1)
        except Exception:
            sw, sh = 2560, 1440
        nl = max(nl, 0); nt = max(nt, 0)
        nr = min(nr, sw); nb = min(nb, sh)
        if nr <= nl or nb <= nt:
            # 兜底：极端情况退回复制位置为中心的最小区域
            nl = max(cx - 260, 0); nt = max(cy - 110, 0)
            nr = min(cx + 260, sw); nb = min(cy + 110, sh)
            if nr <= nl or nb <= nt:
                return (l, t, r, b)
        return (nl, nt, nr, nb)

    # ---- 执行一次抓取（UIA 选区变化事件触发）----
    def _report(source, text, anchor):
        text = ' '.join(text.split())[:MAX_GRAB_CHARS]
        # 过滤噪音选区：Chromium 类应用（Chrome_WidgetWin_1，如 opencode/浏览器）
        # 光标移动/聚焦/渲染变化也会触发 20014 事件并读出光标附近几个字（len≈2-20），
        # 未选中也弹很烦。这类应用需选中足够长文本才弹，避免误触；记事本等保持灵敏。
        if len(text) < 2 or text == last_text[0]:
            return
        fgcls = _fg_class()
        if fgcls.startswith("Chrome_WidgetWin"):
            if len(text) < 30:
                dbg("noise filtered (chromium len=%d < 30)" % len(text))
                return
        last_text[0] = text
        dbg("grab hit (%s) len=%d" % (source, len(text)))
        out({"id": 0, "ok": True, "grab": True, "text": text,
             "x": anchor[0], "y": anchor[1]})

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
                # 兜底：自动模拟 Ctrl+C 读剪贴板（读取后完整还原，不打扰用户）
                text = _auto_copy_fallback(el)
            if not text:
                # 最终兜底：OCR 截取选区包围盒识别（注入被 UIPI 拦截时仍能抓）。
                # 必须放后台线程：RapidOCR 首次加载 onnx 模型 + 推理耗时较长，
                # 若在主线程跑会卡死消息泵（窗口无响应、后续事件全丢）。
                rect = _element_rect(el)
                # 优化：当元素 rect 异常巨大（≈整个窗口而非精确选区，常见于
                # Chromium/Electron 应用不暴露选区边界）时，改为以鼠标位置为中心
                # 截取聚焦区域，避免 OCR 读到整屏无关内容、识别不准。
                rect = _focus_ocr_rect(rect)
                anchor = _element_anchor(el, anchor)
                if rect:
                    dbg("  [ocr] spawn background OCR rect=%s" % (rect,))
                    threading.Thread(
                        target=_ocr_job, args=(rect, anchor, source),
                        daemon=True).start()
                    return
                dbg("  [ocr] no element rect, skip OCR")
            dbg("uia consumed, read len=%d" % len(text))
            anchor = _element_anchor(el, anchor)
        _report(source, text, anchor)

    def _ocr_job(rect, anchor, source):
        """后台 OCR：截图选区包围盒 + RapidOCR 识别，完成后上报。"""
        try:
            text = _ocr_capture(rect)
        except BaseException as e:
            dbg("  [ocr] job failed: %r" % e)
            text = ''
        dbg("uia consumed (ocr), read len=%d" % len(text))
        _report(source, text, anchor)

    def _ocr_cmd_job(rect):
        """后台 OCR（来自全屏框选命令）：识别后上报，锚点为框选区下方。"""
        try:
            text = _ocr_capture(rect)
        except BaseException as e:
            dbg("  [ocr-cmd] job failed: %r" % e)
            out({"id": 0, "ok": False, "grab": False, "error": repr(e)})
            return
        anchor = ((rect[0] + rect[2]) // 2, rect[3] + 12)
        dbg("ocr-cmd consumed, read len=%d" % len(text))
        _report("ocr", text, anchor)

    def _selread_job():
        """点按钮主动读取前台窗口选中文本（可读长文本）。UIA 扫描 + Ctrl+C 兜底。"""
        text, rect = _uia_scan_selection()
        source = "selread"
        anchor = None
        if rect:
            anchor = ((rect[0] + rect[2]) // 2, rect[3] + 12)
        if not text:
            # UIA 读不到：尝试自动 Ctrl+C（会聚焦前台窗口，读完还原剪贴板）
            dbg("  [selread] UIA empty, trying auto-copy")
            text = _auto_copy_fallback(None)
            if text:
                source = "selread-cc"
        if not text:
            dbg("  [selread] no selection found")
            out({"id": 0, "ok": False, "grab": False, "error": "no selection"})
            return
        dbg("selread consumed, read len=%d" % len(text))
        _report(source, text, anchor or _cursor_pos())

    def _on_clipboard_changed():
        """剪贴板变化（WM_CLIPBOARDUPDATE）→ 若在监听窗口期内且有新文本则上报朗读。

        用户点「朗读」后开启窗口期，在浏览器/notepad++/Edge 等无法 UIA 读取的
        应用里手动 Ctrl+C 复制文本，脚本据此朗读。安全：仅读剪贴板，不注入。
        """
        try:
            if time.time() > clipwatch["until"]:
                return
            # 自身 _auto_copy_fallback 备份/还原会触发本消息，必须跳过
            if auto_copy["busy"]:
                return
            text = _read_clipboard_text()
            if not text or len(text) < 2:
                return
            if text == clipwatch["last_clip"] or text == last_text[0]:
                return
            clipwatch["last_clip"] = text
            _report("clipwatch", text, _cursor_pos())
        except BaseException as e:
            dbg("  [clipwatch] failed: %r" % e)

    def _clipwatch_cmd_job():
        """开启剪贴板监听窗口期（点「朗读」按钮，selread 读不到时兜底）。"""
        clipwatch["until"] = time.time() + 6.0
        clipwatch["last_clip"] = _read_clipboard_text()
        dbg("  [clipwatch] watching clipboard for 6s (copy text to read)")
        out({"id": 0, "ok": True, "grab": False, "clipwatch": True})

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
            elif cmd == "ocr":
                # 全屏框选截图：rect 为 [l,t,r,b] 屏幕坐标，RapidOCR 识别后上报。
                # 全程后台线程执行，绝不阻塞 stdin 主循环；锚点用 rect 中心（悬浮框出现在框选区下方）。
                dbg("received ocr cmd")
                raw = msg.get("text") or ""
                try:
                    rect = json.loads(raw)
                    if len(rect) != 4:
                        raise ValueError("rect must have 4 numbers")
                    rect = [int(v) for v in rect]
                except Exception as e:
                    dbg("  [ocr-cmd] bad rect: %r" % e)
                    out({"id": 0, "ok": False, "grab": False, "error": "bad rect"})
                    continue
                threading.Thread(
                    target=_ocr_cmd_job, args=(rect,), daemon=True).start()
            elif cmd == "selread":
                # 点按钮主动读取前台窗口选中文本（可读长文本，不依赖 20014 事件）
                dbg("received selread cmd")
                threading.Thread(target=_selread_job, daemon=True).start()
            elif cmd == "clipwatch":
                # 开启剪贴板监听窗口期：用户在无法 UIA 读取的应用里手动 Ctrl+C 复制
                dbg("received clipwatch cmd")
                _clipwatch_cmd_job()

    threading.Thread(target=_stdin_reader, daemon=True).start()

    # ---- pynput 钩子（主动触发，老版本方式）：双击左键 / 拖动选中松开 / Ctrl+A
    #      触发主动读取前台选区。不再自动监听 UIA 事件（避免光标/聚焦变化乱跳）。----
    def _start_hooks():
        try:
            from pynput import mouse, keyboard
        except Exception as e:
            dbg("  [hooks] pynput import failed: %r" % e)
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
                    # 双击判定（300ms 内、位移<6px）。不立即触发：双击选中的一行
                    # 要等第二次「松开」才真正完成选择，按下时选区尚未形成会读到空。
                    if now - lt < 0.3 and abs(x - lx) < 6 and abs(y - ly) < 6:
                        _last_click[0] = 0.0
                        _dbl_pending[0] = True
                    else:
                        _last_click[0], _last_click[1], _last_click[2] = now, x, y
                    _drag_start[0] = (x, y)
                else:
                    # 双击松开：此时双击选中的一行已稳定，触发读取
                    if _dbl_pending[0]:
                        _dbl_pending[0] = False
                        _do_trigger()
                        return
                    if _drag_start[0]:
                        sx, sy = _drag_start[0]
                        _drag_start[0] = None
                        # 拖拽选中：位移>=8px 视为选区拖选，触发读取
                        if max(abs(x - sx), abs(y - sy)) >= 8:
                            _do_trigger()
            except Exception as e:
                dbg("  [hooks] on_click err: %r" % e)

        def _on_key(key):
            try:
                from pynput import keyboard as kb
                # 跟踪 Ctrl 状态；Ctrl+A（vk=65 且 Ctrl 按下）触发主动读取
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
            dbg("  [hooks] pynput mouse/keyboard listener started")
        except Exception as e:
            dbg("  [hooks] listener start failed: %r" % e)

    _start_hooks()

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
        if uMsg == WM_TIMER and wParam == SHUTDOWN_ID:
            # 单实例接管：有更新的抓取实例请求退出 → 干净退出（释放 UIA 事件/互斥锁）
            k32 = ctypes.windll.kernel32
            k32.WaitForSingleObject.restype = ctypes.c_uint
            k32.WaitForSingleObject.argtypes = [ctypes.wintypes.HANDLE, ctypes.c_uint]
            if k32.WaitForSingleObject(single["evt"], 0) == WAIT_OBJECT_0:
                dbg("single-instance: takeover requested, exiting")
                # 注意：user32 是本函数局部变量（下方 DEBOUNCE 分支赋值），必须在此显式取
                ctypes.windll.user32.PostQuitMessage(0)
            return 0
        if uMsg == WM_TIMER and wParam == DEBOUNCE_ID:
            user32 = ctypes.windll.user32
            user32.KillTimer(hwnd_box["hwnd"], DEBOUNCE_ID)
            if armed["on"] and pending["dirty"]:
                _do_grab("uia")
            return 0
        if uMsg == WM_CLIPBOARDUPDATE:
            _on_clipboard_changed()
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
        # 单实例接管轮询：定期检查是否有更新的抓取实例请求接管退出
        user32.SetTimer(hwnd, SHUTDOWN_ID, 500, None)
        # 注册剪贴板监听（WM_CLIPBOARDUPDATE）：仅窗口期内处理，无钩子无轮询
        user32.AddClipboardFormatListener.restype = ctypes.c_int
        user32.AddClipboardFormatListener.argtypes = [ctypes.wintypes.HWND]
        user32.AddClipboardFormatListener(hwnd)
    else:
        dbg("hidden window create failed; UIA debounce timer off")

    out({"id": 0, "ok": True, "grab": False, "text": "", "x": None, "y": None,
         "note": "grabber started (event mode, no mouse hook)"})
    dbg("grabber ready: UIA selection events + auto-copy fallback + OCR")
    # 注意：不在此预加载 OCR 引擎。RapidOCR/numpy 导入耗时长且容易被打断，
    # 改在首次真正需要 OCR 时（_ocr_job 后台线程）惰性加载，绝不阻塞主流程。

    # ---- 主消息泵（驱动 UIA 事件与防抖定时器，无任何鼠标钩子/剪贴板监听）----
    msg = ctypes.wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


if __name__ == "__main__":
    main()
