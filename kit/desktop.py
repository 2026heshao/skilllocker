#!/usr/bin/env python3
"""skillkit 桌面端外壳（pywebview）。

- 直接 `import skillkit`，不启 HTTP 服务、不做 REST/序列化层；
- 前端通过 window.pywebview.api 调用这里的方法，数据来自 skillkit.db；
- 窗口无边框，标题栏由前端 pointer 事件拖动（app.js window move），
  标题栏双击切换最大化；
- 启动时建窗即按 1200x760 居中（含 WinForms 去边框的尺寸补偿），不做二次摆位；
- 关闭即退出进程，编辑内容即时写库（不做去抖，避免隐藏窗口丢编辑）。

用法：
    双击 D:\\skillkit\\启动 skillkit.bat   # 推荐：pythonw 静默启动，无控制台
    python desktop.py            # 启动（带控制台，便于看日志）
    python desktop.py --debug    # 打开 WebView2 调试面板
    python desktop.py --selftest # 无人值守自检（离屏跑一遍 UI 与写库）
"""
import argparse
import ctypes
import ctypes.wintypes  # noqa: F401 - GetSystemMetrics 等需要 wintypes 结构体
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time

import webview

import skillkit

KIT = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(os.path.dirname(KIT), "ui")
INDEX = os.path.join(UI_DIR, "index.html")
PROFILE = os.path.join(KIT, "desktop_profile")
ICON = os.path.join(KIT, "assets", "skillkit.ico")
WIN_W, WIN_H = 1200, 760
MIN_W, MIN_H = 1180, 720

# 无边框 WinForms 窗口会被系统去掉 WS_MINIMIZEBOX，任务栏按钮点击就不会最小化；
# 这里在窗口显示后补回该样式位，并在每次窗口状态变化后重新补一次。
WS_MINIMIZEBOX = 0x00020000
_WINDOW_ICONS = {}  # (slot, px) -> HICON，缓存住句柄，重复应用图标时不再泄漏

if sys.platform == "win32":
    # 独立 WinDLL 实例承载签名。不能给全局 ctypes.windll.user32 设 argtypes：
    # pywebview 的 BrowserForm.move() 拖动时把 None 当宽高传给 SetWindowPos，
    # 全局声明会把它变成 ctypes.ArgumentError，整个标题栏拖动直接失效。
    _spos = ctypes.WinDLL("user32")
    _spos.SetWindowPos.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.HWND,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_uint]
    _spos.SetWindowPos.restype = ctypes.c_bool


# log_ui 的并发写保护（pywebview 每个 js_api 调用一个线程）
_log_lock = threading.Lock()


# ---------------------------------------------------------------- 尺寸/DPI
def _dpi_scale():
    """系统缩放（120 DPI = 125% = 1.25）。取不到就按 1.0。"""
    if sys.platform != "win32":
        return 1.0
    try:
        dpi = ctypes.windll.user32.GetDpiForSystem()
        return (dpi or 96) / 96.0
    except Exception:  # noqa: BLE001 - 老系统没有该 API
        return 1.0


def _work_area_css():
    """屏幕工作区，换算成 CSS 像素。

    pywebview 建窗时按 CSS 像素传入，系统再乘以缩放；进程若未声明 DPI 感知，
    Windows 返回的坐标本身已被虚拟化成 CSS 像素，不能重复折算。
    """
    if sys.platform != "win32":
        return WIN_W, WIN_H
    scale = _dpi_scale()
    aware = 0  # 0=unaware 1=system 2=per-monitor
    try:
        value = ctypes.c_int(0)
        ctypes.windll.shcore.GetProcessDpiAwareness(None, ctypes.byref(value))
        aware = value.value
    except Exception:  # noqa: BLE001
        pass
    try:
        rect = ctypes.wintypes.RECT()
        ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)
        width = rect.right - rect.left
        height = rect.bottom - rect.top
    except Exception:  # noqa: BLE001
        return WIN_W, WIN_H
    if not width or not height:
        return WIN_W, WIN_H
    if not aware:
        return width, height
    return int(width / scale), int(height / scale)


def fit_size():
    """按工作区收口：默认 1200x760，屏幕放不下就压到工作区（留 16px 余量）。"""
    work_w, work_h = _work_area_css()
    width = min(WIN_W, max(MIN_W, work_w - 16))
    height = min(WIN_H, max(MIN_H, work_h - 16))
    return width, height


def _make_dpi_aware():
    """建窗之前声明 DPI 感知，之后的坐标统一按物理像素算，避免系统二次虚拟化。"""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass


def _nc_css():
    """frameless 建窗时 WinForms 会裁掉的标题栏/边框，换算成 CSS 像素。

    实测 125%（120 DPI）：左右共 18、上下共 47 物理像素，正好是
    2*(SM_CXSIZEFRAME+SM_CXPADDEDBORDER) 和 SM_CYCAPTION+2*(SM_CYSIZEFRAME+SM_CXPADDEDBORDER)。
    create_window 的 width/height 是“改边框之前”的 Size，所以要把这份差额加回去，
    第一帧外框才是目标尺寸，启动后不用再 SetWindowPos 补一刀。
    """
    if sys.platform != "win32":
        return 0, 0
    try:
        metrics = ctypes.windll.user32.GetSystemMetrics
        frame_x = metrics(32)   # SM_CXSIZEFRAME
        frame_y = metrics(33)   # SM_CYSIZEFRAME
        padded = metrics(92)    # SM_CXPADDEDBORDER
        caption = metrics(4)    # SM_CYCAPTION
    except Exception:  # noqa: BLE001
        return 0, 0
    scale = _dpi_scale() or 1.0
    # 用 ceil：WinForms 的裁剪量按物理像素取整，round 会少 1px（实测 125% 下 1200 变成 1199）
    return (int(math.ceil(2 * (frame_x + padded) / scale)),
            int(math.ceil((caption + 2 * (frame_y + padded)) / scale)))


def _centered_rect(width, height):
    """工作区居中的左上角（CSS 像素），与 create_window 的 x/y 同一坐标系。"""
    work_w, work_h = _work_area_css()
    return max(0, (work_w - width) // 2), max(0, (work_h - height) // 2)


def _hwnd_by_title(prefix="skillkit", visible_only=True):
    """按标题前缀找主窗口。

    WebView2 会挂出同前缀的小工具窗（class = Chrome_WidgetWin_1），
    这里只认 WinForms 主窗口（class = WindowsForms10.*），避免抓错句柄。
    visible_only=False 供 --selftest 用：自检窗口是隐藏建出来的，但外框照样可读写。
    """
    hits = []

    def _cb(hwnd, _arg):
        if visible_only and not ctypes.windll.user32.IsWindowVisible(hwnd):
            return True
        n = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        if not n:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, n + 1)
        if not buf.value.startswith(prefix):
            return True
        cls = ctypes.create_unicode_buffer(256)
        ctypes.windll.user32.GetClassNameW(hwnd, cls, 256)
        if not cls.value.startswith("WindowsForms10"):
            return True
        rect = ctypes.wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        area = (rect.right - rect.left) * (rect.bottom - rect.top)
        hits.append((area, hwnd))
        return True

    ctypes.windll.user32.EnumWindows(
        ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)(_cb), 0)
    return max(hits)[1] if hits else None


def window_bounds():
    """主窗口外框，换算成 CSS 像素（前端 grip 的坐标系）。

    进程已声明 DPI 感知，GetWindowRect 给的是物理像素；前端 screenX/innerWidth
    都是 CSS 像素，所以这里统一除以系统缩放，读写两侧才闭合。
    """
    if sys.platform != "win32":
        return None
    hwnd = _hwnd_by_title(visible_only=False)
    if not hwnd:
        return None
    rect = ctypes.wintypes.RECT()
    if not ctypes.windll.user32.GetWindowRect(ctypes.wintypes.HWND(hwnd), ctypes.byref(rect)):
        return None
    scale = _dpi_scale()
    return {
        "x": int(round(rect.left / scale)),
        "y": int(round(rect.top / scale)),
        "w": int(round((rect.right - rect.left) / scale)),
        "h": int(round((rect.bottom - rect.top) / scale)),
    }


