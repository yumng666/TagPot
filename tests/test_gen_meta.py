# -*- coding: utf-8 -*-
"""生成参数解析的回归测试（ComfyUI / NovelAI）。

    python tests/test_gen_meta.py

用 Pillow 现造带 PNG 文本块的样本图，断言解析出的模型 / Lora / 正向提示词。
"""
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "vendor"))
sys.path.insert(0, ROOT)

from PIL import Image, PngImagePlugin  # noqa: E402

import server  # noqa: E402

fails = []


def check(label, actual, expected):
    ok = expected(actual) if callable(expected) else actual == expected
    print(("  ok   " if ok else "  FAIL ") + label + f" = {actual!r}"
          + ("" if ok else f"  (期望 {expected})"))
    if not ok:
        fails.append(label)


COMFY_NODES = {
    "3": {"class_type": "KSampler", "inputs": {
        "seed": 1, "steps": 30, "cfg": 7.0, "sampler_name": "euler", "denoise": 1.0,
        "model": ["8", 0], "positive": ["6", 0], "negative": ["7", 0],
        "latent_image": ["5", 0]}},
    "4": {"class_type": "CheckpointLoaderSimple",
          "inputs": {"ckpt_name": "illustriousXL_v01.safetensors"}},
    "5": {"class_type": "EmptyLatentImage",
          "inputs": {"width": 832, "height": 1216, "batch_size": 1}},
    "6": {"class_type": "CLIPTextEncode",
          "inputs": {"text": "masterpiece, best quality, 1girl, purple eyes", "clip": ["8", 1]}},
    "7": {"class_type": "CLIPTextEncode",
          "inputs": {"text": "lowres, bad anatomy, worst quality", "clip": ["8", 1]}},
    "8": {"class_type": "LoraLoader", "inputs": {
        "lora_name": "characters\\yumng_style.safetensors",
        "strength_model": 0.85, "strength_clip": 0.7,
        "model": ["10", 0], "clip": ["10", 1]}},
    "10": {"class_type": "LoraLoader", "inputs": {
        "lora_name": "detail_tweaker.safetensors",
        "strength_model": 0.4, "strength_clip": 0.4,
        "model": ["4", 0], "clip": ["4", 1]}},
}

NAI3 = {
    "Software": "NovelAI",
    "Source": "Stable Diffusion XL C1E1DE52",
    "Comment": json.dumps({
        "prompt": "1girl, best quality, purple eyes",
        "uc": "bad anatomy",
        "steps": 28, "sampler": "k_euler", "seed": 123,
        "noise_schedule": "karras", "sm": False, "sm_dyn": False,
    }),
}

NAI4 = {
    "Software": "NovelAI",
    "Source": "NovelAI Diffusion V4.5 16.5M",
    "Comment": json.dumps({
        "prompt": "1girl",
        "uc": "bad anatomy",
        "steps": 28,
        "v4_prompt": {"caption": {
            "base_caption": "1girl, hoodie, night city",
            "char_captions": [{"char_caption": "purple eyes, short hair"}],
        }},
        "noise_schedule": "karras",
    }),
}


def make_png(path, texts):
    im = Image.new("RGB", (48, 64), (150, 190, 250))
    info = PngImagePlugin.PngInfo()
    for k, v in texts.items():
        info.add_text(k, v)
    im.save(path, pnginfo=info)


