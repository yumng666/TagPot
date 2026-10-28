# -*- coding: utf-8 -*-
"""Tag Pot · 本地相册画廊 —— 服务端

仅依赖标准库 + Pillow（读取图片尺寸 / 生成缩略图），
piexif 存放在 vendor/ 内，用于把「备注」写回 JPEG 的 EXIF UserComment。
其他格式（png/webp/...）退化为伴随文件 data/comments.json。

启动： python server.py [--port 8765] [--no-browser]
"""

from __future__ import annotations

import ctypes
import hashlib
import io
import json
import mimetypes
import os
import re
import shutil
import string
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FROZEN = bool(getattr(sys, "frozen", False))
# 源码运行时一切都在项目目录；打包成 exe 后，只读资源在 _MEIPASS，
# 而配置/缓存/演示素材必须落在 exe 旁边，否则每次启动都会丢。
APP_DIR = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.abspath(__file__))
RES_DIR = getattr(sys, "_MEIPASS", APP_DIR)
BASE = APP_DIR

_vendor = os.path.join(APP_DIR, "vendor")
if os.path.isdir(_vendor):
    sys.path.insert(0, _vendor)

WEB_DIR = os.path.join(RES_DIR, "web")
ASSETS_DIR = os.path.join(RES_DIR, "assets")


def _writable(d):
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".write-probe")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("1")
        os.remove(probe)
        return True
    except Exception:
        return False


def _pick_data_dir():
    """绿色版把 data 放在 exe 旁边；装到 Program Files 这类只读位置时退到
    %LOCALAPPDATA%\\TagPot，这样升级/卸载都不会把用户配置一起带走。"""
    portable = os.path.join(APP_DIR, "data")
    wants_portable = os.path.isdir(portable) or os.path.isfile(os.path.join(APP_DIR, "portable.txt"))
    if wants_portable and _writable(portable):
        return portable
    local = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "TagPot")
    if _writable(local):
        return local
    return portable


DATA_DIR = _pick_data_dir()
THUMB_DIR = os.path.join(DATA_DIR, "cache", "thumbs")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
COMMENTS_PATH = os.path.join(DATA_DIR, "comments.json")

IMAGE_EXTS = {".jpg", ".jpeg", ".jpe", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
JPEG_EXTS = {".jpg", ".jpeg", ".jpe"}

try:
    from PIL import Image, ImageOps
except Exception:  # pragma: no cover
    Image = None
    ImageOps = None

try:
    import piexif
    from piexif import helper as piexif_helper
except Exception:  # pragma: no cover
    piexif = None
    piexif_helper = None

XPCOMMENT_TAG = 0x9C9C  # Windows 资源管理器「备注」

_lock = threading.Lock()
STATE = {"roots": [], "active": None}
_meta_cache: dict = {}
_dir_cache: dict = {}
_dirs_cache: dict = {}
_drop_lock = threading.Lock()
_dropped: list = []


def push_drop(path: str):
    """WebView2 把拖入项的真实绝对路径回传到这里，等前端来取。"""
    with _drop_lock:
        if path and path not in _dropped:
            _dropped.append(path)


def take_drop():
    with _drop_lock:
        items = list(_dropped)
        _dropped.clear()
    return items


# --------------------------------------------------------------------------- 持久化
def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    if os.path.exists(path):
        try:
            shutil.copyfile(path, path + ".bak")
        except Exception:
            pass
    os.replace(tmp, path)


def load_state():
    raw = None
    for attempt in range(3):
        raw = _load_json(CONFIG_PATH, None)
        if raw is not None:
            break
        if not os.path.exists(CONFIG_PATH):
            break                     # 文件确实不存在（首次运行）
        time.sleep(0.15)              # 大概率是被杀软/索引器短暂占用，重试
    if raw is None:
        raw = _load_json(CONFIG_PATH + ".bak", None)
        if raw is not None and os.environ.get("TAGPOT_LOG"):
            _log("[config] 主配置读取失败，已从 .bak 恢复")
    if raw is None:
        raw = {"roots": [], "active": None}
        if os.path.exists(CONFIG_PATH) and os.environ.get("TAGPOT_LOG"):
            _log("[config] 主配置与备份都读不出来，按空配置启动（暂不覆盖磁盘）")
    roots = []
    seen = set()
    for r in raw.get("roots", []):
        p = os.path.abspath(r.get("path", ""))
        if not p or p in seen:
            continue
        seen.add(p)
        roots.append({"id": _rid(p), "path": p})
    STATE["roots"] = roots
    STATE["active"] = raw.get("active")


_allow_empty_save = False


def save_state():
    global _allow_empty_save
    if not STATE["roots"] and not _allow_empty_save:
        # 防御：内存里的根目录是空的，但磁盘上明明有 —— 八成是启动时读取失败，
        # 这时候绝不能把用户的根目录列表覆盖掉
        cfg = _load_json(CONFIG_PATH, None) or {}
        if cfg.get("roots"):
            if os.environ.get("TAGPOT_LOG"):
                _log("[config] 拒绝用空 roots 覆盖已有配置")
            return
    _save_json(CONFIG_PATH, {"roots": STATE["roots"], "active": STATE["active"]})
    _allow_empty_save = False


def _rid(path: str) -> str:
    return hashlib.sha1(os.path.normcase(path).encode("utf-8")).hexdigest()[:12]


def root_payload(r):
    p = r["path"]
    exists = os.path.isdir(p)
    has_dirs = False
    if exists:
        try:
            with os.scandir(p) as it:
                for e in it:
                    try:
                        if e.is_dir() and not e.name.startswith("."):
                            has_dirs = True
                            break
                    except Exception:
                        continue
        except Exception:
            has_dirs = False
    return {
        "id": r["id"],
        "path": p,
        "name": os.path.basename(p.rstrip("\\/")) or p,
        "exists": exists,
        "hasDirs": has_dirs,
    }


# --------------------------------------------------------------------------- 路径安全
def resolve_allowed(raw: str):
    """只允许访问已登记根目录内部的文件，避免任意文件读取。"""
    if not raw:
        return None
    try:
        rp = os.path.realpath(raw)
    except Exception:
        return None
    for r in STATE["roots"]:
        root = os.path.realpath(r["path"])
        if rp == root or rp.startswith(root + os.sep):
            return rp
    return None


# --------------------------------------------------------------------------- 目录 / 图片
SKIP_DIRS = {"$RECYCLE.BIN", "System Volume Information", "$SysReset", "Config.Msi"}


def _visible_dir(e):
    n = e.name
    return not n.startswith(".") and n not in SKIP_DIRS


def _scan_dirs(path: str, with_children_hint=True):
    out = []
    with os.scandir(path) as it:
        for e in it:
            try:
                if not e.is_dir(follow_symlinks=False):
                    continue
                if not _visible_dir(e):
                    continue
                has = False
                if with_children_hint:
                    try:
                        with os.scandir(e.path) as sub:
                            for s in sub:
                                if s.is_dir(follow_symlinks=False):
                                    has = True
                                    break
                    except Exception:
                        has = False
                out.append({"name": e.name, "path": e.path, "hasDirs": has,
                            "mtime": e.stat(follow_symlinks=False).st_mtime_ns})
            except Exception:
                continue
    out.sort(key=lambda x: x["name"].lower())
    return out


_DIRS_TTL = 1.2   # 秒：侧栏要按「修改时间」实时刷新，缓存只做短去抖


def list_dirs(path: str, with_children_hint=True):
    """列出子目录。带短 TTL 缓存：既挡住高频轮询的重复扫盘，
    又能在一两秒内拿到子文件夹最新的修改时间（新图写入后顺序立即跟上）。"""
    key = (path, with_children_hint)
    now = time.time()
    hit = _dirs_cache.get(key)
    if hit and (now - hit[0]) < _DIRS_TTL:
        return hit[1]
    try:
        out = _scan_dirs(path, with_children_hint)
    except Exception:
        return hit[1] if hit else []
    if len(_dirs_cache) > 300:
        _dirs_cache.clear()
    _dirs_cache[key] = (now, out)
    return out


def volume_label(root: str) -> str:
    try:
        k32 = ctypes.windll.kernel32
        buf = ctypes.create_unicode_buffer(261)
        fs = ctypes.create_unicode_buffer(261)
        ok = k32.GetVolumeInformationW(
            ctypes.c_wchar_p(root), buf, 260, None, None, None, fs, 260
        )
        return buf.value if ok else ""
    except Exception:
        return ""


DRIVE_KIND = {
    2: ("removable", "可移动磁盘"),
    3: ("fixed", "本地磁盘"),
    4: ("remote", "网络驱动器"),
    5: ("cdrom", "光驱"),
    6: ("ram", "内存盘"),
}


def list_drives():
    """用 GetLogicalDrives 取盘符：即使网络盘暂时没连上也照样列出来，且不会卡住。"""
    out = []
    mask = 0
    try:
        mask = ctypes.windll.kernel32.GetLogicalDrives()
    except Exception:
        mask = 0
    for i, ch in enumerate(string.ascii_uppercase):
        if not (mask >> i) & 1:
            continue
        root = "%s:\\" % ch
        try:
            dtype = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root))
        except Exception:
            dtype = 3
        kind, default_label = DRIVE_KIND.get(dtype, ("fixed", "本地磁盘"))
        label = default_label
        if kind in ("fixed", "removable", "ram"):
            label = volume_label(root) or default_label
        out.append({
            "name": "%s:" % ch,
            "path": root,
            "label": label,
            "kind": kind,
        })
    return out


