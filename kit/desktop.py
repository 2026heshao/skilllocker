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
import logging
import math
import os
import sys
import time
from logging.handlers import RotatingFileHandler

import webview

import skillkit

KIT = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(os.path.dirname(KIT), "ui")
INDEX = os.path.join(UI_DIR, "index.html")
PROFILE = os.path.join(KIT, "desktop_profile")
ICON = os.path.join(KIT, "assets", "skillkit.ico")
WIN_W, WIN_H = 1200, 760
MIN_W, MIN_H = 1180, 720
LOG_FILE = os.path.join(KIT, "skillkit.log")

# 无边框 WinForms 窗口会被系统去掉 WS_MINIMIZEBOX，任务栏按钮点击就不会最小化；
# 这里在窗口显示后补回该样式位，并在每次窗口状态变化后重新补一次。
WS_MINIMIZEBOX = 0x00020000
_WINDOW_ICONS = {}  # (slot, px) -> HICON，缓存住句柄，重复应用图标时不再泄漏

# 统一日志：launch.log / ui-drag.log / stdout 三套旁路合并成一个 logging 管线，
# 全部落 kit/skillkit.log（256KB×3 轮转）。pythonw 没有控制台，文件是唯一出口；
# CLI 的 stdout 输出是命令结果，不在此管线内。logging 自身线程安全。
_LOG = logging.getLogger("skillkit")


