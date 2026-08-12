# -*- coding: utf-8 -*-
"""分析实验截图的像素分布：判断模型是否可见 + 背景是什么颜色"""
import sys
from collections import Counter
from PIL import Image

for path in sys.argv[1:]:
    img = Image.open(path).convert("RGB")
    w, h = img.size
    px = img.load()
    counter = Counter()
    for y in range(0, h, 2):
        for x in range(0, w, 2):
            counter[px[x, y]] += 1
    total = sum(counter.values())
    print(f"=== {path} ({w}x{h}) top10 colors ===")
    for color, count in counter.most_common(10):
        print(f"  {color}  {count}  {count*100/total:.1f}%")
    # 模型特征：统计"非纯背景"像素数
    bg = counter.most_common(1)[0][0]
    nonbg = total - counter[bg]
    print(f"  -> 背景色 {bg} 占 {counter[bg]*100/total:.1f}%, 非背景像素 {nonbg*100/total:.1f}%")
