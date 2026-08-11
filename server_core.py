import asyncio
import base64
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import wave

sys.path.insert(0, r'E:\open-source-research\CosyVoice')
sys.path.insert(0, r'E:\open-source-research\CosyVoice\third_party\Matcha-TTS')
os.chdir(r'E:\open-source-research\CosyVoice')

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel

MODEL_DIR = r'E:\CosyVoiceModels\CosyVoice2-0.5B'
REF_AUDIO = r'E:\IndexTTS2\refs\male_yunjian.wav'
PROMPT_TEXT = '我站在大江边上，听着涛声一阵一阵。天色已经暗了，远处的船影渐渐模糊。'
SAMPLE_RATE = 24000
MAX_BLOCK = 380
EDGE_VOICE = 'zh-CN-YunjianNeural'
EDGE_MAX_SINGLE = 2000
CACHE_DIR = os.path.join(os.environ.get('TEMP', '.'), 'browsertts_cache')
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

VOICES = {
    'yunJian': {'name': '云健·男', 'edge': 'zh-CN-YunjianNeural',
                'ref': r'E:\IndexTTS2\refs\male_yunjian.wav',
                'prompt': '我站在大江边上，听着涛声一阵一阵。天色已经暗了，远处的船影渐渐模糊。'},
    'yunYang': {'name': '云扬·男', 'edge': 'zh-CN-YunyangNeural', 'ref': None},
    'yunXia': {'name': '云夏·男', 'edge': 'zh-CN-YunxiaNeural', 'ref': None},
    'xiaoYi': {'name': '晓伊·女', 'edge': 'zh-CN-XiaoyiNeural',               'ref': r'E:\IndexTTS2\refs\female_xiaoyi_long.wav',
               'prompt': '冬夜风轻，远处的灯一盏一盏地暗了。她站在窗前，把一条围巾挽了又挽，像是等人，又像是怕人看见。这些年走过的路，她都记得。有些话不必说出口，等天亮，自然会有人懂。'},
    'xiaoXiao': {'name': '晓晓·女', 'edge': 'zh-CN-XiaoxiaoNeural', 'ref': None},
    'xiaoXuan': {'name': '晓萱·女', 'edge': 'zh-CN-XiaoxuanNeural', 'ref': None},
}


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
    ref = cfg.get('ref') or VOICES['yunJian']['ref']
    prompt = cfg.get('prompt') or VOICES['yunJian']['prompt']
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
    if req.voice not in VOICES:
        return Response(status_code=400, content='unknown voice')
    try:
        data, media = edge_audio(text[:5000], req.speed, req.voice, req.pitch)
        return Response(content=data, media_type=media)
    except Exception as e:
        print('edge failed, fallback local:', e, flush=True)
        model = get_model()
        buf = synth_to_wav(model, text[:5000], req.speed, req.voice)
        return Response(content=buf.getvalue(), media_type='audio/wav')