def list_places():
    out = []
    home = os.path.expanduser("~")
    known = [
        ("桌面", "Desktop"),
        ("下载", "Downloads"),
        ("图片", "Pictures"),
        ("文档", "Documents"),
        ("视频", "Videos"),
        ("音乐", "Music"),
    ]
    for label, sub in known:
        p = os.path.join(home, sub)
        if os.path.isdir(p):
            out.append({"name": label, "path": p, "kind": "place"})
    if os.path.isdir(home):
        out.append({"name": "用户目录", "path": home, "kind": "place"})
    return out


def browse(path: str):
    """文件夹选择器用的通用目录浏览（不受已登记根目录限制，只列目录名）。"""
    if not path:
        return {
            "path": "",
            "parent": "",
            "name": "此电脑",
            "dirs": [],
            "drives": list_drives(),
            "places": list_places(),
            "ok": True,
        }
    p = os.path.abspath(path)
    if not os.path.isdir(p):
        return {"path": p, "ok": False, "error": "文件夹不存在或无法访问", "dirs": []}

    parent = os.path.dirname(p.rstrip("\\/"))
    if parent and len(parent) == 2 and parent[1] == ":":
        parent = parent + "\\"
    if os.path.normcase(parent) == os.path.normcase(p):
        parent = ""
    try:
        dirs = _scan_dirs(p, with_children_hint=False)
        ok = True
        err = ""
    except Exception as exc:
        dirs, ok, err = [], False, str(exc)
    return {
        "path": p,
        "parent": parent,
        "name": os.path.basename(p.rstrip("\\/")) or p,
        "dirs": dirs,
        "ok": ok,
        "error": err,
    }


def _orientation_swap(size, orientation):
    if orientation in (5, 6, 7, 8):
        return size[1], size[0]
    return size


# --------------------------------------------------------------------------- 生成参数（ComfyUI / NovelAI）
ANNO_PATH = os.path.join(DATA_DIR, "annotations.json")
_anno_cache = {"mtime": None, "data": {}}


def _num(v, default=None):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f


def _base_name(v):
    return os.path.basename((v or "").replace("\\", "/"))


MODEL_EXTS = {".safetensors", ".pt", ".pth", ".ckpt", ".bin", ".gguf", ".onnx", ".sft"}


def _pretty_name(v):
    """去掉目录与常见权重后缀，界面上显示纯名字。"""
    n = _base_name(v)
    root, ext = os.path.splitext(n)
    return root if ext.lower() in MODEL_EXTS else n


def _parse_lora_list(val):
    """AnimaMultiLoraLoader 这类节点把 lora 列表放在一个 JSON 字符串输入里。

    形如 [{"name":"xx.safetensors","strength":1,"enabled":true}]；
    enabled 为 false 的不显示。
    """
    if isinstance(val, str):
        if not val.strip().startswith("["):
            return []
        try:
            val = json.loads(val)
        except Exception:
            return []
    if not isinstance(val, list):
        return []
    out = []
    for it in val:
        if not isinstance(it, dict):
            continue
        if it.get("enabled") is False:
            continue
        name = _pretty_name(it.get("name") or "")
        if not name:
            continue
        strength = _num(it.get("strength"), 1.0)
        if strength == 0:
            continue          # 强度为 0 等于没挂，不显示
        out.append({"name": name, "strength": strength})
    return out


def _anno_all():
    try:
        mtime = os.stat(ANNO_PATH).st_mtime_ns
    except Exception:
        return {}
    if _anno_cache["mtime"] != mtime:
        _anno_cache["data"] = _load_json(ANNO_PATH, {}) or {}
        _anno_cache["mtime"] = mtime
    return _anno_cache["data"]


def _anno_get(path):
    return _anno_all().get(path) or {}


def _anno_set(path, patch):
    data = dict(_anno_all())
    item = dict(data.get(path) or {})
    for key, val in patch.items():
        if val is None:      # None 表示撤销该项自定义
            item.pop(key, None)
            continue
        if key == "model" and isinstance(val, dict):
            val = str(val.get("name") or "").strip()   # 旧格式统一成纯名字
            if not val:
                item.pop(key, None)
                continue
        item[key] = val
    if item:
        data[path] = item
    else:
        data.pop(path, None)
    _save_json(ANNO_PATH, data)
    _anno_cache["mtime"] = None
    return item


