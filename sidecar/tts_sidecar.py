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

# edge-tts 单块合成超时（秒）：断网/网络异常时快速放弃，避免 UI 长时间卡住
EDGE_TIMEOUT = 15
# edge-tts 失败（断网/超时）时自动回退的本地自然音（离线，Windows 神经语音）
FALLBACK_LOCAL_VOICE = "Microsoft Xiaoxiao (Natural)"

# ---- 配置持久化：音色/语速/语调存 settings.json，重启不丢 ----
SETTINGS_FILE = os.path.join(os.path.dirname(BASE), "settings.json")
DEFAULT_STATE = {"voice": "zh-CN-XiaoxiaoNeural", "rate": 0, "pitch": "medium"}
state = dict(DEFAULT_STATE)


def _load_settings():
    global state
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            state = dict(DEFAULT_STATE)
            for k in ("voice", "rate", "pitch"):
                if k in data and isinstance(data[k], (str, int, float)):
                    state[k] = data[k]
    except Exception:
        pass


def _save_settings():
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


_load_settings()

# ---- 后台 asyncio 事件循环（edge-tts 跑在这里，便于取消）----
_loop = None
_loop_lock = threading.Lock()
_active_future = None
_future_lock = threading.Lock()
_stop_flag = threading.Event()

# 全局 stdout 写锁（主线程与抓取线程并发写时避免交错）
_STDOUT_LOCK = threading.Lock()


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
    with _STDOUT_LOCK:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def _edge_synth(text, fname):
    """在后台 loop 上跑 edge-tts；返回文件名或抛异常/InterruptedError。

    加了超时上限（EDGE_TIMEOUT 秒/块）：断网/网络异常时 edge-tts 会一直挂着，
    超时即取消并抛 TimeoutError，由上层快速回退 SAPI，避免 UI 长时间卡住。
    """
    import edge_tts

    rate_s = f"{state['rate'] * 10:+d}%" if state["rate"] else "+0%"
    pitch_s = {"low": "-10Hz", "medium": "+0Hz", "high": "+10Hz"}.get(state["pitch"], "+0Hz")

    async def _save():
        await edge_tts.Communicate(text, state["voice"], rate=rate_s, pitch=pitch_s).save(fname)

    fut = _submit(_save())
    try:
        fut.result(timeout=EDGE_TIMEOUT)  # 阻塞当前线程，超时即放弃
    except (asyncio.CancelledError, concurrent.futures.CancelledError):
        raise InterruptedError("interrupted by stop")
    except concurrent.futures.TimeoutError:
        # 超时：取消后台任务，避免残留，然后回退 SAPI
        with _future_lock:
            if _active_future is not None and not _active_future.done():
                _active_future.cancel()
        raise RuntimeError("edge-tts timeout (network?)")
    if os.path.getsize(fname) > 0:
        return fname
    raise RuntimeError("edge-tts produced empty file")


def _sapi_synth(text, fname, voice_name=None):
    """SAPI 合成（同步，较快；未实现打断）。

    voice_name：可选，形如 "Microsoft Xiaoxiao (Natural)"，会从系统已安装语音里
    模糊匹配（忽略大小写、括号差异）；缺省用系统默认语音。
    """
    import win32com.client

    wav = os.path.splitext(fname)[0] + ".wav"
    voice = win32com.client.Dispatch("SAPI.SpVoice")
    stream = win32com.client.Dispatch("SAPI.SpFileStream")
    stream.Format.Type = 39  # 48kHz 16bit mono
    stream.Open(wav, 3)  # SSFMCreateForWrite
    voice.AudioOutputStream = stream
    voice.Rate = int(state["rate"] * 5)
    try:
        if voice_name:
            want = voice_name.lower().replace("(", "").replace(")", "")
            for v in voice.GetVoices():
                desc = v.GetDescription().lower().replace("(", "").replace(")", "")
                if want and (want in desc or desc in want):
                    voice.Voice = v
                    break
        voice.Speak(text)
    finally:
        stream.Close()
    return wav


