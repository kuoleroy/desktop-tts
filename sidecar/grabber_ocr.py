# -*- coding: utf-8 -*-
"""抓取进程 OCR 模块：基于 RapidOCR 的屏幕文字识别。

提供惰性加载的 OCR 引擎、选区截图、聚焦框等工具函数。
"""
import ctypes
import threading
from grabber_utils import dbg


_ocr_engine = {"engine": None, "lock": threading.Lock()}


def _get_ocr_engine():
    if _ocr_engine["engine"] is None:
        with _ocr_engine["lock"]:
            if _ocr_engine["engine"] is None:
                try:
                    from rapidocr_onnxruntime import RapidOCR
                    _ocr_engine["engine"] = RapidOCR()
                except BaseException as e:
                    dbg("  [ocr] engine load failed: %r" % e)
                    _ocr_engine["engine"] = False
    return _ocr_engine["engine"]


def _ocr_capture(rect):
    """rect=(l,t,r,b) 屏幕坐标。返回识别文本；依赖缺失/失败返回 ''。"""
    try:
        from PIL import Image, ImageGrab
    except BaseException as e:
        dbg("  [ocr] imports failed: %r" % e)
        return ''
    try:
        l, t, r, b = (int(v) for v in rect)
        if r <= l or b <= t:
            return ''
        img = ImageGrab.grab(bbox=(l, t, r, b))
        if img is None or img.width < 4 or img.height < 4:
            return ''
        img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
        import numpy as np
        arr = np.array(img.convert("RGB"))
        engine = _get_ocr_engine()
        if not engine:
            return ''
        res, _ = engine(arr)
        if not res:
            dbg("  [ocr] rect=%s recognized nothing" % (rect,))
            return ''
        text = "\n".join(line[1] for line in res)
        text = ' '.join(text.split())
        dbg("  [ocr] rect=%s got_len=%d" % (rect, len(text)))
        return text
    except BaseException as e:
        dbg("  [ocr] failed: %r" % e)
        return ''


def _element_rect(el):
    """取 UIA 元素的屏幕包围盒 (l,t,r,b)；失败返回 None。"""
    try:
        r = el.CurrentBoundingRectangle
        if hasattr(r, "left"):
            return (int(r.left), int(r.top), int(r.right), int(r.bottom))
        if isinstance(r, (tuple, list)) and len(r) >= 4:
            return (int(r[0]), int(r[1]), int(r[2]), int(r[3]))
    except Exception:
        pass
    return None


def _element_anchor(el, fallback):
    """取选区包围盒下方作为悬浮框位置；失败用 fallback（鼠标位置）。"""
    try:
        r = el.CurrentBoundingRectangle
        if isinstance(r, (tuple, list)) and len(r) >= 4:
            return (int(r[0]) + 8, int(r[3]) + 12)
        if hasattr(r, "left"):
            return (int(r.left) + 8, int(r.bottom) + 12)
    except Exception:
        pass
    return fallback


def _focus_ocr_rect(rect):
    """将 OCR 区域聚焦到鼠标位置附近，避免对整窗口/整屏识别不准。"""
    if not rect:
        return None
    l, t, r, b = rect
    w, h = r - l, b - t
    if 80 <= w <= 900 and 40 <= h <= 350:
        return (l, t, r, b)
    from grabber_utils import _cursor_pos
    cx, cy = _cursor_pos()
    FOCUS_W, FOCUS_H = 520, 220
    nl = cx - FOCUS_W // 2
    nt = cy - FOCUS_H // 2
    nr = cx + FOCUS_W // 2
    nb = cy + FOCUS_H // 2
    try:
        from ctypes import windll
        sw = windll.user32.GetSystemMetrics(0)
        sh = windll.user32.GetSystemMetrics(1)
    except Exception:
        sw, sh = 2560, 1440
    nl = max(nl, 0); nt = max(nt, 0)
    nr = min(nr, sw); nb = min(nb, sh)
    if nr <= nl or nb <= nt:
        nl = max(cx - 260, 0); nt = max(cy - 110, 0)
        nr = min(cx + 260, sw); nb = min(cy + 110, sh)
        if nr <= nl or nb <= nt:
            return (l, t, r, b)
    return (nl, nt, nr, nb)