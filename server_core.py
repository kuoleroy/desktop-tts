import asyncio
import base64
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
import wave

APP_DIR = os.path.dirname(os.path.abspath(
    sys.executable if getattr(sys, 'frozen', False) else __file__))


def _load_env():
    env = {}
    try:
        with open(os.path.join(APP_DIR, '.env'), encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return env


_ENV = _load_env()


def env_get(key, default):
    return os.environ.get(key) or _ENV.get(key) or default


COSYVOICE_HOME = env_get('COSYVOICE_HOME', '')
if COSYVOICE_HOME and os.path.isdir(COSYVOICE_HOME):
    sys.path.insert(0, COSYVOICE_HOME)
    sys.path.insert(0, os.path.join(COSYVOICE_HOME, 'third_party', 'Matcha-TTS'))
    try:
        os.chdir(COSYVOICE_HOME)
    except OSError:
        pass

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel

MODEL_DIR = env_get('MODEL_DIR', r'E:\CosyVoiceModels\CosyVoice2-0.5B')
REF_AUDIO = env_get('REF_AUDIO', r'E:\IndexTTS2\refs\male_yunjian.wav')
PROMPT_TEXT = env_get('PROMPT_TEXT', '我站在大江边上，听着涛声一阵一阵。天色已经暗了，远处的船影渐渐模糊。')
SAMPLE_RATE = 24000
MAX_BLOCK = 380
EDGE_VOICE = 'zh-CN-YunjianNeural'
EDGE_MAX_SINGLE = env_get('EDGE_MAX_SINGLE', '2000')
try:
    EDGE_MAX_SINGLE = int(EDGE_MAX_SINGLE)
except (TypeError, ValueError):
    EDGE_MAX_SINGLE = 2000
CACHE_DIR = os.path.join(APP_DIR, 'cache')
CONFIG_PATH = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')),
                           'desktop-tts', 'config.json')


def get_cache_limit_mb():
    try:
        with open(CONFIG_PATH, encoding='utf-8') as f:
            return int(json.load(f).get('cache_limit', 500))
    except Exception:
        return 500


def clean_cache():
    limit_mb = get_cache_limit_mb()
    if limit_mb <= 0:
        return
    try:
        files, total = [], 0
        for name in os.listdir(CACHE_DIR):
            p = os.path.join(CACHE_DIR, name)
            try:
                sz = os.path.getsize(p)
                total += sz
                files.append((os.path.getmtime(p), p, sz))
            except OSError:
                pass
        limit = limit_mb * 1024 * 1024
        if total <= limit:
            return
        files.sort()
        for _, p, sz in files:
            if total <= limit:
                break
            try:
                os.remove(p)
                total -= sz
            except OSError:
                pass
    except OSError:
        pass

def _load_voices():
    fallback = {
        'yunJian': {'name': '云健·男', 'edge': 'zh-CN-YunjianNeural', 'ref': None},
        'yunYang': {'name': '云扬·男', 'edge': 'zh-CN-YunyangNeural', 'ref': None},
        'yunXia': {'name': '云夏·男', 'edge': 'zh-CN-YunxiaNeural', 'ref': None},
        'xiaoYi': {'name': '晓伊·女', 'edge': 'zh-CN-XiaoyiNeural', 'ref': None},
        'xiaoXiao': {'name': '晓晓·女', 'edge': 'zh-CN-XiaoxiaoNeural', 'ref': None},
        'xiaoXuan': {'name': '晓萱·女', 'edge': 'zh-CN-XiaoxuanNeural', 'ref': None},
    }
    try:
        with open(os.path.join(APP_DIR, 'voices.json'), encoding='utf-8') as f:
            data = json.load(f)
        out = {}
        for vid, cfg in data.get('voices', {}).items():
            out[vid] = {'name': cfg.get('name', vid), 'edge': cfg.get('edge', ''),
                        'ref': cfg.get('ref'), 'prompt': cfg.get('prompt')}
        if out:
            return out
    except Exception:
        pass
    return fallback