def _png_text_raw(path):
    """直接按 PNG 结构抽文本块（tEXt/zTXt/iTXt），不限位置。

    Pillow 的 im.text 只包含图片数据之前的块；有些工具会把元数据写在文件尾部。
    """
    import zlib
    out = {}

    def put(k, v):
        k = (k or "").strip()
        if k and k not in out and v and v.strip():
            try:
                out[k] = v.decode("utf-8", "ignore") if isinstance(v, bytes) else str(v)
            except Exception:
                pass

    try:
        with open(path, "rb") as f:
            if f.read(8)[:4] != b"\x89PNG":
                return out
            while True:
                head = f.read(8)
                if len(head) < 8:
                    break
                length = int.from_bytes(head[:4], "big")
                ctype = head[4:8]
                data = f.read(length) if ctype in (b"tEXt", b"zTXt", b"iTXt") else b""
                if ctype not in (b"tEXt", b"zTXt", b"iTXt"):
                    f.seek(length + 4, 1)
                    if ctype == b"IEND" or length > 64 * 1024 * 1024:
                        break
                    continue
                f.read(4)  # CRC
                try:
                    if ctype == b"tEXt":
                        k, _, v = data.partition(b"\x00")
                        put(k.decode("latin1", "ignore"), v)
                    elif ctype == b"zTXt":
                        k, _, rest = data.partition(b"\x00")
                        put(k.decode("latin1", "ignore"),
                            zlib.decompress(rest[1:]) if rest[:1] == b"\x00" else rest[1:])
                    else:  # iTXt
                        k, _, rest = data.partition(b"\x00")
                        comp = rest[:1]
                        rest = rest[2:]            # 压缩标记 + 方法
                        _lang, _, rest = rest.partition(b"\x00")
                        _trans, _, txt = rest.partition(b"\x00")
                        if comp == b"\x01":
                            txt = zlib.decompress(txt)
                        put(k.decode("utf-8", "ignore"), txt.decode("utf-8", "ignore"))
                except Exception:
                    continue
                if ctype == b"IEND" or length > 64 * 1024 * 1024:
                    break
    except Exception:
        pass
    return out


def _png_chunk_types(path):
    """直接按 PNG 结构扫块类型，绕开 Pillow —— 用于诊断元数据是否存在。"""
    types = []
    try:
        with open(path, "rb") as f:
            if f.read(8)[:4] != b"\x89PNG":
                return types
            while True:
                head = f.read(8)
                if len(head) < 8:
                    break
                length = int.from_bytes(head[:4], "big")
                ctype = head[4:8].decode("latin1", "ignore")
                types.append(ctype)
                f.seek(length + 4, 1)          # 跳过数据与 CRC
                if ctype == "IEND" or len(types) > 512:
                    break
    except Exception:
        pass
    return types


def _png_text(path):
    """取 PNG 的文本块：Pillow 读头部块，再叠加原始扫描（不限位置）。"""
    out = {}
    try:
        if Image is not None:
            with Image.open(path) as im:
                if (im.format or "").upper() != "PNG":
                    return {}
                out = {str(k): (v.decode("utf-8", "ignore") if isinstance(v, bytes) else str(v))
                       for k, v in (getattr(im, "text", None) or {}).items()}
    except Exception:
        out = {}
    for k, v in _png_text_raw(path).items():
        out.setdefault(k, v)          # Pillow 没读到的位置（如文件尾部）在这里补上
    return out


def _follow_text(nodes, link, seen=None, depth=0):
    """顺着 KSampler 的 positive 连线找到 CLIPTextEncode 的 text。"""
    if depth > 6 or not (isinstance(link, (list, tuple)) and len(link) == 2):
        return ""
    nid = str(link[0])
    seen = seen if seen is not None else set()
    if nid in seen:
        return ""
    seen.add(nid)
    node = nodes.get(nid)
    if not isinstance(node, dict):
        return ""
    inputs = node.get("inputs") or {}
    for tkey in ("text", "text_g", "text_l", "string", "positive_prompt", "prompt"):
        val = inputs.get(tkey)
        if isinstance(val, str) and val.strip():
            return val
    for key in ("positive", "conditioning", "conditioning_1", "conditioning_to",
                "conditioning_from", "clip", "text_from", "value", "conditioning_2"):
        if key in inputs:
            got = _follow_text(nodes, inputs[key], seen, depth + 1)
            if got:
                return got
    for val in inputs.values():
        if isinstance(val, (list, tuple)) and len(val) == 2 and isinstance(val[0], str):
            got = _follow_text(nodes, val, seen, depth + 1)
            if got:
                return got
    return ""


def _comfy_prompts(nodes):
    """返回 (正向, 负向)。

    ComfyUI 的采样器节点都把连线叫 positive / negative，跟类名无关
    （KSampler、SamplerCustom、AnimaKSampler……），所以直接按输入名找，
    不再依赖类名识别。
    """
    pos = neg = ""
    for node in nodes.values():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs") or {}
        if "positive" not in inputs:
            continue
        pos = _follow_text(nodes, inputs.get("positive"))
        neg = _follow_text(nodes, inputs.get("negative"))
        if pos or neg:
            return pos, neg
    # 兜底：类名里带 sampler 的
    for node in nodes.values():
        if not isinstance(node, dict):
            continue
        ct = str(node.get("class_type") or "")
        if "sampler" in ct.lower():
            got = _follow_text(nodes, (node.get("inputs") or {}).get("positive"))
            if got:
                return got, ""
    # 再兜底：取最长的一段文本（正向通常更长）
    texts = []
    for node in nodes.values():
        if not isinstance(node, dict):
            continue
        t = (node.get("inputs") or {}).get("text")
        if isinstance(t, str) and t.strip():
            texts.append(t)
    texts.sort(key=len, reverse=True)
    return (texts[0] if texts else ""), ""


def _parse_comfy(nodes):
    model, loras = "", []
    for node in nodes.values():
        if not isinstance(node, dict):
            continue
        ct = str(node.get("class_type") or "")
        inputs = node.get("inputs") or {}
        if not model and ct in ("CheckpointLoaderSimple", "CheckpointLoader",
                                "ImageOnlyCheckpointLoader"):
            model = _pretty_name(inputs.get("ckpt_name"))
        if not model and ct in ("UNETLoader", "UnetLoaderGGUF"):
            model = _pretty_name(inputs.get("unet_name"))
        if ct in ("LoraLoader", "LoraLoaderModelOnly"):
            name = _pretty_name(inputs.get("lora_name"))
            strength = _num(inputs.get("strength_model"), 1.0)
            if name and strength != 0:
                loras.append({"name": name, "strength": strength})
        # 其它加载器：凡输入里带 lora 字样的 JSON 列表都认（AnimaMultiLoraLoader 等）
        for key, val in inputs.items():
            if "lora" not in key.lower():
                continue
            loras.extend(_parse_lora_list(val))
    pos, neg = _comfy_prompts(nodes)
    return model, loras, pos, neg


def _extract_comfy(text):
    raw = text.get("prompt")
    if not raw or not raw.strip().startswith("{"):
        return None
    try:
        nodes = json.loads(raw)
    except Exception:
        return None
    if not isinstance(nodes, dict) or not nodes:
        return None
    sample = next(iter(nodes.values()))
    if not isinstance(sample, dict) or "class_type" not in sample:
        return None
    model, loras, prompt, negative = _parse_comfy(nodes)
    return {"engine": "comfyui", "model": model,
            "loras": loras, "prompt": prompt, "naiVersion": None,
            "negative": negative}


