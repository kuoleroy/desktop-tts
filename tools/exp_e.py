# -*- coding: utf-8 -*-
"""E 组：穿透 on -> off -> on 循环，验证模型是否恢复（用户方案的循环可行性）
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

OUT = os.path.join(BASE, "tools", "exp", "E")
os.makedirs(OUT, exist_ok=True)


def grab(tag):
    from PIL import ImageGrab
    img = ImageGrab.grab()
    img.save(os.path.join(OUT, f"{tag}_full.png"))
    hwnd = pet3d._hwnd()
    user32 = ctypes.windll.user32
    r = wintypes.RECT()
    user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r))
    print(f"[E] {tag} rect=({r.left},{r.top},{r.right},{r.bottom})", flush=True)


def worker():
    time.sleep(12)
    grab("E_initial")
    r = pet3d.set_click_through(os.getpid(), True)
    print(f"[E] on -> {r}", flush=True)
    time.sleep(3)
    grab("E_after_on1")
    r2 = pet3d.set_click_through(os.getpid(), False)
    print(f"[E] off -> {r2}", flush=True)
    time.sleep(3)
    grab("E_after_off")
    r3 = pet3d.set_click_through(os.getpid(), True)
    print(f"[E] on2 -> {r3}", flush=True)
    time.sleep(3)
    grab("E_after_on2")
    time.sleep(5)
    grab("E_after_on2_t20")
    print("[E] done", flush=True)
    os._exit(0)


threading.Thread(target=worker, daemon=True).start()
pet3d.main()
