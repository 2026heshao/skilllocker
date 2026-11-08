#!/usr/bin/env python3
"""skilllocker 基础层：常量、SQLite、config、Windows 链接语义、内容解析与统计，
分发状态机基元 target_state，以及分类文件夹的数据层（folders 表 CRUD 与迁移）。

上层模块（见 skilllocker.py 的模块地图）单向依赖本层，本层不反向 import，保证无环：
  skilllocker_store  工具表与三类扫描（中央仓库 / 独有 / agent 看板）
  skilllocker_links  分发（matrix / do_sync）与增删（import / unlink / remove / 冲突处置）
  skilllocker_meta   skill_meta（显示名 / 备注 / 分类）的播种与读写
  skilllocker_boards list_boards 一次性快照与冲突对照

单测会打补丁的模块级名字（DB_FILE / CONFIG_FILE / ADAPTERS_FILE / EVENTS_KEEP）
只在本模块内被函数体在调用时现读；跨模块读取一律走 `skilllocker_base.X` 属性。
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

KIT = os.path.dirname(os.path.abspath(__file__))
FROZEN = bool(getattr(sys, "frozen", False))
# 安装版：程序与中央仓库放在安装目录（用户看得见、带得走）；db / config /
# 日志仍写 %LOCALAPPDATA%\SkillLocker（程序目录可能被同步/压缩软件碰）。
# 源码运行：保持仓库根 store、kit/ 旁 config、db，开发机零迁移。
# onedir 下 _MEIPASS 指向 <安装目录>\_internal，因此程序根 = RES_DIR 上一级。
RES_DIR = getattr(sys, "_MEIPASS", KIT) if FROZEN else KIT
APP_ROOT = os.path.dirname(RES_DIR) if FROZEN else os.path.dirname(KIT)
DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                        "SkillLocker") if FROZEN else KIT
os.makedirs(DATA_DIR, exist_ok=True)
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
ADAPTERS_FILE = os.path.join(RES_DIR, "adapters.json")
DB_FILE = os.path.join(DATA_DIR, "skilllocker.db")
HOME = os.path.expanduser("~")
IGNORE_NAMES = {".git", ".DS_Store", "Thumbs.db", ".gitignore"}
# 中央仓库：源码运行 = 仓库根 store/；安装版 = <安装目录>/store/。
def default_store_for(res_dir, frozen):
    """由资源目录推导默认仓库（纯函数，供自检/单测打桩）。

    onedir 冻结版 res_dir = <安装目录>\\_internal，仓库在它上一级；
    源码运行 res_dir = kit/，仓库在 kit 上一级（仓库根）。
    """
    app_root = os.path.dirname(res_dir) if frozen else os.path.dirname(KIT)
    return os.path.normpath(os.path.join(app_root, "store")).replace(os.sep, "/")


DEFAULT_STORE = default_store_for(RES_DIR, FROZEN)
APP_VERSION = "0.3.4"
# 首启自动建库 / 自动接管的一次性标记（events.kind）。CLI / 桌面 / 自检共用。
FIRST_RUN_SEED_EVENT = "first-run-seed"
FIRST_RUN_ADOPT_EVENT = "first-run-adopt"

# UI 上的两字母标记；不在表里的工具用 label 首字母推导
TOOL_MARKS = {
    "claude_code": "CC", "codex": "CX", "deepseek_harness": "DS", "qoder": "QD",
    "trae_cn": "TR", "workbuddy": "WB", "zcode": "ZC", "codebuddy": "CB",
    "github_copilot": "GH", "pi": "PI",
}

# 进程内操作互斥：桌面端桥方法用它把「读快照 / 写库 + 文件系统操作」整段
# 串行化（见 desktop._serialized）。SQLite WAL 能保证库不坏，但 junction 的
# 建/删、rmtree 这些文件系统操作一旦交错会互相绊倒（make_junction 撞上对方
# 刚建好的路径就抛 RuntimeError）。用 RLock：CLI 的 import→rescan 这类同线程
# 嵌套调用也安全。
OPS_LOCK = threading.RLock()

EVENTS_KEEP = 5000  # events 表按条数轮转的目标水位


# ---------------------------------------------------------------- 基础设施
def _cfg_int(value, default, lo, hi):
    """config 里的整数项：非数字 / 越界一律退回默认，绝不把非法值带进运行时。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(n, lo), hi)


def _cfg_bool(value, default):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off", ""):
            return False
    return default


def store_has_content(path):
    """仓库里至少有一个带 SKILL.md 的直属目录。用于「回退只发生在首启空库」判据。"""
    try:
        names = os.listdir(path)
    except OSError:
        return False
    for n in names:
        p = os.path.join(path, n)
        if os.path.isdir(p) and has_skill_md(p):
            return True
    return False


def _store_writable(path):
    """默认仓库是否真的可写：必须实际写一次，不能只看 os.access。

    Windows 的 os.access(dir, W_OK) 只反映只读属性，对 ACL 拒绝的目录
    （Program Files / System32）仍返回 True；用它判断会让「安装目录不可写 →
    回退 %LOCALAPPDATA%\\store」的守卫永远不触发。这里在目标目录（或最近
    一级已存在的父目录）创建再删除一个临时文件，真正走一次写权限检查。
    """
    probe = path if os.path.isdir(path) else os.path.dirname(os.path.normpath(path))
    if not os.path.isdir(probe):
        return False
    try:
        fd, tmp = tempfile.mkstemp(prefix=".sk-write-", dir=probe)
    except OSError:
        return False
    os.close(fd)
    try:
        os.remove(tmp)
    except OSError:
        pass
    return True


