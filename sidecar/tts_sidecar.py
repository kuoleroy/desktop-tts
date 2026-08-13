# -*- coding: utf-8 -*-
"""TTS Sidecar daemon：NDJSON 行协议（可打断版）

输入(每行): {"id": n, "cmd": "tts|stop|export|voice|rate|pitch", "text": "..."}
输出(每行): {"id": n, "ok": true, "mp3": "t123.mp3"} 或 {"id": n, "ok": false, "error": "..."}

设计要点：
- 主线程持续读 stdin，即时响应「stop/voice/rate/pitch」等轻量命令，不阻塞。
- 「tts/export」的 edge-tts 合成放到后台线程，通过 run_coroutine_threadsafe
  提交到独立 asyncio 事件循环；收到「stop」时取消当前 Future，
  从而真正打断进行中的网络合成，而不是排队等待。
- edge-tts 失败自动回退 SAPI wav。
"""
import asyncio
import concurrent.futures
import json
import os
import sys
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(os.path.dirname(BASE), "tts_cache")
os.makedirs(CACHE, exist_ok=True)

state = {"voice": "zh-CN-XiaoxiaoNeural", "rate": 0, "pitch": "medium"}

# ---- 后台 asyncio 事件循环（edge-tts 跑在这里，便于取消）----
_loop = None
_loop_lock = threading.Lock()
_active_future = None
_future_lock = threading.Lock()
_stop_flag = threading.Event()


def _ensure_loop():
    global _loop
    with _loop_lock:
        if _loop is None:
            _loop = asyncio.new_event_loop()
            t = threading.Thread(target=_loop.run_forever, daemon=True, name="edge-loop")
            t.start()
    return _loop


def _submit(coro):
    """把协程提交到后台 loop，记录为当前活动任务，并取消上一个未完成的任务。"""
    global _active_future
    _stop_flag.clear()
    loop = _ensure_loop()
    with _future_lock:
        old = _active_future
        _active_future = None
        if old is not None and not old.done():
            old.cancel()
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        _active_future = fut
    return fut


def _cancel_active():
    """打断当前正在合成的任务（stop 命令）。"""
    global _active_future
    _stop_flag.set()
    with _future_lock:
        fut = _active_future
        _active_future = None
    if fut is not None and not fut.done():
        fut.cancel()


def out(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _edge_synth(text, fname):
    """在后台 loop 上跑 edge-tts；返回文件名或抛异常/InterruptedError。"""
    import edge_tts

    rate_s = f"{state['rate'] * 10:+d}%" if state["rate"] else "+0%"
    pitch_s = {"low": "-10Hz", "medium": "+0Hz", "high": "+10Hz"}.get(state["pitch"], "+0Hz")

    async def _save():
        await edge_tts.Communicate(text, state["voice"], rate=rate_s, pitch=pitch_s).save(fname)

    fut = _submit(_save())
    try:
        fut.result()  # 阻塞当前线程，直至完成或取消
    except (asyncio.CancelledError, concurrent.futures.CancelledError):
        raise InterruptedError("interrupted by stop")
    if os.path.getsize(fname) > 0:
        return fname
    raise RuntimeError("edge-tts produced empty file")


def _sapi_synth(text, fname):
    """SAPI 兜底（同步，较快；未实现打断）。"""
    import win32com.client

    wav = os.path.splitext(fname)[0] + ".wav"
    voice = win32com.client.Dispatch("SAPI.SpVoice")
    stream = win32com.client.Dispatch("SAPI.SpFileStream")
    stream.Format.Type = 39  # 48kHz 16bit mono
    stream.Open(wav, 3)  # SSFMCreateForWrite
    voice.AudioOutputStream = stream
    voice.Rate = int(state["rate"] * 5)
    try:
        voice.Speak(text)
    finally:
        stream.Close()
    return wav


def _synth(text, fname):
    try:
        return _edge_synth(text, fname)
    except InterruptedError:
        raise
    except Exception as cause:
        # 被用户停止时不回退，直接中断
        if _stop_flag.is_set():
            raise InterruptedError("interrupted by stop")
        try:
            with open(os.path.join(BASE, "sidecar_edge.log"), "a", encoding="utf-8") as _f:
                _f.write(f"[{time.strftime('%H:%M:%S')}] voice={state['voice']} edge FAIL: {cause!r}\n")
        except Exception:
            pass
        try:
            return _sapi_synth(text, fname)
        except Exception as e2:
            raise RuntimeError(f"edge-tts: {cause!r}; sapi: {e2!r}")


def _tts(text):
    name = f"t{int(time.time() * 1000)}"
    p = _synth(text, os.path.join(CACHE, name + ".mp3"))
    return os.path.basename(p)


def _export_mp3(text):
    dl = os.path.join(os.path.expanduser("~"), "Downloads")
    os.makedirs(dl, exist_ok=True)
    fname = os.path.join(dl, "桌面朗读_" + time.strftime("%Y%m%d_%H%M%S") + ".mp3")
    return _synth(text, fname)


def _run_heavy(cmd, text, rid):
    """后台线程执行耗时合成（tts/export），完成后回写结果。"""
    try:
        if cmd == "tts":
            p = _tts(text)
            out({"id": rid, "ok": True, "mp3": p})
        elif cmd == "export":
            p = _export_mp3(text)
            out({"id": rid, "ok": True, "file": p})
    except InterruptedError:
        out({"id": rid, "ok": False, "error": "interrupted by stop"})
    except Exception as e:
        out({"id": rid, "ok": False, "error": repr(e)})


def handle(cmd, text, rid):
    # 轻量命令：主线程直接处理，立即响应
    if cmd == "stop":
        _cancel_active()
        out({"id": rid, "ok": True})
        return
    if cmd == "voice":
        state["voice"] = text
        out({"id": rid, "ok": True})
        return
    if cmd == "rate":
        try:
            state["rate"] = int(text)
        except ValueError:
            pass
        out({"id": rid, "ok": True})
        return
    if cmd == "pitch":
        state["pitch"] = text
        out({"id": rid, "ok": True})
        return

    # 耗时命令：后台线程执行，主线程继续读 stdin 以便响应 stop
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
