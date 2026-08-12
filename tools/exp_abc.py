# -*- coding: utf-8 -*-
"""A/B/C 三组对照实验：钉死模型消失的元凶
A: transparent+frameless，不设 TransparencyKey，不 patch
B: A + 启动后一次 set_click_through(True)（单向常开穿透）
C: A + TransparencyKey=240（当前生产默认）
每组：窗口区域抓屏 PNG + diag.json 快照
用法: python tools/exp_abc.py A|B|C
"""
import os
import sys
import time
import shutil
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
    return (r.left, r.top, r.right, r.bottom)


def grab(tag):
    from PIL import ImageGrab
    bbox = _rect()
    img = ImageGrab.grab(bbox=bbox)
    p = os.path.join(OUT, f"{tag}.png")
    img.save(p)
    src = os.path.join(BASE, "diag.json")
    if os.path.exists(src):
        shutil.copy(src, os.path.join(OUT, f"{tag}.diag.json"))
    print(f"[{MODE}] saved {p} rect={bbox}", flush=True)


def worker():
    if MODE == "A":
        time.sleep(12)
        grab("A_initial")
        time.sleep(6)
        grab("A_t18")
    elif MODE == "B":
        time.sleep(12)
        grab("B_before")
        r = pet3d.set_click_through(os.getpid(), True)
        print(f"[B] click_through -> {r}", flush=True)
        time.sleep(3)
        grab("B_after_once_on")
        time.sleep(5)
        grab("B_after_t20")
    else:  # C
        time.sleep(12)
        grab("C_initial")
        time.sleep(6)
        grab("C_t18")
    print(f"[{MODE}] done, exiting", flush=True)
    os._exit(0)


threading.Thread(target=worker, daemon=True).start()
pet3d.main()