def _load_sapi_voices():
    try:
        with open(os.path.join(APP_DIR, 'voices.json'), encoding='utf-8') as f:
            data = json.load(f)
        out = {}
        for vid, cfg in data.get('sapi_voices', {}).items():
            if cfg.get('name') and cfg.get('sapi'):
                out[vid] = {'name': cfg['name'], 'sapi': cfg['sapi']}
        if out:
            return out
    except Exception:
        pass
    return {'xiaoXiaoLocal': {'name': '晓晓·本地', 'sapi': 'Microsoft Xiaoxiao (Natural)'},
            'yunXiLocal': {'name': '云希·本地', 'sapi': 'Microsoft Yunxi (Natural)'}}


VOICES = _load_voices()
SAPI_VOICES = _load_sapi_voices()
SAPI_PS1 = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sapi_proc.ps1')


def cache_get(text, speed, voice, pitch):
    key = hashlib.md5(('%s|%s|%s|%s' % (text, speed, voice, pitch)).encode('utf-8')).hexdigest()
    path = os.path.join(CACHE_DIR, key + '.pkl')
    if os.path.exists(path):
        with open(path, 'rb') as f:
            return f.read()
    return None


def cache_set(text, speed, voice, pitch, payload):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        key = hashlib.md5(('%s|%s|%s|%s' % (text, speed, voice, pitch)).encode('utf-8')).hexdigest()
        with open(os.path.join(CACHE_DIR, key + '.pkl'), 'wb') as f:
            f.write(payload)
        clean_cache()
    except OSError:
        pass

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['*'], allow_headers=['*'])

_model = None


def get_model():
    global _model
    if _model is None:
        from cosyvoice.cli.cosyvoice import AutoModel
        _model = AutoModel(model_dir=MODEL_DIR, fp16=True)
        print('model loaded, sample_rate:', _model.sample_rate, flush=True)
    return _model


def split_blocks(text, max_len=MAX_BLOCK):
    text = re.sub(r'\s+', ' ', text).strip()
    if len(text) <= max_len:
        return [text]
    blocks, cur = [], ''
    for sent in re.split(r'(?<=[。！？；])', text):
        if not sent:
            continue
        cur += sent
        if len(cur) >= max_len:
            blocks.append(cur)
            cur = ''
    if cur:
        blocks.append(cur)
    return blocks


def synth_to_wav(model, text, speed, voice):
    cfg = VOICES[voice]
    ref = cfg.get('ref') or REF_AUDIO
    prompt = cfg.get('prompt') or PROMPT_TEXT
    buf = io.BytesIO()
    wf = wave.open(buf, 'wb')
    wf.setnchannels(1)
    wf.setsampwidth(2)
    wf.setframerate(SAMPLE_RATE)
    for b in split_blocks(text):
        for r in model.inference_zero_shot(b, prompt, ref,
                                           stream=False, text_frontend=False,
                                           speed=speed):
            wf.writeframes((r['tts_speech'].numpy().flatten() * 32767)
                           .clip(-32768, 32767).astype('<i2').tobytes())
    wf.close()
    return buf


PYTHON_EXE = r'D:\Programs\miniconda3\envs\cosyvoice\python.exe'
CREATE_NO_WINDOW = 0x08000000


def edge_mp3(text, speed, voice, pitch):
    rate = f'{(speed - 1) * 100:+.0f}%'
    p = subprocess.run([PYTHON_EXE, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                 'edge_proc.py'),
                        base64.b64encode(text.encode('utf-8')).decode('ascii'), rate,
                        VOICES[voice]['edge'], pitch],
                       capture_output=True, timeout=90, creationflags=CREATE_NO_WINDOW)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode('utf-8', 'replace').strip() or 'edge proc failed')
    return p.stdout