def _nai_version(data, source):
    m = re.search(r"[Vv](\d+(?:\.\d+)?)", source or "")
    if m:
        return m.group(1)
    if "v4_prompt" in data:
        return "4"
    if "xl" in (source or "").lower():
        return "3"
    return ""


def _extract_nai(text):
    software = (text.get("Software") or "").strip()
    source = (text.get("Source") or "").strip()
    raw = text.get("Comment") or text.get("comment") or ""
    data = {}
    if raw.strip().startswith("{"):
        try:
            data = json.loads(raw)
        except Exception:
            data = {}
    looks_nai = (
        "novelai" in software.lower()
        or "novelai" in source.lower()
        or ("uc" in data and "steps" in data)
        or "noise_schedule" in data
        or "v4_prompt" in data
    )
    if not looks_nai:
        return None

    ver = _nai_version(data, source)
    if not data:
        return {
            "engine": "novelai",
            "model": ("Nai " + ver).strip() if ver else "Nai",

            "loras": [],
            "prompt": raw.strip(),
            "naiVersion": ver or None,
        }

    if isinstance(data.get("v4_prompt"), dict):
        cap = data["v4_prompt"].get("caption") or {}
        parts = [str(cap.get("base_caption") or "")]
        for c in (cap.get("char_captions") or []):
            if isinstance(c, dict) and c.get("char_caption"):
                parts.append(str(c["char_caption"]))
        prompt = "\n\n".join(p for p in parts if p.strip())
    else:
        prompt = str(data.get("prompt") or "")

    return {
        "engine": "novelai",
        "model": ("Nai " + ver).strip() if ver else "Nai",
        "loras": [],
        "prompt": prompt,
        "naiVersion": ver or None,
    }


def _extract_workflow(text):
    """prompt 块缺失时，退而解析 workflow（界面图）块。

    界面图里提示词存在文本节点的 widgets_values，顺着采样器的
    positive / negative 连线在 links 表里回溯即可。
    """
    raw = text.get("workflow")
    if not raw or not raw.strip().startswith("{"):
        return None
    try:
        wf = json.loads(raw)
    except Exception:
        return None
    nodes = {}
    for n in (wf.get("nodes") or []):
        if isinstance(n, dict) and n.get("id") is not None:
            nodes[n["id"]] = n
    links = {}
    for l in (wf.get("links") or []):
        try:
            links[int(l[0])] = (int(l[1]), int(l[3]))     # link id -> (源节点, 目标节点)
        except Exception:
            continue
    if not nodes:
        return None

    def text_of(nid, depth=0):
        n = nodes.get(nid)
        if not isinstance(n, dict) or depth > 6:
            return ""
        wv = n.get("widgets_values")
        if isinstance(wv, list) and wv and isinstance(wv[0], str) and wv[0].strip():
            return wv[0]
        for inp in (n.get("inputs") or []):
            try:
                src = links[int(inp.get("link"))][0]
            except Exception:
                continue
            got = text_of(src, depth + 1)
            if got:
                return got
        return ""

    model, loras, pos, neg = "", [], "", ""
    for n in nodes.values():
        t = str(n.get("type") or "").lower()
        wv = n.get("widgets_values") if isinstance(n.get("widgets_values"), list) else []
        if not model and ("checkpoint" in t or "unet" in t) and wv:
            model = _pretty_name(wv[0])
        if "loraloader" in t.replace(" ", ""):
            for w in wv:
                if isinstance(w, str):
                    loras.extend(_parse_lora_list(w))
            if wv and isinstance(wv[0], str) and not wv[0].strip().startswith("["):
                nm = _pretty_name(wv[0])
                st = _num(wv[1] if len(wv) > 1 else None, 1.0)
                if nm and st != 0:
                    loras.append({"name": nm, "strength": st})
        if "sampler" in t:
            for inp in (n.get("inputs") or []):
                nm = inp.get("name")
                lid = inp.get("link")
                if nm in ("positive", "negative") and lid in links:
                    got = text_of(links[lid][0])
                    if nm == "positive" and got:
                        pos = got
                    elif nm == "negative" and got:
                        neg = got
    if not (pos or model or loras):
        return None
    return {"engine": "comfyui", "model": model,
            "loras": loras, "prompt": pos, "naiVersion": None, "negative": neg}


# --------------------------------------------------------------------------- 批量写入备注
IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
_aw_lock = threading.Lock()
_aw_queue = []            # 待处理的目录
_aw_queued = set()        # 已排队的目录（去重）
_aw_state = {"active": False, "done": 0, "total": 0, "written": 0,
             "skipped": 0, "nodata": 0, "failed": 0, "current": ""}
_aw_done = set()          # 本次运行已处理过的文件（重复触发时秒过）


def _autowrite_one(path):
    """对单张图：属性里已有备注 → skip；能检测到提示词且属性为空 → 写入。"""
    try:
        c = read_native_comment(path)
        if c:
            return "skip"
        g = read_gen_meta(path)
        prompt = (g.get("prompt") or "").strip()
        if not prompt:
            return "nodata"
        if write_native_comment(path, prompt):
            return "written"
        return "failed"
    except Exception:
        return "failed"