def store_location(store):
    """仓库落点分类：install = 安装目录默认位，localappdata = 回退位，custom = 用户改指。

    首启接管会在运行中改 cfg["store"]，必须用同一个判据重算，否则界面会把
    接管来的自定义仓库继续标成「安装目录」。
    """
    fallback = os.path.normpath(os.path.join(DATA_DIR, "store")).replace(os.sep, "/")
    np = norm_path(store)
    if np == norm_path(DEFAULT_STORE):
        return "install"
    if np == norm_path(fallback):
        return "localappdata"
    return "custom"


def load_config():
    """读 config.json；文件损坏时退回默认值并在 cfg 里留痕（记事件用），
    不能让刷新链路第一步就崩（与 agent_entries / extra_dirs_map 的容错同口径）。

    默认仓库在安装目录里。若它不可写（例如有人把包装到 Program Files 的机器级
    安装），仅在空库首启时回退到 %LOCALAPPDATA%\\SkillLocker\\store 并写明原因；
    已有内容的库绝不中途换落点，避免「两份仓库」。
    """
    cfg = {"store": DEFAULT_STORE}
    explicit_store = False
    if os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                explicit_store = "store" in loaded
                cfg.update(loaded)
        except (OSError, ValueError) as exc:
            cfg["_load_error"] = "config.json 解析失败，已退回默认配置: %s" % exc
    # 用户是否在 config.json 里显式写过 store：首启自动接管只服务「没配置过
    # 仓库」的安装版；显式配置再空也不替用户改指（他自己的选择优先）。
    cfg["_store_explicit"] = explicit_store
    cfg["store"] = os.path.normpath(str(cfg.get("store") or "")).replace(os.sep, "/")
    if cfg["store"] in ("", ".", ".."):
        # 空串 / 相对根：normpath("") 得 "."，会把中央仓库指到 kit 目录本身，
        # 下一轮 rescan 在 kit 下扫不到 SKILL.md 就清库 + 清 meta
        cfg["_load_error"] = "config.json 的 store 为空或相对根，已退回默认仓库目录"
        cfg["store"] = os.path.normpath(DEFAULT_STORE).replace(os.sep, "/")
    # 安装目录不可写：只在「用户没显式配置 store」且默认仓库还没内容时回退，
    # 已有库 / 显式指定的坏路径绝不静默改指（后者由 _load_error 如实报错）
    fallback = os.path.normpath(os.path.join(DATA_DIR, "store")).replace(os.sep, "/")
    if (not explicit_store
            and norm_path(cfg["store"]) == norm_path(DEFAULT_STORE)
            and norm_path(fallback) != norm_path(DEFAULT_STORE)
            and not store_has_content(cfg["store"])
            and not _store_writable(cfg["store"])):
        cfg["_load_error"] = (
            "默认仓库目录不可写：%s；已回退到 %s。可把应用装到用户目录，"
            "或手动修改 config.json 的 store" % (cfg["store"], fallback))
        cfg["store"] = fallback
    try:
        os.makedirs(cfg["store"], exist_ok=True)
    except OSError as exc:
        # 盘符不存在 / 权限拒绝不该让启动链路第一步就崩：留痕继续走，
        # rescan 会抛「中央仓库目录不存在」，由前端错误页如实展示
        cfg["_load_error"] = ("中央仓库目录不可创建: %s（%s）；"
                              "请检查磁盘或修改 config.json 的 store" % (cfg["store"], exc))
    # 界面/文档要如实显示仓库落在哪：install = 程序目录，localappdata = 回退/custom
    cfg["storeIn"] = store_location(cfg["store"])
    # 首启自动建库的可选参数（config.json 手写即可）。非法值回默认，不让启动崩。
    cfg["seed_auto"] = _cfg_bool(cfg.get("seed_auto"), True)
    cfg["seed_min_agents"] = _cfg_int(cfg.get("seed_min_agents"), 2, lo=1, hi=8)
    cfg["seed_max_skills"] = _cfg_int(cfg.get("seed_max_skills"), 300, lo=1, hi=5000)
    cfg["seed_budget_s"] = _cfg_int(cfg.get("seed_budget_s"), 20, lo=1, hi=120)
    cfg["seed_max_bytes"] = _cfg_int(cfg.get("seed_max_bytes"), 400 << 20,
                                     lo=1 << 20, hi=8 << 30)
    return cfg


def write_config(cfg):
    """把 store 写回 config.json（临时名 + os.replace 原子替换）。

    首启「接管已有中央仓库」是唯一会改仓库路径的入口，改完必须落盘，否则
    下次启动又回到空的默认仓库。**合并旧文件**：用户手写的 seed_auto /
    seed_min_agents 等键不能被一次接管抹掉；_load_error 等运行期留痕不进文件。
    失败抛 RuntimeError，由调用方（adopt_store）如实回给前端。
    """
    store = os.path.normpath(str(cfg.get("store") or "")).replace(os.sep, "/")
    if store in ("", ".", ".."):
        raise RuntimeError("拒绝写入空的中央仓库路径")
    payload = {}
    if os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as fh:
                old = json.load(fh)
            if isinstance(old, dict):
                payload.update({k: v for k, v in old.items() if not k.startswith("_")})
        except (OSError, ValueError):
            payload = {}  # 旧文件坏了：以本次 store 为准重写，不把损坏内容带回去
    payload["store"] = store
    tmp = "%s.tmp-%d" % (CONFIG_FILE, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_FILE)
    except OSError as exc:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise RuntimeError("config.json 写入失败（%s）：%s" % (CONFIG_FILE, exc))
    return store


