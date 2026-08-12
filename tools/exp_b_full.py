# -*- coding: utf-8 -*-
"""B 组复现：全屏抓屏 + 窗口矩形标注，确认窗口真实位置与内容"""
import os
import sys
import time
import ctypes
import threading
from ctypes import wintypes

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.environ["EXP_MODE"] = "A"

import pet3d

OUT = os.path.join(BASE, "tools", "exp", "B")
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
    print(f"[B] hwnd={hwnd} rect={bbox}", flush=True)
    img = ImageGrab.grab()
    img.save(os.path.join(OUT, f"{tag}_full.png"))
    if mark:
        d = ImageDraw.Draw(img)
        d.rectangle(bbox, outline=(255, 0, 0), width=4)
    img.save(os.path.join(OUT, f"{tag}.png"))


def worker():
    time.sleep(12)
    grab("B_before")
    r = pet3d.set_click_through(os.getpid(), True)
    print(f"[B] click_through -> {r}", flush=True)
    time.sleep(3)
    grab("B_after_once_on")
    time.sleep(3)
    grab("B_after_t18")
    print("[B] done", flush=True)
    os._exit(0)


threading.Thread(target=worker, daemon=True).start()
pet3d.main()
