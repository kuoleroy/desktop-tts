# -*- coding: utf-8 -*-
"""端到端验证：观赏/交互模式切换循环（模拟热键 toggle_mode 路径）
watch: 模型可见(特征色>0) + 穿透True
interact: 模型隐藏(opacity 0 -> 特征色≈0) + 穿透False
再 watch: 模型恢复(实验E验证)
"""
import os
import sys
import time
import ctypes
import threading
from ctypes import wintypes

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import pet3d

OUT = os.path.join(BASE, "tools", "exp", "F")
os.makedirs(OUT, exist_ok=True)

skin = {(221, 208, 205), (220, 207, 204), (222, 209, 206), (219, 206, 203)}
dress = {(202, 212, 224), (203, 213, 225), (201, 211, 223), (202, 211, 221), (204, 212, 221)}
eye = {(124, 172, 208), (125, 173, 209), (128, 175, 208), (124, 171, 208)}


def features():
    from PIL import ImageGrab
    img = ImageGrab.grab().convert("RGB")
    w, h = img.size
    px = img.load()
    n = 0
    for y in range(0, h, 2):
        for x in range(0, w, 2):
            c = px[x, y]
            if c in skin or c in dress or c in eye:
                n += 1
    return n


def toggle_sim():
    """模拟热键路径：穿透切换 + JS setMode（与 Api.toggle_mode 等价）"""
    import webview
    cur = pet3d.get_click_through(os.getpid())
    on = not cur if cur is not None else True
    pet3d.set_click_through(os.getpid(), on)
    mode = "watch" if on else "interact"
    webview.windows[0].evaluate_js(f"setMode('{mode}')")
    return mode


def worker():
    import webview
    time.sleep(13)
    n0 = features()
    print(f"[F] watch: features={n0} through={pet3d.get_click_through(os.getpid())}", flush=True)
    m1 = toggle_sim()
    time.sleep(4)
    n1 = features()
    print(f"[F] after toggle1 ({m1}): features={n1} through={pet3d.get_click_through(os.getpid())}", flush=True)
    m2 = toggle_sim()
    time.sleep(4)
    n2 = features()
    print(f"[F] after toggle2 ({m2}): features={n2} through={pet3d.get_click_through(os.getpid())}", flush=True)
    ok = (n0 > 300) and (n1 <= n0 * 0.2) and (n2 > 300)
    print(f"[F] RESULT: {'PASS' if ok else 'FAIL'}", flush=True)
    os._exit(0 if ok else 1)


threading.Thread(target=worker, daemon=True).start()
pet3d.main()
