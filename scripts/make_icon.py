# -*- coding: utf-8 -*-
"""生成 Tag Pot 应用图标 assets/tagpot.ico（与界面里的品牌标识同款：蓝底白色锅形）。

运行： python scripts/make_icon.py
"""
import math
import os

from PIL import Image, ImageDraw

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "assets", "tagpot.ico")

S = 1024                 # 超采样画布
K = S / 32.0             # SVG viewBox 0 0 32 32 的缩放系数
BLUE_TOP = (75, 131, 255)
BLUE_BOT = (20, 73, 214)
WHITE = (255, 255, 255)


def rounded_mask(size, radius):
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return m


def diagonal_gradient(size, c0, c1):
    n = 192
    small = Image.new("RGB", (n, n))
    px = small.load()
    d = 1.0 / (n - 1)
    for y in range(n):
        for x in range(n):
            t = ((x + y) * d) / 2.0
            t = 0.0 if t < 0 else (1.0 if t > 1 else t)
            px[x, y] = (
                int(c0[0] + (c1[0] - c0[0]) * t),
                int(c0[1] + (c1[1] - c0[1]) * t),
                int(c0[2] + (c1[2] - c0[2]) * t),
            )
    return small.resize((size, size), Image.BICUBIC)


def cubic(p0, p1, p2, p3, n=40):
    out = []
    for i in range(n + 1):
        t = i / n
        u = 1 - t
        x = u ** 3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t ** 3 * p3[0]
        y = u ** 3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t ** 3 * p3[1]
        out.append((x * K, y * K))
    return out


def build():
    canvas = diagonal_gradient(S, BLUE_TOP, BLUE_BOT).convert("RGBA")
    canvas.putalpha(rounded_mask(S, int(S * 0.205)))

    d = ImageDraw.Draw(canvas)
    W = WHITE + (255,)

    # 蒸汽：三根等高的圆头短棒
    for i, cx in enumerate((11.8, 16.0, 20.2)):
        a = 200 - i * 16
        d.rounded_rectangle([(cx - 0.8) * K, 3.9 * K, (cx + 0.8) * K, 7.7 * K],
                            radius=int(0.8 * K), fill=WHITE + (a,))

    # 锅身
    d.rounded_rectangle([8.4 * K, 12.0 * K, 23.6 * K, 24.8 * K],
                        radius=int(6.4 * K), corners=(False, False, True, True), fill=W)

    # 锅口
    d.rounded_rectangle([5.2 * K, 9.9 * K, 26.8 * K, 13.4 * K],
                        radius=int(1.6 * K), fill=W)

    # 两侧把手
    d.rounded_rectangle([2.8 * K, 12.5 * K, 8.4 * K, 15.3 * K], radius=int(1.4 * K), fill=W)
    d.rounded_rectangle([23.6 * K, 12.5 * K, 29.2 * K, 15.3 * K], radius=int(1.4 * K), fill=W)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    big = canvas.resize((512, 512), Image.LANCZOS)
    big.save(OUT, format="ICO",
             sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    big.save(os.path.join(os.path.dirname(OUT), "tagpot.png"), "PNG")
    print("已生成", OUT)


if __name__ == "__main__":
    build()
