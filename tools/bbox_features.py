# -*- coding: utf-8 -*-
"""扫描特征色边界框，确认模型实际所在位置"""
import sys
from PIL import Image

path = sys.argv[1]
skin = {(221, 208, 205), (220, 207, 204), (222, 209, 206), (219, 206, 203)}
dress = {(202, 212, 224), (203, 213, 225), (201, 211, 223), (202, 211, 221), (204, 212, 221)}
eye = {(124, 172, 208), (125, 173, 209), (128, 175, 208), (124, 171, 208)}
img = Image.open(path).convert("RGB")
w, h = img.size
px = img.load()
minx, miny, maxx, maxy = w, h, -1, -1
count = 0
for y in range(0, h):
    for x in range(0, w):
        c = px[x, y]
        if c in skin or c in dress or c in eye:
            count += 1
            if x < minx: minx = x
            if x > maxx: maxx = x
            if y < miny: miny = y
            if y > maxy: maxy = y
print(f"=== {path} ===")
print(f"  count={count} bbox=({minx},{miny},{maxx},{maxy}) size=({maxx-minx+1}x{maxy-miny+1})")
