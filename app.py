# -*- coding: utf-8 -*-
"""Tag Pot 桌面版启动器。

用 WebView2 起一个原生窗口承载界面，不打开浏览器、不显示地址栏与标签页。
后端仍然是 server.py 里的本地 HTTP 服务，只在 127.0.0.1 上监听。
"""

import ctypes
import os
import shutil
import sys
import threading
import time

FROZEN = bool(getattr(sys, "frozen", False))
if FROZEN:
    BASE = os.path.dirname(sys.executable)
else:
    BASE = os.path.dirname(os.path.abspath(__file__))

_vendor = os.path.join(BASE, "vendor")
if os.path.isdir(_vendor):
    sys.path.insert(0, _vendor)
os.chdir(BASE)

import server  # noqa: E402  （先把它要的 vendor 路径准备好）

try:
    import webview  # noqa: E402
except Exception as exc:  # pragma: no cover
    webview = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None

TITLE = "Tag Pot"
ICON = os.path.join(server.RES_DIR, "assets", "tagpot.ico")
DROP_MSG = "TagPotDrop"


def _install_webview2_hooks():
    """打开 WebView2 的外部拖放，并把拖入项的真实绝对路径交给后端暂存。

    HTML5 拖放拿不到绝对路径，只有 WebView2 的 AdditionalObjects 能给出
    CoreWebView2File.Path；这里截获页面发来的 TagPotDrop 消息。
    """
    try:
        from webview.platforms import edgechromium as ec
    except Exception:
        return False

    original_notify = ec.EdgeChrome.on_script_notify

    def on_script_notify(self, sender, args):
        try:
            if args.get_WebMessageAsJson() == '"%s"' % DROP_MSG:
                objs = args.get_AdditionalObjects()
                for f in list(objs or []):
                    path = getattr(f, "Path", None)
                    if path:
                        server.push_drop(path)
                return
        except Exception:
            pass
        return original_notify(self, sender, args)

    ec.EdgeChrome.on_script_notify = on_script_notify

    original_init = ec.EdgeChrome.__init__

    def __init__(self, *a, **kw):
        original_init(self, *a, **kw)
        ok = False
        try:
            self.webview.AllowExternalDrop = True
            ok = bool(self.webview.AllowExternalDrop)
        except Exception as exc:
            ok = "ERR %r" % (exc,)
        if os.environ.get("TAGPOT_LOG"):
            server._log("[dnd] AllowExternalDrop=%s" % ok)

    ec.EdgeChrome.__init__ = __init__
    return True


def _install_webview2_hooks():
    """打开 WebView2 的外部拖放，并把拖入项的真实绝对路径交给后端暂存。

    HTML5 拖放拿不到绝对路径，只有 WebView2 的 AdditionalObjects 能给出
    CoreWebView2File.Path；这里截获页面发来的 TagPotDrop 消息。
    """
    try:
        from webview.platforms import edgechromium as ec
    except Exception:
        return False

    original_notify = ec.EdgeChrome.on_script_notify

    def on_script_notify(self, sender, args):
        try:
            if args.get_WebMessageAsJson() == '"%s"' % DROP_MSG:
                objs = args.get_AdditionalObjects()
                for f in list(objs or []):
                    path = getattr(f, "Path", None)
                    if path:
                        server.push_drop(path)
                return
        except Exception:
            pass
        return original_notify(self, sender, args)

    ec.EdgeChrome.on_script_notify = on_script_notify

    original_init = ec.EdgeChrome.__init__

    def __init__(self, *a, **kw):
        original_init(self, *a, **kw)
        ok = False
        try:
            self.webview.AllowExternalDrop = True
            ok = bool(self.webview.AllowExternalDrop)
        except Exception as exc:
            ok = "ERR %r" % (exc,)
        if os.environ.get("TAGPOT_LOG"):
            server._log("[dnd] AllowExternalDrop=%s" % ok)

    ec.EdgeChrome.__init__ = __init__
    return True


def _seed_demo():
    """首次运行时把随包演示素材复制到 data/demo，并写好配置。

    用 .seeded 标记防止重复播种；若用户已经有自己的根目录，绝不覆盖。
    （配置意外被清空时，这里能让演示相册回来。）
    """
    try:
        marker = os.path.join(server.DATA_DIR, ".seeded")
        cfg = server._load_json(server.CONFIG_PATH, None) or {}
        if cfg.get("roots"):
            open(marker, "w").write("1")
            return
        if os.path.exists(marker):
            return
        src = server.DEMO_DIR
        if not os.path.isdir(src):
            return
        dst = os.path.join(server.DATA_DIR, "demo")
        if not os.path.isdir(dst):
            shutil.copytree(src, dst)
        roots = []
        for name in sorted(os.listdir(dst)):
            p = os.path.join(dst, name)
            if os.path.isdir(p):
                roots.append({"id": server._rid(p), "path": p})
        if roots and not cfg.get("roots"):
            # 优先选中带子文件夹的那个，首屏就能看到展开效果
            active = roots[0]["id"]
            for r in roots:
                try:
                    if any(os.path.isdir(os.path.join(r["path"], n)) for n in os.listdir(r["path"])):
                        active = r["id"]
                        break
                except Exception:
                    pass
            server._save_json(server.CONFIG_PATH, {"roots": roots, "active": active})
        open(marker, "w").write("1")
    except Exception:
        pass