def _autowrite_tree(root):
    """递归处理一个目录树，结束后把汇总写进 genmeta.log。"""
    st = _aw_state
    files = []
    for r, _dirs, fs in os.walk(root):
        for f in fs:
            if os.path.splitext(f)[1].lower() in IMG_EXTS:
                files.append(os.path.join(r, f))
    files.sort()
    st["total"] += len(files)
    counts = {"written": 0, "skipped": 0, "nodata": 0, "failed": 0}
    for i, fp in enumerate(files):
        st["current"] = fp
        if fp in _aw_done:
            counts["skipped"] += 1
            st["done"] += 1
            continue
        _aw_done.add(fp)
        r = _autowrite_one(fp)
        counts[r] = counts.get(r, 0) + 1
        st["done"] += 1
        for k in counts:
            st[k] = st.get(k, 0) + (1 if k == r else 0)
    try:
        entry = json.dumps({"time": time.strftime("%m-%d %H:%M:%S"),
                            "root": root, "total": len(files),
                            "written": counts["written"], "skipped": counts["skipped"],
                            "nodata": counts["nodata"], "failed": counts["failed"]},
                           ensure_ascii=False)
        logp = os.path.join(DATA_DIR, "genmeta.log")
        lines = []
        if os.path.exists(logp):
            with open(logp, encoding="utf-8") as f:
                lines = f.read().splitlines()[-199:]
        lines.append(entry)
        with open(logp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except Exception:
        pass


def _autowrite_worker():
    st = _aw_state
    st["active"] = True
    while True:
        with _aw_lock:
            if not _aw_queue:
                break
            root = _aw_queue.pop(0)
        try:
            _autowrite_tree(root)
        except Exception:
            pass
    st["active"] = False
    st["current"] = ""


def _queue_autowrite_files(files):
    """只处理指定文件（用于「打开着文件夹时新出现的图」）。"""
    todo = [f for f in files if f not in _aw_done]
    if not todo:
        return

    def run():
        for fp in todo:
            if fp in _aw_done:
                continue
            _aw_done.add(fp)
            _autowrite_one(fp)

    threading.Thread(target=run, daemon=True).start()


def _queue_autowrite(path):
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        return
    with _aw_lock:
        if path in _aw_queued:
            return
        _aw_queued.add(path)
        _aw_queue.append(path)
    t = threading.Thread(target=_autowrite_worker, daemon=True)
    t.start()


def _extract_a1111(text):
    """A1111 WebUI 格式：tEXt 关键字 parameters。

    结构：正向提示词（可多行）\nNegative prompt: 负向\nSteps: 20, Sampler: ..., Model: xxx, ...
    Lora 以 <lora:名字:强度> 内联在正向里。
    """
    raw = text.get("parameters")
    if not raw or "Steps:" not in raw:
        return None
    prompt_lines, neg_lines, params = [], [], {}
    state = 0                        # 0=正向 1=负向
    for ln in raw.splitlines():
        t = ln.strip()
        if state == 0 and t.startswith("Negative prompt:"):
            state = 1
            neg_lines.append(t[len("Negative prompt:"):].strip())
            continue
        if state == 1 and t.startswith("Steps:") and ":" in t:
            for part in t.split(","):
                if ":" in part:
                    k, _, v = part.partition(":")
                    params[k.strip().lower()] = v.strip()
            continue
        (neg_lines if state else prompt_lines).append(ln)
    prompt = "\n".join(prompt_lines).strip()
    neg = "\n".join(neg_lines).strip()
    if not prompt and not params:
        return None
    model = _pretty_name(params.get("model", "").split(" [")[0].strip())
    loras = []
    for m in re.finditer(r"<lora:([^:>]+):(-?\d+\.?\d*)>", prompt):
        nm = _pretty_name(m.group(1))
        st = float(m.group(2))
        if nm and st != 0:
            loras.append({"name": nm, "strength": st})
    return {"engine": "a1111", "model": model,
            "loras": loras, "prompt": prompt, "naiVersion": None, "negative": neg}


def _diag_write(path):
    """属性写入失败时做一次现场诊断，结果进日志。

    分别验证：文件自身属性、同目录能否新建并写属性、把该文件复制到同目录能否写。
    """
    info = {}
    try:
        st = os.stat(path)
        info["size_mb"] = round(st.st_size / 1048576, 2)
        info["readonly"] = not bool(st.st_mode & 0o200)
        attr = getattr(st, "st_file_attributes", None)
        if attr is not None:
            info["attrs"] = hex(attr)
    except Exception as exc:
        info["stat_err"] = repr(exc)
    d = os.path.dirname(path)
    try:
        from PIL import Image as _Img
        tmp = os.path.join(d, "_tagpot_wtest.png")
        _Img.new("RGB", (8, 8), (1, 2, 3)).save(tmp)
        info["new_in_dir"] = write_native_comment(tmp, "x")
        try:
            os.remove(tmp)
        except Exception:
            pass
    except Exception as exc:
        info["new_in_dir_err"] = repr(exc)
    try:
        import shutil as _sh
        cp = path + ".tagpottest.png"
        _sh.copy2(path, cp)
        info["copy_write"] = write_native_comment(cp, "x")
        try:
            os.remove(cp)
        except Exception:
            pass
    except Exception as exc:
        info["copy_write_err"] = repr(exc)
    return info


def _log_genmeta(path, text, note):
    """识别失败时记一条诊断日志（data/genmeta.log，只保留最近 200 条）。"""
    try:
        entry = json.dumps({"time": time.strftime("%m-%d %H:%M:%S"), "path": path,
                            "chunks": _png_chunk_types(path)[:16],
                            "keys": sorted(text.keys()), "note": note}, ensure_ascii=False)
        logp = os.path.join(DATA_DIR, "genmeta.log")
        lines = []
        if os.path.exists(logp):
            with open(logp, encoding="utf-8") as f:
                lines = f.read().splitlines()[-199:]
        lines.append(entry)
        with open(logp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except Exception:
        pass


def read_gen_meta(path, debug=False):
    """从图片里读出 ComfyUI / NovelAI 的模型、Lora、正向提示词。"""
    empty = {"engine": "", "model": "",
             "loras": [], "prompt": "", "naiVersion": None,
             "modelOverridden": False, "lorasOverridden": False, "promptOverridden": False}
    text = _png_text(path)
    if debug:
        empty["debug"] = {"chunkKeys": sorted(text.keys()),
                          "pngChunks": _png_chunk_types(path)}
    if not text:
        return empty
    try:
        found = (_extract_comfy(text) or _extract_nai(text)
                 or _extract_a1111(text) or _extract_workflow(text))
    except Exception as exc:
        found = None
        if debug:
            empty["debug"]["parseError"] = repr(exc)
        _log_genmeta(path, text, "解析异常 " + repr(exc))
    if not found:
        if debug:
            empty["debug"]["note"] = "没有可识别的生成元数据"
            raw = text.get("prompt")
            if raw:
                empty["debug"]["promptChunkHead"] = raw[:200]
        _log_genmeta(path, text, "没有可识别的生成元数据")
        return empty
    empty.update(found)
    return empty


def image_meta(path: str):
    try:
        st = os.stat(path)
    except Exception:
        return None
    key = (path, st.st_mtime_ns, st.st_size)
    hit = _meta_cache.get(key)
    if hit:
        return hit

    w = h = 0
    if Image is not None:
        try:
            with Image.open(path) as im:
                w, h = _orientation_swap(im.size, im.getexif().get(274))
        except Exception:
            w = h = 0

    data = {
        "path": path,
        "name": os.path.basename(path),
        "w": int(w),
        "h": int(h),
        "size": int(st.st_size),
        "ctime": int(st.st_ctime * 1000),
        "mtime": int(st.st_mtime * 1000),
    }
    if len(_meta_cache) > 20000:
        _meta_cache.clear()
    _meta_cache[key] = data
    return data


def list_images(path: str):
    st = os.stat(path)
    sig = (st.st_mtime_ns,)
    hit = _dir_cache.get(path)
    if hit and hit[0] == sig:
        return hit[1]

    items = []
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    if not e.is_file():
                        continue
                    ext = os.path.splitext(e.name)[1].lower()
                    if ext not in IMAGE_EXTS:
                        continue
                    m = image_meta(e.path)
                    if m:
                        items.append(m)
                except Exception:
                    continue
    except Exception:
        pass

    items.sort(key=lambda x: (-x["mtime"], x["name"].lower()))
    if len(_dir_cache) > 400:
        _dir_cache.clear()
    _dir_cache[path] = (sig, items)
    return items


# --------------------------------------------------------------------------- Windows 属性系统
# 「备注」以 Windows 文件属性里的 System.Comment 为准（资源管理器「详细信息 → 备注」），
# 这样读到的就是用户在系统里看到的那个值，写回去也能被系统认得。
_ole32 = ctypes.WinDLL("ole32")
_shell32 = ctypes.WinDLL("shell32")
_propsys = ctypes.WinDLL("propsys")


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8)]


class _PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", ctypes.c_ulong)]


class _PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort),
                ("r2", ctypes.c_ushort), ("r3", ctypes.c_ushort),
                ("pad", ctypes.c_byte * 16)]