def main():
    tmp = tempfile.mkdtemp(prefix="tagpot-gen-")
    server.ANNO_PATH = os.path.join(tmp, "annotations.json")
    try:
        comfy = os.path.join(tmp, "comfy.png")
        make_png(comfy, {"prompt": json.dumps(COMFY_NODES),
                         "workflow": json.dumps({"nodes": []})})
        nai3 = os.path.join(tmp, "nai3.png")
        make_png(nai3, NAI3)
        nai4 = os.path.join(tmp, "nai4.png")
        make_png(nai4, NAI4)
        plain = os.path.join(tmp, "plain.png")
        make_png(plain, {"Software": "Photoshop"})

        print("\n[ComfyUI]")
        g = server.read_gen_meta(comfy)
        check("engine", g["engine"], "comfyui")
        check("模型", g["model"], "illustriousXL_v01.safetensors")
        check("模型强度", g["modelStrength"], 1.0)
        check("Lora 数量", len(g["loras"]), 2)
        check("Lora1 名（去掉目录）", g["loras"][0]["name"], "yumng_style.safetensors")
        check("Lora1 强度", g["loras"][0]["strength"], 0.85)
        check("Lora2 名", g["loras"][1]["name"], "detail_tweaker.safetensors")
        check("正向提示词来自 positive 而非 negative", g["prompt"],
              "masterpiece, best quality, 1girl, purple eyes")

        print("\n[NovelAI v3]")
        g3 = server.read_gen_meta(nai3)
        check("engine", g3["engine"], "novelai")
        check("模型显示为 Nai 3", g3["model"], "Nai 3")
        check("无模型强度", g3["modelStrength"], None)
        check("无 Lora", g3["loras"], [])
        check("正向提示词", g3["prompt"], "1girl, best quality, purple eyes")

        print("\n[NovelAI v4.5]")
        g4 = server.read_gen_meta(nai4)
        check("模型显示为 Nai 4.5", g4["model"], "Nai 4.5")
        check("v4 版本号", g4["naiVersion"], "4.5")
        check("正向提示词含角色描述", g4["prompt"],
              lambda v: "1girl, hoodie, night city" in v and "purple eyes" in v)

        print("\n[ComfyUI · SDXL 双文本]")
        sdxl = os.path.join(tmp, "sdxl.png")
        make_png(sdxl, {"prompt": json.dumps({
            "1": {"class_type": "CheckpointLoaderSimple",
                  "inputs": {"ckpt_name": "sdxl_base_1.0.safetensors"}},
            "2": {"class_type": "KSampler", "inputs": {"positive": ["3", 0], "negative": ["4", 0]}},
            "3": {"class_type": "CLIPTextEncodeSDXL",
                  "inputs": {"text_g": "anime style, night city", "text_l": "", "clip": ["1", 1]}},
            "4": {"class_type": "CLIPTextEncodeSDXL",
                  "inputs": {"text_g": "blurry", "text_l": "", "clip": ["1", 1]}},
        })})
        gs = server.read_gen_meta(sdxl)
        check("SDXL 取到 text_g", gs["prompt"], "anime style, night city")
        check("SDXL 模型", gs["model"], "sdxl_base_1.0.safetensors")

        print("\n[NovelAI · Comment 不是 JSON]")
        nairaw = os.path.join(tmp, "nairaw.png")
        make_png(nairaw, {"Software": "NovelAI",
                          "Source": "NovelAI Diffusion V4 16.5M",
                          "Comment": "1girl, solo, purple eyes"})
        gr = server.read_gen_meta(nairaw)
        check("仍然识别为 NovelAI", gr["engine"], "novelai")
        check("版本 Nai 4", gr["model"], "Nai 4")
        check("原文当提示词", gr["prompt"], "1girl, solo, purple eyes")

        print("\n[普通图片]")
        gp = server.read_gen_meta(plain)
        check("engine 为空", gp["engine"], "")
        check("无模型", gp["model"], "")
        check("无 Lora", gp["loras"], [])

        print("\n[覆盖存储]")
        server._anno_set(comfy, {"model": {"name": "我的模型", "strength": 0.6}})
        anno = server._anno_get(comfy)
        check("模型覆盖已写入", anno["model"]["name"], "我的模型")
        check("强度覆盖已写入", anno["model"]["strength"], 0.6)
        server._anno_set(comfy, {"loras": [{"name": "only_one", "strength": 0.3}]})
        check("Lora 覆盖已写入", server._anno_get(comfy)["loras"][0]["name"], "only_one")
        check("模型覆盖仍在", server._anno_get(comfy)["model"]["name"], "我的模型")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + ("✅ 全部通过" if not fails else f"❌ 失败 {len(fails)} 项：" + " / ".join(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