def db():
    con = sqlite3.connect(DB_FILE, timeout=20)
    con.row_factory = sqlite3.Row
    con.execute("pragma journal_mode=WAL")
    con.executescript(
        """
        create table if not exists skills(
          name text primary key, label text, description text,
          content_hash text, enabled integer default 1,
          origin text, added_at integer, updated_at integer,
          files integer, bytes integer);
        create table if not exists tools(
          key text primary key, label text, skills_dir text, detect_dir text,
          enabled integer default 1, mode text default 'junction',
          detected integer default 0);
        create table if not exists events(
          id integer primary key autoincrement, ts integer, kind text, detail text);
        create table if not exists suppressed(
          tool text, skill text, kind text, note text,
          primary key(tool, skill));
        create table if not exists skill_meta(
          scope text not null, key text not null, tool text,
          display_name text, note text, categories text, seed text,
          updated_at integer,
          starred integer not null default 0,
          starred_at integer,
          primary key(scope, key));
        create table if not exists folders(
          id text primary key,
          name text not null,
          color text not null,
          position integer not null default 0,
          created_at integer);
        """
    )
    # 收藏列（v0.3.1）：只增不改。pragma 判存在再 alter，放在 db() 而不是
    # migrate_folders —— CLI / 桌面 / 自检所有入口打开库时就地补齐，成本微秒级。
    _cols = {r[1] for r in con.execute("pragma table_info(skill_meta)")}
    if "starred" not in _cols:
        con.execute("alter table skill_meta add column starred integer not null default 0")
    if "starred_at" not in _cols:
        con.execute("alter table skill_meta add column starred_at integer")
    # 中央仓库统计列（刷新提速）：rescan 那一遍全树哈希本来就读到了文件数与
    # 字节，顺路存下来，快照算 storeSize 就不用再走一遍全树。只增列不回填：
    # NULL 由 store_size() 识别出来并退回真实遍历，下一轮 rescan 自动补齐。
    _scols = {r[1] for r in con.execute("pragma table_info(skills)")}
    if "files" not in _scols:
        con.execute("alter table skills add column files integer")
    if "bytes" not in _scols:
        con.execute("alter table skills add column bytes integer")
    return con


def has_first_run_seed(con):
    """首启自动建库是否跑过（幂等判据）。基础层唯一实现，CLI/桌面/自检共用。"""
    return bool(con.execute("select 1 from events where kind=? limit 1",
                            (FIRST_RUN_SEED_EVENT,)).fetchone())


def has_first_run_action(con):
    """首启是否已经自动建库 / 自动接管 / 手动接管过（幂等总判据）。

    三种动作都会改变「中央仓库指针」或「仓库内容」，任意一条留痕都说明首启
    已经做过决策；之后即使仓库被清空，也不该再自动改指，避免用户手动清库后
    被下一轮启动灌回来。
    """
    return bool(con.execute(
        "select 1 from events where kind in (?,?,?) limit 1",
        (FIRST_RUN_SEED_EVENT, FIRST_RUN_ADOPT_EVENT, "adopt-store")).fetchone())


def log_event(con, kind, detail):
    con.execute("insert into events(ts,kind,detail) values(?,?,?)",
                (int(time.time() * 1000), kind, detail))
    # events 只增不减会拖慢每次快照里的 count(*)：按 id 粗略轮转（max(id) 是
    # O(1)），超过 2 倍水位就把老的一半裁掉。轮转语句随调用方事务一起提交。
    try:
        top = con.execute("select max(id) from events").fetchone()[0] or 0
        if top > EVENTS_KEEP * 2:
            con.execute("delete from events where id <= ?",
                        (top - EVENTS_KEEP,))
    except sqlite3.Error:
        pass


def now_ms():
    return int(time.time() * 1000)


# ---------------------------------------------------------------- Windows 链接语义
try:
    import ctypes

    _GetFileAttributesW = ctypes.windll.kernel32.GetFileAttributesW
    _INVALID = 0xFFFFFFFF
    _REPARSE = 0x400
    _DIR = 0x10
except Exception:  # 非 Windows 或 ctypes 不可用
    _GetFileAttributesW = None


def is_reparse(path):
    if _GetFileAttributesW is None:
        return os.path.islink(path)
    attrs = _GetFileAttributesW(str(path))
    # 失败返回 INVALID_FILE_ATTRIBUTES(0xFFFFFFFF)，而 ctypes 默认 restype 是
    # 带符号 c_int，实际拿到 -1；不补 -1 判断，不存在的路径会被 -1 & _REPARSE
    # 误判成 reparse point（import_preflight 的死链预检踩中过）
    if attrs == _INVALID or attrs == -1:
        return False
    return bool(attrs & _REPARSE)


def norm_path(p):
    """归一化路径：去掉 Windows 扩展前缀，统一分隔符与盘符大小写，便于比较。"""
    if not p:
        return ""
    s = str(p)
    prefix = chr(92) + chr(92) + "?" + chr(92)
    while s.startswith(prefix):
        s = s[len(prefix):]
    s = os.path.normpath(s).replace(os.sep, "/")
    if len(s) >= 2 and s[1] == ":":
        s = s[0].lower() + s[1:]
    return s.rstrip("/")


def link_target(path):
    try:
        return norm_path(os.readlink(path))
    except OSError:
        return None


