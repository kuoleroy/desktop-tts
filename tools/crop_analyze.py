# -*- coding: utf-8 -*-
"""裁剪全屏图中指定矩形区域并做颜色分析"""
import sys
from collections import Counter
from PIL import Image

path, rect = sys.argv[1], eval(sys.argv[2])
l, t, r, b = rect
img = Image.open(path).convert("RGB").crop((l, t, r, b))
w, h = img.size
px = img.load()
counter = Counter()
for y in range(0, h, 1):
    for x in range(0, w, 1):
        counter[px[x, y]] += 1
total = sum(counter.values())
print(f"=== {path} rect={rect} ({w}x{h}) top12 ===")
for color, count in counter.most_common(12):
    print(f"  {color}  {count}  {count*100/total:.1f}%")
bg = counter.most_common(1)[0][0]
print(f"  -> 背景色 {bg} 占 {counter[bg]*100/total:.1f}%")
