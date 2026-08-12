# -*- coding: utf-8 -*-
"""A/C 组全屏版重跑：全屏抓屏 + 窗口矩形标注，与 B 组同方法对比
用法: python tools\exp_ac_full.py A|C
"""
import os
import sys
import time
import ctypes
import threading
from ctypes import wintypes

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
MODE = sys.argv[1] if len(sys.argv) > 1 else "A"
os.environ["EXP_MODE"] = MODE

import pet3d

OUT = os.path.join(BASE, "tools", "exp", MODE)
os.makedirs(OUT, exist_ok=True)


def _rect():
    hwnd = pet3d._hwnd()
    user32 = ctypes.windll.user32
    r = wintypes.RECT()
    user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r))
    return hwnd, (r.left, r.top, r.right, r.bottom)


def grab(tag, mark=True):
    from PIL import Image, ImageDraw, ImageGrab
    hwnd, bbox = _rect()
    print(f"[{MODE}] hwnd={hwnd} rect={bbox}", flush=True)
    img = ImageGrab.grab()
    img.save(os.path.join(OUT, f"{tag}_full.png"))
    if mark:
        d = ImageDraw.Draw(img)
        d.rectangle(bbox, outline=(255, 0, 0), width=4)
    img.save(os.path.join(OUT, f"{tag}.png"))


def worker():
    time.sleep(12)
    grab(f"{MODE}_initial")
    time.sleep(5)
    grab(f"{MODE}_t17")
    print(f"[{MODE}] done", flush=True)
    os._exit(0)


threading.Thread(target=worker, daemon=True).start()
pet3d.main()
