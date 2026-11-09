#!/usr/bin/env python3
"""skilllocker 桌面端外壳（pywebview）。

- 直接 `import skilllocker`，不启 HTTP 服务、不做 REST/序列化层；
- 前端通过 window.pywebview.api 调用这里的方法，数据来自 skilllocker.db；
- 窗口无边框，标题栏由前端 pointer 事件拖动（app.js window move），
  标题栏双击切换最大化；
- 启动时建窗即按 1200x760 居中（含 WinForms 去边框的尺寸补偿），不做二次摆位；
- 关闭即退出进程，编辑内容即时写库（不做去抖，避免隐藏窗口丢编辑）。

用法：
    双击 D:\\skilllocker\\启动 skilllocker.bat   # 推荐：pythonw 静默启动，无控制台
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
import threading
import time
from logging.handlers import RotatingFileHandler

try:
    import webview
except ImportError:
    # 缺 pywebview 不能在顶层崩：pythonw 无控制台、此时日志还没装配，
    # 进程会静默死亡。置 None，main() 装配完日志后给弹窗 + 日志 + 退出码 2
    webview = None

import skilllocker
import skilllocker_base

if getattr(sys, "frozen", False):
    RES_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    KIT = os.path.join(RES_DIR, "kit")
    DATA_DIR = skilllocker_base.DATA_DIR
    UI_DIR = os.path.join(RES_DIR, "ui")
    ICON = os.path.join(RES_DIR, "kit", "assets", "skilllocker.ico")
else:
    KIT = os.path.dirname(os.path.abspath(__file__))
    DATA_DIR = skilllocker_base.DATA_DIR
    UI_DIR = os.path.join(os.path.dirname(KIT), "ui")
    ICON = os.path.join(KIT, "assets", "skilllocker.ico")
os.makedirs(DATA_DIR, exist_ok=True)
INDEX = os.path.join(UI_DIR, "index.html")
PROFILE = os.path.join(DATA_DIR, "desktop_profile")
WIN_W, WIN_H = 1200, 760
MIN_W, MIN_H = 1180, 720
LOG_FILE = os.path.join(DATA_DIR, "skilllocker.log")

# 无边框 WinForms 窗口会被系统去掉 WS_MINIMIZEBOX，任务栏按钮点击就不会最小化；
# 这里在窗口显示后补回该样式位，并在每次窗口状态变化后重新补一次。
WS_MINIMIZEBOX = 0x00020000
_WINDOW_ICONS = {}  # (slot, px) -> HICON，缓存住句柄，重复应用图标时不再泄漏

# 统一日志：launch.log / ui-drag.log / stdout 三套旁路合并成一个 logging 管线，
# 全部落 kit/skilllocker.log（256KB×3 轮转）。pythonw 没有控制台，文件是唯一出口；
# CLI 的 stdout 输出是命令结果，不在此管线内。logging 自身线程安全。
_LOG = logging.getLogger("skilllocker")


def _setup_logging():
    """幂等装配：桥异常、启动错误、前端诊断（log_ui）统一进 skilllocker.log。"""
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


def _hwnd_by_title(prefix="skilllocker", visible_only=True):
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


def _dwm_transitions(hwnd, disable):
    """临时关掉 DWM 的最小化 / 最大化 / 还原缩放动画（DWMWA_TRANSITIONS_FORCEDISABLED）。

    WebView2 在系统缩放动画期间内容不跟帧：最大化窗口还原时先播一段缩放动画，
    网页布局却要等落定才重排，肉眼看就是「闪动 + 震荡」。程序化切换窗口态时
    把动画关掉、落定后再打开，窗口瞬移到目标尺寸，一帧到位。失败静默放过
    （dwmapi 缺席只影响动画，不影响功能）。
    """
    if sys.platform != "win32" or not hwnd:
        return False
    try:
        val = ctypes.c_int(1 if disable else 0)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            ctypes.wintypes.HWND(hwnd), 3, ctypes.byref(val), ctypes.sizeof(val))
        return True
    except Exception:  # noqa: BLE001 - 动画开关失败不拖累窗口操作
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
            # 前端只拿到一行 error 文案；这里把堆栈落进 skilllocker.log，排障有据
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
    skilllocker.OPS_LOCK（进程内生效；CLI 是独立进程，跨进程不在此防线内）。
    win_* / log_ui / copy_path 不碰库，不参与串行化，避免拖慢窗口反馈。
    """
    def wrapper(self, *args, **kwargs):
        with skilllocker.OPS_LOCK:
            return fn(self, *args, **kwargs)
    wrapper.__name__ = fn.__name__
    return wrapper


# 看板快照的发号器：与 OPS_LOCK 分开，只管自增。发号点本身在调用方的锁内，
# 这把锁是给将来某个不串行化也用 _boards_payload 的调用方留的兜底。
_SEQ = [0]
_SEQ_LOCK = threading.Lock()