def mp3_to_pcm(mp3):
    p = subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', 'pipe:0',
                        '-ar', '24000', '-ac', '1', '-f', 's16le', 'pipe:1'],
                       input=mp3, capture_output=True, creationflags=CREATE_NO_WINDOW)
    if p.returncode != 0:
        raise RuntimeError('ffmpeg decode failed')
    return p.stdout


def pcm_to_wav(pcm):
    buf = io.BytesIO()
    wf = wave.open(buf, 'wb')
    wf.setnchannels(1)
    wf.setsampwidth(2)
    wf.setframerate(SAMPLE_RATE)
    wf.writeframes(pcm)
    wf.close()
    return buf


def edge_audio(text, speed, voice, pitch):
    cached = cache_get(text, speed, voice, pitch)
    if cached is not None:
        media, data = cached.split(b'\x00', 1)
        return data, media.decode('ascii')
    if len(text) <= EDGE_MAX_SINGLE:
        data, media = edge_mp3(text, speed, voice, pitch), 'audio/mpeg'
    else:
        blocks = [b for b in split_blocks(text, EDGE_MAX_SINGLE) if b]
        pcm = b''.join(mp3_to_pcm(edge_mp3(b, speed, voice, pitch)) for b in blocks)
        data, media = pcm_to_wav(pcm).getvalue(), 'audio/wav'
    cache_set(text, speed, voice, pitch, media.encode('ascii') + b'\x00' + data)
    return data, media


def sapi_audio(text, voice, speed):
    import tempfile
    t = os.path.join(tempfile.gettempdir(), 'dtts_in_%d.txt' % time.time_ns())
    o = t[:-4] + '.wav'
    try:
        with open(t, 'wb') as f:
            f.write(b'\xef\xbb\xbf' + text.encode('utf-8'))
        rate = max(-10, min(10, int(round((speed - 1.0) * 20))))
        p = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                            '-File', SAPI_PS1, t, o, SAPI_VOICES[voice]['sapi'],
                            str(rate)],
                           capture_output=True, timeout=120,
                           creationflags=CREATE_NO_WINDOW)
        if p.returncode != 0 or not os.path.exists(o):
            raise RuntimeError('sapi failed: %s' %
                               p.stderr.decode('utf-8', 'replace')[:200])
        with open(o, 'rb') as f:
            return f.read()
    finally:
        for x in (t, o):
            try:
                os.remove(x)
            except OSError:
                pass


def sapi_audio_cached(text, speed, voice):
    cached = cache_get(text, speed, voice, '+0Hz')
    if cached is not None:
        return cached.split(b'\x00', 1)[1]
    data = sapi_audio(text, voice, speed)
    cache_set(text, speed, voice, '+0Hz', b'audio/wav\x00' + data)
    return data


class TTSReq(BaseModel):
    text: str
    speed: float = 0.9
    voice: str = 'yunJian'
    pitch: str = '+0Hz'


@app.get('/health')
def health():
    edge_ok = True
    try:
        import edge_tts  # noqa
    except ImportError:
        edge_ok = False
    return {'ok': True, 'model': 'CosyVoice2-0.5B', 'voice': 'yunJian', 'edge': edge_ok}


@app.post('/tts')
def tts(req: TTSReq):
    text = req.text.strip()
    if not text:
        return Response(status_code=400)
    if req.voice not in VOICES and req.voice not in SAPI_VOICES:
        return Response(status_code=400, content='unknown voice')
    if req.voice in SAPI_VOICES:
        try:
            data = sapi_audio_cached(text[:5000], req.speed, req.voice)
            return Response(content=data, media_type='audio/wav')
        except Exception as e:
            print('sapi failed:', e, flush=True)
            return Response(status_code=500, content=str(e))
    try:
        data, media = edge_audio(text[:5000], req.speed, req.voice, req.pitch)
        return Response(content=data, media_type=media)
    except Exception as e:
        print('edge failed, fallback local:', e, flush=True)
        model = get_model()
        buf = synth_to_wav(model, text[:5000], req.speed, req.voice)
        return Response(content=buf.getvalue(), media_type='audio/wav')