def _is_local_voice(v):
    """本地音色：以 'local:' 前缀标识（面板传来的 value 形如 'local:Microsoft Xiaoxiao (Natural)'）。"""
    return isinstance(v, str) and v.startswith("local:")


def _synth(text, fname):
    if _is_local_voice(state["voice"]):
        # 本地自然音色：直接走 SAPI 指定语音，不经过 edge（离线、无网络依赖）
        try:
            return _sapi_synth(text, fname, state["voice"][len("local:"):])
        except Exception as e2:
            raise RuntimeError(f"local sapi synth failed: {e2!r}")

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
                _f.write(f"[{time.strftime('%H:%M:%S')}] voice={state['voice']} edge FAIL: {cause!r} -> fallback local {FALLBACK_LOCAL_VOICE}\n")
        except Exception:
            pass
        # 断网/失败：自动切到本地自然音（离线，Windows 神经语音），而非系统默认音
        try:
            return _sapi_synth(text, fname, FALLBACK_LOCAL_VOICE)
        except Exception as e2:
            raise RuntimeError(f"edge-tts: {cause!r}; local sapi: {e2!r}")


# edge-tts 单次合成上限（字）。超长文本切成多块，每块独立合成、依次播放
MAX_BLOCK = 2000


def split_blocks(text, max_len=MAX_BLOCK):
    """把长文本切成 max_len 字以内的若干块（保留段落边界，避免切半句话）。"""
    if len(text) <= max_len:
        return [text]
    blocks = []
    cur = ""
    # 按换行优先切分，保持段落完整；否则按最大长度硬切
    for para in text.split("\n"):
        if not para:
            continue
        if cur and len(cur) + 1 + len(para) <= max_len:
            cur += "\n" + para
            continue
        if cur:
            blocks.append(cur)
            cur = ""
        # 单个段落仍超长时，按字符硬切
        while len(para) > max_len:
            blocks.append(para[:max_len])
            para = para[max_len:]
        cur = para
    if cur:
        blocks.append(cur)
    return [b for b in blocks if b.strip()]


def _tts(text):
    base = f"t{int(time.time() * 1000)}"
    files = []
    for i, block in enumerate(split_blocks(text)):
        p = _synth(block, os.path.join(CACHE, f"{base}_{i}.mp3"))
        files.append(os.path.basename(p))
    return files


def _export_mp3(text):
    dl = os.path.join(os.path.expanduser("~"), "Downloads")
    os.makedirs(dl, exist_ok=True)
    fname = os.path.join(dl, "桌面朗读_" + time.strftime("%Y%m%d_%H%M%S") + ".mp3")
    return _synth(text, fname)


def _run_heavy(cmd, text, rid):
    """后台线程执行耗时合成（tts/export），完成后回写结果。"""
    try:
        if cmd == "tts":
            files = _tts(text)
            out({"id": rid, "ok": True, "files": files})
        elif cmd == "export":
            p = _export_mp3(text)
            out({"id": rid, "ok": True, "file": p})
    except InterruptedError:
        out({"id": rid, "ok": False, "error": "interrupted by stop"})
    except Exception as e:
        out({"id": rid, "ok": False, "error": repr(e)})


def handle(cmd, text, rid):
    # 轻量命令：主线程直接处理，立即响应
    if cmd == "settings":
        out({"id": rid, "ok": True, "settings": {
            "voice": state["voice"], "rate": state["rate"], "pitch": state["pitch"],
        }})
        return
    if cmd == "stop":
        _cancel_active()
        out({"id": rid, "ok": True})
        return
    if cmd == "voice":
        state["voice"] = text
        _save_settings()
        out({"id": rid, "ok": True})
        return
    if cmd == "rate":
        try:
            state["rate"] = int(text)
        except ValueError:
            pass
        _save_settings()
        out({"id": rid, "ok": True})
        return
    if cmd == "pitch":
        state["pitch"] = text
        _save_settings()
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