_IID_IPropertyStore = _GUID(
    0x886D8EEB, 0x8CF2, 0x4446,
    (ctypes.c_ubyte * 8)(0x8D, 0x02, 0xCD, 0xBA, 0x1D, 0xBD, 0xCF, 0x99))
_VT_LPWSTR = 31

_shell32.SHGetPropertyStoreFromParsingName.argtypes = [
    ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_int,
    ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
_shell32.SHGetPropertyStoreFromParsingName.restype = ctypes.c_long
_propsys.PSGetPropertyKeyFromName.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(_PROPERTYKEY)]
_propsys.PSGetPropertyKeyFromName.restype = ctypes.c_long

_comment_key = None


def _vfn(punk, idx, *argtypes):
    vtbl = ctypes.cast(punk, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtbl[idx])


def _ensure_com():
    """属性系统的调用（含按名字查属性键）都要求当前线程先初始化 COM。"""
    try:
        _ole32.CoInitializeEx(None, 2)  # APARTMENTTHREADED
    except Exception:
        pass


def _pk_comment():
    global _comment_key
    if _comment_key is None:
        _ensure_com()
        pk = _PROPERTYKEY()
        if _propsys.PSGetPropertyKeyFromName("System.Comment", ctypes.byref(pk)) != 0:
            return None
        _comment_key = pk
    return _comment_key


def _open_propstore(path, writable):
    store, _hr = _open_propstore_hr(path, writable)
    return store


def _open_propstore_hr(path, writable):
    """打开属性存储，返回 (store, HRESULT)。失败时 store 为 None。"""
    _ensure_com()
    store = ctypes.c_void_p()
    hr = _shell32.SHGetPropertyStoreFromParsingName(
        os.path.normpath(os.path.abspath(path)), None,
        0x2 if writable else 0,
        ctypes.byref(_IID_IPropertyStore), ctypes.byref(store))
    if hr != 0 or not store.value:
        return None, hr
    return store, hr


_prop_lock = threading.Lock()   # 属性存储不支持多线程并发写：串行化，否则 SetValue/Commit 随机报错


def read_native_comment(path):
    """读 Windows 属性里的备注。取不到返回 None（而不是空串），便于上层回退。"""
    pk = _pk_comment()
    if pk is None:
        return None
    with _prop_lock:
        return _read_native_comment_locked(path, pk)


def _read_native_comment_locked(path, pk):
    store = _open_propstore(path, False)
    if store is None:
        return None
    try:
        pv = _PROPVARIANT()
        fn = _vfn(store, 5, ctypes.POINTER(_PROPERTYKEY), ctypes.POINTER(_PROPVARIANT))
        if fn(store, ctypes.byref(pk), ctypes.byref(pv)) != 0:
            return None
        if pv.vt == 0:            # VT_EMPTY：系统里就是空的
            return ""
        if pv.vt != _VT_LPWSTR:
            return ""
        val = ctypes.c_wchar_p.from_address(ctypes.addressof(pv) + 8).value or ""
        _ole32.PropVariantClear(ctypes.byref(pv))
        return val
    except Exception:
        return None
    finally:
        try:
            _vfn(store, 2)(store)
        except Exception:
            pass


def write_native_comment(path, text, tries=3):
    """把备注写回 Windows 属性。成功返回 True。

    属性存储偶尔会被杀软/索引器短暂占用，重试几次基本都能过。
    """
    pk = _pk_comment()
    if pk is None:
        return False
    with _prop_lock:
        return _write_native_comment_locked(path, text, pk, tries)


def _write_native_comment_locked(path, text, pk, tries):
    store = None
    last = "open"
    for attempt in range(tries):
        store, hr = _open_propstore_hr(path, True)
        if store is not None:
            break
        last = "open hr=0x%08X" % (hr & 0xFFFFFFFF)
        time.sleep(0.12)
    if store is None:
        _log_genmeta(path, {}, "属性存储打不开（" + last + "）")
        return False
    try:
        buf = ctypes.create_unicode_buffer(text or "")
        pv = _PROPVARIANT()
        pv.vt = _VT_LPWSTR
        ptr = ctypes.c_void_p(ctypes.addressof(buf))
        ctypes.memmove(ctypes.addressof(pv) + 8, ctypes.byref(ptr), 8)
        fn = _vfn(store, 6, ctypes.POINTER(_PROPERTYKEY), ctypes.POINTER(_PROPVARIANT))
        hr = fn(store, ctypes.byref(pk), ctypes.byref(pv))
        if hr != 0:
            _log_genmeta(path, {}, "SetValue 失败 hr=0x%08X 诊断=%s"
                         % (hr & 0xFFFFFFFF, json.dumps(_diag_write(path), ensure_ascii=False)))
            return False
        hr = _vfn(store, 7)(store)   # Commit
        if hr != 0:
            _log_genmeta(path, {}, "Commit 失败 hr=0x%08X" % (hr & 0xFFFFFFFF))
            return False
        return True
    except Exception as exc:
        _log_genmeta(path, {}, "写入异常 " + repr(exc))
        return False
    finally:
        try:
            _vfn(store, 2)(store)
        except Exception:
            pass


# --------------------------------------------------------------------------- 备注（EXIF 兜底）
def _comments():
    return _load_json(COMMENTS_PATH, {})


def _set_sidecar(path, text):
    with _lock:
        d = _comments()
        if text:
            d[path] = text
        else:
            d.pop(path, None)
        _save_json(COMMENTS_PATH, d)


def _decode_user_comment(b):
    if not b:
        return ""
    try:
        head, body = b[:8], b[8:]
        if head.startswith(b"UNICODE"):
            if body[:2] in (b"\xfe\xff", b"\xff\xfe"):
                return body.decode("utf-16", "ignore").strip("\x00").strip()
            return body.decode("utf-16-be", "ignore").strip("\x00").strip()
        if head.startswith(b"ASCII") or head.startswith(b"\x00" * 8):
            return body.decode("utf-8", "ignore").strip("\x00").strip()
        return b.decode("utf-8", "ignore").strip("\x00").strip()
    except Exception:
        return ""


def _as_bytes(v):
    """piexif 有时把 BYTE 型标签读成 int 元组（XPComment 就是），统一成 bytes。"""
    if isinstance(v, (bytes, bytearray)):
        return bytes(v)
    if isinstance(v, (tuple, list)):
        out = bytearray()
        for item in v:
            if isinstance(item, (tuple, list)):
                out.extend(_as_bytes(item))
            else:
                out.append(int(item) & 0xFF)
        return bytes(out)
    return b""


def read_comment(path: str):
    text, source = "", ""
    if piexif is not None and os.path.splitext(path)[1].lower() in JPEG_EXTS:
        try:
            ex = piexif.load(path)
            uc = ex.get("Exif", {}).get(piexif.ExifIFD.UserComment)
            if uc:
                text = _decode_user_comment(_as_bytes(uc))
                if text:
                    source = "exif"
            if not text:
                xp = _as_bytes(ex.get("0th", {}).get(XPCOMMENT_TAG))
                if xp:
                    text = xp.decode("utf-16-le", "ignore").strip("\x00").strip()
                    if text:
                        source = "exif"
        except Exception:
            pass
    if not text:
        sc = _comments().get(path, "")
        if sc:
            text, source = sc, "sidecar"
    return text, source


