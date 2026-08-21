# -*- coding: utf-8 -*-
"""TTS 引擎模块：封装 edge-tts、SAPI、自导入 API 等合成引擎。

提供统一的 _synth() 入口，自动选择引擎并处理失败回退。
"""
import asyncio
import concurrent.futures
import json
import os
import threading
import time
import urllib.request

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

# ============================================================
# 自导入音色 API（防止 edge-tts 官方端点失效的备用引擎）。
# 配置存根目录 voice_providers.json，由前端/Rust 管理，sidecar 合成时读取。
# 合成时音色值形如 "api:<provider名>:<voice标识>"。
# ============================================================
PROVIDERS_FILE = os.path.join(os.path.dirname(BASE), "voice_providers.json")
# OpenAI 兼容接口没有「列举音色」的能力，内置常用音色作为兜底（可被配置覆盖）
OPENAI_VOICES = ["alloy", "ash", "ballad", "coral", "echo", "fable", "onyx", "nova", "sage", "shimmer"]
# 自导入 API 合成超时（秒）
API_TIMEOUT = 30


def _load_providers():
    """读取 voice_providers.json → {name: provider}。文件损坏/不存在返回 {}。"""
    try:
        with open(PROVIDERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        lst = data.get("providers") if isinstance(data, dict) else data
        return {p.get("name"): p for p in lst if isinstance(p, dict) and p.get("name")}
    except Exception:
        return {}


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
        fut.result(timeout=EDGE_TIMEOUT)
    except (asyncio.CancelledError, concurrent.futures.CancelledError):
        raise InterruptedError("interrupted by stop")
    except concurrent.futures.TimeoutError:
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
    stream.Format.Type = 39
    stream.Open(wav, 3)
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
        try:
            return _sapi_synth(text, fname, state["voice"][len("local:"):])
        except Exception as e2:
            raise RuntimeError(f"local sapi synth failed: {e2!r}")

    v = state["voice"]
    if isinstance(v, str) and v.startswith("api:"):
        parts = v.split(":", 2)
        name = parts[1] if len(parts) > 1 else ""
        api_voice = parts[2] if len(parts) > 2 else ""
        provider = _load_providers().get(name)
        if provider is None:
            raise RuntimeError(f"provider {name!r} not found in voice_providers.json")
        try:
            return _api_synth(text, fname, provider, api_voice)
        except Exception as cause:
            try:
                with open(os.path.join(BASE, "sidecar_edge.log"), "a", encoding="utf-8") as _f:
                    _f.write(f"[{time.strftime('%H:%M:%S')}] api-provider={name} voice={api_voice} FAIL: {cause!r} -> fallback local {FALLBACK_LOCAL_VOICE}\n")
            except Exception:
                pass
            try:
                return _sapi_synth(text, fname, FALLBACK_LOCAL_VOICE)
            except Exception as e2:
                raise RuntimeError(f"api({name}) {cause!r}; local sapi: {e2!r}")

    try:
        return _edge_synth(text, fname)
    except InterruptedError:
        raise
    except Exception as cause:
        if _stop_flag.is_set():
            raise InterruptedError("interrupted by stop")
        try:
            with open(os.path.join(BASE, "sidecar_edge.log"), "a", encoding="utf-8") as _f:
                _f.write(f"[{time.strftime('%H:%M:%S')}] voice={state['voice']} edge FAIL: {cause!r} -> fallback local {FALLBACK_LOCAL_VOICE}\n")
        except Exception:
            pass
        try:
            return _sapi_synth(text, fname, FALLBACK_LOCAL_VOICE)
        except Exception as e2:
            raise RuntimeError(f"edge-tts: {cause!r}; local sapi: {e2!r}")


# ---- 自导入音色 API 合成：azure（SSML）与 openai 兼容（JSON /audio/speech）----
def _http_to_file(req, fname):
    """发起 HTTP 合成请求，响应体（mp3/wav 字节）写入 fname。失败抛 RuntimeError。"""
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT) as r:
            data = r.read()
    except Exception as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace") if hasattr(e, "read") else ""
        except Exception:
            body = ""
        raise RuntimeError(f"http synth failed: {e!r} {body[:300]}")
    if not data:
        raise RuntimeError("http synth returned empty body")
    with open(fname, "wb") as f:
        f.write(data)
    return fname


def _azure_synth(text, fname, p, voice):
    region = (p.get("region") or "").strip()
    key = (p.get("key") or "").strip()
    if not region or not key:
        raise RuntimeError("azure provider requires region and key")
    url = f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"
    ssml = (
        "<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='zh-CN'>"
        f"<voice name='{voice}'>{text}</voice></speak>"
    ).encode("utf-8")
    req = urllib.request.Request(url, data=ssml, method="POST", headers={
        "Ocp-Apim-Subscription-Key": key,
        "Content-Type": "application/ssml+xml",
        "X-Microsoft-OutputFormat": "audio-24khz-160kbitrate-mono-mp3",
        "User-Agent": "desktop-tts",
    })
    return _http_to_file(req, fname)


def _openai_synth(text, fname, p, voice):
    """openai 与 custom 共用 OpenAI 兼容的 /audio/speech 请求形态。"""
    base = (p.get("base") or "").rstrip("/")
    if not base:
        raise RuntimeError("provider base url missing")
    body = json.dumps({
        "model": p.get("model") or "tts-1",
        "input": text,
        "voice": voice,
        "response_format": "mp3",
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    key = (p.get("key") or "").strip()
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(base + "/audio/speech", data=body, method="POST", headers=headers)
    return _http_to_file(req, fname)


def _api_synth(text, fname, p, voice):
    if p.get("type") == "azure":
        return _azure_synth(text, fname, p, voice)
    return _openai_synth(text, fname, p, voice)


def _fetch_api_voices(p):
    """拉取某 provider 的可用音色列表。azure 走官方列举接口；openai/custom 用内置默认。"""
    if p.get("type") == "azure":
        region = (p.get("region") or "").strip()
        key = (p.get("key") or "").strip()
        if not region or not key:
            raise RuntimeError("azure requires region and key to list voices")
        url = f"https://{region}.tts.speech.microsoft.com/cognitiveservices/voices/list"
        req = urllib.request.Request(url, headers={
            "Ocp-Apim-Subscription-Key": key, "User-Agent": "desktop-tts",
        })
        with urllib.request.urlopen(req, timeout=API_TIMEOUT) as r:
            arr = json.loads(r.read().decode("utf-8"))
        names = sorted(v.get("ShortName") for v in arr if v.get("ShortName"))
        return names
    voices = p.get("voices") or []
    return list(voices) if voices else list(OPENAI_VOICES)


def _tts(text, filter_text=None):
    """合成文本，返回缓存文件名列表。

    filter_text: 可选，在合成前对文本进行过滤（如 strip_ignored）。
    """
    base = f"t{int(time.time() * 1000)}"
    files = []
    from filters import split_blocks
    for i, block in enumerate(split_blocks(text)):
        p = _synth(block, os.path.join(CACHE, f"{base}_{i}.mp3"))
        files.append(os.path.basename(p))
    return files


def _export_mp3(text):
    dl = os.path.join(os.path.expanduser("~"), "Downloads")
    os.makedirs(dl, exist_ok=True)
    fname = os.path.join(dl, "桌面朗读_" + time.strftime("%Y%m%d_%H%M%S") + ".mp3")
    return _synth(text, fname)