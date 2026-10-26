# -*- coding: utf-8 -*-
"""生成演示相册素材 + 初始化 Tag Pot 配置。

运行： python scripts/make_demo.py
"""
import hashlib
import json
import math
import os
import random
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageChops

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "vendor"))
DEMO = os.path.join(BASE, "demo")

random.seed(20261026)

PALETTES = [
    [(250, 252, 255), (184, 208, 252), (52, 108, 246), (14, 42, 118)],
    [(246, 250, 255), (168, 196, 250), (74, 128, 246), (18, 52, 148)],
    [(252, 253, 255), (206, 224, 254), (96, 146, 246), (26, 66, 168)],
    [(240, 246, 255), (158, 188, 246), (42, 96, 232), (10, 32, 100)],
]


def gradient(w, h, c0, c1, angle=90):
    """小块逐像素做对角渐变，再放大 —— 干净、无条纹。"""
    n = 192
    small = Image.new("RGB", (n, n))
    px = small.load()
    rad = math.radians(angle)
    dx, dy = math.cos(rad), math.sin(rad)
    denom = (abs(dx) + abs(dy)) or 1.0
    for y in range(n):
        ny = 2.0 * y / (n - 1) - 1.0
        for x in range(n):
            nx = 2.0 * x / (n - 1) - 1.0
            t = 0.5 + 0.5 * (nx * dx + ny * dy) / denom
            t = 0.0 if t < 0 else (1.0 if t > 1 else t)
            px[x, y] = (
                int(c0[0] + (c1[0] - c0[0]) * t),
                int(c0[1] + (c1[1] - c0[1]) * t),
                int(c0[2] + (c1[2] - c0[2]) * t),
            )
    return small.resize((w, h), Image.BICUBIC)


def art_mesh(w, h, pal):
    img = gradient(w, h, pal[0], pal[1], random.choice([70, 110, 140]))
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for i in range(5):
        cx = random.uniform(0.1, 0.9) * w
        cy = random.uniform(0.1, 0.9) * h
        r = random.uniform(0.16, 0.44) * max(w, h)
        c = random.choice(pal[2:])
        a = random.randint(120, 195)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=c + (a,))
    layer = layer.filter(ImageFilter.GaussianBlur(max(w, h) * 0.045))
    return Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")


def art_waves(w, h, pal):
    img = gradient(w, h, pal[1], pal[0], 100)
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    n = random.randint(6, 10)
    for i in range(n):
        amp = h * random.uniform(0.03, 0.09)
        freq = random.uniform(1.2, 3.2)
        phase = random.uniform(0, 6.28)
        y0 = h * (0.18 + 0.68 * i / max(1, n - 1))
        c = pal[2] if i % 2 == 0 else pal[3]
        a = int(235 - i * (130 / n))
        pts = []
        for x in range(0, w + 8, 6):
            t = x / w * math.pi * freq
            pts.append((x, y0 + math.sin(t + phase) * amp + math.sin(t * 2.7 + phase) * amp * 0.4))
        d.line(pts, fill=c + (max(70, a),), width=max(2, int(h * 0.007)), joint="curve")
    return Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")


def art_arcs(w, h, pal):
    img = gradient(w, h, pal[0], pal[2], 135)
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cx = random.choice([0.0, 0.5, 1.0]) * w
    cy = random.choice([0.0, 0.5, 1.0]) * h
    base = max(w, h)
    for i in range(random.randint(7, 12)):
        r = base * (0.12 + i * 0.085)
        c = random.choice([pal[1], pal[3], (255, 255, 255)])
        d.arc([cx - r, cy - r, cx + r, cy + r], random.randint(0, 200), random.randint(220, 360),
              fill=c + (random.randint(90, 175),), width=max(2, int(base * 0.009)))
    layer = layer.filter(ImageFilter.GaussianBlur(1.1))
    return Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")


def art_dots(w, h, pal):
    img = gradient(w, h, pal[1], pal[0], 60)
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    step = int(max(w, h) / random.randint(26, 40))
    for y in range(step, h, step):
        for x in range(step, w, step):
            t = (x / w + y / h) / 2
            r = step * (0.09 + 0.3 * (1 - abs(t - 0.45) * 1.6)) * random.uniform(.8, 1.2)
            if r <= 0.4:
                continue
            a = int(70 + 165 * t)
            d.ellipse([x - r, y - r, x + r, y + r], fill=pal[3] + (a,))
    return Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")


