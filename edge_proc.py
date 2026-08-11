import asyncio
import base64
import os
import sys


def main():
    text_b64 = sys.argv[1]
    rate = sys.argv[2] if len(sys.argv) > 2 else '0%'
    voice = sys.argv[3] if len(sys.argv) > 3 else 'zh-CN-YunjianNeural'
    pitch = sys.argv[4] if len(sys.argv) > 4 else '+0Hz'
    text = base64.b64decode(text_b64).decode('utf-8')

    async def _run():
        from edge_tts import Communicate
        com = Communicate(text, voice, rate=rate, pitch=pitch)
        mp3 = b''
        async for c in com.stream():
            if c['type'] == 'audio':
                mp3 += c['data']
        return mp3

    try:
        mp3 = asyncio.run(_run())
        if not mp3:
            os.write(2, b'ERR:no audio')
            sys.exit(1)
        os.write(1, mp3)
    except Exception as e:
        os.write(2, ('ERR:%s' % e).encode('utf-8', 'replace'))
        sys.exit(1)


if __name__ == '__main__':
    main()