def write_comment(path: str, text: str):
    """把备注写回图片属性。成功返回写入位置，失败抛异常由上层处理。"""
    ext = os.path.splitext(path)[1].lower()
    if piexif is not None and ext in JPEG_EXTS:
        ex = None
        try:
            ex = piexif.load(path)
        except Exception:
            ex = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
        for k in ("0th", "Exif", "GPS", "1st"):
            ex.setdefault(k, {})
        if text:
            try:
                ex["Exif"][piexif.ExifIFD.UserComment] = piexif_helper.UserComment.dump(
                    text, encoding="unicode"
                )
            except Exception:
                ex["Exif"][piexif.ExifIFD.UserComment] = b"UNICODE\x00" + text.encode("utf-16-be")
            try:
                ex["0th"][XPCOMMENT_TAG] = text.encode("utf-16-le")
            except Exception:
                pass
        else:
            ex["Exif"].pop(piexif.ExifIFD.UserComment, None)
            ex["0th"].pop(XPCOMMENT_TAG, None)
        piexif.insert(piexif.dump(ex), path)
        # 曾经写过 sidecar 的清掉，避免两处不一致
        if _comments().get(path):
            _set_sidecar(path, "")
        return "exif"

    _set_sidecar(path, text)
    return "sidecar"


# --------------------------------------------------------------------------- 缩略图
def thumb_bytes(path: str, width: int):
    st = os.stat(path)
    key = hashlib.sha1(
        f"{path}|{st.st_mtime_ns}|{st.st_size}|{width}".encode("utf-8")
    ).hexdigest()[:28]
    out = os.path.join(THUMB_DIR, key + ".jpg")
    if os.path.exists(out):
        try:
            with open(out, "rb") as f:
                return f.read()
        except Exception:
            pass
    if Image is None:
        raise RuntimeError("Pillow 不可用")

    os.makedirs(THUMB_DIR, exist_ok=True)
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        else:
            im = im.convert("RGB")
        tw = max(1, min(width, im.width))
        th = max(1, round(im.height * tw / im.width))
        im = im.resize((tw, th), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=84, optimize=True, progressive=True)
    data = buf.getvalue()
    try:
        with open(out, "wb") as f:
            f.write(data)
    except Exception:
        pass
    return data


# --------------------------------------------------------------------------- HTTP
MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


def _log(line):
    """打包成窗口程序后 sys.stderr 是 None，日志统一走这个口子。"""
    if os.environ.get("TAGPOT_LOG"):
        try:
            with open(os.path.join(DATA_DIR, "http.log"), "a", encoding="utf-8") as f:
                f.write(line + "\n")
            return
        except Exception:
            pass
    try:
        if sys.stderr is not None:
            sys.stderr.write(line + "\n")
            sys.stderr.flush()
    except Exception:
        pass