def path_under(child, parent):
    """child 是否就是 parent 或在 parent 之下（两侧都先 norm_path 归一化）。

    接管已有仓库 / 批量收编都要判断「这个目录是不是 agent 自己管的地盘」，
    字符串前缀比会漏掉分隔符（C:/x 与 C:/xy），统一走这里。
    """
    c, p = norm_path(child), norm_path(parent)
    if not c or not p:
        return False
    return c == p or c.startswith(p + "/")


_CREATE_NO_WINDOW = 0x08000000  # Windows: 子进程不分配控制台


def _quiet_kwargs():
    """跑 cmd / powershell 时把窗口藏掉。

    desktop.py 用 pythonw 启动（没有控制台），此时每个控制台子进程都会新分配一个
    黑色控制台窗口再消失。make_junction 是「一个 skill 一次」，重连一个 agent
    就是 49 次闪窗，肉眼看起来就是「反复横跳的黑窗口」。
    CREATE_NO_WINDOW 让子进程根本拿不到控制台；SW_HIDE 兜住 powershell 的自绘窗口。
    """
    if sys.platform != "win32":
        return {}
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = 0  # SW_HIDE
    return {"creationflags": _CREATE_NO_WINDOW, "startupinfo": info}


def _decode(raw):
    """cmd / powershell 的输出是 ANSI（本机 cp936），不能用 text=True（默认按 UTF-8
    解码会抛 UnicodeDecodeError，连失败原因都拿不到）。按 UTF-8 → 系统 ANSI 依次试。"""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    for enc in ("utf-8", "mbcs", "gbk"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def make_junction(link, target):
    """建目录 junction（无需管理员权限）。先试 cmd mklink，再退到 PowerShell。"""
    link, target = os.path.normpath(link), os.path.normpath(target)
    attempts = []
    cmds = [
        ["cmd", "/c", "mklink", "/J", link, target],
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         "New-Item -ItemType Junction -Path %r -Target %r | Out-Null" % (link, target)],
    ]
    for cmd in cmds:
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=60,
                               **_quiet_kwargs())
        except Exception as exc:
            attempts.append("%s: %s" % (cmd[0], exc))
            continue
        if r.returncode == 0 and os.path.isdir(link):
            return True
        attempts.append("%s rc=%s %s" % (cmd[0], r.returncode,
                                         _decode(r.stderr or r.stdout).strip()[:160]))
    raise RuntimeError("junction failed: " + " | ".join(attempts))


def make_symlink(link, target):
    os.symlink(target, link)


def remove_link(path):
    """只删链接本身。非链接时拒绝执行，避免误删实体。"""
    if not os.path.lexists(path):
        return False
    if not (is_reparse(path) or os.path.islink(path)):
        raise RuntimeError("refuse to remove non-link path: %s" % path)
    try:
        os.rmdir(path)
    except OSError:
        os.unlink(path)
    return True


def delete_tree(path):
    """永久删除实体目录（不进回收站）。指向链接的一律拒绝。"""
    if not os.path.isdir(path):
        return False
    if is_reparse(path) or os.path.islink(path):
        raise RuntimeError("refuse to rmtree a reparse point: %s" % path)
    shutil.rmtree(path)
    return True