def art_blocks(w, h, pal):
    img = gradient(w, h, pal[0], pal[1], 120)
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for i in range(random.randint(4, 7)):
        bw = random.uniform(0.12, 0.4) * w
        bh = random.uniform(0.12, 0.5) * h
        x = random.uniform(-0.08, 0.92) * w
        y = random.uniform(-0.08, 0.9) * h
        c = random.choice(pal[2:])
        a = random.randint(60, 135)
        d.rounded_rectangle([x, y, x + bw, y + bh], radius=int(min(bw, bh) * 0.22), fill=c + (a,))
    layer = layer.filter(ImageFilter.GaussianBlur(max(w, h) * 0.012))
    return Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")


def art_night(w, h, pal):
    """深底 + 亮线，给瀑布流一点节奏对比。"""
    img = gradient(w, h, pal[3], (6, 16, 40), 115)
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    base = max(w, h)
    cx, cy = random.uniform(0.3, 0.7) * w, random.uniform(0.3, 0.7) * h
    for i in range(random.randint(9, 15)):
        r = base * (0.06 + i * 0.07)
        tone = random.choice([(255, 255, 255), pal[1], pal[2]])
        d.arc([cx - r, cy - r, cx + r, cy + r], random.randint(0, 300), random.randint(320, 420),
              fill=tone + (random.randint(60, 150),), width=max(2, int(base * 0.0035)))
    for i in range(3):
        y = h * random.uniform(0.15, 0.85)
        d.line([(0, y), (w, y + random.uniform(-h * .12, h * .12))],
               fill=(255, 255, 255, random.randint(30, 70)), width=max(1, int(base * 0.0015)))
    layer = layer.filter(ImageFilter.GaussianBlur(0.8))
    return Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")


def grain(img, amount=6):
    n = Image.effect_noise(img.size, amount).convert("L")
    n = n.point(lambda v: 128 + (v - 128) * 0.35)
    return ImageChops.blend(img, Image.merge("RGB", (n, n, n)), 0.06)


ARTS = [art_mesh, art_waves, art_arcs, art_dots, art_blocks, art_night, art_arcs, art_mesh]

RATIOS = [(1500, 1000), (1000, 1500), (1400, 1400), (1600, 900), (1100, 1400), (1400, 1050), (1700, 800)]


def make(path, ratio=None):
    w, h = ratio or random.choice(RATIOS)
    pal = random.choice(PALETTES)
    img = random.choice(ARTS)(w, h, pal)
    img = grain(img)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path, "JPEG", quality=92, optimize=True, progressive=True)
    return path


PLAN = [
    ("Studio", ["poster-01", "poster-02", "poster-03", "poster-04", "poster-05",
                "poster-06", "poster-07", "poster-08"]),
    ("Studio/Posters", ["launch-a", "launch-b", "launch-c", "launch-d", "launch-e"]),
    ("Studio/Sketches", ["draft-line", "draft-shape", "draft-tonal"]),
    ("Archive", ["ref-01", "ref-02", "ref-03", "ref-04", "ref-05", "ref-06"]),
]


def set_comment(path, text):
    try:
        import piexif
        from piexif import helper
    except Exception:
        return
    try:
        exif = piexif.load(path)
    except Exception:
        exif = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
    for k in ("0th", "Exif", "GPS", "1st"):
        exif.setdefault(k, {})
    exif["Exif"][piexif.ExifIFD.UserComment] = helper.UserComment.dump(text, encoding="unicode")
    piexif.insert(piexif.dump(exif), path)


def main():
    created = []
    for folder, names in PLAN:
        d = os.path.join(DEMO, folder.replace("/", os.sep))
        for n in names:
            p = os.path.join(d, n + ".jpg")
            make(p, random.choice(RATIOS))
            created.append(p)

    # 给两张图预置备注，演示「读取图片属性中的备注」
    studio = os.path.join(DEMO, "Studio")
    set_comment(os.path.join(studio, "poster-01.jpg"),
                "主视觉初稿，冷蓝渐变。等客户确认后再定稿。")
    set_comment(os.path.join(studio, "Posters", "launch-a.jpg"),
                "发布页头图，16:9 安全区已预留。")

    # 写入配置
    roots = []
    active = None
    for name in ("Studio", "Archive"):
        p = os.path.abspath(os.path.join(DEMO, name))
        rid = hashlib.sha1(os.path.normcase(p).encode("utf-8")).hexdigest()[:12]
        roots.append({"id": rid, "path": p})
        if name == "Studio":
            active = rid
    cfg = os.path.join(BASE, "data", "config.json")
    os.makedirs(os.path.dirname(cfg), exist_ok=True)
    with open(cfg, "w", encoding="utf-8") as f:
        json.dump({"roots": roots, "active": active}, f, ensure_ascii=False, indent=2)

    print("生成图片 %d 张" % len(created))
    print("演示目录:", DEMO)


if __name__ == "__main__":
    main()