def _boards_payload(con, cfg, prune=True, include_unique=True, unique_pending=False):
    """五个桥方法共用的看板回包：快照 →（可选）修剪 meta → 读 meta → 发序号。
    顺序有讲究：prune 会删行，get_meta 必须发生在它之后。

    include_unique=False（刷新第一段、开关切换）：不扫独有技能。这时回包带两个
    各管各的标记：
      data.uniqueDeferred = 本轮没扫独有，前端**沿用上一轮的独有行**（hydrate 的
        判据），不是「这里没有独有技能」；
      meta.uniquePending = 「第二段补扫还在路上」，只有刷新第一段该为 True（由
        rescan_shared 显式传 unique_pending=True）。开关切换后面没有人补扫，
        留 True 就是界面上一个永远不消失的「独有扫描中…」。
    这种回包**必须** prune=False 一起用 —— prune 按 boards["unique"] 判存活，
    拿没扫独有的快照去修会把用户写在独有行上的备注当成已删除清掉；
    prune_meta 里另有同一条防呆，两道闸任何一道生效都不会出事。
    """
    boards = skilllocker.list_boards(con, cfg, include_unique=include_unique)
    if not include_unique:
        boards["uniqueDeferred"] = True
        boards["meta"]["uniquePending"] = bool(unique_pending)
    if prune:
        skilllocker.prune_meta(con, boards)
    # seq：这份快照描述的世界比上一份新。调用方全在 OPS_LOCK 里，快照算完即发号，
    # 所以序号单调递增就等于快照的新旧次序。前端据此丢弃晚到的旧快照（见 ui/app.js
    # 的 isStaleSnapshot）：连开多个智能体时每笔 set_agent 都回一份整套看板，
    # 「谁后到谁生效」会把已经连上的刷回未连接 —— 用户看到的就是「只剩一个连上」。
    with _SEQ_LOCK:
        _SEQ[0] += 1
        seq = _SEQ[0]
    return {"ok": True, "data": boards, "meta": skilllocker.get_meta(con), "seq": seq}