# ---------------------------------------------------------------- 内容解析
def iter_files_stat(root, onerror=None):
    """遍历目录内文件（不跟进链接 / junction），产出 (相对路径, 绝对路径, 字节数)。

    旧实现 os.walk + 每个路径一次 GetFileAttributesW（is_reparse）：本机
    500+ 个独有目录要调 3.8 万次系统调用，首屏多花 4~5 秒。改用 os.scandir：
    DirEntry 自带缓存属性，is_symlink / is_junction 一次拿够；输出顺序不变
    （调用方 sha_tree / file_list 都会再排序）。

    字节数一并产出，不再让调用方二次 stat：dir_stats / file_list 旧实现遍历完
    还要 os.path.getsize(p) 一遍，实测一次刷新 5333 次 nt.stat ≈ 0.4s，而
    DirEntry 早就把这个数拿到了。取不到大小按 0 计（与旧 getsize 的 OSError
    分支同口径：计数照旧，体积少算这一个）。
    """
    stack = [root]
    while stack:
        dirpath = stack.pop()
        try:
            entries = list(os.scandir(dirpath))
        except OSError as exc:
            if onerror is not None:
                onerror(exc)
            continue
        subdirs = []
        for entry in entries:
            name = entry.name
            if name in IGNORE_NAMES:
                continue
            try:
                # 链接 / junction 一律不跟进：删库、哈希都不能顺着链接跑到别处
                if entry.is_symlink() or getattr(entry, "is_junction", lambda: False)():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    subdirs.append(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    try:
                        size = entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        size = 0
                    yield (os.path.relpath(entry.path, root).replace(os.sep, "/"),
                           entry.path, size)
            except OSError:
                continue
        # 逆序入栈，出栈时按路径升序（与旧 iter_files 的稳定顺序一致）
        for d in sorted(subdirs, reverse=True):
            stack.append(d)


def iter_files(root, onerror=None):
    """iter_files_stat 的旧接口形状：只给 (相对路径, 绝对路径)。"""
    for rel, path, _size in iter_files_stat(root, onerror):
        yield rel, path


def sha_tree_stats(root):
    """一次遍历同时得到 (内容哈希, 文件数, 总字节)。

    rescan 每轮都要把中央仓库整树读一遍算哈希，只要哈希的话文件数 / 字节就
    被丢掉了，list_boards 随后得为 storeSize 再走一遍全树（实测 190~440ms）。
    哈希口径就是 sha_tree 的口径（同一份实现），两者不会漂移。
    """
    h = hashlib.sha256()
    count = size = 0

    def _onerror(exc):
        # 目录存在但（整体或子树）不可枚举（权限 / 杀软锁）时 os.walk 默认静默
        # 跳过，会把哈希算成「缺了一块甚至空树」的值 —— target_state 据此判
        # stale-copy，--fix 就按假判定删了重拷。哈希路径必须严格：拿不到完整
        # 证据就抛错，展示用的 dir_stats / file_list 保持宽容（不传 onerror）。
        raise RuntimeError("目录不可枚举: %s" % exc)

    for rel, p, fsize in sorted(iter_files_stat(root, onerror=_onerror)):
        try:
            with open(p, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()
        except OSError:
            # 单文件被杀软锁定 / 权限拒绝 / 扫描间隙被删（TOCTOU）：跳过这一个，
            # 别让整个 rescan / list_boards 毁掉（与 dir_stats 的容错同口径）
            continue
        count += 1
        size += fsize
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(digest.encode("ascii"))
        h.update(b"\0")
    return h.hexdigest(), count, size


def sha_tree(root):
    return sha_tree_stats(root)[0]


def parse_frontmatter(md_path):
    """极简 YAML 子集：key: value、key: > 折叠块、key: | 保留块。"""
    out = {}
    try:
        lines = open(md_path, encoding="utf-8-sig").read().splitlines()
    except OSError:
        return out
    if not lines or lines[0].strip() != "---":
        return out
    i = 1
    while i < len(lines):
        line = lines[i]
        if line.strip() == "---":
            break
        i += 1
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        if line.startswith((" ", "\t")):
            continue
        key, _, rest = line.partition(":")
        key, rest = key.strip(), rest.strip()
        if rest in (">", "|", ">-", "|-", ">+", "|+"):
            block, fold = [], rest.startswith(">")
            while i < len(lines) and (lines[i].startswith((" ", "\t")) or not lines[i].strip()):
                block.append(lines[i].strip())
                i += 1
            while block and not block[-1]:
                block.pop()
            sep = " " if fold else "\n"
            out[key] = sep.join(x for x in block if x).strip()
        else:
            if len(rest) >= 2 and rest[0] in "\"'" and rest[-1] == rest[0]:
                rest = rest[1:-1]
            out[key] = rest
    return out


# ---------------------------------------------------------------- skill 名与身份契约
#
# 不变量：**中央仓库的目录名 == SKILL.md frontmatter 的 name**。
#
# 为什么这是硬约束而不是风格问题：目录名是唯一被所有 agent 和分发逻辑使用的
# 注册名（skills.name → `<skills_dir>/<目录名>`，见 target_state / do_sync），
# 而 frontmatter name 是 Agent Skills 规范里 agent 实际注册技能时读的名字。
# 两者一分叉，「链接建好了」就不再等于「用得上」，而且坏得很安静：
#   - UI 把 frontmatter name 当 label 显示在第二行，用户照着它在 agent 里输
#     `/high-end-visual-design`，而 agent 只认识 `soft-skill` —— 看起来就是
#     「分发 7/7 全绿，但技能没接入」；
#   - 两个目录声明同一个 name 时，按 name 注册的 agent 里必然互相遮蔽，被遮
#     的那个永远调用不到（本仓库 4 组 `__skillhub` 副本使用数全是 0）。
# 所以：入口归一化（adopt_declared_name）+ 扫描期检出（contract_rows）+
# 一键修复（repair_contract），三层都指向同一个判据 contract_issue。
def check_skill_name(name):
    """skill 名 = 中央仓库 / skills_dir 的直属目录名，所有删除与导入入口共用。

    - 路径分隔符 / . .. ：copytree、delete_tree 会写到目录树之外
    - Windows 保留名（CON/NUL/COM1...）：copytree 失败或产出无法访问的目录
    - 尾点 / 尾空格：NTFS 静默吞掉，注册名与磁盘名不一致
    - 冒号：NTFS 备用数据流语法，不是目录名
    """
    if not name or name in (".", "..") or os.path.basename(name) != name:
        raise RuntimeError("非法 skill 名: %s（不能含路径分隔符）" % name)
    if re.match(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])$", str(name).lower()):
        raise RuntimeError("非法 skill 名: %s（Windows 保留名）" % name)
    if name != name.rstrip(". ") or ":" in name:
        raise RuntimeError("非法 skill 名: %s（不能以点/空格结尾或含冒号）" % name)
    return name


def skill_md_of(dirpath):
    """包内主说明文件：SKILL.md 优先，其次 skill.md（与 has_skill_md 同一口径）。"""
    for n in ("SKILL.md", "skill.md"):
        p = os.path.join(dirpath, n)
        if os.path.isfile(p):
            return p
    return None


def declared_name(dirpath):
    """包内声明名 = SKILL.md frontmatter 的 name；没声明或读不到返回 ''。"""
    md = skill_md_of(dirpath)
    if not md:
        return ""
    return str(parse_frontmatter(md).get("name") or "").strip()


def contract_issue(dir_name, declared):
    """目录名与声明名的契约检查。合规返回 None，否则 (kind, declared)。

    '' = 包内没声明 name，规范回落到目录名，算合规（不算违规，避免逼用户给
    每个手写包补 frontmatter）。
    """
    declared = str(declared or "").strip()
    if not declared or declared == dir_name:
        return None
    try:
        check_skill_name(declared)
    except RuntimeError:
        return "invalid", declared
    return "mismatch", declared


_CONTRACT_TEXT = {
    "mismatch": "包内声明名 %s ≠ 目录名 %s",
    "invalid": "包内声明名 %s 不是合法技能名（目录名 %s 才是注册名）",
}


def contract_text(issue, dir_name):
    """违规的人类可读说明；issue 为 None 时返回 ''。"""
    if not issue:
        return ""
    kind, declared = issue
    return _CONTRACT_TEXT[kind] % (declared, dir_name)


def rewrite_declared_name(md_path, new_name):
    """把 SKILL.md frontmatter 的 name 改成 new_name，正文与其余键一字不动。

    只动第一个 frontmatter 块里行首的 `name:`；没有该键就在块首插入。文件没有
    frontmatter 时原样返回（没声明就等于用目录名，本来就合规）。写盘走同级临时
    文件 + os.replace：中途崩溃或杀软拦截不会留下半份 SKILL.md。
    返回 True 表示真的改了内容。
    """
    try:
        raw = open(md_path, "rb").read()
    except OSError as exc:
        raise RuntimeError("SKILL.md 读不了: %s（%s）" % (md_path, exc))
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig") if bom else raw.decode("utf-8")
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(nl)
    if not lines or lines[0].strip() != "---":
        return False
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return False
    hit = None
    for i in range(1, end):
        line = lines[i]
        if line[:1] in (" ", "\t") or not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.partition(":")[0].strip() == "name":
            hit = i
            break
    if hit is None:
        lines.insert(1, "name: %s" % new_name)
    else:
        lines[hit] = "name: %s" % new_name
    out = (b"\xef\xbb\xbf" if bom else b"") + nl.join(lines).encode("utf-8")
    if out == raw:
        return False
    tmp = md_path + ".skname.tmp"
    try:
        with open(tmp, "wb") as fh:
            fh.write(out)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, md_path)
    except OSError as exc:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise RuntimeError("SKILL.md 写不回: %s（%s）" % (md_path, exc))
    return True


