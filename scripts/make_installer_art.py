# -*- coding: utf-8 -*-
"""生成安装程序的侧边图与页头图（NSIS 需要 24 位 BMP）。

运行： python scripts/make_installer_art.py
输出： installer/welcome.bmp (164x314)  installer/header.bmp (150x57)
"""
import os

from PIL import Image, ImageDraw, ImageFont

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(BASE, "installer")

BLUE_TOP = (60, 120, 255)
BLUE_BOT = (16, 56, 200)
WHITE = (255, 255, 255)


def font(size, bold=False):
    cands = [
        r"C:\Windows\Fonts\msyhbd.ttc" if bold else r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\segoeuib.ttf" if bold else r"C:\Windows\Fonts\segoeui.ttf",
        r"C:\Windows\Fonts\arialbd.ttf" if bold else r"C:\Windows\Fonts\arial.ttf",
    ]
    for c in cands:
        if os.path.exists(c):
            try:
                return ImageFont.truetype(c, size)
            except Exception:
                continue
    return ImageFont.load_default()


def gradient(size, c0, c1):
    w, h = size
    small = Image.new("RGB", (1, 96))
    px = small.load()
    for y in range(96):
        t = y / 95.0
        px[0, y] = (
            int(c0[0] + (c1[0] - c0[0]) * t),
            int(c0[1] + (c1[1] - c0[1]) * t),
            int(c0[2] + (c1[2] - c0[2]) * t),
        )
    return small.resize((w, h), Image.BICUBIC)


def draw_pot(d, cx, cy, s, color=WHITE, alpha=255, handles=True):
    """按应用图标同一套比例画锅形标记（32 单位网格，居中于 cx, cy）。"""
    u = s / 32.0
    ox = cx - 16 * u
    oy = cy - 16 * u

    def box(x0, y0, x1, y1):
        return [ox + x0 * u, oy + y0 * u, ox + x1 * u, oy + y1 * u]

    fill = color + (alpha,)
    # 蒸汽
    for i, x in enumerate((11.8, 16.0, 20.2)):
        a = int(alpha * (0.88 - i * 0.08))
        d.rounded_rectangle(box(x - 0.8, 3.9, x + 0.8, 7.7),
                            radius=max(1, int(0.8 * u)), fill=color + (a,))
    # 锅身
    d.rounded_rectangle(box(8.4, 12.0, 23.6, 24.8), radius=int(6.4 * u),
                        corners=(False, False, True, True), fill=fill)
    # 锅口
    d.rounded_rectangle(box(5.2, 9.9, 26.8, 13.4), radius=int(1.6 * u), fill=fill)
    # 把手（安装图里不画，否则锅口 + 把手会连成一条帽檐）
    if handles:
        d.rounded_rectangle(box(2.8, 12.5, 8.4, 15.3), radius=int(1.4 * u), fill=fill)
        d.rounded_rectangle(box(23.6, 12.5, 29.2, 15.3), radius=int(1.4 * u), fill=fill)


def welcome():
    W, H = 164, 314
    img = gradient((W, H), BLUE_TOP, BLUE_BOT).convert("RGBA")
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    # 背景装饰圈（画在独立图层上再合成，否则 ImageDraw 不跟底色混合）
    d.ellipse([W / 2 - 70, 56, W / 2 + 70, 196], outline=(255, 255, 255, 44), width=2)
    d.ellipse([W / 2 - 56, 70, W / 2 + 56, 182], fill=(255, 255, 255, 26))

    draw_pot(d, W / 2, 122, 112, handles=False)
    img = Image.alpha_composite(img, layer)
    d = ImageDraw.Draw(img)

    f_big = font(24, bold=True)
    f_small = font(11)
    t = "Tag Pot"
    tw = d.textlength(t, font=f_big)
    d.text(((W - tw) / 2, 214), t, font=f_big, fill=WHITE + (255,))
    sub = "本地相册画廊"
    sw = d.textlength(sub, font=f_small)
    d.text(((W - sw) / 2, 244), sub, font=f_small, fill=WHITE + (185,))

    d.text((14, H - 26), "v1.0.0", font=font(10), fill=WHITE + (150,))
    img.convert("RGB").save(os.path.join(OUT_DIR, "welcome.bmp"), "BMP")


def header():
    W, H = 150, 57
    img = gradient((W, H), (74, 133, 255), (32, 82, 220)).convert("RGBA")
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw_pot(ImageDraw.Draw(layer), 26, 30, 30, alpha=235, handles=False)
    img = Image.alpha_composite(img, layer)
    d = ImageDraw.Draw(img)
    d.text((50, 17), "Tag Pot", font=font(15, bold=True), fill=WHITE + (250,))
    d.text((50, 35), "本地相册画廊", font=font(9), fill=WHITE + (175,))
    img.convert("RGB").save(os.path.join(OUT_DIR, "header.bmp"), "BMP")


if __name__ == "__main__":
    welcome()
    header()
    print("已生成 installer/welcome.bmp 与 installer/header.bmp")