def _setup_logging():
    """幂等装配：桥异常、启动错误、前端诊断（log_ui）统一进 skillkit.log。"""
    if _LOG.handlers:
        return _LOG
    _LOG.setLevel(logging.DEBUG)
    handler = RotatingFileHandler(LOG_FILE, maxBytes=256 * 1024,
                                  backupCount=2, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    _LOG.addHandler(handler)
    return _LOG

if sys.platform == "win32":
    # 独立 WinDLL 实例承载签名。不能给全局 ctypes.windll.user32 设 argtypes：
    # pywebview 的 BrowserForm.move() 拖动时把 None 当宽高传给 SetWindowPos，
    # 全局声明会把它变成 ctypes.ArgumentError，整个标题栏拖动直接失效。
    _spos = ctypes.WinDLL("user32")
    _spos.SetWindowPos.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.HWND,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_uint]
    _spos.SetWindowPos.restype = ctypes.c_bool


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
    """按工作区收口：默认 1200x760，屏幕放不下就压到工作区（留 16px 余量）。

    下限取 MIN 与工作区的较小者：正常屏幕仍守住 1180x720；工作区比 MIN 还小
    （小屏 / 200% 缩放）时贴着工作区走 —— 硬钳到 1180x720 会得到比屏幕还大
    的窗口，自检的「≤工作区」断言也必败。
    """
    work_w, work_h = _work_area_css()
    width = max(min(MIN_W, work_w), min(WIN_W, work_w - 16))
    height = max(min(MIN_H, work_h), min(WIN_H, work_h - 16))
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
    这里只认 WinForms 主窗口（class = WindowsForms10.*）+ 本进程 PID，
    双开实例时也不会把另一个实例的窗口当成自己的来修补/摆位。
    visible_only=False 供 --selftest 用：自检窗口是隐藏建出来的，但外框照样可读写。
    """
    hits = []
    my_pid = os.getpid()

    def _cb(hwnd, _arg):
        if visible_only and not ctypes.windll.user32.IsWindowVisible(hwnd):
            return True
        wpid = ctypes.wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value != my_pid:
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
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p  # 默认 c_int 会截断 64 位指针
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
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
        if not ptr:
            kernel32.GlobalFree(handle)
            raise RuntimeError("剪贴板锁定失败")
        try:
            ctypes.memmove(ptr, ctypes.create_unicode_buffer(text), size)
        finally:
            kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            # SetClipboardData 失败时内存块所有权不转移给系统，得自己释放
            kernel32.GlobalFree(handle)
            raise RuntimeError("剪贴板写入失败")
    finally:
        user32.CloseClipboard()


def _guard(fn):
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 统一回给前端显示
            # 前端只拿到一行 error 文案；这里把堆栈落进 skillkit.log，排障有据
            _LOG.exception("api %s 失败", fn.__name__)
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    wrapper.__name__ = fn.__name__
    return wrapper


def _serialized(fn):
    """数据/文件系统类桥方法统一串行化。

    pywebview 每个 js_api 调用一个线程；SQLite WAL 能保证库不坏，但
    rescan（delete from skills + 全盘扫描）与 delete_skill / set_agent
    （建删 junction、rmtree）交错时文件系统操作会互相绊倒——比如
    make_junction 撞上对方刚建好的路径就抛 RuntimeError。锁取自
    skillkit.OPS_LOCK（进程内生效；CLI 是独立进程，跨进程不在此防线内）。
    win_* / log_ui / copy_path 不碰库，不参与串行化，避免拖慢窗口反馈。
    """
    def wrapper(self, *args, **kwargs):
        with skillkit.OPS_LOCK:
            return fn(self, *args, **kwargs)
    wrapper.__name__ = fn.__name__
    return wrapper


def _boards_payload(con, cfg, prune=True):
    """五个桥方法共用的看板回包：快照 →（可选）修剪 meta → 读 meta。
    顺序有讲究：prune 会删行，get_meta 必须发生在它之后。"""
    boards = skillkit.list_boards(con, cfg)
    if prune:
        skillkit.prune_meta(con, boards)
    return {"ok": True, "data": boards, "meta": skillkit.get_meta(con)}


class Api:
    """pywebview js_api：每个方法都在独立线程调用，连接按调用新建。"""

    def __init__(self):
        self._window = None
        self._maxed = False

    def attach(self, window):
        self._window = window

    # ---------------------------------------------------------- 数据
    @_guard
    @_serialized
    def bootstrap(self):
        con = skillkit.db()
        try:
            cfg = skillkit.load_config()
            if cfg.get("_load_error"):
                skillkit.log_event(con, "warn", cfg["_load_error"])
                con.commit()
            # 首屏探测：幂等、毫秒级（读 adapters.json + 每工具一次 isdir）。
            # 必须在 seed_meta 之前：seed 的独有播种依赖 tools 表
            skillkit.sync_tools_from_adapters(con, cfg)
            # 全新数据库：中央仓库内容从未登记过，自动扫一次（仅 skills 表为空时，
            # 避免每次启动都全量哈希 store）。store 不可读时 rescan 抛错 →
            # _guard 返回 ok:false → 前端启动错误页如实展示，不给演示数据
            if not con.execute("select 1 from skills limit 1").fetchone():
                skillkit.rescan(con, cfg)
            skillkit.migrate_folders(con, cfg)
            skillkit.seed_meta(con, cfg)
            return _boards_payload(con, cfg)
        finally:
            con.close()

    @_guard
    @_serialized
    def save_meta(self, scope, key, payload):
        con = skillkit.db()
        try:
            return {"ok": True, "row": skillkit.set_meta(con, scope, key, payload)}
        finally:
            con.close()

    @_guard
    @_serialized
    def clear_meta(self, scope, key):
        con = skillkit.db()
        try:
            skillkit.clear_meta(con, scope, key)
            return {"ok": True}
        finally:
            con.close()

    @_guard
    @_serialized
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
            payload = _boards_payload(con, cfg)
            payload["action"] = action
            return payload
        finally:
            con.close()

    @_guard
    @_serialized
    def uninstall_unique(self, key):
        """卸载独有技能：不受 deletable 限制，覆盖外部来源行（extra 目录 /
        容器子目录 / foreign-link）。foreign-link 只摘链接；其余永久删除目录。
        main 直属实体的「删除副本」仍走 delete_skill，原防线不变。"""
        if "/" not in key:
            raise RuntimeError("独有 skill 的 key 必须带 tool 前缀")
        tool, name = key.split("/", 1)
        con = skillkit.db()
        try:
            cfg = skillkit.load_config()
            action = skillkit.uninstall_unique(con, cfg, tool, name)
            payload = _boards_payload(con, cfg)
            payload["action"] = action
            return payload
        finally:
            con.close()

    @_guard
    @_serialized
    def reseed_meta(self):
        con = skillkit.db()
        try:
            cfg = skillkit.load_config()
            skillkit.reseed_meta(con, cfg)
            return _boards_payload(con, cfg, prune=False)
        finally:
            con.close()

    # ---------------------------------------------------------- 分类文件夹
    @_guard
    @_serialized
    def folder_create(self, name, color):
        con = skillkit.db()
        try:
            cfg = skillkit.load_config()
            row = skillkit.folder_create(con, name, color)
            skillkit.log_event(con, "folder-create", "%s (%s)" % (row["name"], row["color"]))
            con.commit()
            payload = _boards_payload(con, cfg)
            payload["folder"] = row
            return payload
        finally:
            con.close()

    @_guard
    @_serialized
    def folder_rename(self, folder_id, name, color):
        con = skillkit.db()
        try:
            cfg = skillkit.load_config()
            row = skillkit.folder_rename(con, folder_id, name, color)
            skillkit.log_event(con, "folder-rename",
                               "%s -> %s (%s)" % (folder_id, row["name"], row["color"]))
            con.commit()
            payload = _boards_payload(con, cfg)
            payload["folder"] = row
            return payload
        finally:
            con.close()

    @_guard
    @_serialized
    def folder_delete(self, folder_id):
        con = skillkit.db()
        try:
            cfg = skillkit.load_config()
            action = skillkit.folder_delete(con, folder_id)
            skillkit.log_event(con, "folder-delete",
                               "%s（%d 个 skill 回到未归档）" % (folder_id, action["moved"]))
            con.commit()
            payload = _boards_payload(con, cfg)
            payload["action"] = action
            return payload
        finally:
            con.close()

    @_guard
    @_serialized
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
    @_serialized
    def rescan(self):
        con = skillkit.db()
        try:
            cfg = skillkit.load_config()
            skillkit.sync_tools_from_adapters(con, cfg)
            count = skillkit.rescan(con, cfg)
            payload = _boards_payload(con, cfg)
            payload["scanned"] = count
            return payload
        finally:
            con.close()

    # ---------------------------------------------------------- 智能体
    @_guard
    @_serialized
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
                try:
                    plan = skillkit.do_sync(con, cfg, only_tools=[key], apply_changes=True)
                except Exception:
                    # junction 建失败很常见（权限 / 路径被占用）。enabled=1 已写
                    # 未提交，但 do_sync 内部的 commit 会把它一起带上去 —— 失败时
                    # 必须显式回写 0，否则重启后「开关开着、链接没建」持续不一致。
                    con.execute("update tools set enabled=0 where key=?", (key,))
                    con.commit()
                    raise
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
            payload = _boards_payload(con, cfg)
            payload["action"] = action
            return payload
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
    @_serialized
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
                # shared：key 必须是库里有登记的 skill 目录名。os.path.join
                # 遇到绝对路径会整体替换、`..` 会往外逃 —— 不查库不过滤的话
                # key="C:/Windows" 就能打开本机任意目录
                key = str(key or "")
                if not key or key in (".", "..") or os.path.basename(key) != key:
                    raise RuntimeError("非法 skill 名: %s" % key)
                if not con.execute("select name from skills where name=?", (key,)).fetchone():
                    raise RuntimeError("skill 不存在：%s" % key)
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
        # 最大化态以系统为准现查：Win+Up 等系统快捷键改过状态后，Python 侧
        # 缓存 _maxed 已经不准，按缓存走会出现按钮「失灵」一次
        hwnd = _hwnd_by_title(visible_only=False)
        zoomed = bool(hwnd and ctypes.windll.user32.IsZoomed(ctypes.wintypes.HWND(hwnd)))
        if zoomed:
            self._window.restore()
        else:
            self._window.maximize()
        self._maxed = not zoomed
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
        """前端诊断进统一日志（kit/skillkit.log，RotatingFileHandler 自动轮转）。

        真实鼠标拖不动时，这个日志能说清是「没收到 pointerdown」「没拿到 bounds」
        还是「后端拒绝移动」，不用再猜。logging 线程安全、自带轮转，
        旧版「加锁 + 手动截断」的 ui-drag.log 通道已并入这里。
        """
        _LOG.info("ui %-9s %s", str(kind)[:20],
                  str(detail).replace("\r", "\\r").replace("\n", "\\n")[:900])
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


def build_window(api, hidden=False, noboot=False):
    """建窗即定位：首帧就是目标外框尺寸 + 工作区居中，之后不再移动，避免启动闪现。

    pywebview 的 width/height 是 WinForms 改边框之前的 Size；frameless 会把标题栏
    和边框裁掉，所以请求值要加上 _nc_css() 的差额，外框才正好等于 fit_size()。
    noboot=1 关掉启动加载动画（selftest 用：断言时序不能被编排时钟干扰）。
    """
    width, height = fit_size()
    nc_x, nc_y = _nc_css()
    x, y = _centered_rect(width, height)
    return webview.create_window(
        "skillkit · Skill Library",
        INDEX + "?desktop=1" + ("&noboot=1" if noboot else "") + "&v=" + _asset_version(),
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


def _report(message):
    """pythonw 没有控制台：错误进统一日志（kit/skillkit.log），并弹系统提示框。"""
    _LOG.error("%s", message)
    try:
        ctypes.windll.user32.MessageBoxW(
            ctypes.wintypes.HWND(0), str(message), "skillkit 启动失败", 0x10)
    except Exception:  # noqa: BLE001 - 非 Windows 或弹窗失败
        try:
            print(message, file=sys.stderr)
        except (AttributeError, OSError):
            pass  # pythonw 下连 stderr 都是 None，别让兜底自己再炸一次


def main():
    parser = argparse.ArgumentParser(description="skillkit 桌面端")
    parser.add_argument("--debug", action="store_true", help="打开 WebView2 调试工具")
    parser.add_argument("--selftest", action="store_true", help="离屏自检后退出")
    args = parser.parse_args()
    _setup_logging()
    _LOG.info("launch debug=%s selftest=%s py=%s",
              args.debug, args.selftest, sys.version.split()[0])

    if not os.path.isfile(INDEX):
        _report("找不到界面文件：%s" % INDEX)
        return 2

    try:
        _make_dpi_aware()
        api = Api()
        window = build_window(api, hidden=args.selftest, noboot=args.selftest)
        api.attach(window)
        os.makedirs(PROFILE, exist_ok=True)
        # 不传 icon：不同 pywebview 版本对它的处理不一致（有的把 icon 透传给
        # create_window()，直接抛 "create_window() got an unexpected keyword
        # argument 'icon'" 让启动整个失败）。Windows 侧图标统一由 on_start()
        # 里的 _apply_window_icon() 走 WM_SETICON 设置，这里不需要再传。
        start_fn = on_start
        if args.selftest:
            import selftest  # 懒加载：正常启动不背自检代码；selftest 反向 import 本模块
            start_fn = selftest.run
        webview.start(start_fn, window,
                      debug=args.debug, private_mode=False, storage_path=PROFILE)
    except Exception as exc:  # noqa: BLE001 - 双击启动时看不到控制台
        _report("启动失败：%s: %s" % (type(exc).__name__, exc))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
