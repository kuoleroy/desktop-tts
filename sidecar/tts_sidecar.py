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
import signal
import sys
import threading
import time
import urllib.request

# 后台工作进程：生命周期由 Rust 管理（stdin 指令 + 看门狗），无需响应 Ctrl+C。
# 忽略 SIGINT，避免终端 Ctrl+C 把进程杀掉。
signal.signal(signal.SIGINT, signal.SIG_IGN)

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

    v = state["voice"]
    if isinstance(v, str) and v.startswith("api:"):
        # 自导入音色 API：voice 形如 "api:provider名:voice标识"
        parts = v.split(":", 2)
        name = parts[1] if len(parts) > 1 else ""
        api_voice = parts[2] if len(parts) > 2 else ""
        provider = _load_providers().get(name)
        if provider is None:
            raise RuntimeError(f"provider {name!r} not found in voice_providers.json")
        try:
            return _api_synth(text, fname, provider, api_voice)
        except Exception as cause:
            # 自导入 API 失败：记录后自动回退本地自然音，保证朗读不中断
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


# edge-tts 单次合成上限（字）。超长文本切成多块，每块独立合成、依次播放
MAX_BLOCK = 500

# 朗读时忽略的成对符号（含包裹内容不读）。key=左符号, value=右符号。
# 默认：方括号、花括号、中文括号、英文括号、书名号、尖括号。
DEFAULT_IGNORE_PAIRS = {
    "[": "]", "{": "}", "【": "】", "（": "）", "(": ")", "《": "》", "<": ">",
}
# 面板设置存 settings_app.json（Rust 写入），sidecar 每次合成时实时读取开关与自定义符号对
APP_SETTINGS_FILE = os.path.join(os.path.dirname(BASE), "sidecar", "settings_app.json")


def _ignore_config():
    """读取 settings_app.json 的 ignore_pairs 开关与 ignore_symbols 自定义符号对。

    返回 (enabled, pairs)。enabled 默认 True；pairs 为 {左符号: 右符号}，
    用户未配置时用内置默认。
    """
    try:
        with open(APP_SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        enabled = bool(data.get("ignore_pairs", True))
        syms = data.get("ignore_symbols")
        if isinstance(syms, list) and syms:
            pairs = {}
            for s in syms:
                if isinstance(s, str) and len(s) >= 2:
                    pairs[s[0]] = s[-1]
            if pairs:
                return enabled, pairs
        return enabled, dict(DEFAULT_IGNORE_PAIRS)
    except Exception:
        return True, dict(DEFAULT_IGNORE_PAIRS)


def strip_ignored(text):
    """删除成对符号包裹的内容（含符号本身），用于跳过注释/编者注等不朗读的片段。

    只处理左右符号正确配对且不嵌套的片段；未闭合的符号保留原文。
    """
    enabled, pairs = _ignore_config()
    if not enabled or not text or not pairs:
        return text
    out = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in pairs:
            end = pairs[ch]
            j = text.find(end, i + 1)
            if j != -1:
                i = j + 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


# 朗读时跳过的孤立符号（Markdown 标记等）。只删符号本身，保留其中文字。
DEFAULT_STRIP_SYMBOLS = set("*~`#>|_-")
STRIP_SYMBOLS_FILE = os.path.join(os.path.dirname(BASE), "sidecar", "settings_app.json")


def _strip_symbols_config():
    """读取 settings_app.json 的 strip_symbols 开关与自定义符号集合。"""
    try:
        with open(STRIP_SYMBOLS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        enabled = bool(data.get("strip_symbols", True))
        custom = data.get("strip_symbol_chars")
        if isinstance(custom, str) and custom:
            return enabled, set(custom)
        return enabled, set(DEFAULT_STRIP_SYMBOLS)
    except Exception:
        return True, set(DEFAULT_STRIP_SYMBOLS)


def strip_symbols(text):
    """删除 Markdown 标记符号（不删文字），用于让 TTS 不念出星号/井号等噪音。"""
    enabled, syms = _strip_symbols_config()
    if not enabled or not text or not syms:
        return text
    return "".join(ch for ch in text if ch not in syms)


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
        text = strip_ignored(text)
        text = strip_symbols(text)
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


def _fetch_voices_heavy(name, rid):
    """后台线程拉取 provider 音色列表（网络可能较慢），完成后回写 voices。"""
    try:
        p = _load_providers().get(name)
        if p is None:
            raise RuntimeError(f"provider {name!r} not found")
        voices = _fetch_api_voices(p)
        out({"id": rid, "ok": True, "voices": voices})
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
    if cmd == "fetch-voices":
        # 拉取某自导入 provider 的音色列表（text = provider 名）
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