class Handler(BaseHTTPRequestHandler):
    server_version = "TagPot/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        msg = fmt % args
        if os.environ.get("TAGPOT_LOG") or "404" in msg or "500" in msg:
            _log("[%s] %s" % (time.strftime("%H:%M:%S"), msg))

    # ---- 输出helpers
    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=None, cache="no-store"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _err(self, code, msg):
        self._send(code, {"error": msg})

    # ---- 路由
    def do_GET(self):
        try:
            self._route_get()
        except BrokenPipeError:
            pass
        except Exception as e:
            self._err(500, repr(e))

    def do_POST(self):
        try:
            self._route_post()
        except Exception as e:
            self._err(500, repr(e))

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _route_get(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        p = u.path

        if p == "/" or p == "/index.html":
            return self._static("index.html")
        if p.startswith("/static/"):
            return self._static(p[len("/static/"):])

        if p == "/api/state":
            return self._send(200, {
                "roots": [root_payload(r) for r in STATE["roots"]],
                "active": STATE["active"],
            })

        if p == "/api/autowrite/status":
            return self._send(200, dict(_aw_state))

        if p == "/api/dirs":
            target = resolve_allowed((qs.get("path") or [""])[0])
            if not target or not os.path.isdir(target):
                return self._err(404, "目录不可访问")
            return self._send(200, {"path": target, "dirs": list_dirs(target)})

        if p == "/api/images":
            target = resolve_allowed((qs.get("path") or [""])[0])
            if not target or not os.path.isdir(target):
                return self._err(404, "目录不可访问")
            imgs = list_images(target)
            return self._send(200, {
                "path": target,
                "name": os.path.basename(target.rstrip("\\/")) or target,
                "images": imgs,
                "dirs": list_dirs(target),
            })

        if p == "/api/fs/places":
            return self._send(200, {"drives": list_drives(), "places": list_places()})

        if p == "/api/fs/list":
            raw = (qs.get("path") or [""])[0]
            return self._send(200, browse(raw))

        if p == "/api/drop/take":
            return self._send(200, {"paths": take_drop()})

        if p == "/api/meta":
            target = resolve_allowed((qs.get("path") or [""])[0])
            if not target or not os.path.isfile(target):
                return self._err(404, "文件不可访问")
            m = image_meta(target)
            if not m:
                return self._err(404, "无法读取")
            gen = read_gen_meta(target, debug=("debug" in qs))
            anno = _anno_get(target)

            c = read_native_comment(target)
            if c is None:
                c, src = read_comment(target)
            else:
                src = "property"
            # 早期版本曾把负向提示词误认成正向写进属性：这里改写回来
            neg = (gen.get("negative") or "").strip()
            if c and neg and c.strip() == neg:
                try:
                    if write_native_comment(target, gen["prompt"]):
                        c, src = gen["prompt"], "property"
                        _log_genmeta(target, {}, "修复：属性里的负向已改写为正向")
                except Exception:
                    pass

            if (not c and not anno.get("promptEdited")
                    and (gen.get("prompt") or "").strip()):
                # 属性里还没有备注，而图里有正向提示词：自动补写，
                # 以后在资源管理器里也能直接看到
                try:
                    if write_native_comment(target, gen["prompt"]):
                        c, src = gen["prompt"], "property"
                        _log_genmeta(target, {}, "自动写入成功")
                    else:
                        _log_genmeta(target, {}, "自动写入失败（属性系统拒绝写入）")
                except Exception as exc:
                    _log_genmeta(target, {}, "自动写入异常 " + repr(exc))

            if c:
                comment, prompt_done = c, True
            elif anno.get("promptEdited"):
                comment, prompt_done = "", True
            else:
                comment, prompt_done = gen.get("prompt") or "", False
                if comment:
                    src = "generated"

            m_ov = anno.get("model")
            if isinstance(m_ov, dict):          # 旧格式 {name, strength}
                gen["model"] = m_ov.get("name") or gen.get("model", "")
                gen["modelOverridden"] = bool(m_ov.get("name"))
            elif isinstance(m_ov, str) and m_ov.strip():
                gen["model"] = m_ov.strip()
                gen["modelOverridden"] = True
            if isinstance(anno.get("loras"), list):
                gen["loras"] = anno["loras"]
                gen["lorasOverridden"] = True
            gen["promptOverridden"] = prompt_done
            gen.pop("negative", None)   # 内部字段，不下发
            m = dict(m)
            m["comment"] = comment
            m["commentSource"] = src
            m["gen"] = gen
            m["format"] = os.path.splitext(target)[1].lstrip(".").upper()
            return self._send(200, m)

        if p == "/api/thumb":
            target = resolve_allowed((qs.get("path") or [""])[0])
            if not target or not os.path.isfile(target):
                return self._err(404, "文件不可访问")
            try:
                w = max(80, min(1200, int((qs.get("w") or ["480"])[0])))
            except Exception:
                w = 480
            try:
                data = thumb_bytes(target, w)
            except Exception:
                return self._err(500, "缩略图生成失败")
            return self._send(200, data, "image/jpeg", cache="public, max-age=604800")

        if p == "/api/full":
            target = resolve_allowed((qs.get("path") or [""])[0])
            if not target or not os.path.isfile(target):
                return self._err(404, "文件不可访问")
            ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
            try:
                with open(target, "rb") as f:
                    data = f.read()
            except Exception:
                return self._err(500, "读取失败")
            return self._send(200, data, ctype, cache="public, max-age=60")

        return self._err(404, "not found")

    def _route_post(self):
        u = urllib.parse.urlparse(self.path)
        p = u.path

        if p == "/api/roots":
            data = self._body()
            raw = (data.get("path") or "").strip().strip('"')
            if not raw:
                return self._err(400, "路径为空")
            if not os.path.isdir(raw):
                return self._err(400, "不是一个有效的文件夹")
            ap = os.path.abspath(raw)
            with _lock:
                if any(os.path.normcase(r["path"]) == os.path.normcase(ap) for r in STATE["roots"]):
                    return self._send(200, {"roots": [root_payload(r) for r in STATE["roots"]], "added": None})
                STATE["roots"].append({"id": _rid(ap), "path": ap})
                save_state()

            _queue_autowrite(ap)          # 新加的文件夹：后台批量写入备注
            return self._send(200, {
                "roots": [root_payload(r) for r in STATE["roots"]],
                "added": _rid(ap),
            })

        if p == "/api/autowrite":
            data = self._body()
            paths = data.get("paths")
            if isinstance(paths, list) and paths:
                files = []
                for raw in paths[:500]:
                    fp = resolve_allowed(str(raw or "").strip())
                    if fp and os.path.isfile(fp):
                        files.append(fp)
                if files:
                    _queue_autowrite_files(files)
                return self._send(200, {"ok": True, "files": len(files)})
            target = resolve_allowed((data.get("path") or "").strip())
            if not target or not os.path.isdir(target):
                return self._err(404, "目录不可访问")
            _queue_autowrite(target)
            return self._send(200, {"ok": True, "status": _aw_state})

        if p == "/api/roots/remove":
            data = self._body()
            rid = data.get("id")
            with _lock:
                STATE["roots"] = [r for r in STATE["roots"] if r["id"] != rid]
                if STATE["active"] == rid:
                    STATE["active"] = None
                if not STATE["roots"]:
                    global _allow_empty_save
                    _allow_empty_save = True   # 用户主动删光，允许保存空列表
                save_state()
            return self._send(200, {"roots": [root_payload(r) for r in STATE["roots"]]})

        if p == "/api/comment":
            data = self._body()
            target = resolve_allowed((data.get("path") or "").strip())
            if not target or not os.path.isfile(target):
                return self._err(404, "文件不可访问")
            text = data.get("comment") or ""
            if not text.strip():
                text = ""
            try:
                if write_native_comment(target, text):
                    where = "property"
                    if _comments().get(target):
                        _set_sidecar(target, "")
                else:
                    where = write_comment(target, text)
            except Exception as e:
                return self._err(500, "写入失败：" + repr(e))
            _meta_cache.clear()
            _dir_cache.clear()
            _anno_set(target, {"promptEdited": True})
            return self._send(200, {"ok": True, "where": where, "comment": text})

        if p == "/api/annotation":
            data = self._body()
            target = resolve_allowed((data.get("path") or "").strip())
            if not target or not os.path.isfile(target):
                return self._err(404, "文件不可访问")
            patch = {}
            # 传 null 表示「恢复成自动检测」，用于撤销自定义
            if "model" in data and data.get("model") is None:
                patch["model"] = None
            if "loras" in data and data.get("loras") is None:
                patch["loras"] = None
            if isinstance(data.get("model"), str):
                patch["model"] = data["model"].strip()          # 新格式：纯名字
            elif isinstance(data.get("model"), dict):            # 旧客户端兼容
                nm = str(data["model"].get("name") or "").strip()
                if nm:
                    patch["model"] = nm
            if isinstance(data.get("loras"), list):
                rows = []
                for it in data["loras"]:
                    if not isinstance(it, dict):
                        continue
                    name = str(it.get("name") or "").strip()
                    if not name:
                        continue
                    rows.append({"name": name, "strength": _num(it.get("strength"), 1.0)})
                patch["loras"] = rows
            if not patch:
                return self._err(400, "没有可保存的内容")
            _anno_set(target, patch)
            _meta_cache.clear()
            return self._send(200, {"ok": True})

        if p == "/api/active":
            data = self._body()
            with _lock:
                STATE["active"] = data.get("id")
                save_state()
            return self._send(200, {"ok": True})

        return self._err(404, "not found")

    def _static(self, name):
        name = name.split("?")[0].replace("\\", "/").lstrip("/")
        full = os.path.normpath(os.path.join(WEB_DIR, name))
        if not full.startswith(os.path.normpath(WEB_DIR)) or not os.path.isfile(full):
            return self._err(404, "not found")
        ext = os.path.splitext(full)[1].lower()
        with open(full, "rb") as f:
            data = f.read()
        return self._send(200, data, MIME.get(ext, "application/octet-stream"),
                          cache="no-cache" if ext in (".html", ".js", ".css") else "public, max-age=3600")


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # 浏览器/WebView 关闭窗口时常直接掐断连接，不必刷堆栈
        import sys as _sys
        et = _sys.exc_info()[0]
        if et in (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            return
        super().handle_error(request, client_address)


def build_httpd(port=8765, tries=12):
    """准备数据目录、载入配置并绑定端口。返回 (httpd, 实际端口)。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(THUMB_DIR, exist_ok=True)
    load_state()
    for p in range(port, port + tries):
        try:
            return QuietServer(("127.0.0.1", p), Handler), p
        except OSError:
            continue
    return None, port


def main():
    port = 8765
    open_browser = False          # 默认不打开浏览器；桌面外壳用 app.py
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--port" and i + 1 < len(args):
            port = int(args[i + 1])
        if a == "--browser":
            open_browser = True

    httpd, port = build_httpd(port)
    if httpd is None:
        print("没有可用端口")
        return 1

    url = f"http://127.0.0.1:{port}/"
    print(f"Tag Pot 已启动 -> {url}")
    if Image is None:
        print("!! 未检测到 Pillow，缩略图与尺寸读取不可用")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