class Api:
    """pywebview js_api：每个方法都在独立线程调用，连接按调用新建。"""

    def __init__(self):
        self._window = None
        self._maxed = False
        self._fetch_cancel = None
        self._fetch_lock = threading.Lock()

    def attach(self, window):
        self._window = window

    # ---------------------------------------------------------- 数据
    @_guard
    @_serialized
    def bootstrap(self):
        started = time.monotonic()
        con = skilllocker.db()
        try:
            cfg = skilllocker.load_config()
            # 单次 bootstrap 内共享扫描备忘：seed_meta 与 list_boards 会各扫一遍
            # 独有目录，不共享的话同样的全树 dir_stats / 哈希要做两遍（首屏慢）。
            # 下划线键不会写进 config.json，且每次桥调用都是新 cfg，不会跨操作变旧。
            cfg["_stats_cache"] = {}
            cfg["_hash_cache"] = {}
            cfg["_unique_cache"] = None  # 哨兵：seed_meta 扫完，list_boards 复用
            cfg["_unique_errors"] = []   # 与缓存同生共死：复用结果时也要复用扫描错误
            # 空仓库首启只登记名字与来源，不做文件树统计/内容哈希：本机 526 条
            # 独有能省掉十几秒。只限真正的首启（tools 表还没有任何登记）：之后
            # 即使仓库仍为空，也走完整扫描，避免独有看板长期停在 files=0、
            # 同名副本不合并。必须在 sync_tools_from_adapters 之前取快照。
            fresh_db = not con.execute("select 1 from tools limit 1").fetchone()
            cfg["_unique_light"] = False
            if cfg.get("_load_error"):
                skilllocker.log_event(con, "warn", cfg["_load_error"])
                # events 表前端取不到（payload 恒 []），必须同时进 skilllocker.log
                _LOG.warning("config: %s", cfg["_load_error"])
                con.commit()
            # 首屏探测：幂等、毫秒级（读 adapters.json + 每工具一次 isdir）。
            # 必须在 seed_meta 之前：seed 的独有播种依赖 tools 表
            skilllocker.sync_tools_from_adapters(con, cfg)
            # 全新数据库：中央仓库内容从未登记过，自动扫一次（仅 skills 表为空时，
            # 避免每次启动都全量哈希 store）。store 不可读时 rescan 抛错 →
            # _guard 返回 ok:false → 前端启动错误页如实展示，不给演示数据
            startup_dist = None
            if not con.execute("select 1 from skills limit 1").fetchone():
                skilllocker.rescan(con, cfg)
                # 首启自动接管优先：安装版默认仓库是空的，但各 agent 目录里
                # 往往早就汇聚到一份真仓库。先把它接管过来，共有看板首屏就是
                # 满的；没有候选才退到「自动建库」。两者都只在空库首启跑。
                if not con.execute("select 1 from skills limit 1").fetchone():
                    adopted = skilllocker.auto_adopt_store(con, cfg)
                    if adopted:
                        cfg["_unique_light"] = False
                        cfg["_unique_cache"] = None
                        cfg["_unique_errors"] = []
                        cfg["_seed_note"] = adopted.get("note")
                        _LOG.info("first-run-adopt: %s", cfg["_seed_note"])
                # 首启自动建库：接管后仓库仍为空才跑（DB 为空但 store 有内容
                # 的升级/换库场景不能往里灌）。必须在 sync_tools 之后（候选扫描
                # 依赖 tools 表）、seed_meta 之前；其余守卫在 seed_shared_store。
                if not con.execute("select 1 from skills limit 1").fetchone():
                    cfg["_unique_light"] = bool(fresh_db)
                    seeded = skilllocker.seed_shared_store(con, cfg)
                    if seeded:
                        # 建库成功后仓库非空，后续 seed_meta / 快照走完整扫描
                        cfg["_unique_light"] = False
                        cfg["_unique_cache"] = None
                        cfg["_unique_errors"] = []
                        cfg["_seed_note"] = skilllocker.seed_note(seeded)
                        # events 前端取不到，同时进 skilllocker.log 供排障
                        if seeded.get("error"):
                            _LOG.warning("first-run-seed-failed: %s", cfg["_seed_note"])
                        else:
                            _LOG.info("first-run-seed: %s（%dms，候选 %d）", cfg["_seed_note"],
                                      seeded.get("ms") or 0, seeded.get("candidates") or 0)
            else:
                # 上次运行后仓库里被放进新技能（拖拽 / 解压 / 别的管理器）：
                # 一次 listdir 对账就够，有新增才 rescan + 分发，避免每次启动
                # 全量哈希。仓库不可读时保持现状，交给用户手动刷新，绝不在
                # 启动路径上把「暂时看不见」当「已删除」。
                try:
                    present = {n for n, _p in skilllocker.store_skill_dirs(cfg)}
                except RuntimeError as exc:
                    present = None
                    _LOG.warning("bootstrap store check: %s", exc)
                if present:
                    known = {r["name"] for r in con.execute(
                        "select name from skills")}
                    fresh = sorted(present - known)
                    if fresh:
                        before = {r["name"]: r["content_hash"] for r in con.execute(
                            "select name,content_hash from skills")}
                        skilllocker.rescan(con, cfg)
                        after = {r["name"]: r["content_hash"] for r in con.execute(
                            "select name,content_hash from skills")}
                        new = sorted(n for n in after if n not in before)
                        updated = sorted(n for n in after
                                         if n in before and before[n] != after[n])
                        dist = skilllocker.auto_distribute(con, cfg, new)
                        startup_dist = {
                            "skills": dist["skills"], "new": new,
                            "updated": updated, "tools": dist["tools"],
                            "changed": dist["changed"], "errors": dist["errors"],
                            "isolated": dist.get("isolated", False),
                        }
                        _LOG.info("startup auto-sync: %s（%d 处链接）",
                                  ", ".join(dist["skills"]), dist["changed"])
            skilllocker.migrate_folders(con, cfg)
            skilllocker.seed_meta(con, cfg)
            payload = _boards_payload(con, cfg)
            if startup_dist:
                payload["distributed"] = startup_dist
            # 首屏耗时留痕：用户报「启动卡很久」时先看这一行，区分扫描 / 哈希 /
            # 播种各占多少，不要再靠猜。events 前端取不到，直接进日志。
            _LOG.info("bootstrap %dms · skills=%d unique=%d groups=%d seed=%s",
                      int((time.monotonic() - started) * 1000),
                      payload["data"]["meta"].get("skillCount", 0),
                      payload["data"]["meta"].get("uniqueCount", 0),
                      payload["data"]["meta"].get("uniqueLogical", 0),
                      "yes" if cfg.get("_seed_note") else "no")
            return payload
        finally:
            con.close()

    @_guard
    @_serialized
    def save_meta(self, scope, key, payload):
        con = skilllocker.db()
        try:
            return {"ok": True, "row": skilllocker.set_meta(con, scope, key, payload)}
        finally:
            con.close()

    @_guard
    @_serialized
    def clear_meta(self, scope, key):
        con = skilllocker.db()
        try:
            skilllocker.clear_meta(con, scope, key)
            return {"ok": True}
        finally:
            con.close()

    @_guard
    @_serialized
    def delete_skill(self, scope, key):
        """彻底删除：shared 删中央实体 + 所有链接；unique 只删该 agent 的副本。"""
        if scope not in ("shared", "unique"):
            raise RuntimeError("未知 scope: %s" % scope)
        con = skilllocker.db()
        try:
            cfg = skilllocker.load_config()
            if scope == "unique":
                if "/" not in key:
                    raise RuntimeError("独有 skill 的 key 必须带 tool 前缀")
                tool, name = key.split("/", 1)
                urow = skilllocker.find_unique_row(con, cfg, tool, name)
                if urow and not urow.get("deletable", True):
                    raise RuntimeError(
                        "这条来自外部来源（%s），由 agent 自己管理，本工具不删除；"
                        "确要删除请打开该目录手动处理" % urow["path"])
                action = skilllocker.remove_unique(con, cfg, tool, name)
            else:
                row = con.execute("select name from skills where name=?", (key,)).fetchone()
                if not row:
                    raise RuntimeError("skill 不存在：%s" % key)
                central = os.path.join(cfg["store"], key)
                files, size = skilllocker.dir_stats(central)
                links = skilllocker.remove_skill(con, cfg, key, purge=True)
                skilllocker.clear_meta(con, "shared", key)
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
        con = skilllocker.db()
        try:
            cfg = skilllocker.load_config()
            action = skilllocker.uninstall_unique(con, cfg, tool, name)
            payload = _boards_payload(con, cfg)
            payload["action"] = action
            return payload
        finally:
            con.close()

    @_guard
    @_serialized
    def promote_unique(self, key):
        """独有技能一键转共有：拷入中央仓库、迁移显示名/备注、源副本对齐分发。

        key 规则与 delete_skill / uninstall_unique 一致（tool/name）；哪些行
        能收（同名覆盖 / foreign-link 不收）由 skilllocker.promote_unique 把关。
        """
        if "/" not in key:
            raise RuntimeError("独有 skill 的 key 必须带 tool 前缀")
        tool, name = key.split("/", 1)
        con = skilllocker.db()
        try:
            cfg = skilllocker.load_config()
            action = skilllocker.promote_unique(con, cfg, tool, name)
            payload = _boards_payload(con, cfg)
            payload["action"] = action
            return payload
        finally:
            con.close()

    @_guard
    @_serialized
    def adopt_store(self, path):
        """首启引导①：接管本机其实已经存在的中央仓库。

        安装版的 %LOCALAPPDATA%\\SkillLocker\\store 是空目录，装完首启必然
        「共有 0 / 全是独有」。各 agent 目录里已有的 junction 汇聚点就是用户
        原先的仓库，指过来即可（skilllocker.adopt_store 负责校验与守卫）。
        守卫不通过就抛错，前端 toast 如实显示，不会静默换仓库。
        """
        con = skilllocker.db()
        try:
            cfg = skilllocker.load_config()
            action = skilllocker.adopt_store(con, cfg, path)
            skilllocker.migrate_folders(con, cfg)
            skilllocker.seed_meta(con, cfg)
            payload = _boards_payload(con, cfg)
            payload["action"] = action
            return payload
        finally:
            con.close()

    @_guard
    @_serialized
    def adopt_local_skills(self):
        """首启引导②：一键收编各 agent 主目录里的实体技能（反推不出仓库时的兜底）。

        范围、同名处置与链接对齐规则都在 skilllocker.adopt_local_skills 里，
        前端只负责确认与展示回包汇总（收编 / 跳过 / 对齐链接数）。
        """
        con = skilllocker.db()
        try:
            cfg = skilllocker.load_config()
            action = skilllocker.adopt_local_skills(con, cfg)
            skilllocker.migrate_folders(con, cfg)
            payload = _boards_payload(con, cfg)
            payload["action"] = action
            return payload
        finally:
            con.close()

    @_guard
    def choose_dir(self):
        """原生目录选择（导入弹窗「浏览」用）。create_file_dialog 在 js_api
        线程阻塞等待用户属预期行为；不碰库，不参与串行化。"""
        # pywebview ≥5 弃用 FOLDER_DIALOG 常量；新枚举存在就用新 API，旧版回退
        dlg = getattr(webview, "FileDialog", None)
        r = self._window.create_file_dialog(
            dlg.FOLDER if dlg else webview.FOLDER_DIALOG)
        return {"ok": True, "data": (r[0] if r else "")}

    @_guard
    def import_preflight(self, src, name=None):
        """导入前预检：只读（统计 + 同名时一次哈希比对），不碰库不写盘，
        不参与串行化——与 rescan / import 并发安全（WAL 读不阻塞）。"""
        cfg = skilllocker.load_config()
        pre = skilllocker.import_preflight(cfg, src, name)
        return {"ok": True, "data": pre}

    @_guard
    @_serialized
    def import_skill(self, src, name, folder=""):
        """执行导入：import_dir（原子拷贝 + rescan + log_event）+ 可选归档。

        folder 传非空必须是存在的 folders.id；set_meta 单行 upsert 会新建该
        skill 的 meta 行，下次 bootstrap 的 seed_meta 对已存在行是 no-op，
        两不冲突。返回与 bootstrap 同构的 boards/meta（导入含 rescan，计数
        已刷新），前端只走 applyBootstrap 一条路。"""
        con = skilllocker.db()
        try:
            cfg = skilllocker.load_config()
            got = skilllocker.import_dir(con, cfg, src, name)
            if folder:
                skilllocker.set_meta(con, "shared", got, {"folder": folder})
            # 分发开关是持续状态：导入即按各 agent 当前开关补齐链接。旧实现
            # 只落库不补链，用户必须再手动同步 / 重开一次开关才能用上。
            dist = skilllocker.auto_distribute(con, cfg, [got])
            payload = _boards_payload(con, cfg)
            files, size = skilllocker.dir_stats(os.path.join(cfg["store"], got))
            payload["action"] = {
                "name": got,
                "path": os.path.join(cfg["store"], got).replace(os.sep, "/"),
                "files": files, "bytes": size, "folder": folder or "",
                "distributed": dist["changed"],
                "distributedTools": dist["tools"],
                "distributeErrors": dist["errors"],
            }
            return payload
        finally:
            con.close()

    @_guard
    def online_search(self, query, limit=20):
        """skills.sh 在线检索：纯网络只读，不碰库与仓库，不参与串行化。"""
        rows = skilllocker.online_search(query, limit)
        return {"ok": True, "data": rows}

    @_guard
    def online_fetch(self, source, name, branch=None):
        """在线获取：从 GitHub 下载 skill 到系统临时目录（%TEMP%/skilllocker-dl），
        不写中央仓库；前端拿 data.path 走标准 import_preflight → import_skill。
        下载有 180s 总预算：不持 OPS_LOCK、不参与串行化（只写系统临时目录，
        与 store / 库操作并发安全），失败信息原样回给前端展示。
        下载期间通过 window.__SK_FETCH_PROGRESS 回传 done/total/bytes；
        online_fetch_cancel 置位后最多等当前 HTTP 请求返回/超时即退出。"""
        cancel = threading.Event()
        with self._fetch_lock:
            if self._fetch_cancel is not None:
                raise RuntimeError("已有下载在进行")
            self._fetch_cancel = cancel

        def progress(done, total, nbytes, path):
            if not self._window:
                return
            try:
                self._window.evaluate_js(
                    "window.__SK_FETCH_PROGRESS && window.__SK_FETCH_PROGRESS(%s)"
                    % json.dumps({"done": done, "total": total, "bytes": nbytes,
                                  "path": path}, ensure_ascii=False))
            except Exception:  # noqa: BLE001 - 窗口已关 / 注入失败不影响下载
                pass

        try:
            try:
                data = skilllocker.online_fetch(source, name, branch,
                                                progress=progress, cancel=cancel)
            except Exception as exc:  # noqa: BLE001
                if "已取消" in str(exc):
                    _LOG.info("online fetch cancelled %s/%s", source, name)
                    return {"ok": False, "error": "已取消"}
                raise
            tm = data.get("timings") or {}
            _LOG.info("online fetch %s/%s@%s -> %s（%s 清单，%s 文件，清单 %sms / 下载 %sms / 并发 %s）",
                      source, name, data.get("branch"), data.get("path"),
                      data.get("listing"), data.get("files"),
                      tm.get("listing_ms"), tm.get("download_ms"), tm.get("parallel"))
            return {"ok": True, "data": data}
        finally:
            with self._fetch_lock:
                if self._fetch_cancel is cancel:
                    self._fetch_cancel = None
            if self._window:
                try:
                    self._window.evaluate_js("window.__SK_FETCH_PROGRESS=null")
                except Exception:  # noqa: BLE001
                    pass

    @_guard
    def online_fetch_cancel(self):
        """请求取消当前在线获取；实际退出在下一次请求检查点（≤ NET_TIMEOUT）。"""
        with self._fetch_lock:
            ev = self._fetch_cancel
        if ev is not None:
            ev.set()
            _LOG.info("online fetch cancel requested")
        return {"ok": True, "data": {"cancelled": bool(ev)}}

    @_guard
    @_serialized
    def reseed_meta(self):
        con = skilllocker.db()
        try:
            cfg = skilllocker.load_config()
            skilllocker.reseed_meta(con, cfg)
            return _boards_payload(con, cfg, prune=False)
        finally:
            con.close()

    # ---------------------------------------------------------- 分类文件夹
    @_guard
    @_serialized
    def folder_create(self, name, color):
        con = skilllocker.db()
        try:
            cfg = skilllocker.load_config()
            row = skilllocker.folder_create(con, name, color)
            skilllocker.log_event(con, "folder-create", "%s (%s)" % (row["name"], row["color"]))
            con.commit()
            payload = _boards_payload(con, cfg)
            payload["folder"] = row
            return payload
        finally:
            con.close()

    @_guard
    @_serialized
    def folder_rename(self, folder_id, name, color):
        con = skilllocker.db()
        try:
            cfg = skilllocker.load_config()
            row = skilllocker.folder_rename(con, folder_id, name, color)
            skilllocker.log_event(con, "folder-rename",
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
        con = skilllocker.db()
        try:
            cfg = skilllocker.load_config()
            action = skilllocker.folder_delete(con, folder_id)
            skilllocker.log_event(con, "folder-delete",
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
        con = skilllocker.db()
        try:
            cfg = skilllocker.load_config()
            if scope == "unique":
                if "/" not in key:
                    raise RuntimeError("独有 skill 的 key 必须带 tool 前缀")
                tool, name = key.split("/", 1)
            else:
                name, tool = key, skilllocker.find_conflict_tool(con, cfg, key)
                if not tool:
                    raise RuntimeError("这个 skill 没有同名副本可对照")
            return {"ok": True, "conflict": skilllocker.conflict_detail(con, cfg, tool, name)}
        finally:
            con.close()

    @_guard
    @_serialized
    def rescan(self):
        started = time.monotonic()
        con = skilllocker.db()
        try:
            cfg = skilllocker.load_config()
            # 单次桥调用内的扫描备忘，与 bootstrap 同口径：独有看板的同名行要对
            # 中央仓库补统计、外部来源要算内容哈希，不给缓存就是同一棵树在同一次
            # 刷新里反复全走。下划线键不写进 config.json，每次调用新建，不会变旧。
            cfg["_stats_cache"] = {}
            cfg["_hash_cache"] = {}
            skilllocker.sync_tools_from_adapters(con, cfg)
            t_tools = time.monotonic()
            before = {r["name"]: r["content_hash"] for r in con.execute(
                "select name,content_hash from skills")}
            count = skilllocker.rescan(con, cfg)
            t_scan = time.monotonic()
            after = {r["name"]: r["content_hash"] for r in con.execute(
                "select name,content_hash from skills")}
            new = sorted(n for n in after if n not in before)
            updated = sorted(n for n in after
                             if n in before and before[n] != after[n])
            # 只自动分「新增」：内容变更不在这里静默重拷 —— copy 模式下 agent
            # 目录里的实体可能被用户手改过，扫描一下就覆盖太危险；需要刷新
            # 副本时到智能体页重新打开开关（那是一次明确的同步动作）。
            dist = skilllocker.auto_distribute(con, cfg, new)
            t_dist = time.monotonic()
            payload = _boards_payload(con, cfg)
            payload["scanned"] = count
            payload["distributed"] = {
                "skills": dist["skills"], "new": new, "updated": updated,
                "tools": dist["tools"], "changed": dist["changed"],
                "errors": dist["errors"],
                "isolated": dist.get("isolated", False),
            }
            # 刷新耗时留痕（与 bootstrap 同一行式样）：用户报「刷新慢」时先分清
            # 是重扫仓库、自动分发还是看板扫描慢，不要再靠猜。events 前端取不到，
            # 直接进 skilllocker.log。
            _LOG.info("rescan %dms · 探测%d 扫描%d 分发%d 看板%d · skills=%d new=%d "
                      "unique=%d dist=%d",
                      int((time.monotonic() - started) * 1000),
                      int((t_tools - started) * 1000),
                      int((t_scan - t_tools) * 1000),
                      int((t_dist - t_scan) * 1000),
                      int((time.monotonic() - t_dist) * 1000),
                      count, len(new),
                      payload["data"]["meta"].get("uniqueCount", 0),
                      dist["changed"])
            return payload
        finally:
            con.close()

    @_guard
    @_serialized
    def rescan_shared(self):
        """刷新第一段：探测 agent + 重扫中央仓库 + 分发新增 + 共有/智能体看板。

        与 rescan() 唯一的区别是不扫独有技能（本机实测占一次刷新的 55%~70%，
        因为要把各 agent 声明的 extra_dirs —— 包括 `根/**` 这类应用安装树 ——
        整个走一遍）。第二段 rescan_unique() 补上，界面上共有看板因此约早
        1.5 秒出现。独有数据缺省期间前端沿用上一轮的行，meta.uniquePending
        标记「还在扫」，所以这一轮绝不修剪 meta（见 _boards_payload）。
        """
        started = time.monotonic()
        con = skilllocker.db()
        try:
            cfg = skilllocker.load_config()
            cfg["_stats_cache"] = {}
            cfg["_hash_cache"] = {}
            skilllocker.sync_tools_from_adapters(con, cfg)
            before = {r["name"]: r["content_hash"] for r in con.execute(
                "select name,content_hash from skills")}
            count = skilllocker.rescan(con, cfg)
            after = {r["name"]: r["content_hash"] for r in con.execute(
                "select name,content_hash from skills")}
            new = sorted(n for n in after if n not in before)
            updated = sorted(n for n in after
                             if n in before and before[n] != after[n])
            # 与 rescan() 同一条分发口径：只自动分「新增」，内容变更不静默重拷
            dist = skilllocker.auto_distribute(con, cfg, new)
            payload = _boards_payload(con, cfg, prune=False, include_unique=False,
                                      unique_pending=True)
            payload["scanned"] = count
            payload["distributed"] = {
                "skills": dist["skills"], "new": new, "updated": updated,
                "tools": dist["tools"], "changed": dist["changed"],
                "errors": dist["errors"],
                "isolated": dist.get("isolated", False),
            }
            _LOG.info("rescan_shared %dms · skills=%d new=%d 分发=%d",
                      int((time.monotonic() - started) * 1000), count, len(new),
                      dist["changed"])
            return payload
        finally:
            con.close()

    @_guard
    @_serialized
    def rescan_unique(self):
        """刷新第二段：只补独有技能看板（含依赖它的首启引导与同名冲突对照）。

        中央仓库这一轮已经扫过、链接也补完了，所以这里既不再重扫仓库、也不再
        重算共有 / 智能体看板 —— 那两遍在第一段刚算完并画过，第二段要的只多
        是「独有」这份盘外扫描（见 skilllocker.unique_payload）。
        修剪 meta 用第一段的共有名单（只查名字）+ 本段现扫的独有名单，口径与
        一次性 rescan() 完全一致。
        """
        started = time.monotonic()
        con = skilllocker.db()
        try:
            cfg = skilllocker.load_config()
            cfg["_stats_cache"] = {}
            cfg["_hash_cache"] = {}
            snap = skilllocker.unique_payload(con, cfg)
            skilllocker.prune_meta(con, {
                "meta": snap["meta"],
                "skills": [{"name": r["name"]}
                           for r in con.execute("select name from skills")],
                "unique": snap["unique"],
            })
            payload = {"ok": True, "data": snap, "meta": skilllocker.get_meta(con)}
            _LOG.info("rescan_unique %dms · unique=%s",
                      int((time.monotonic() - started) * 1000),
                      snap["meta"]["uniqueCount"])
            return payload
        finally:
            con.close()

    @_guard
    @_serialized
    def align_name(self, name):
        """把某个包 SKILL.md 的声明名对齐到目录名（身份契约修复，见 base.contract_issue）。

        只重写中央仓库里那一个包的 SKILL.md name 一行，不动正文、不动任何 agent
        的链接；改完 rescan 让注册表 label 回到与 name 一致，再回一份新看板。
        """
        con = skilllocker.db()
        try:
            cfg = skilllocker.load_config()
            plan = skilllocker.repair_contract(con, cfg, [name], apply_changes=True)
            if not plan:
                raise RuntimeError("这个包的名字本来就与目录名一致，无需对齐")
            bad = [p for p in plan if p.get("error")]
            if bad:
                raise RuntimeError(bad[0]["error"])
            aligned = [p["skill"] for p in plan if p["done"]]
            if not aligned:
                # 违规在注册表里、盘上却已经一致：说明注册表是旧的（用户直接改了
                # SKILL.md / 换了仓库目录）。此时必须报错而不是回一个 ok=True ——
                # 否则界面弹「已对齐」，用户以为修好了，实际什么都没发生。
                raise RuntimeError("注册表显示名字不符，但仓库文件里已经没有要改的行 —— "
                                   "注册表是旧的，请先点顶栏「刷新扫描」再试")
            skilllocker.sync_tools_from_adapters(con, cfg)
            payload = _boards_payload(con, cfg)
            payload["action"] = {"aligned": aligned,
                                 "declared": [p["declared"] for p in plan]}
            return payload
        finally:
            con.close()

    # ---------------------------------------------------------- 智能体
    @_guard
    @_serialized
    def set_agent(self, key, on):
        """智能体分发开关：开=按中央仓库补齐 junction，关=只撤链接不动实体。

        开的时候不看「上次是不是已经开着」就跳过 —— 连着拨多个开关时，界面可能把
        还在排队的那几个刷回「已关闭」，用户会再拨一次；旧写法第二笔落在 no-op 分支，
        什么都不补，用户看到的就是「剩下的还得重新打开」。改成每次都走同一条
        do_sync：已就位的格口 state=ok-link 不动作，缺的才建（实测整仓库重跑
        一次 0.15 秒，比让界面撒谎便宜得多），顺带把 agent 目录被外部清空的场景
        自愈回来。撤链方向仍按原语义：只撤本工具建的链接，不碰实体。

        回包不扫独有技能（本机 152 条独有要 1.05 秒，占一次开关的大头）：开关
        不增删技能，独有看板沿用上一轮，下一次刷新自然重算。
        """
        started = time.monotonic()
        con = skilllocker.db()
        try:
            cfg = skilllocker.load_config()
            row = con.execute("select * from tools where key=?", (key,)).fetchone()
            if not row:
                raise RuntimeError("未知 agent: %s" % key)
            if not row["detected"]:
                raise RuntimeError("%s 未安装（没有检测到 %s）" % (row["label"], row["detect_dir"]))
            t_probe = time.monotonic()
            if bool(on):
                was_enabled = bool(row["enabled"])
                if not was_enabled:
                    con.execute("update tools set enabled=1 where key=?", (key,))
                try:
                    plan = skilllocker.do_sync(con, cfg, only_tools=[key], apply_changes=True)
                except Exception:
                    # junction 建失败很常见（权限 / 路径被占用）。enabled=1 已写
                    # 未提交，但 do_sync 内部的 commit 会把它一起带上去 —— 失败时
                    # 必须显式回写 0，否则重启后「开关开着、链接没建」持续不一致。
                    # 本来就读到 enabled=1 的不回写：那不是本次造成的。
                    if not was_enabled:
                        con.execute("update tools set enabled=0 where key=?", (key,))
                    con.commit()
                    raise
                changed = sum(1 for p in plan if p.get("done"))
                conflicts = [p["skill"] for p in plan
                             if p["state"] in ("conflict-dir", "conflict-file")]
                skipped = [p["skill"] for p in plan
                           if p["action"] is None and p["state"] not in
                           ("ok-link", "ok-copy", "off", "conflict-dir", "conflict-file")]
                if not was_enabled:
                    skilllocker.log_event(con, "agent-on", "%s（建 %d 处）" % (key, changed))
                elif changed:
                    # 开关本来开着却又补上了链接 = agent 目录被外部清过，界面上
                    # 这条留痕，用户报「链接莫名消失」时不用猜是谁动的
                    skilllocker.log_event(con, "agent-repair",
                                          "%s 补齐 %d 处（开关原本已开）" % (key, changed))
                con.commit()
                action = {"on": True, "changed": changed, "repaired": was_enabled,
                          "conflicts": conflicts, "skipped": skipped}
            elif row["enabled"]:
                removed = skilllocker.unlink_tool(con, cfg, key)
                skilllocker.toggle(con, "tools", key, 0)
                kept = [s["name"] for s in con.execute(
                    "select name from skills order by name")
                    if os.path.lexists(os.path.join(row["skills_dir"], s["name"]))]
                skilllocker.log_event(con, "agent-off", "%s（撤 %d 处，留 %d 处）"
                                   % (key, len(removed), len(kept)))
                con.commit()
                action = {"on": False, "removed": len(removed), "kept": kept}
            else:
                action = {"on": bool(on), "changed": 0, "removed": 0,
                          "conflicts": [], "skipped": [], "kept": [], "repaired": False}
            # 开关不改变独有技能的存在性 → prune=False + 不扫独有（见 _boards_payload）
            t_work = time.monotonic()
            payload = _boards_payload(con, cfg, prune=False, include_unique=False)
            payload["action"] = action
            _LOG.info("set_agent %s %s %dms · 探测%d 同步%d 看板%d · 变更%d",
                      key, "on" if on else "off",
                      int((time.monotonic() - started) * 1000),
                      int((t_probe - started) * 1000),
                      int((t_work - t_probe) * 1000),
                      int((time.monotonic() - t_work) * 1000),
                      action.get("changed") or action.get("removed") or 0)
            return payload
        finally:
            con.close()

    # ---------------------------------------------------------- 系统
    @_guard
    def export_json(self, name, payload):
        if not self._window:
            raise RuntimeError("窗口未就绪")
        safe = "".join(c for c in str(name or "skilllocker-meta.json")
                       if c.isalnum() or c in "._-") or "skilllocker-meta.json"
        target = self._window.create_file_dialog(
            webview.SAVE_DIALOG, directory=DATA_DIR,
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
    def open_url(self, url):
        """系统浏览器打开外链（导入弹窗在线检索的「页面」按钮）。
        只放行 skills.sh 与 GitHub 的 https 页面，其余一律拒绝。"""
        import urllib.parse
        u = urllib.parse.urlparse(str(url or ""))
        if u.scheme != "https" or u.netloc.lower() not in (
                "www.skills.sh", "skills.sh", "github.com", "www.github.com"):
            raise RuntimeError("不允许打开这个地址：%s" % (url or "（空）"))
        os.startfile(url)  # noqa: S606 - 白名单内的用户主动跳转
        return {"ok": True}

    @_guard
    @_serialized
    def open_dir(self, scope, key):
        """只接受 scope+key，路径由库和磁盘现算，避免任意路径注入。"""
        con = skilllocker.db()
        try:
            cfg = skilllocker.load_config()
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
                urow = skilllocker.find_unique_row(con, cfg, tool, name)
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
        if hwnd:
            _dwm_transitions(hwnd, True)  # 缩放动画期间 WebView2 内容不跟帧，瞬移代替
            # pywebview 的 maximize/restore 带 @_shown_call：从未 Shown 的窗口
            # （--selftest 的隐藏窗）调用是静默 no-op，自检里最大化永远不生效。
            # 直接走 ShowWindow，隐藏 / 可见窗口一视同仁。
            ctypes.windll.user32.ShowWindow(
                ctypes.wintypes.HWND(hwnd), 9 if zoomed else 3)  # SW_RESTORE / SW_MAXIMIZE
            _LOG.info("winmax pre-zoomed=%s post-zoomed=%s", zoomed,
                      bool(ctypes.windll.user32.IsZoomed(ctypes.wintypes.HWND(hwnd))))
        elif zoomed:
            self._window.restore()
        else:
            self._window.maximize()
        self._maxed = not zoomed
        time.sleep(0.2)  # 等 WinForms 把 Location/Size 落定，再把动画开关还回去
        if hwnd:
            _dwm_transitions(hwnd, False)
        _refresh_window_chrome()
        threading.Thread(target=_keep_window_chrome, daemon=True).start()
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
        """最大化窗口拖动越过阈值后先还原，再按还原后的外框继续拖（Windows 习惯）。

        前端只在真实拖动（位移 > 6px）时才调到这里；单击标题栏不还原，
        缩小交给双击（win_max 切换）。
        """
        if not self._window:
            raise RuntimeError("窗口未就绪")
        hwnd = _hwnd_by_title(visible_only=False)
        zoomed = bool(hwnd and ctypes.windll.user32.IsZoomed(ctypes.wintypes.HWND(hwnd)))
        if self._maxed or zoomed:
            if hwnd:
                _dwm_transitions(hwnd, True)  # 缩放动画期间 WebView2 内容不跟帧，瞬移代替
                # 同 win_max：不走 @_shown_call 的 pywebview restore，隐藏窗也能真还原
                ctypes.windll.user32.ShowWindow(
                    ctypes.wintypes.HWND(hwnd), 9)  # SW_RESTORE
            else:
                self._window.restore()
            self._maxed = False
            time.sleep(0.2)  # 等 WinForms 把 Location/Size 落定，再读基准矩形
            if hwnd:
                _dwm_transitions(hwnd, False)
            _LOG.info("winrestore pre-zoomed=%s post-zoomed=%s",
                      zoomed, bool(ctypes.windll.user32.IsZoomed(
                          ctypes.wintypes.HWND(hwnd))))
            _refresh_window_chrome()
            threading.Thread(target=_keep_window_chrome, daemon=True).start()
        bounds = window_bounds()
        if not bounds:
            raise RuntimeError("窗口未就绪")
        return {"ok": True, "bounds": bounds, "maximized": False}

    @_guard
    def log_ui(self, kind, detail):
        """前端诊断进统一日志（kit/skilllocker.log，RotatingFileHandler 自动轮转）。

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
        "skilllocker · Skill Library",
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


def _report(message, title="skilllocker 启动失败"):
    """pythonw 没有控制台：错误进统一日志（kit/skilllocker.log），并弹系统提示框。"""
    _LOG.error("%s", message)
    try:
        ctypes.windll.user32.MessageBoxW(
            ctypes.wintypes.HWND(0), str(message), title, 0x10)
    except Exception:  # noqa: BLE001 - 非 Windows 或弹窗失败
        try:
            print(message, file=sys.stderr)
        except (AttributeError, OSError):
            pass  # pythonw 下连 stderr 都是 None，别让兜底自己再炸一次


def _selftest_runner():
    """取 --selftest 的执行体；自检模块不可用时给出明确提示并返回 None。

    --selftest 的数据断言依赖开发库里的真实技能，只对源码 / 开发包有意义。
    发布包是空库、也不带 selftest.py，误加 --selftest 不该被报成「启动失败」。
    """
    try:
        import selftest  # 懒加载：正常启动不背自检代码；selftest 反向 import 本模块
    except Exception as exc:  # noqa: BLE001 - 自检入口失败要弹窗，不能静默死
        if isinstance(exc, ImportError) and getattr(exc, "name", "") == "selftest":
            detail = "当前安装包未包含自检模块 kit/selftest.py。"
        else:
            detail = "自检模块加载失败：%s: %s" % (type(exc).__name__, exc)
        _report(detail + "\n\n--selftest 只在源码 / 开发包中可用；日常启动请直接双击"
                "“启动 skilllocker.bat”，不要附加 --selftest。",
                title="skilllocker 自检不可用")
        return None
    return selftest.run


def main():
    parser = argparse.ArgumentParser(description="skilllocker 桌面端")
    parser.add_argument("--debug", action="store_true", help="打开 WebView2 调试工具")
    parser.add_argument("--selftest", action="store_true", help="离屏自检后退出")
    args = parser.parse_args()
    _setup_logging()
    _LOG.info("launch debug=%s selftest=%s py=%s",
              args.debug, args.selftest, sys.version.split()[0])

    if webview is None:
        _report("缺少依赖：pywebview 未安装。请在当前 Python 环境执行 "
                "pip install pywebview 后重试")
        return 2

    if not os.path.isfile(INDEX):
        _report("找不到界面文件：%s" % INDEX)
        return 2

    start_fn = on_start
    if args.selftest:
        start_fn = _selftest_runner()
        if start_fn is None:
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
        webview.start(start_fn, window,
                      debug=args.debug, private_mode=False, storage_path=PROFILE)
    except Exception as exc:  # noqa: BLE001 - 双击启动时看不到控制台
        _report("启动失败：%s: %s" % (type(exc).__name__, exc))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