def set_window_bounds(x, y, width, height):
    """按前端算好的 CSS 矩形摆窗；物理像素换算 + 最小尺寸兜底。

    SWP_NOZORDER | SWP_NOACTIVATE：拖动中不要抢焦点、不要改变置顶层级。
    """
    if sys.platform != "win32":
        raise RuntimeError("仅 Windows 桌面端支持调整窗口尺寸")
    hwnd = _hwnd_by_title(visible_only=False)
    if not hwnd:
        raise RuntimeError("窗口未就绪")
    handle = ctypes.wintypes.HWND(hwnd)
    if ctypes.windll.user32.IsZoomed(handle):
        return False  # 最大化态不跟随 grip，先还原再拖
    scale = _dpi_scale()
    px = lambda value: int(round(float(value) * scale))  # noqa: E731
    win_w = max(px(MIN_W), px(width))
    win_h = max(px(MIN_H), px(height))
    return bool(_spos.SetWindowPos(handle, None, px(x), px(y), win_w, win_h,
                                   0x0004 | 0x0010))


def _apply_window_icon(hwnd):
    """按系统真实尺寸重设窗口图标。

    pywebview 交给 WinForms 的 Form.Icon 只会取 .ico 里的一帧（32px），任务栏与
    Alt-Tab 在 125%/150% 缩放下会糊；这里按 SM_CXSMICON / SM_CXICON 分别从
    多尺寸 ico 里取最合适的一帧，再 SendMessage(WM_SETICON) 落到窗口上。

    必须等窗口真正 Show 之后再调用：`webview.start(func)` 的函数线程是**先于**
    `guilib.create_window()` 启动的，此时窗口句柄还不存在；早期实现直接
    `_hwnd_by_title()` 会拿到 None，导致图标与最小化位一次都没生效。
    """
    if sys.platform != "win32" or not os.path.isfile(ICON):
        return False
    try:
        user32 = ctypes.WinDLL("user32")  # 独立实例：不改全局 windll 的 argtypes
        user32.LoadImageW.argtypes = [ctypes.wintypes.HINSTANCE, ctypes.wintypes.LPCWSTR,
                                      ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        user32.LoadImageW.restype = ctypes.c_void_p
        user32.SendMessageW.argtypes = [ctypes.wintypes.HWND, ctypes.c_uint,
                                        ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]
        user32.SendMessageW.restype = ctypes.c_void_p
        applied = False
        for metric, slot in ((49, 0), (11, 1)):  # SM_CXSMICON→ICON_SMALL，SM_CXICON→ICON_BIG
            px = user32.GetSystemMetrics(metric) or 32
            key = (slot, px)
            handle = _WINDOW_ICONS.get(key)
            if not handle:
                handle = user32.LoadImageW(None, ICON, 1, px, px, 0x0010)  # IMAGE_ICON | LR_LOADFROMFILE
                if handle:
                    _WINDOW_ICONS[key] = handle
            if handle:
                user32.SendMessageW(ctypes.wintypes.HWND(hwnd), 0x0080, slot, handle)
                applied = True
        # 不要再发 WM_SETICON(ICON_SMALL2)：实测（Win11 22631）给 slot 2 设小图会把
        # ICON_BIG 一起改成这张小图，Alt-Tab 的大图标反过来变得又糊又小。slot 2
        # 本来就会回退到 ICON_SMALL（实测 WM_GETICON(2) 不设也返回同一张），无需干预。
        return applied
    except Exception:  # noqa: BLE001 - 图标失败不能拖累启动
        return False


def _window_icon(hwnd, slot):
    """读回窗口当前图标句柄（自检用）；0 表示没设置。"""
    if sys.platform != "win32" or not hwnd:
        return 0
    try:
        user32 = ctypes.WinDLL("user32")
        user32.SendMessageW.argtypes = [ctypes.wintypes.HWND, ctypes.c_uint,
                                        ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]
        user32.SendMessageW.restype = ctypes.c_void_p
        return user32.SendMessageW(ctypes.wintypes.HWND(hwnd), 0x007F, slot, 0) or 0  # WM_GETICON
    except Exception:  # noqa: BLE001
        return 0


def _ensure_minimize_box(hwnd):
    """补回 WS_MINIMIZEBOX，让任务栏按钮点击 = 最小化 / 再点恢复。"""
    if sys.platform != "win32" or not hwnd:
        return False
    try:
        user32 = ctypes.windll.user32
        handle = ctypes.wintypes.HWND(hwnd)
        style = user32.GetWindowLongW(handle, -16)  # GWL_STYLE
        if style & WS_MINIMIZEBOX:
            return True
        user32.SetWindowLongW(handle, -16, style | WS_MINIMIZEBOX)
        return bool(user32.GetWindowLongW(handle, -16) & WS_MINIMIZEBOX)
    except Exception:  # noqa: BLE001
        return False


def _refresh_window_chrome(hwnd=None):
    """窗口图标 + 最小化位统一兜底；返回命中的主窗口句柄。"""
    if sys.platform != "win32":
        return None
    hwnd = hwnd or _hwnd_by_title(visible_only=False)
    if not hwnd:
        return None
    _apply_window_icon(hwnd)
    _ensure_minimize_box(hwnd)
    return hwnd


def _keep_window_chrome(*_args):
    """窗口最小化/最大化/还原后，WinForms 会重算样式并抹掉我们补的位。

    pywebview 的 events.*.set() 在 UI 线程触发后另起线程回调这里；分几次重试，
    避开 WinForms 在同一轮消息里晚一步的样式更新。
    """
    if sys.platform != "win32":
        return
    for delay in (0.0, 0.15, 0.5):
        if delay:
            time.sleep(delay)
        if not _refresh_window_chrome():
            return


def on_start(window):
    """正常启动：等窗口真正出现后再补图标/最小化位，并兜底 pythonw 的最小化态。

    `webview.start(func)` 会先启动 func 线程、再创建窗口（见 pywebview.start），
    因此这里必须等 shown 事件；直接查句柄会得到 None（旧版的 bug）。
    """
    if sys.platform != "win32":
        return
    try:
        window.events.shown.wait(10)
    except Exception:  # noqa: BLE001
        pass
    deadline = time.time() + 5
    hwnd = None
    while time.time() < deadline:
        hwnd = _hwnd_by_title(visible_only=False)
        if hwnd:
            break
        time.sleep(0.05)
    if not hwnd:
        return
    _refresh_window_chrome(hwnd)
    handle = ctypes.wintypes.HWND(hwnd)
    if ctypes.windll.user32.IsIconic(handle):
        ctypes.windll.user32.ShowWindow(handle, 9)  # SW_RESTORE
    # 任务栏点击最小化/恢复时 WinForms 会重算样式，挂上事件后每次再补一遍
    for event in (window.events.minimized, window.events.maximized, window.events.restored):
        try:
            event += _keep_window_chrome
        except Exception:  # noqa: BLE001
            pass
    time.sleep(0.2)  # 等 WinForms 首帧消息处理完，再补一次
    _refresh_window_chrome()




def _clipboard_set(text):
    """不装 pyperclip：直接用 Win32 剪贴板写 Unicode 文本。"""
    text = str(text)[:2000]
    kernel32 = ctypes.windll.kernel32
    user32 = ctypes.windll.user32
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    user32.SetClipboardData.restype = ctypes.c_void_p
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    if not user32.OpenClipboard(None):
        raise RuntimeError("剪贴板被占用")
    try:
        user32.EmptyClipboard()
        size = (len(text) + 1) * ctypes.sizeof(ctypes.c_wchar)
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            raise RuntimeError("剪贴板分配失败")
        ptr = kernel32.GlobalLock(handle)
        ctypes.memmove(ptr, ctypes.create_unicode_buffer(text), size)
        kernel32.GlobalUnlock(handle)
        user32.SetClipboardData(CF_UNICODETEXT, handle)
    finally:
        user32.CloseClipboard()


def _guard(fn):
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 统一回给前端显示
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    wrapper.__name__ = fn.__name__
    return wrapper


class Api:
    """pywebview js_api：每个方法都在独立线程调用，连接按调用新建。"""

    def __init__(self):
        self._window = None
        self._maxed = False

    def attach(self, window):
        self._window = window

    # ---------------------------------------------------------- 数据
    @_guard
    def bootstrap(self):
        con = skillkit.db()
        try:
            cfg = skillkit.load_config()
            skillkit.seed_meta(con, cfg)
            boards = skillkit.list_boards(con, cfg)
            skillkit.prune_meta(con, boards)
            return {"ok": True, "data": boards, "meta": skillkit.get_meta(con)}
        finally:
            con.close()

    @_guard
    def save_meta(self, scope, key, payload):
        con = skillkit.db()
        try:
            return {"ok": True, "row": skillkit.set_meta(con, scope, key, payload)}
        finally:
            con.close()

    @_guard
    def clear_meta(self, scope, key):
        con = skillkit.db()
        try:
            skillkit.clear_meta(con, scope, key)
            return {"ok": True}
        finally:
            con.close()

    @_guard
    def delete_skill(self, scope, key):
        """彻底删除：shared 删中央实体 + 所有链接；unique 只删该 agent 的副本。"""
        if scope not in ("shared", "unique"):
            raise RuntimeError("未知 scope: %s" % scope)
        con = skillkit.db()
        try:
            cfg = skillkit.load_config()
            if scope == "unique":
                if "/" not in key:
                    raise RuntimeError("独有 skill 的 key 必须带 tool 前缀")
                tool, name = key.split("/", 1)
                urow = skillkit.find_unique_row(con, cfg, tool, name)
                if urow and not urow.get("deletable", True):
                    raise RuntimeError(
                        "这条来自外部来源（%s），由 agent 自己管理，本工具不删除；"
                        "确要删除请打开该目录手动处理" % urow["path"])
                action = skillkit.remove_unique(con, cfg, tool, name)
            else:
                row = con.execute("select name from skills where name=?", (key,)).fetchone()
                if not row:
                    raise RuntimeError("skill 不存在：%s" % key)
                central = os.path.join(cfg["store"], key)
                files, size = skillkit.dir_stats(central)
                links = skillkit.remove_skill(con, cfg, key, purge=True)
                skillkit.clear_meta(con, "shared", key)
                action = {"scope": "shared", "key": key, "files": files,
                          "bytes": size, "links": len(links), "central": True,
                          "path": os.path.normpath(central).replace(os.sep, "\\")}
            boards = skillkit.list_boards(con, cfg)
            skillkit.prune_meta(con, boards)
            return {"ok": True, "data": boards, "meta": skillkit.get_meta(con),
                    "action": action}
        finally:
            con.close()

    @_guard
    def reseed_meta(self):
        con = skillkit.db()
        try:
            cfg = skillkit.load_config()
            skillkit.reseed_meta(con, cfg)
            boards = skillkit.list_boards(con, cfg)
            return {"ok": True, "data": boards, "meta": skillkit.get_meta(con)}
        finally:
            con.close()

    @_guard
    def diff(self, scope, key):
        con = skillkit.db()
        try:
            cfg = skillkit.load_config()
            if scope == "unique":
                if "/" not in key:
                    raise RuntimeError("独有 skill 的 key 必须带 tool 前缀")
                tool, name = key.split("/", 1)
            else:
                name, tool = key, skillkit.find_conflict_tool(con, cfg, key)
                if not tool:
                    raise RuntimeError("这个 skill 没有同名副本可对照")
            return {"ok": True, "conflict": skillkit.conflict_detail(con, cfg, tool, name)}
        finally:
            con.close()

    @_guard
    def rescan(self):
        con = skillkit.db()
        try:
            cfg = skillkit.load_config()
            skillkit.sync_tools_from_adapters(con, cfg)
            count = skillkit.rescan(con, cfg)
            boards = skillkit.list_boards(con, cfg)
            skillkit.prune_meta(con, boards)
            return {"ok": True, "data": boards, "meta": skillkit.get_meta(con),
                    "scanned": count}
        finally:
            con.close()

    # ---------------------------------------------------------- 智能体
    @_guard
    def set_agent(self, key, on):
        """智能体分发开关：开=按中央仓库补齐 junction，关=只撤链接不动实体。"""
        con = skillkit.db()
        try:
            cfg = skillkit.load_config()
            row = con.execute("select * from tools where key=?", (key,)).fetchone()
            if not row:
                raise RuntimeError("未知 agent: %s" % key)
            if not row["detected"]:
                raise RuntimeError("%s 未安装（没有检测到 %s）" % (row["label"], row["detect_dir"]))
            if bool(on) and not row["enabled"]:
                con.execute("update tools set enabled=1 where key=?", (key,))
                con.commit()
                plan = skillkit.do_sync(con, cfg, only_tools=[key], apply_changes=True)
                changed = sum(1 for p in plan if p.get("done"))
                conflicts = [p["skill"] for p in plan
                             if p["state"] in ("conflict-dir", "conflict-file")]
                skipped = [p["skill"] for p in plan
                           if p["action"] is None and p["state"] not in
                           ("ok-link", "ok-copy", "off", "conflict-dir", "conflict-file")]
                skillkit.log_event(con, "agent-on", "%s（建 %d 处）" % (key, changed))
                con.commit()
                action = {"on": True, "changed": changed,
                          "conflicts": conflicts, "skipped": skipped}
            elif not bool(on) and row["enabled"]:
                removed = skillkit.unlink_tool(con, cfg, key)
                skillkit.toggle(con, "tools", key, 0)
                kept = [s["name"] for s in con.execute(
                    "select name from skills order by name")
                    if os.path.lexists(os.path.join(row["skills_dir"], s["name"]))]
                skillkit.log_event(con, "agent-off", "%s（撤 %d 处，留 %d 处）"
                                   % (key, len(removed), len(kept)))
                con.commit()
                action = {"on": False, "removed": len(removed), "kept": kept}
            else:
                action = {"on": bool(on), "changed": 0, "removed": 0,
                          "conflicts": [], "skipped": [], "kept": []}
            boards = skillkit.list_boards(con, cfg)
            skillkit.prune_meta(con, boards)
            return {"ok": True, "data": boards, "meta": skillkit.get_meta(con),
                    "action": action}
        finally:
            con.close()

    # ---------------------------------------------------------- 系统
    @_guard
    def export_json(self, name, payload):
        if not self._window:
            raise RuntimeError("窗口未就绪")
        safe = "".join(c for c in str(name or "skillkit-meta.json")
                       if c.isalnum() or c in "._-") or "skillkit-meta.json"
        target = self._window.create_file_dialog(
            webview.SAVE_DIALOG, directory=os.path.join(KIT, ".."),
            save_filename=safe, file_types=("JSON (*.json)",))
        if not target:
            return {"ok": True, "path": ""}
        path = target[0] if isinstance(target, (list, tuple)) else target
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return {"ok": True, "path": path}

    @_guard
    def copy_path(self, text):
        _clipboard_set(text)
        return {"ok": True, "text": str(text)[:2000]}

    @_guard
    def open_dir(self, scope, key):
        """只接受 scope+key，路径由库和磁盘现算，避免任意路径注入。"""
        con = skillkit.db()
        try:
            cfg = skillkit.load_config()
            if scope == "agent":
                row = con.execute("select * from tools where key=?", (key,)).fetchone()
                if not row:
                    raise RuntimeError("未知 agent: %s" % key)
                if not row["detected"]:
                    raise RuntimeError("这个 agent 没有装在本机")
                path = row["skills_dir"]
            elif scope == "unique":
                if "/" not in key:
                    raise RuntimeError("独有 skill 的 key 必须带 tool 前缀")
                tool, name = key.split("/", 1)
                # 路径一律现查：独有行可能来自 main 之外的 extra 目录或容器，
                # 不能想当然地按 skills_dir 直拼
                urow = skillkit.find_unique_row(con, cfg, tool, name)
                if not urow:
                    raise RuntimeError("独有 skill 不存在或已消失：%s" % key)
                path = urow["path"]
            else:
                path = os.path.join(cfg["store"], key)
            if not os.path.isdir(path):
                raise RuntimeError("目录不存在：%s" % path)
            os.startfile(path)  # noqa: S606 - 用户主动点"打开目录"
            return {"ok": True, "path": path}
        finally:
            con.close()

    @_guard
    def win_min(self):
        if self._window:
            self._window.minimize()
            time.sleep(0.15)  # 等 WinForms 重算样式，再把最小化位补回去
            _refresh_window_chrome()
        return {"ok": True}

    @_guard
    def win_max(self):
        if not self._window:
            return {"ok": False, "error": "窗口未就绪"}
        if self._maxed:
            self._window.restore()
        else:
            self._window.maximize()
        self._maxed = not self._maxed
        time.sleep(0.2)
        _refresh_window_chrome()
        return {"ok": True, "maximized": self._maxed}

    @_guard
    def win_close(self):
        if self._window:
            self._window.destroy()
        return {"ok": True}

    @_guard
    def win_bounds(self):
        """前端 grip 拖动前先取当前外框（CSS 像素），作为位移的基准矩形。"""
        bounds = window_bounds()
        if not bounds:
            raise RuntimeError("窗口未就绪")
        hwnd = _hwnd_by_title(visible_only=False)
        maximized = bool(hwnd and ctypes.windll.user32.IsZoomed(ctypes.wintypes.HWND(hwnd)))
        return {"ok": True, "bounds": bounds, "maximized": maximized}

    @_guard
    def win_set_bounds(self, x, y, width, height):
        moved = set_window_bounds(x, y, width, height)
        if moved:
            self._maxed = False  # 手动拖过尺寸后，最大化按钮回到“未最大化”
        return {"ok": True, "bounds": window_bounds()}

    @_guard
    def win_restore(self):
        """最大化窗口开始拖动时先还原，再按还原后的外框继续拖（Windows 习惯）。"""
        if not self._window:
            raise RuntimeError("窗口未就绪")
        hwnd = _hwnd_by_title(visible_only=False)
        zoomed = bool(hwnd and ctypes.windll.user32.IsZoomed(ctypes.wintypes.HWND(hwnd)))
        if self._maxed or zoomed:
            self._window.restore()
            self._maxed = False
            time.sleep(0.2)  # 等 WinForms 把 Location/Size 落定，再读基准矩形
            _refresh_window_chrome()
        bounds = window_bounds()
        if not bounds:
            raise RuntimeError("窗口未就绪")
        return {"ok": True, "bounds": bounds, "maximized": False}

    @_guard
    def log_ui(self, kind, detail):
        """前端诊断落盘：拖动链路写 kit/ui-drag.log，只保留最近 200 行。

        真实鼠标拖不动时，这个日志能说清是「没收到 pointerdown」「没拿到 bounds」
        还是「后端拒绝移动」，不用再猜。
        每条 API 各跑一个线程，必须「加锁 + 只追加」：读-改-写会让并发写互相截断，
        日志里出现半行残片、甚至把启动留痕冲掉。
        """
        path = os.path.join(KIT, "ui-drag.log")
        line = "%s %-9s %s\n" % (time.strftime("%H:%M:%S"), str(kind)[:20],
                                 str(detail)[:900])
        try:
            with _log_lock:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(line)
                if os.path.getsize(path) > 48 * 1024:
                    with open(path, encoding="utf-8") as fh:
                        keep = fh.readlines()[-200:]
                    with open(path, "w", encoding="utf-8") as fh:
                        fh.writelines(keep)
        except OSError:
            pass
        return {"ok": True}


def _asset_version():
    """文件 mtime 拼成 URL 版本号：WebView2 会缓存 file:// 资源，改完界面要能立刻看到。"""
    stamps = []
    for name in ("index.html", "styles.css", "app.js", "bridge.js", "data.js"):
        path = os.path.join(UI_DIR, name)
        try:
            stamps.append(int(os.path.getmtime(path)))
        except OSError:
            pass
    return str(max(stamps) if stamps else 0)


def build_window(api, hidden=False):
    """建窗即定位：首帧就是目标外框尺寸 + 工作区居中，之后不再移动，避免启动闪现。

    pywebview 的 width/height 是 WinForms 改边框之前的 Size；frameless 会把标题栏
    和边框裁掉，所以请求值要加上 _nc_css() 的差额，外框才正好等于 fit_size()。
    """
    width, height = fit_size()
    nc_x, nc_y = _nc_css()
    x, y = _centered_rect(width, height)
    return webview.create_window(
        "skillkit · Skill Library",
        INDEX + "?desktop=1&v=" + _asset_version(),
        js_api=api,
        width=width + nc_x,
        height=height + nc_y,
        x=x,
        y=y,
        min_size=(MIN_W, MIN_H),
        frameless=True,
        easy_drag=False,
        resizable=True,
        zoomable=False,
        background_color="#f3f6fb",
        hidden=hidden,
    )


# ---------------------------------------------------------------- 自检
def _wait_ready(window, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if window.evaluate_js("!!window.__SK_READY"):
                return True
        except Exception:  # noqa: BLE001 - 页面还在加载
            pass
        time.sleep(0.1)
    return False


def _check(window, results, name, code, expect=None):
    try:
        value = window.evaluate_js(code)
    except Exception as exc:  # noqa: BLE001
        results.append((name, False, "抛出异常 %r" % (exc,)))
        return None
    ok = value == expect if expect is not None else bool(value)
    results.append((name, ok, repr(value)))
    return value


def _await_js(window, key, timeout=15.0):
    """等一个 JS promise 把结果塞到 window.<key>，再取回解析后的对象。

    bootstrap 现在要扫插件缓存（秒级），隐藏窗口还会节流定时器，5 秒不够。
    就绪即返回，超时只是兜底。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if window.evaluate_js("!!window.%s" % key):
            break
        time.sleep(0.1)
    raw = window.evaluate_js("JSON.stringify(window.%s||null)" % key)
    return json.loads(raw) if raw else None


def _selftest_native_window(results):
    """自检原生窗口：任务栏图标位 + 最小化位（以及被 WinForms 抹掉后能补回）。"""
    if sys.platform != "win32":
        results.append(("任务栏原生窗口", True, "非 Windows，跳过"))
        return
    hwnd = _refresh_window_chrome()
    if not hwnd:
        results.append(("任务栏原生窗口", False, "找不到主窗口"))
        return
    style = ctypes.windll.user32.GetWindowLongW(ctypes.wintypes.HWND(hwnd), -16)
    results.append(("任务栏最小化位", bool(style & WS_MINIMIZEBOX), hex(style & 0xffffffff)))
    small_px = ctypes.windll.user32.GetSystemMetrics(49) or 32
    expect_small = _WINDOW_ICONS.get((0, small_px))
    expect_big = _WINDOW_ICONS.get((1, ctypes.windll.user32.GetSystemMetrics(11) or 32))
    got_small = _window_icon(hwnd, 0)
    got_big = _window_icon(hwnd, 1)
    got_small2 = _window_icon(hwnd, 2)
    results.append(("任务栏小图标=skillkit.ico", bool(expect_small) and got_small == expect_small,
                    "got=%r expect=%r" % (got_small, expect_small)))
    results.append(("任务栏大图标=skillkit.ico", bool(expect_big) and got_big == expect_big,
                    "got=%r expect=%r" % (got_big, expect_big)))
    results.append(("任务栏小图标2=skillkit.ico", bool(expect_small) and got_small2 == expect_small,
                    "got=%r expect=%r" % (got_small2, expect_small)))
    # 模拟 WinForms 在状态变化时抹掉最小化位，确保兜底函数能补回
    ctypes.windll.user32.SetWindowLongW(ctypes.wintypes.HWND(hwnd), -16,
                                       style & ~WS_MINIMIZEBOX)
    _ensure_minimize_box(hwnd)
    style2 = ctypes.windll.user32.GetWindowLongW(ctypes.wintypes.HWND(hwnd), -16)
    results.append(("最小化位可自动补回", bool(style2 & WS_MINIMIZEBOX), hex(style2 & 0xffffffff)))


def _edited_count(meta_map):
    """与 app.js `editedCount()` 同一规则：有显示名 / 备注，或分类被改过（seed 不再单值）。"""
    total = 0
    for m in meta_map.values():
        if m.get("display") or m.get("note"):
            total += 1
        elif m.get("seed"):
            cats = m.get("cats") or []
            if not (len(cats) == 1 and cats[0] == m["seed"]):
                total += 1
    return total


def _selftest_body(window, results):
    _selftest_native_window(results)
    # 期望值一律取自真实后端，不写死数字：store 里增删 skill 后自检仍应成立
    con = skillkit.db()
    try:
        boards = skillkit.list_boards(con, skillkit.load_config())
    finally:
        con.close()
    sh_total = len(boards["skills"])
    ag_rows = boards["agents"]
    # 独有 / 冲突 / agent 计数同样现算：库里增删 skill、冲突收口后自检仍应成立
    un_rows = boards.get("unique") or []
    un_total = len(un_rows)
    un_keys = sorted("%s/%s" % (u["tool"], u["name"]) for u in un_rows)
    conflict = boards.get("conflict")
    ag_connected = sum(1 for a in ag_rows if a["status"] == "connected")
    ag_missing = [a["key"] for a in ag_rows if a["status"] == "missing"]
    # 分类数 = app.js 里的 7 个内置分类 + 用户在 prefs 里自建的分类
    custom_cats = window.evaluate_js(
        "(function(){try{var p=JSON.parse(localStorage.getItem('skillkit.library.prefs.v1')"
        "||'{}');return (p.customCats||[]).length}catch(e){return 0}})()")
    cat_total = 7 + int(custom_cats or 0)
    store_path = skillkit.load_config()["store"].replace("/", "\\")
    con = skillkit.db()
    try:
        meta_map = skillkit.get_meta(con)
    finally:
        con.close()
    edited0 = _edited_count(meta_map)

    # 上次运行可能停留在「智能体」看板，自检统一从共有技能页 + pdf 详情开始
    window.evaluate_js("document.getElementById('nav-shared').click()")
    time.sleep(0.3)
    window.evaluate_js("document.querySelector('.row[data-key=\"pdf\"]').click()")
    time.sleep(0.3)
    _check(window, results, "运行模式=db", "window.__SK_MODE", "db")
    _check(window, results, "无 JS 错误", "window.__SK_ERRORS.length", 0)
    _check(window, results, "共有行数=%d" % sh_total,
           "document.querySelectorAll('.row').length", sh_total)
    _check(window, results, "侧栏共有=%d" % sh_total,
           "document.getElementById('nav-shared').textContent", str(sh_total))
    _check(window, results, "侧栏独有=%d" % un_total,
           "document.getElementById('nav-unique').textContent", str(un_total))
    _check(window, results, "已编辑初始=%d" % edited0,
           "document.getElementById('stat-edited').textContent", str(edited0))
    _check(window, results, "分类数=%d" % cat_total,
           "document.getElementById('stat-cats').textContent", str(cat_total))
    _check(window, results, "agent=%d" % ag_connected,
           "document.getElementById('stat-agents').textContent", str(ag_connected))
    _check(window, results, "路径来自 cfg", "document.getElementById('store-path').textContent",
           store_path)
    _check(window, results, "选中 pdf", "window.__SK_APP.state.selected", "pdf")
    _check(window, results, "列表上方筛选行已移除",
           "document.querySelectorAll('.filters,.fchip,#filters,#count-text').length", 0)
    # 序号列：按当前列表顺序 1..N，纯展示
    _check(window, results, "首行序号=1", "document.querySelector('.row .row-num').textContent", "1")
    _check(window, results, "序号连续 1..%d" % sh_total,
           "(function(){var n=[].map.call(document.querySelectorAll('.row .row-num'),"
           "function(e){return +e.textContent});return n.length===" + str(sh_total) +
           "&&n.every(function(v,i){return v===i+1})})()", True)
    # 两位数（第 10 行）仍要装得进 26×26 的格子里，不溢出
    _check(window, results, "序号格 26x26 不溢出",
           "(function(){var e=document.querySelectorAll('.row .row-num')[9];"
           "var r=e.getBoundingClientRect();"
           "return Math.round(r.width)+'x'+Math.round(r.height)+':'+e.textContent+':'"
           "+(e.scrollWidth<=e.clientWidth)})()", "26x26:10:true")
    _check(window, results, "详情栏有删除按钮",
           "document.querySelectorAll('[data-delete]').length", 1)

    # 独有看板：行数 / 行键集合 / 序号连续 1..N，全部按后端现算
    window.evaluate_js("document.getElementById('nav-unique').click()")
    time.sleep(0.3)
    _check(window, results, "独有行数=%d" % un_total,
           "document.querySelectorAll('.row').length", un_total)
    _check(window, results, "独有行键=后端集合",
           "(function(){var a=[].map.call(document.querySelectorAll('.row'),"
           "function(e){return e.getAttribute('data-key')});a.sort();"
           "return JSON.stringify(a)})()",
           json.dumps(un_keys, ensure_ascii=False, separators=(",", ":")))
    _check(window, results, "独有序号连续 1..%d" % un_total,
           "(function(){var n=[].map.call(document.querySelectorAll('.row .row-num'),"
           "function(e){return +e.textContent});return n.length===" + str(un_total) +
           "&&n.every(function(v,i){return v===i+1})})()", True)

    # 独有看板的外部徽标：src != main 的行（extra 目录 / 容器展开 / 外部链接）
    # 都要带「外部」标记；这类行不允许出现删除按钮（详情栏选中第一行核对）
    un_ext = sum(1 for u in un_rows if u.get("src") and u["src"] != "main")
    if un_ext:
        _check(window, results, "独有外部徽标=%d" % un_ext,
               "document.querySelectorAll('.row .mini-flag.ext').length", un_ext)
        u0 = next(u for u in un_rows if u.get("src") and u["src"] != "main")
        window.evaluate_js("document.querySelector('.row[data-key=%s]').click()"
                           % json.dumps("%s/%s" % (u0["tool"], u0["name"])))
        time.sleep(0.3)
        _check(window, results, "外部行详情无删除按钮",
               "document.querySelectorAll('[data-delete]').length", 0)

    # 差异弹窗（走 Python conflict_detail）：是否有「同名副本」由后端现算——
    # 冲突可能只挂在独有板（如 Trae 内置 skill-creator 与中央同名不同内容），
    # 也可能同时是共有 skill 的实体冲突（如当年的 workbuddy/pdf）。
    # 共有板分支只在 find_conflict_tool 现查到实体冲突时才测，其余走独有板分支。
    if conflict:
        c_names = {f["name"] for f in conflict["central"]["files"]}
        c_names |= {f["name"] for f in conflict["local"]["files"]}
        header = "CONFLICT · %s / %s" % (conflict["tool"], conflict["skill"])
        con2 = skillkit.db()
        try:
            shared_conflict = skillkit.find_conflict_tool(
                con2, skillkit.load_config(), conflict["skill"]) is not None
        finally:
            con2.close()
        if shared_conflict:
            window.evaluate_js("document.getElementById('nav-shared').click()")
            time.sleep(0.3)
            window.evaluate_js(
                "var r=document.querySelector('.row[data-key=%s]');if(r)r.click()"
                % json.dumps(conflict["skill"]))
            time.sleep(0.3)
            _check(window, results, "差异按钮存在",
                   "document.querySelectorAll('[data-diff]').length", 1)
            window.evaluate_js("var b=document.querySelector('[data-diff]');if(b)b.click()")
            time.sleep(0.5)
            _check(window, results, "差异弹窗出现", "!!document.querySelector('.diff-table')", True)
            _check(window, results, "差异行数=%d" % len(c_names),
                   "document.querySelectorAll('.diff-row').length", len(c_names))
            _check(window, results, "冲突表头=%s" % header,
                   "document.querySelector('.modal-eyebrow').textContent", header)
            window.evaluate_js("var b=document.querySelector('[data-close]');if(b)b.click()")
        else:
            _check(window, results, "共有板无实体冲突时不显示差异按钮",
                   "document.querySelectorAll('[data-diff]').length", 0)

        # 独有看板的同名副本差异（scope=unique 路径），只有存在同名副本时才测
        u_same = [u for u in un_rows if u.get("sameName")]
        if u_same:
            u_key = "%s/%s" % (u_same[0]["tool"], u_same[0]["name"])
            window.evaluate_js("document.getElementById('nav-unique').click()")
            time.sleep(0.3)
            window.evaluate_js(
                "var r=document.querySelector('.row[data-key=%s]');if(r)r.click()"
                % json.dumps(u_key))
            time.sleep(0.3)
            window.evaluate_js("var b=document.querySelector('[data-diff]');if(b)b.click()")
            time.sleep(0.5)
            _check(window, results, "独有差异弹窗出现",
                   "!!document.querySelector('.diff-table')", True)
            _check(window, results, "独有差异表头",
                   "document.querySelector('.modal-eyebrow').textContent", header)
            window.evaluate_js("var b=document.querySelector('[data-close]');if(b)b.click()")
    else:
        _check(window, results, "无同名副本时详情栏无差异按钮",
               "document.querySelectorAll('[data-diff]').length", 0)
        window.evaluate_js(
            "window.__SK_DIFF=null;window.pywebview.api.diff('shared','pdf')"
            ".then(function(r){window.__SK_DIFF=r})")
        data_diff = _await_js(window, "__SK_DIFF")
        ok_diff = bool(data_diff) and data_diff.get("ok") is False \
            and "没有同名副本" in (data_diff.get("error") or "")
        results.append(("无同名副本时 diff 被拒", ok_diff,
                        json.dumps(data_diff, ensure_ascii=False)))

    window.evaluate_js("document.getElementById('nav-shared').click()")
    time.sleep(0.3)
    window.evaluate_js("document.querySelector('.row[data-key=\"pdf\"]').click()")
    time.sleep(0.3)

    # 写库：改名后落 skill_meta。目标选一条「当前没有人工改名 / 备注」的共有 skill，
    # 「已编辑」数量由 bootstrap 的 meta 现算；结束把目标元数据原样恢复，不吃用户编辑。
    rename_target = next(
        (s["name"] for s in boards["skills"]
         if not (meta_map.get(s["name"]) or {}).get("display")
         and not (meta_map.get(s["name"]) or {}).get("note")), "pdf")
    old_target = meta_map.get(rename_target)
    window.evaluate_js("document.querySelector('.row[data-key=%s]').click()"
                       % json.dumps(rename_target))
    time.sleep(0.3)
    window.evaluate_js(
        "var i=document.getElementById('ins-name');"
        "i.value='自检改名';i.dispatchEvent(new Event('input',{bubbles:true}))")
    time.sleep(1.2)
    con = skillkit.db()
    row = con.execute("select display_name, updated_at from skill_meta "
                      "where scope='shared' and key=?", (rename_target,)).fetchone()
    ok = bool(row) and row["display_name"] == "自检改名" and (row["updated_at"] or 0) > 0
    results.append(("改名写入 skill_meta", ok, repr(dict(row) if row else None)))
    con.close()
    _check(window, results, "改名后已编辑=%d" % (edited0 + 1),
           "document.getElementById('stat-edited').textContent", str(edited0 + 1))
    window.evaluate_js(
        "var i=document.getElementById('ins-name');"
        "i.value='';i.dispatchEvent(new Event('input',{bubbles:true}))")
    time.sleep(0.8)
    # 清掉测试写入；原来有元数据就整行还原，原来没有就删掉这一行
    con = skillkit.db()
    if old_target:
        con.execute(
            "insert or replace into skill_meta"
            "(scope,key,tool,display_name,note,categories,seed,updated_at) "
            "values(?,?,?,?,?,?,?,?)",
            ("shared", rename_target, old_target.get("tool") or "",
             old_target["display"], old_target["note"],
             json.dumps(old_target["cats"] or [], ensure_ascii=False),
             old_target["seed"], old_target["updated"]))
    else:
        con.execute("delete from skill_meta where scope='shared' and key=?", (rename_target,))
    con.commit()
    con.close()
    window.evaluate_js(
        "window.__SK_R3=0;window.pywebview.api.bootstrap().then(function(r){"
        "window.__SK_APP.refresh(r);window.__SK_R3=1})")
    _await_js(window, "__SK_R3")
    _check(window, results, "还原后已编辑=%d" % edited0,
           "document.getElementById('stat-edited').textContent", str(edited0))

    # 搜索：不写死命中条数（模糊匹配结果随库内数据变），只校验「命中 pdf + 列表确实收窄」
    window.evaluate_js(
        "var s=document.getElementById('gsearch');"
        "s.value='pdf';s.dispatchEvent(new Event('input',{bubbles:true}))")
    time.sleep(0.3)
    _check(window, results, "搜索 pdf 命中 pdf",
           "!!document.querySelector('.row[data-key=\"pdf\"]')", True)
    _check(window, results, "搜索 pdf 列表收窄",
           "(function(){var n=document.querySelectorAll('.row').length;"
           "return n>=1&&n<%d})()" % sh_total, True)
    window.evaluate_js(
        "var s=document.getElementById('gsearch');"
        "s.value='';s.dispatchEvent(new Event('input',{bubbles:true}))")
    time.sleep(0.3)
    _check(window, results, "清筛选后=%d" % sh_total,
           "document.querySelectorAll('.row').length", sh_total)

    # 删除接口的安全兜底：不存在的 key 必须被拒绝，不能删任何东西
    window.evaluate_js(
        "window.__SK_D=null;window.pywebview.api.delete_skill('shared','__no_such_skill__')"
        ".then(function(r){window.__SK_D=r})")
    data_d = _await_js(window, "__SK_D")
    ok_d = bool(data_d) and data_d.get("ok") is False and "不存在" in (data_d.get("error") or "")
    results.append(("不存在 skill 拒绝删除", ok_d, json.dumps(data_d, ensure_ascii=False)))

    # 真实删除：临时造一个 skill，从 UI 详情栏点「彻底删除」→ 确认，磁盘与列表都必须干净
    temp_name = "__selftest_delete__"
    cfg = skillkit.load_config()
    temp_dir = os.path.join(cfg["store"], temp_name)
    try:
        shutil.rmtree(temp_dir, ignore_errors=True)
        os.makedirs(temp_dir, exist_ok=True)
        with open(os.path.join(temp_dir, "SKILL.md"), "w", encoding="utf-8") as fh:
            fh.write("# selftest delete\n")
        con = skillkit.db()
        try:
            skillkit.rescan(con, cfg)
        finally:
            con.close()
        window.evaluate_js(
            "window.__SK_R=0;window.__SK_R_ERR='';window.pywebview.api.bootstrap().then(function(r){"
            "try{window.__SK_APP.refresh(r)}catch(e){window.__SK_R_ERR=String(e&&e.message||e)}"
            "window.__SK_R=1})")
        _await_js(window, "__SK_R")
        # 隐藏窗口会节流定时器，渲染可能慢半拍：等行真的出现再点；等不到就留存现场
        row_sel = ".row[data-key=%s]" % json.dumps(temp_name)
        deadline = time.time() + 10
        while time.time() < deadline and not window.evaluate_js(
                "!!document.querySelector(%s)" % json.dumps(row_sel)):
            time.sleep(0.2)
        diag = window.evaluate_js(
            "JSON.stringify({rErr:window.__SK_R_ERR,rows:document.querySelectorAll('.row').length,"
            "nav:document.getElementById('nav-shared').textContent,"
            "board:window.__SK_APP&&window.__SK_APP.state.board,"
            "jsErr:window.__SK_ERRORS})")
        row_present = bool(window.evaluate_js(
            "!!document.querySelector(%s)" % json.dumps(row_sel)))
        window.evaluate_js(
            "var r=document.querySelector(%s);if(r)r.click()" % json.dumps(row_sel))
        time.sleep(0.2)
        window.evaluate_js("var b=document.querySelector('[data-delete]');if(b)b.click()")
        time.sleep(0.3)
        modal_ok = bool(window.evaluate_js("!!document.getElementById('confirm-ok')"))
        if modal_ok:
            window.evaluate_js("document.getElementById('confirm-ok').click()")
        deadline = time.time() + 8
        row_gone = False
        while time.time() < deadline and (os.path.isdir(temp_dir) or not row_gone):
            time.sleep(0.2)
            row_gone = bool(window.evaluate_js(
                "!document.querySelector('.row[data-key=%s]')" % json.dumps(temp_name)))
        selected = window.evaluate_js("window.__SK_APP.state.selected")
        ok_d2 = modal_ok and row_gone and not os.path.isdir(temp_dir) \
            and selected != temp_name
        results.append(("UI 点击彻底删除临时 skill", ok_d2,
                        "modal=%s row_gone=%s dir_gone=%s selected=%r diag=%s"
                        % (modal_ok, row_gone, not os.path.isdir(temp_dir),
                           selected, "" if row_present else diag)))
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        con = skillkit.db()
        try:
            con.execute("delete from skills where name=?", (temp_name,))
            con.execute("delete from skill_meta where scope='shared' and key=?", (temp_name,))
            con.commit()
        finally:
            con.close()
        window.evaluate_js(
            "window.__SK_R2=0;window.pywebview.api.bootstrap().then(function(r){"
            "window.__SK_APP.refresh(r);window.__SK_R2=1})")
        _await_js(window, "__SK_R2")


    # 设置弹窗 + 主题
    window.evaluate_js("document.getElementById('open-settings').click()")
    time.sleep(0.3)
    _check(window, results, "设置弹窗出现", "!!document.getElementById('set-theme')", True)
    _check(window, results, "设置文案指向 skill_meta",
           "!!document.querySelector('.modal-body p').textContent.match('skill_meta')", True)
    window.evaluate_js("document.querySelector('[data-close]').click()")
    window.evaluate_js("document.documentElement.setAttribute('data-theme','dark')")
    _check(window, results, "深色主题属性生效",
           "document.documentElement.getAttribute('data-theme')", "dark")

    # 智能体看板：三态来自 skillkit.list_boards，开关按真实磁盘链接计数
    ag_total = len(ag_rows)

    window.evaluate_js("document.getElementById('nav-agents').click()")
    time.sleep(0.4)
    _check(window, results, "智能体行数=%d" % ag_total,
           "document.querySelectorAll('.row[data-row-board=\"agents\"]').length", ag_total)
    _check(window, results, "侧栏智能体=已知数",
           "document.getElementById('nav-agents').textContent", str(ag_total))
    _check(window, results, "智能体 tab 已连接数",
           "document.getElementById('tab-agents-em').textContent",
           "%d 已连接" % ag_connected)
    _check(window, results, "智能体页标题",
           "document.getElementById('page-title').textContent", "智能体")
    _check(window, results, "智能体状态筛=4",
           "document.querySelectorAll('[data-agent-status]').length >= 4", True)
    _check(window, results, "每行都有开关",
           "document.querySelectorAll('.row[data-row-board=\"agents\"] .switch').length", ag_total)
    _check(window, results, "未安装开关禁用数",
           "document.querySelectorAll('.row[data-row-board=\"agents\"] .switch input:disabled').length",
           len(ag_missing))

    # 外部来源（方案 A）：~/.agents/skills 这类 agent 自己安装/读取的目录只检测展示，
    # 开关不管它们。有与中央仓库同名的实体时，行上要有「外部 N」，详情里要说明。
    ag_ext = [a for a in ag_rows if a.get("external")]
    if ag_ext:
        a0 = ag_ext[0]
        e0 = a0["external"][0]
        e_sum = sum(e["count"] for e in a0["external"])
        _check(window, results, "%s 行有外部来源标记" % a0["key"],
               "!!document.querySelector('.row[data-key=\"%s\"] .ext-badge')" % a0["key"], True)
        _check(window, results, "外部标记数量=%d" % e_sum,
               "(function(){var b=document.querySelector('.row[data-key=\"%s\"] .ext-badge');"
               "return b?b.textContent.trim():''})()" % a0["key"],
               "外部 %d" % e_sum)
        window.evaluate_js(
            "var r=document.querySelector('.row[data-key=\"%s\"]');if(r)r.click()" % a0["key"])
        time.sleep(0.3)
        _check(window, results, "详情里有外部来源行",
               "(function(){var ok=false;"
               "Array.prototype.forEach.call(document.querySelectorAll('#ins-body .meta-row'),"
               "function(x){if(x.textContent.indexOf('外部来源')===0)ok=true});return ok})()", True)

    # 开关方向回归：点“开”着的开关必须先问「要不要关闭」，不能再走一次连接。
    # 老实现读 click 里的 !box.checked —— 点击冒到 document 时 checked 已被 Chromium
    # 翻转（label 还会把点击转给 input，一次按下两个 click），于是点开着反而又 open 一次。
    # 这里只走到确认框再取消，不产生任何磁盘写。
    ag_connected_rows = [a for a in ag_rows if a["status"] == "connected"]
    if ag_connected_rows:
        sw_key = ag_connected_rows[0]["key"]
        window.evaluate_js(
            "var h=document.createElement('div');h.id='__SK_SW';"
            "h.innerHTML='<label class=\"switch\" data-agent-switch=\"%s\">"
            "<input type=\"checkbox\" checked><i></i></label>';"
            "document.body.appendChild(h)" % sw_key)
        time.sleep(0.2)
        window.evaluate_js("document.querySelector('#__SK_SW label i').click()")
        time.sleep(0.4)
        _check(window, results, "开关点开→先问关闭",
               "(function(){var m=document.querySelector('#modal-root .modal');"
               "return !!m&&m.querySelector('h2').textContent.indexOf('关闭')===0})()", True)
        _check(window, results, "开关一次点击只弹一个确认框",
               "document.querySelectorAll('#modal-root .modal').length", 1)
        window.evaluate_js(
            "var b=document.querySelector('#modal-root .modal-foot [data-close]');"
            "if(b)b.click()")
        time.sleep(0.3)
        _check(window, results, "取消关闭→开关回到开",
               "document.querySelector('#__SK_SW input').checked", True)
        window.evaluate_js("document.getElementById('__SK_SW').remove()")

    _check(window, results, "窗口缩放 grip=8",
           "document.querySelectorAll('[data-resize]').length", 8)

    # 外框 API：grip 拖动以它为基准矩形；隐藏建窗的 --selftest 也能读写
    window.evaluate_js(
        "window.__SK_B=null;window.pywebview.api.win_bounds()"
        ".then(function(r){window.__SK_B=r})")
    deadline = time.time() + 5
    while time.time() < deadline and not window.evaluate_js("!!window.__SK_B"):
        time.sleep(0.1)
    raw = window.evaluate_js("JSON.stringify(window.__SK_B||null)")
    bounds = (json.loads(raw) if raw else None) or {}
    ok = bounds.get("ok") is True and abs((bounds.get("bounds") or {}).get("w", 0) - WIN_W) <= 40
    results.append(("窗口外框可读≈1200", ok, raw))

    # 标题栏拖动：合成 pointerdown/move/up，窗口外框应跟着位移（不依赖 pywebview drag-region）
    b0 = bounds.get("bounds") or {}
    window.evaluate_js(
        "var bar=document.querySelector('.titlebar');"
        "bar.dispatchEvent(new PointerEvent('pointerdown',{pointerId:91,button:0,bubbles:true,"
        "screenX:500,screenY:300,clientX:200,clientY:20}))")
    time.sleep(0.4)
    window.evaluate_js(
        "document.dispatchEvent(new PointerEvent('pointermove',{pointerId:91,bubbles:true,"
        "screenX:560,screenY:340}))")
    time.sleep(0.5)
    window.evaluate_js(
        "document.dispatchEvent(new PointerEvent('pointerup',{pointerId:91,bubbles:true,"
        "screenX:560,screenY:340}))")
    time.sleep(0.5)
    window.evaluate_js(
        "window.__SK_B4=null;window.pywebview.api.win_bounds()"
        ".then(function(r){window.__SK_B4=r})")
    b4 = _await_js(window, "__SK_B4") or {}
    nb = b4.get("bounds") or {}
    moved_ok = b4.get("ok") is True \
        and abs(nb.get("x", 0) - (b0.get("x", 0) + 60)) <= 4 \
        and abs(nb.get("y", 0) - (b0.get("y", 0) + 40)) <= 4
    results.append(("标题栏拖动窗口 (dx=60,dy=40)", moved_ok,
                    "from=(%s,%s) to=(%s,%s)" % (b0.get("x"), b0.get("y"),
                                                 nb.get("x"), nb.get("y"))))
    # 复位，避免影响后续尺寸断言
    window.evaluate_js(
        "window.__SK_B5=null;window.pywebview.api.win_set_bounds(%s,%s,%s,%s)"
        ".then(function(r){window.__SK_B5=r})"
        % (b0.get("x", 0), b0.get("y", 0), b0.get("w", WIN_W), b0.get("h", WIN_H)))
    _await_js(window, "__SK_B5")

    # 最大化态拖标题栏：应先还原再继续位移，不能静默放弃（老实现直接 endMove）
    window.evaluate_js(
        "window.__SK_MX=null;window.pywebview.api.win_max()"
        ".then(function(r){window.__SK_MX=r})")
    _await_js(window, "__SK_MX")
    time.sleep(0.5)
    window.evaluate_js(
        "var bar=document.querySelector('.titlebar');"
        "bar.dispatchEvent(new PointerEvent('pointerdown',{pointerId:92,button:0,bubbles:true,"
        "screenX:500,screenY:300,clientX:200,clientY:20}))")
    time.sleep(0.6)
    window.evaluate_js(
        "document.dispatchEvent(new PointerEvent('pointermove',{pointerId:92,bubbles:true,"
        "screenX:530,screenY:320}))")
    time.sleep(0.6)
    window.evaluate_js(
        "document.dispatchEvent(new PointerEvent('pointerup',{pointerId:92,bubbles:true,"
        "screenX:530,screenY:320}))")
    time.sleep(0.6)
    window.evaluate_js(
        "window.__SK_B6=null;window.pywebview.api.win_bounds()"
        ".then(function(r){window.__SK_B6=r})")
    b6 = _await_js(window, "__SK_B6") or {}
    nb6 = b6.get("bounds") or {}
    restored_ok = b6.get("ok") is True and b6.get("maximized") is False \
        and abs(nb6.get("w", 0) - b0.get("w", WIN_W)) <= 40 \
        and abs(nb6.get("x", 0) - (b0.get("x", 0) + 30)) <= 40
    results.append(("最大化拖动先还原再位移", restored_ok,
                    "from=(%s,%s,%s) to=(%s,%s,%s)" % (b0.get("x"), b0.get("y"), b0.get("w"),
                                                       nb6.get("x"), nb6.get("y"), nb6.get("w")))
                    )

    if ag_missing:
        key_js = json.dumps(ag_missing[0])
        window.evaluate_js(
            "window.__SK_T=null;window.pywebview.api.set_agent(%s,true)"
            ".then(function(r){window.__SK_T=r})" % key_js)
        deadline = time.time() + 5
        while time.time() < deadline:
            if window.evaluate_js("!!window.__SK_T"):
                break
            time.sleep(0.1)
        raw = window.evaluate_js("JSON.stringify(window.__SK_T||null)")
        data = json.loads(raw) if raw else None
        ok = bool(data) and data.get("ok") is False and "未安装" in (data.get("error") or "")
        results.append(("未安装 agent 无法开启", ok, raw))
    window.evaluate_js("document.getElementById('nav-shared').click()")
    time.sleep(0.4)
    window.evaluate_js("document.querySelector('.row[data-key=\"pdf\"]').click()")
    time.sleep(0.3)

    # 布局：建窗时已按 _nc_css() 补偿 frameless 去边框的差额，内容区应正好 1200x760；
    # 这里仍用区间断言，兼容小屏（fit_size 会收口到工作区，最小 1180x720）
    _check(window, results, "启动宽 1180~1200",
           "window.innerWidth>=%d && window.innerWidth<=%d" % (MIN_W, WIN_W), True)
    _check(window, results, "启动高 720~760",
           "window.innerHeight>=%d && window.innerHeight<=%d" % (MIN_H, WIN_H), True)
    _check(window, results, "启动宽≤工作区",
           "window.innerWidth<=screen.availWidth", True)
    _check(window, results, "启动高≤工作区",
           "window.innerHeight<=screen.availHeight", True)
    _check(window, results, "底部设置按钮完整可见",
           "(function(){var f=document.getElementById('open-settings').getBoundingClientRect();"
           "var a=document.querySelector('.app-body').getBoundingClientRect();"
           "return f.bottom<=a.bottom&&f.top>=a.top})()", True)
    _check(window, results, "版本号行可见",
           "(function(){var v=document.querySelector('.version').getBoundingClientRect();"
           "var a=document.querySelector('.app-body').getBoundingClientRect();"
           "return v.bottom<=a.bottom&&v.top>=a.top})()", True)
    _check(window, results, "详情栏完整可见",
           "Math.round(document.querySelector('.inspector').getBoundingClientRect().right)"
           "<=window.innerWidth", True)
    window.resize(MIN_W, MIN_H)
    time.sleep(0.8)
    _check(window, results, "1180 无横向溢出",
           "document.documentElement.scrollWidth<=window.innerWidth", True)
    _check(window, results, "1180 窗口宽=100vw",
           "Math.round(document.getElementById('app-window').getBoundingClientRect().width)", MIN_W)

    # 外框写入 + 最小尺寸兜底：grip 拖动最终落到 win_set_bounds，放在最后避免影响上面的内宽断言
    window.evaluate_js(
        "window.__SK_B2=null;window.pywebview.api.win_set_bounds(210,35,1240,780)"
        ".then(function(r){window.__SK_B2=r})")
    raw2 = _await_js(window, "__SK_B2")
    rect2 = ((raw2 or {}).get("bounds") or {})
    ok2 = (raw2 or {}).get("ok") is True and abs(rect2.get("w", 0) - 1240) <= 3 \
        and abs(rect2.get("h", 0) - 780) <= 3
    results.append(("外框写入 1240x780", ok2, json.dumps(raw2, ensure_ascii=False)))

    window.evaluate_js(
        "window.__SK_B3=null;window.pywebview.api.win_set_bounds(10,10,900,500)"
        ".then(function(r){window.__SK_B3=r})")
    raw3 = _await_js(window, "__SK_B3")
    rect3 = ((raw3 or {}).get("bounds") or {})
    ok3 = (raw3 or {}).get("ok") is True and abs(rect3.get("w", 0) - MIN_W) <= 3 \
        and abs(rect3.get("h", 0) - MIN_H) <= 3
    results.append(("外框最小尺寸兜底 1180x720", ok3, json.dumps(raw3, ensure_ascii=False)))


def selftest(window):
    results = []
    try:
        if not _wait_ready(window):
            raise RuntimeError("页面未就绪（__SK_READY 未置位）")
        time.sleep(0.5)
        _selftest_body(window, results)
    except Exception as exc:  # noqa: BLE001
        results.append(("自检异常", False, "%s: %s" % (type(exc).__name__, exc)))
    finally:
        passed = sum(1 for _n, ok, _v in results if ok)
        print("\n=== SELFTEST %d/%d ===" % (passed, len(results)))
        for name, ok, value in results:
            print("  [%s] %-28s %s" % ("PASS" if ok else "FAIL", name, value))
        try:
            print("ERRORS:", window.evaluate_js("JSON.stringify(window.__SK_ERRORS)"))
        except Exception as exc:  # noqa: BLE001
            print("ERRORS: <不可读 %r>" % (exc,))
        window.destroy()


def _report(message):
    """pythonw 没有控制台：错误写 launch.log，并弹一个系统提示框。"""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(os.path.join(KIT, "launch.log"), "a", encoding="utf-8") as fh:
            fh.write("[%s] %s\n" % (stamp, message))
    except OSError:
        pass
    try:
        ctypes.windll.user32.MessageBoxW(
            ctypes.wintypes.HWND(0), str(message), "skillkit 启动失败", 0x10)
    except Exception:  # noqa: BLE001 - 非 Windows 或弹窗失败
        print(message, file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="skillkit 桌面端")
    parser.add_argument("--debug", action="store_true", help="打开 WebView2 调试工具")
    parser.add_argument("--selftest", action="store_true", help="离屏自检后退出")
    args = parser.parse_args()

    if not os.path.isfile(INDEX):
        _report("找不到界面文件：%s" % INDEX)
        return 2

    try:
        _make_dpi_aware()
        api = Api()
        window = build_window(api, hidden=args.selftest)
        api.attach(window)
        os.makedirs(PROFILE, exist_ok=True)
        # 不传 icon：不同 pywebview 版本对它的处理不一致（有的把 icon 透传给
        # create_window()，直接抛 "create_window() got an unexpected keyword
        # argument 'icon'" 让启动整个失败）。Windows 侧图标统一由 on_start()
        # 里的 _apply_window_icon() 走 WM_SETICON 设置，这里不需要再传。
        webview.start(selftest if args.selftest else on_start, window,
                      debug=args.debug, private_mode=False, storage_path=PROFILE)
    except Exception as exc:  # noqa: BLE001 - 双击启动时看不到控制台
        _report("启动失败：%s: %s" % (type(exc).__name__, exc))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
