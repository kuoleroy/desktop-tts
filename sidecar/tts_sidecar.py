# -*- coding: utf-8 -*-
"""TTS Sidecar daemon 主入口：NDJSON 行协议（可打断版）

输入(每行): {"id": n, "cmd": "tts|stop|export|voice|rate|pitch", "text": "..."}
输出(每行): {"id": n, "ok": true, "mp3": "t123.mp3"} 或 {"id": n, "ok": false, "error": "..."}

模块构成：
  - engines.py   — TTS 引擎（edge-tts / SAPI / 自导入 API）
  - filters.py   — 文本过滤（跳过符号对 / 删除 Markdown 标记 / 切分长文本）
"""
import json
import os
import signal
import sys
import threading

import engines
import filters

# 后台工作进程：生命周期由 Rust 管理（stdin 指令 + 看门狗），无需响应 Ctrl+C。
# 忽略 SIGINT，避免终端 Ctrl+C 把进程杀掉。
signal.signal(signal.SIGINT, signal.SIG_IGN)

# 全局 stdout 写锁（主线程与抓取线程并发写时避免交错）
_STDOUT_LOCK = threading.Lock()


def out(obj):
    with _STDOUT_LOCK:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def _run_heavy(cmd, text, rid):
    """后台线程执行耗时合成（tts/export），完成后回写结果。"""
    try:
        text = filters.strip_ignored(text)
        text = filters.strip_symbols(text)
        if cmd == "tts":
            files = engines._tts(text)
            out({"id": rid, "ok": True, "files": files})
        elif cmd == "export":
            p = engines._export_mp3(text)
            out({"id": rid, "ok": True, "file": p})
    except InterruptedError:
        out({"id": rid, "ok": False, "error": "interrupted by stop"})
    except Exception as e:
        out({"id": rid, "ok": False, "error": repr(e)})


def _fetch_voices_heavy(name, rid):
    """后台线程拉取 provider 音色列表（网络可能较慢），完成后回写 voices。"""
    try:
        p = engines._load_providers().get(name)
        if p is None:
            raise RuntimeError(f"provider {name!r} not found")
        voices = engines._fetch_api_voices(p)
        out({"id": rid, "ok": True, "voices": voices})
    except Exception as e:
        out({"id": rid, "ok": False, "error": repr(e)})


def handle(cmd, text, rid):
    # 轻量命令：主线程直接处理，立即响应
    if cmd == "settings":
        out({"id": rid, "ok": True, "settings": {
            "voice": engines.state["voice"],
            "rate": engines.state["rate"],
            "pitch": engines.state["pitch"],
        }})
        return
    if cmd == "stop":
        engines._cancel_active()
        out({"id": rid, "ok": True})
        return
    if cmd == "voice":
        engines.state["voice"] = text
        engines._save_settings()
        out({"id": rid, "ok": True})
        return
    if cmd == "rate":
        try:
            engines.state["rate"] = int(text)
        except ValueError:
            pass
        engines._save_settings()
        out({"id": rid, "ok": True})
        return
    if cmd == "pitch":
        engines.state["pitch"] = text
        engines._save_settings()
        out({"id": rid, "ok": True})
        return

    # 耗时命令：后台线程执行，主线程继续读 stdin 以便响应 stop
    if cmd == "fetch-voices":
        threading.Thread(target=_fetch_voices_heavy, args=(text, rid), daemon=True).start()
        return
    if cmd in ("tts", "export"):
        threading.Thread(target=_run_heavy, args=(cmd, text, rid), daemon=True).start()
        return

    out({"id": rid, "ok": False, "error": f"unknown cmd: {cmd}"})


def main():
    # Windows 中文系统默认按 GBK 码页读 stdin，会导致 Rust 传来的 UTF-8 中文乱码，
    # 强制使用 UTF-8 读取命令、输出回复。
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="strict")
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    except Exception:
        pass
    for line in sys.stdin:
        line = line.strip().lstrip("\ufeff")
        if not line:
            continue
        try:
            msg = json.loads(line)
            rid = msg.get("id", 0)
            cmd = msg.get("cmd", "")
            text = msg.get("text", "")
            try:
                handle(cmd, text, rid)
            except Exception as e:
                out({"id": rid, "ok": False, "error": repr(e)})
        except ValueError:
            out({"id": 0, "ok": False, "error": "bad json"})


if __name__ == "__main__":
    main()