def _msgbox(text, error=True):
    try:
        ctypes.windll.user32.MessageBoxW(None, text, TITLE, 0x10 if error else 0x40)
    except Exception:
        print(text)


def _app_id():
    """让任务栏把它当成独立应用，而不是 python 进程。"""
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("TagPot.Gallery.1")
    except Exception:
        pass


def _dress_titlebar(delay=1.0):
    """把系统标题栏染成与蓝白主题一致的浅色（Win11，失败自动忽略）。"""

    def run():
        time.sleep(delay)
        try:
            u32 = ctypes.windll.user32
            hwnd = u32.FindWindowW(None, TITLE)
            if not hwnd:
                return
            dwm = ctypes.windll.dwmapi

            def setv(attr, val):
                v = ctypes.c_int(val)
                dwm.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), attr, ctypes.byref(v), 4)

            setv(20, 0)            # 关掉深色标题栏
            setv(35, 0x00F8F4F0)   # 标题栏底色 #F0F4F8 左右
            setv(36, 0x00331A0D)   # 标题文字 #0D1A33
        except Exception:
            pass

    threading.Thread(target=run, daemon=True).start()


def _window_size():
    """按屏幕逻辑尺寸取一个合适的初始窗口大小，避免小屏上超出边界。"""
    try:
        u32 = ctypes.windll.user32
        try:
            u32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
        except Exception:
            pass
        sw, sh = u32.GetSystemMetrics(0), u32.GetSystemMetrics(1)
        dpi = u32.GetDpiForSystem() if hasattr(u32, "GetDpiForSystem") else 96
        scale = (dpi or 96) / 96.0
        lw, lh = sw / scale, sh / scale
        return (
            int(min(1360, max(1000, lw - 120))),
            int(min(880, max(640, lh - 140))),
        )
    except Exception:
        return 1360, 880


def _selftest():
    """TAGPOT_SELFTEST=1 时，在真实窗口里跑一遍断言后自动关闭。"""
    print("SELFTEST-BOOT", flush=True)
    time.sleep(4.0)
    print("SELFTEST-WAKE windows=%d" % len(webview.windows), flush=True)
    w = webview.windows[0] if webview.windows else None
    try:
        if w is None:
            print("SELFTEST-ERROR no window", flush=True)
            return
        r = w.evaluate_js(
            "JSON.stringify({"
            "ready: document.documentElement.dataset.appReady || '',"
            "roots: document.querySelectorAll('#menu > .group').length,"
            "subOpen: (document.querySelector('#menu .sub') || {}).offsetHeight || 0,"
            "tiles: document.querySelectorAll('#masonry .tile').length,"
            "decoded: [...document.querySelectorAll('#masonry img')].filter(i => i.naturalWidth > 0).length,"
            "hasActive: !!document.querySelector('#menu .item.active'),"
            "path: document.getElementById('pathText').textContent,"
            "vw: innerWidth, vh: innerHeight"
            "})"
        )
        print("SELFTEST " + str(r), flush=True)
    except Exception as exc:
        print("SELFTEST-ERROR " + repr(exc), flush=True)
    finally:
        try:
            if w is not None:
                w.destroy()
        except Exception:
            pass


def _wait_port(port, timeout=10.0):
    """等服务真正能连上再开窗口，否则 WebView2 有可能先导航到错误页。"""
    import socket
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with socket.create_connection(("127.0.0.1", port), 0.3):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def main():
    if webview is None:
        _msgbox(
            "缺少桌面窗口依赖 pywebview。\n\n请在命令行执行：\n"
            'pip install --target "%s" pywebview\n\n%s' % (os.path.join(BASE, "vendor"), _IMPORT_ERROR)
        )
        return 1

    _app_id()
    _install_webview2_hooks()

    httpd, port = server.build_httpd(8765)
    for r in server.STATE["roots"]:
        server._queue_autowrite(r["path"])   # 启动即后台批量写入备注（已有备注的会跳过）
    if httpd is None:
        _msgbox("没有可用端口（8765 起 12 个都被占用）。")
        return 1

    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    if not _wait_port(port):
        _msgbox("本地服务没有启动起来（端口 %d）。" % port)
        return 1
    url = "http://127.0.0.1:%d/?shell=desktop" % port

    w, h = _window_size()
    webview.create_window(
        TITLE,
        url,
        width=w,
        height=h,
        min_size=(960, 600),
        background_color="#f5f8ff",
        text_select=False,
        zoomable=False,
    )
    _dress_titlebar()

    try:
        webview.start(
            func=_selftest if os.environ.get("TAGPOT_SELFTEST") else None,
            icon=ICON if os.path.exists(ICON) else None,
            private_mode=False,
            storage_path=os.path.join(server.DATA_DIR, "webview"),
        )
    finally:
        try:
            httpd.shutdown()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        log = os.path.join(server.DATA_DIR, "error.log")
        try:
            os.makedirs(server.DATA_DIR, exist_ok=True)
            with open(log, "w", encoding="utf-8") as f:
                f.write(traceback.format_exc())
        except Exception:
            log = "(无法写入日志)"
        _msgbox("Tag Pot 启动失败。\n日志：%s\n\n%s" % (log, traceback.format_exc()[-600:]))
        sys.exit(1)