def adopt_declared_name(dirpath):
    """入口归一化：把包内声明名改成目录名，保证进中央仓库的包恒满足契约。

    import / seed / promote 三条「新建仓库目录」的路径都过这里。返回改前的
    声明名（没改返回 ''），调用方可据此记日志。
    """
    name = os.path.basename(os.path.normpath(dirpath))
    declared = declared_name(dirpath)
    if not contract_issue(name, declared):
        return ""
    md = skill_md_of(dirpath)
    if not md:
        return ""
    if rewrite_declared_name(md, name):
        return declared
    return ""


def contract_rows(con, cfg=None):
    """按注册表列出身份契约违规，供 verify / CLI / 看板使用。

    判据只用 skills 表现有列（label 就是 rescan 从 frontmatter 读到的 name），
    不加新表、不加新列，也就没有迁移风险：label != name 与契约违规严格等价。
    顺带报出「多个目录声明同一名字」的分组 —— 那是遮蔽的直接原因。
    """
    out = []
    rows = [dict(r) for r in con.execute("select name,label from skills order by name")]
    by_label = {}
    for r in rows:
        if r["label"]:
            by_label.setdefault(r["label"], []).append(r["name"])
    for r in rows:
        issue = contract_issue(r["name"], r["label"])
        if not issue:
            continue
        # 同一声明名被几个目录占用：这些目录在按 name 注册的 agent 里互相遮蔽。
        # 必须从全表分组取，只统计违规行的话，被合规正本「占用同名」这一半证据
        # 会漏掉（正本自己合规，不进违规清单）。
        out.append({"name": r["name"], "declared": issue[1], "kind": issue[0],
                    "detail": contract_text(issue, r["name"]),
                    "shadowed_by": [n for n in by_label.get(issue[1], []) if n != r["name"]]})
    return out


# ---------------------------------------------------------------- 统计与工具表基元
def fmt_bytes(n):
    n = int(n or 0)
    if n <= 0:
        return "0 B"
    if n < 1024:
        return "%d B" % n
    if n < 1024 * 1024:
        return ("%.1f KB" if n < 10240 else "%.0f KB") % (n / 1024.0)
    return "%.1f MB" % (n / 1048576.0)


def dir_stats(path):
    count = size = 0
    if os.path.isdir(path):
        for _rel, _p, fsize in iter_files_stat(path):
            count += 1
            size += fsize
    return count, size


def tree_stats(path):
    """目录总体积（含空目录也无所谓，只按文件算）。"""
    return dir_stats(path)[1]


def store_size(con, cfg):
    """中央仓库总体积（字节），给快照里的 storeSize 用。

    已登记技能的体积直接取 skills.bytes —— rescan 那一遍全树哈希本来就读到了
    文件数与字节，顺路存下来，快照就不用再为这个显示值重走一遍 store
    （实测每次刷新省 190~440ms）。

    仓库根下不属于任何已登记技能的东西（`_backup` 这类备份容器、还没入库的
    散装目录、根上的零散文件）照旧真实遍历计入，所以结果与 tree_stats(store)
    逐字节一致；这部分通常只有几十到几百个文件。判定为「已登记技能」的口径
    是名字在 skills 表里且是个实体目录，与 tree_stats 一样不跟进链接。

    有任一行的 bytes 还是 NULL（老库升级 / 别的入口写入）时整体退回全树遍历，
    宁可慢不可错，下一次 rescan 会把统计补齐。
    """
    row = con.execute("select coalesce(sum(bytes), 0) as total, "
                      "sum(case when bytes is null then 1 else 0 end) as missing "
                      "from skills").fetchone()
    if row["missing"]:
        return tree_stats(cfg["store"])
    total = int(row["total"] or 0)
    known = {r["name"] for r in con.execute("select name from skills")}
    root = cfg["store"]
    try:
        entries = list(os.scandir(root))
    except OSError:
        # 根不可枚举：已登记的这部分体积仍然可信，照实返回，别为了尾数把整块
        # 显示值丢掉（与 extra_dirs_map / agent_entries 的容错同口径）
        return total
    for entry in entries:
        name = entry.name
        if name in IGNORE_NAMES:
            continue
        try:
            is_link = entry.is_symlink() or getattr(entry, "is_junction", lambda: False)()
            if is_link:
                continue  # tree_stats 也不跟进链接
            if entry.is_dir(follow_symlinks=False):
                if name in known:
                    continue  # 已计入 skills.bytes
                total += dir_stats(entry.path)[1]
            elif entry.is_file(follow_symlinks=False):
                total += entry.stat(follow_symlinks=False).st_size
        except OSError:
            continue
    return total


