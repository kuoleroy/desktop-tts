# -*- coding: utf-8 -*-
"""D 组：复现用户场景（TransparencyKey 开 + 穿透 on 再 off），全程全屏抓屏
"""
import os
import sys
import time
import ctypes
import threading
from ctypes import wintypes

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.environ["EXP_MODE"] = "C"

import pet3d

OUT = os.path.join(BASE, "tools", "exp", "D")
os.makedirs(OUT, exist_ok=True)


def grab(tag):
    from PIL import ImageGrab
    img = ImageGrab.grab()
    img.save(os.path.join(OUT, f"{tag}_full.png"))
    hwnd = pet3d._hwnd()
    user32 = ctypes.windll.user32
    r = wintypes.RECT()
    user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r))
    print(f"[D] {tag} rect=({r.left},{r.top},{r.right},{r.bottom})", flush=True)


def worker():
    time.sleep(12)
    grab("D_initial")
    r = pet3d.set_click_through(os.getpid(), True)
    print(f"[D] click_through on -> {r}", flush=True)
    time.sleep(3)
    grab("D_after_on")
    r2 = pet3d.set_click_through(os.getpid(), False)
    print(f"[D] click_through off -> {r2}", flush=True)
    time.sleep(3)
    grab("D_after_off")
    time.sleep(5)
    grab("D_after_off_t20")
    print("[D] done", flush=True)
    os._exit(0)


threading.Thread(target=worker, daemon=True).start()
pet3d.main()
