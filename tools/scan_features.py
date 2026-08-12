# -*- coding: utf-8 -*-
"""全局扫描：在整屏找模型特征色的聚集区域（肤色/白裙/蓝眼），并报告窗口 rect 位置"""
import sys
from collections import Counter
from PIL import Image

path = sys.argv[1]
skin = {(221, 208, 205), (220, 207, 204), (222, 209, 206), (219, 206, 203)}
dress = {(202, 212, 224), (203, 213, 225), (201, 211, 223), (202, 211, 221), (204, 212, 221)}
eye = {(124, 172, 208), (125, 173, 209), (128, 175, 208), (124, 171, 208)}
img = Image.open(path).convert("RGB")
w, h = img.size
px = img.load()
buckets = Counter()
for y in range(0, h):
    for x in range(0, w):
        c = px[x, y]
        if c in skin:
            buckets["skin"] += 1
        elif c in dress:
            buckets["dress"] += 1
        elif c in eye:
            buckets["eye"] += 1
print(f"=== {path} ({w}x{h}) feature scan ===")
for k, v in buckets.most_common():
    print(f"  {k}: {v} px")
total = sum(buckets.values())
print(f"  total model-ish px: {total}")