def has_skill_md(p):
    """目录里有没有 SKILL.md —— 独有检测与中央仓库（store_skill_dirs）同一口径。"""
    return (os.path.isfile(os.path.join(p, "SKILL.md"))
            or os.path.isfile(os.path.join(p, "skill.md")))


def file_list(path):
    out = []
    if os.path.isdir(path):
        for rel, _p, size in iter_files_stat(path):
            out.append({"name": rel, "size": size})
    out.sort(key=lambda f: (0 if f["name"] == "SKILL.md" else
                            1 if f["name"].endswith(".md") else 2, f["name"]))
    return out


def _file_digest(path):
    """单文件 sha256（分块读）；读不了返回 None。"""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            while True:
                block = fh.read(65536)
                if not block:
                    break
                h.update(block)
    except OSError:
        return None
    return h.hexdigest()


def _tool_mark(tool):
    mark = TOOL_MARKS.get(tool["key"])
    if mark:
        return mark
    words = re.findall(r"[A-Za-z0-9]+", tool["label"] or tool["key"])
    return "".join(w[0] for w in words[:2]).upper() if words else "??"


def ordered_tools(con):
    """与 UI 快照一致的顺序：已启用在前，同组按 key 字母序。"""
    rows = [dict(r) for r in con.execute("select * from tools where detected=1")]
    rows.sort(key=lambda t: (0 if t["enabled"] else 1, t["key"]))
    return rows


def _hash_tree(path, cache=None):
    """sha_tree 带备忘：同一次看板快照里独有检测与外部来源统计会各算一遍。

    不可枚举的目录缓存 None：拿不到证据不下结论，调用方（target_state /
    _unique_item）按冲突展示、绝不按「空树哈希」自动改写。
    """
    if cache is None:
        cache = {}
    key = norm_path(path)
    if key not in cache:
        try:
            cache[key] = sha_tree(path)
        except (OSError, RuntimeError):
            cache[key] = None
    return cache[key]


# ---------------------------------------------------------------- 分发状态基元
def target_state(link, cfg, skill_row, tool_row, hash_cache=None):
    """返回 (状态, 说明)。状态：ok-link / ok-copy / missing / foreign-link /
    conflict-dir / broken-link / off

    hash_cache：同一次看板快照里同一个实体目录会被 target_state 反复检查
    （state_map 与 agent_entries 各一遍），传共享缓存把全树哈希从两遍降到一遍。
    """
    if tool_row["mode"] == "off":
        return "off", "该工具已停用分发"
    path = os.path.join(tool_row["skills_dir"], skill_row["name"]).replace(os.sep, "/")
    central = (cfg["store"] + "/" + skill_row["name"]).replace(os.sep, "/")
    if not os.path.lexists(path):
        return "missing", path
    if is_reparse(path) or os.path.islink(path):
        tgt = link_target(path)
        if tgt and tgt == norm_path(central):
            if not os.path.isfile(os.path.join(path, "SKILL.md")):
                return "broken-link", path
            return "ok-link", path
        return "foreign-link", "%s -> %s" % (path, tgt)
    if os.path.isdir(path):
        actual = _hash_tree(path, hash_cache)
        if actual is None:
            # 目录存在但不可枚举（权限 / 杀软锁）：绝不下「内容不一致」的结论 ——
            # copy 模式那会判成 stale-copy 让 --fix 删了重拷。conflict-dir 的
            # action 恒为 None：--fix 跳过，等用户修复权限后现算。
            return "conflict-dir", "%s（目录不可读，跳过自动处置）" % path
        if tool_row["mode"] == "copy":
            return ("ok-copy" if actual == skill_row["content_hash"] else "stale-copy"), path
        # junction 模式下出现实体目录：内容一致就接管，不一致就是冲突
        if actual == skill_row["content_hash"]:
            return "takeover", path
        return "conflict-dir", path
    return "conflict-file", path


def last_scan_time(con):
    row = con.execute("select ts from events where kind='scan' order by id desc limit 1").fetchone()
    if row and row["ts"]:
        return datetime.datetime.fromtimestamp(row["ts"] / 1000.0)
    try:
        return datetime.datetime.fromtimestamp(os.path.getmtime(DB_FILE))
    except OSError:
        return datetime.datetime.now()


# ---------------------------------------------------------------- 分类文件夹（数据层）
# 单归属模型：一个 skill 最多属于一个文件夹（skill_meta.folder_id）；
# '' / NULL = 未归档。预置夹沿用旧 7 分类的 id，迁移时 categories 首元素
# 按 id 对号入座。纯 SQL + 本层工具，不依赖 store/links/meta/boards（单向依赖）。
FOLDER_COLORS = ("violet", "accent", "warning", "rose", "teal", "slate", "green")
FOLDER_PRESETS = (
    ("design", "设计", "violet"), ("frontend", "前端", "accent"),
    ("doc", "文档", "warning"), ("video", "视频", "rose"),
    ("memory", "记忆", "teal"), ("eng", "工程", "slate"),
    ("search", "检索", "green"),
)


