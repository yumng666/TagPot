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
DEMO_DIR = os.path.join(RES_DIR, "demo")


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
    raw = _load_json(CONFIG_PATH, None)
    if raw is None:
        # 主文件读不到（不存在 / 被占用 / 损坏）：先试备份，别急着用空配置
        raw = _load_json(CONFIG_PATH + ".bak", {"roots": [], "active": None})
        if raw is None:
            raw = {"roots": [], "active": None}
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


def save_state():
    _save_json(CONFIG_PATH, {"roots": STATE["roots"], "active": STATE["active"]})


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
                out.append({"name": e.name, "path": e.path, "hasDirs": has})
            except Exception:
                continue
    out.sort(key=lambda x: x["name"].lower())
    return out


def list_dirs(path: str, with_children_hint=True):
    """列出子目录（带缓存，轮询时不必反复扫盘）。"""
    try:
        st = os.stat(path)
    except Exception:
        return []
    key = (path, with_children_hint)
    hit = _dirs_cache.get(key)
    if hit and hit[0] == st.st_mtime_ns:
        return hit[1]
    try:
        out = _scan_dirs(path, with_children_hint)
    except Exception:
        return []
    if len(_dirs_cache) > 300:
        _dirs_cache.clear()
    _dirs_cache[key] = (st.st_mtime_ns, out)
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
        out.append({"name": name, "strength": _num(it.get("strength"), 1.0)})
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
        else:
            item[key] = val
    if item:
        data[path] = item
    else:
        data.pop(path, None)
    _save_json(ANNO_PATH, data)
    _anno_cache["mtime"] = None
    return item


def _png_text(path):
    """取 PNG 的 tEXt/iTXt 文本块；非 PNG 返回 {}。"""
    if Image is None:
        return {}
    try:
        with Image.open(path) as im:
            if (im.format or "").upper() != "PNG":
                return {}
            return {str(k): (v.decode("utf-8", "ignore") if isinstance(v, bytes) else str(v))
                    for k, v in (getattr(im, "text", None) or {}).items()}
    except Exception:
        return {}


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


def _comfy_prompt(nodes):
    for node in nodes.values():
        if not isinstance(node, dict):
            continue
        ct = str(node.get("class_type") or "")
        if ct.startswith("KSampler") or ct in ("SamplerCustom", "SamplerCustomAdvanced"):
            got = _follow_text(nodes, (node.get("inputs") or {}).get("positive"))
            if got:
                return got
    # 兜底：取最长的一段文本（正向通常更长）
    texts = []
    for node in nodes.values():
        if not isinstance(node, dict):
            continue
        t = (node.get("inputs") or {}).get("text")
        if isinstance(t, str) and t.strip():
            texts.append(t)
    texts.sort(key=len, reverse=True)
    return texts[0] if texts else ""


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
            if name:
                loras.append({"name": name,
                              "strength": _num(inputs.get("strength_model"), 1.0)})
        # 其它加载器：凡输入里带 lora 字样的 JSON 列表都认（AnimaMultiLoraLoader 等）
        for key, val in inputs.items():
            if "lora" not in key.lower():
                continue
            loras.extend(_parse_lora_list(val))
    return model, loras, _comfy_prompt(nodes)


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
    model, loras, prompt = _parse_comfy(nodes)
    return {"engine": "comfyui", "model": model,
            "modelStrength": 1.0 if model else None,
            "loras": loras, "prompt": prompt, "naiVersion": None}


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
            "modelStrength": None,
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
        "modelStrength": None,
        "loras": [],
        "prompt": prompt,
        "naiVersion": ver or None,
    }


def read_gen_meta(path):
    """从图片里读出 ComfyUI / NovelAI 的模型、Lora、正向提示词。"""
    empty = {"engine": "", "model": "", "modelStrength": None,
             "loras": [], "prompt": "", "naiVersion": None,
             "modelOverridden": False, "lorasOverridden": False, "promptOverridden": False}
    text = _png_text(path)
    if not text:
        return empty
    try:
        found = _extract_comfy(text) or _extract_nai(text)
    except Exception:
        found = None
    if not found:
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
    _ensure_com()
    store = ctypes.c_void_p()
    hr = _shell32.SHGetPropertyStoreFromParsingName(
        os.path.normpath(os.path.abspath(path)), None,
        0x2 if writable else 0,
        ctypes.byref(_IID_IPropertyStore), ctypes.byref(store))
    if hr != 0 or not store.value:
        return None
    return store


def read_native_comment(path):
    """读 Windows 属性里的备注。取不到返回 None（而不是空串），便于上层回退。"""
    pk = _pk_comment()
    if pk is None:
        return None
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


def write_native_comment(path, text):
    """把备注写回 Windows 属性。成功返回 True。"""
    pk = _pk_comment()
    if pk is None:
        return False
    store = _open_propstore(path, True)
    if store is None:
        return False
    try:
        buf = ctypes.create_unicode_buffer(text or "")
        pv = _PROPVARIANT()
        pv.vt = _VT_LPWSTR
        ptr = ctypes.c_void_p(ctypes.addressof(buf))
        ctypes.memmove(ctypes.addressof(pv) + 8, ctypes.byref(ptr), 8)
        fn = _vfn(store, 6, ctypes.POINTER(_PROPERTYKEY), ctypes.POINTER(_PROPVARIANT))
        if fn(store, ctypes.byref(pk), ctypes.byref(pv)) != 0:
            return False
        if _vfn(store, 7)(store) != 0:   # Commit
            return False
        return True
    except Exception:
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
            gen = read_gen_meta(target)
            anno = _anno_get(target)

            c = read_native_comment(target)
            if c is None:
                c, src = read_comment(target)
            else:
                src = "property"

            if (not c and not anno.get("promptEdited")
                    and (gen.get("prompt") or "").strip()):
                # 属性里还没有备注，而图里有正向提示词：自动补写，
                # 以后在资源管理器里也能直接看到
                try:
                    if write_native_comment(target, gen["prompt"]):
                        c, src = gen["prompt"], "property"
                except Exception:
                    pass

            if c:
                comment, prompt_done = c, True
            elif anno.get("promptEdited"):
                comment, prompt_done = "", True
            else:
                comment, prompt_done = gen.get("prompt") or "", False
                if comment:
                    src = "generated"

            if isinstance(anno.get("model"), dict):
                gen["model"] = anno["model"].get("name", gen.get("model", ""))
                if "strength" in anno["model"]:
                    gen["modelStrength"] = anno["model"]["strength"]
                gen["modelOverridden"] = True
            if isinstance(anno.get("loras"), list):
                gen["loras"] = anno["loras"]
                gen["lorasOverridden"] = True
            gen["promptOverridden"] = prompt_done
            if gen.get("engine") == "novelai" and not gen.get("lorasOverridden"):
                gen["modelStrength"] = None

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
            return self._send(200, {
                "roots": [root_payload(r) for r in STATE["roots"]],
                "added": _rid(ap),
            })

        if p == "/api/roots/remove":
            data = self._body()
            rid = data.get("id")
            with _lock:
                STATE["roots"] = [r for r in STATE["roots"] if r["id"] != rid]
                if STATE["active"] == rid:
                    STATE["active"] = None
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
            if isinstance(data.get("model"), dict):
                patch["model"] = {
                    "name": str(data["model"].get("name") or "").strip(),
                    "strength": _num(data["model"].get("strength"), 1.0),
                }
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
