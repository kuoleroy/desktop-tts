# -*- coding: utf-8 -*-
"""全局选区抓取独立进程（与 TTS sidecar 隔离）。

机制：按钮触发式监控，而非持续实时抓取。
- 进程默认待命，不读取选区，因此不会在任何软件里选中文字时让面板乱动。
- Rust 在用户点击「抓取朗读」按钮后，向本进程 stdin 发送 {"cmd":"arm"}。
- 收到 arm 后进入「已武装」状态，每隔 ~0.3s 轻量轮询前台控件选区；
  检测到新的非空选区即以 NDJSON 上报 Rust（移动面板 + 填充文本），
  上报一次后自动解除武装，等待下一次 arm。

输入(每行): {"cmd": "arm"}
输出(每行): {"grab": true, "text": "...", "x": 123, "y": 456}
"""
import ctypes
import ctypes.wintypes
import json
import sys
import threading
import time


def dbg(msg):
    try:
        sys.stderr.write("[grabber] " + msg + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def out(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def cursor_pos():
    try:
        p = ctypes.wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(p))
        return (p.x, p.y)
    except Exception:
        return None


# 剪贴板兜底冷却（秒）：防止对 UIA 失效的应用高频模拟 Ctrl+C 覆盖用户剪贴板。
# 模块级状态，跨轮询共享；距离上次剪贴板兜底不足该值时直接跳过兜底。
CLIP_COOLDOWN = 3.0
_last_clip_attempt = {"t": 0.0}

# 单次抓取最大字符数（5万，约整章/多章；分块按 2000 字切后顺序朗读）
MAX_GRAB_CHARS = 50000


def _clipboard_fallback(auto):
    """剪贴板兜底：模拟 Ctrl+C → 读剪贴板 → 还原原剪贴板。

    带冷却时间，两次兜底之间至少间隔 CLIP_COOLDOWN 秒，避免反复模拟 Ctrl+C
    覆盖用户剪贴板（用户已明确反感）。返回抓到的文本，否则返回 ''。
    """
    now = time.time()
    if now - _last_clip_attempt["t"] < CLIP_COOLDOWN:
        return ''
    _last_clip_attempt["t"] = now
    try:
        old = auto.GetClipboardText()
        auto.SendKeys('{Ctrl}c')
        time.sleep(0.25)
        text = auto.GetClipboardText()
        if text and text != old:
            try:
                auto.SetClipboardText(old)
            except Exception:
                pass
            return text
    except Exception:
        pass
    return ''


def read_selection():
    """读取当前前台控件的选中文字。

    优先用 UIA 全树遍历（深度 10）抓取选区，绝大多数应用可直接命中，
    避免频繁操作剪贴板；仅当 UIA 完全读不到时才回退到剪贴板方案
    （模拟 Ctrl+C 复制→读剪贴板→还原），且带冷却时间，作为最后兜底。
    """
    try:
        import uiautomation as auto
        win = auto.GetForegroundControl()
        if not win:
            return ''
        best = ''
        todo = [(win, 0)]
        while todo:
            ctrl, depth = todo.pop(0)
            if depth > 10:
                continue
            try:
                sp = ctrl.GetSelectionPattern()
                if sp:
                    for s in sp.GetCurrentSelection():
                        t = (s.Name or '')
                        if len(t) > len(best):
                            best = t
            except Exception:
                pass
            try:
                tp = ctrl.GetTextPattern()
                for r in tp.GetSelection():
                    t = (r.GetText(-1) or '')
                    if len(t) > len(best):
                        best = t
            except Exception:
                pass
            try:
                for ch in ctrl.GetChildren():
                    todo.append((ch, depth + 1))
            except Exception:
                pass
        if best:
            return best

        # 剪贴板兜底（带冷却）：仅当 UIA 读不到时才走，且不频繁触发
        return _clipboard_fallback(auto)
    except Exception:
        return ''


def main():
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="strict")
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    except Exception:
        pass

    # 是否处于「已武装」状态：只有 arm 后才监控选区
    armed = {"on": False, "last": ""}

    # stdin 监听线程：收到 {"cmd":"arm"} 开启持续监控；{"cmd":"disarm"} 关闭
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
                armed["last"] = ""
                dbg("received arm, now armed")
                out({"id": 0, "ok": True, "grab": False, "armed": True})
            elif cmd == "disarm":
                armed["on"] = False
                dbg("received disarm")
                out({"id": 0, "ok": True, "grab": False, "armed": False})

    threading.Thread(target=_stdin_reader, daemon=True).start()

    out({"id": 0, "ok": True, "grab": False, "text": "", "x": None, "y": None,
         "note": "grabber started (armed on demand)"})

    while True:
        time.sleep(0.3)
        if not armed["on"]:
            continue
        try:
            text = read_selection()
        except Exception as e:
            dbg("read_selection error: %r" % (e,))
            continue
        text = ' '.join(text.split())[:MAX_GRAB_CHARS]
        if len(text) < 2 or text == armed["last"]:
            if armed["on"]:
                dbg("armed, selection len=%d (skip)" % len(text))
            continue
        armed["last"] = text
        # 保持武装：连续抓取多段文字（直到收到 disarm）
        pos = cursor_pos()
        dbg("grab hit len=%d" % len(text))
        out({"id": 0, "ok": True, "grab": True, "text": text,
             "x": pos[0] if pos else None, "y": pos[1] if pos else None})


if __name__ == "__main__":
    main()