def _folder_dict(row):
    return {"id": row["id"], "name": row["name"], "color": row["color"],
            "position": row["position"]}


def _validate_folder_name(name):
    name = str(name or "").strip()
    if not (1 <= len(name) <= 12):
        raise RuntimeError("文件夹名称需 1~12 个字符")
    return name


def folder_rows(con):
    """全部分类夹，按 position 再按创建顺序稳定排序（list_boards 快照用）。"""
    return [_folder_dict(r) for r in con.execute(
        "select id,name,color,position from folders order by position, created_at")]


def folder_create(con, name, color):
    """新建分类夹。名称不去重（id 唯一即可）；position 排到末尾。"""
    name = _validate_folder_name(name)
    if color not in FOLDER_COLORS:
        raise RuntimeError("不支持的颜色: %s（可选：%s）" % (color, "/".join(FOLDER_COLORS)))
    pos = con.execute("select coalesce(max(position), -1) from folders").fetchone()[0] + 1
    stamp = now_ms()
    fid = "f-%x" % stamp
    n = 0
    while con.execute("select 1 from folders where id=?", (fid,)).fetchone():
        n += 1
        fid = "f-%x-%d" % (stamp, n)
    con.execute("insert into folders(id,name,color,position,created_at) values(?,?,?,?,?)",
                (fid, name, color, pos, stamp))
    return _folder_dict(con.execute("select * from folders where id=?", (fid,)).fetchone())


def folder_rename(con, folder_id, name=None, color=None):
    """重命名 / 换色；两参均可选，至少传一个。folder_id 不存在抛错。"""
    row = con.execute("select * from folders where id=?", (folder_id,)).fetchone()
    if not row:
        raise RuntimeError("分类文件夹不存在: %s" % folder_id)
    if name is None and color is None:
        raise RuntimeError("名称与颜色至少传一个")
    new_name = _validate_folder_name(name) if name is not None else row["name"]
    if color is not None:
        if color not in FOLDER_COLORS:
            raise RuntimeError("不支持的颜色: %s" % color)
        new_color = color
    else:
        new_color = row["color"]
    con.execute("update folders set name=?, color=? where id=?",
                (new_name, new_color, folder_id))
    return _folder_dict(con.execute("select * from folders where id=?", (folder_id,)).fetchone())


def folder_delete(con, folder_id):
    """删除分类夹：夹内 skill 全部回到未归档（folder_id 置空），skill 本身不动。"""
    if not con.execute("select 1 from folders where id=?", (folder_id,)).fetchone():
        raise RuntimeError("分类文件夹不存在: %s" % folder_id)
    cur = con.execute("update skill_meta set folder_id=NULL where folder_id=?", (folder_id,))
    moved = max(0, cur.rowcount)
    con.execute("delete from folders where id=?", (folder_id,))
    return {"moved": moved}


def migrate_folders(con, cfg):
    """建 folder_id 列 + 播种预置夹 + 旧多选分类回填（幂等，bootstrap 即跑）。

    - skill_meta 加 folder_id 列：pragma 先判存在再 alter，禁止重建表 ——
      重建会连带抹掉用户的显示名 / 备注与旧 categories。
    - folders 为空时播种 7 个预置夹（沿用旧分类 id，回填才对得上号）。
    - 回填只认 categories JSON 首元素且必须命中夹 id；用户自建分类留空 =
      未归档。**只在刚加上 folder_id 列的那一次跑**：categories 列按口径
      永不改写，若每次启动都回填，用户在界面里「移出到未归档」的动作会被
      旧 cats 在下次启动静默撤销。加列即升级时刻，回填就该发生一次。
    """
    cols = {r[1] for r in con.execute("pragma table_info(skill_meta)")}
    fresh_column = "folder_id" not in cols
    if fresh_column:
        try:
            con.execute("alter table skill_meta add column folder_id text")
        except sqlite3.OperationalError:
            # 另一个进程（CLI / 第二个桌面实例）刚加上：本次不回填。回填只应
            # 发生在「由我加列」的那一次，否则老 cats 会撤销用户的「移出到未归档」
            fresh_column = False
    changed = False
    if not con.execute("select 1 from folders limit 1").fetchone():
        stamp = now_ms()
        for pos, (fid, name, color) in enumerate(FOLDER_PRESETS):
            con.execute("insert into folders(id,name,color,position,created_at) "
                        "values(?,?,?,?,?)", (fid, name, color, pos, stamp))
        log_event(con, "folders-seed", "预置 %d 个分类文件夹" % len(FOLDER_PRESETS))
        changed = True
    backfilled = 0
    if fresh_column:
        known = {r["id"] for r in con.execute("select id from folders")}
        for r in con.execute(
                "select key, categories from skill_meta "
                "where scope='shared' and (folder_id is null or folder_id='')").fetchall():
            try:
                cats = json.loads(r["categories"] or "[]")
            except ValueError:
                cats = []
            first = cats[0] if isinstance(cats, list) and cats and isinstance(cats[0], str) else ""
            if first in known:
                con.execute("update skill_meta set folder_id=? where scope='shared' and key=?",
                            (first, r["key"]))
                backfilled += 1
        if backfilled:
            log_event(con, "folders-backfill", "回填 %d 条归属" % backfilled)
            changed = True
    if changed:
        con.commit()
    return backfilled
