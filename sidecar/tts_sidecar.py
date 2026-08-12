# -*- coding: utf-8 -*-
"""TTS Sidecar daemon：NDJSON 行协议
输入(每行): {"id": n, "cmd": "tts|stop|voice|rate|pitch", "text": "..."}
输出(每行): {"id": n, "ok": true, "mp3": "t123.mp3"} 或 {"id": n, "ok": false, "error": "..."}

约定：
- mp3/wav 产物写入 <脚本目录>/../tts_cache/（与 Rust 自定义协议 tts:// 映射一致）
- edge-tts 失败自动回退 SAPI wav
"""
import io
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(os.path.dirname(BASE), "tts_cache")
os.makedirs(CACHE, exist_ok=True)

state = {"voice": "zh-CN-XiaoxiaoNeural", "rate": 0, "pitch": "medium"}


def out(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def tts(text):
    name = f"t{int(time.time() * 1000)}"
    fname = os.path.join(CACHE, name + ".mp3")
    try:
        import edge_tts
        import asyncio
        rate_s = f"{state['rate'] * 10:+d}%" if state["rate"] else "+0%"
        pitch_s = {"low": "-10Hz", "medium": "+0Hz", "high": "+10Hz"}.get(state["pitch"], "+0Hz")
        coro = edge_tts.Communicate(text, state["voice"], rate=rate_s, pitch=pitch_s).save(fname)
        asyncio.run(coro)
        return name + ".mp3"
    except Exception as e:
        return sapi_fallback(text, name, e)


def sapi_fallback(text, name, cause):
    try:
        import win32com.client
        fname = os.path.join(CACHE, name + ".wav")
        voice = win32com.client.Dispatch("SAPI.SpVoice")
        stream = win32com.client.Dispatch("SAPI.SpFileStream")
        stream.Format.Type = 39  # 48kHz 16bit mono
        stream.Open(fname, 3)  # SSFMCreateForWrite
        voice.AudioOutputStream = stream
        voice.Rate = int(state["rate"] * 5)
        try:
            voice.Speak(text)
        finally:
            stream.Close()
        return name + ".wav"
    except Exception as e2:
        raise RuntimeError(f"edge-tts: {cause!r}; sapi: {e2!r}")


def handle(cmd, text, rid):
    if cmd == "tts":
        p = tts(text)
        out({"id": rid, "ok": True, "mp3": p})
    elif cmd == "stop":
        out({"id": rid, "ok": True})
    elif cmd == "voice":
        state["voice"] = text
        out({"id": rid, "ok": True})
    elif cmd == "rate":
        try:
            state["rate"] = int(text)
        except ValueError:
            pass
        out({"id": rid, "ok": True})
    elif cmd == "pitch":
        state["pitch"] = text
        out({"id": rid, "ok": True})
    else:
        out({"id": rid, "ok": False, "error": f"unknown cmd: {cmd}"})


def main():
    # 忽略 UTF-8 BOM 行与空行，逐行健壮解析
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