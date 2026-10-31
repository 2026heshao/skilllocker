#!/usr/bin/env python3
"""skillkit 基础层：常量、SQLite、config、Windows 链接语义、内容解析与统计，
分发状态机基元 target_state，以及分类文件夹的数据层（folders 表 CRUD 与迁移）。

上层模块（见 skillkit.py 的模块地图）单向依赖本层，本层不反向 import，保证无环：
  skillkit_store  工具表与三类扫描（中央仓库 / 独有 / agent 看板）
  skillkit_links  分发（matrix / do_sync）与增删（import / unlink / remove / 冲突处置）
  skillkit_meta   skill_meta（显示名 / 备注 / 分类）的播种与读写
  skillkit_boards list_boards 一次性快照与冲突对照

单测会打补丁的模块级名字（DB_FILE / CONFIG_FILE / ADAPTERS_FILE / EVENTS_KEEP）
只在本模块内被函数体在调用时现读；跨模块读取一律走 `skillkit_base.X` 属性。
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
import threading
import time

KIT = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(KIT, "config.json")
ADAPTERS_FILE = os.path.join(KIT, "adapters.json")
DB_FILE = os.path.join(KIT, "skillkit.db")
HOME = os.path.expanduser("~")
IGNORE_NAMES = {".git", ".DS_Store", "Thumbs.db", ".gitignore"}
# 默认中央仓库跟随安装目录（kit/ 的上一级）。不写死盘符：无 D: 盘的机器
# 首启也能解析出正确路径；开发机 D:\skillkit 下推导值与旧硬编码一致，零迁移。
DEFAULT_STORE = os.path.normpath(
    os.path.join(os.path.dirname(KIT), "store")).replace(os.sep, "/")
APP_VERSION = "0.2.1"

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
def load_config():
    """读 config.json；文件损坏时退回默认值并在 cfg 里留痕（记事件用），
    不能让刷新链路第一步就崩（与 agent_entries / extra_dirs_map 的容错同口径）。"""
    cfg = {"store": DEFAULT_STORE}
    if os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as fh:
                cfg.update(json.load(fh))
        except (OSError, ValueError) as exc:
            cfg["_load_error"] = "config.json 解析失败，已退回默认配置: %s" % exc
    cfg["store"] = os.path.normpath(cfg["store"]).replace(os.sep, "/")
    try:
        os.makedirs(cfg["store"], exist_ok=True)
    except OSError as exc:
        # 盘符不存在 / 权限拒绝不该让启动链路第一步就崩：留痕继续走，
        # rescan 会抛「中央仓库目录不存在」，由前端错误页如实展示
        cfg["_load_error"] = ("中央仓库目录不可创建: %s（%s）；"
                              "请检查磁盘或修改 config.json 的 store" % (cfg["store"], exc))
    return cfg


def db():
    con = sqlite3.connect(DB_FILE, timeout=20)
    con.row_factory = sqlite3.Row
    con.execute("pragma journal_mode=WAL")
    con.executescript(
        """
        create table if not exists skills(
          name text primary key, label text, description text,
          content_hash text, enabled integer default 1,
          origin text, added_at integer, updated_at integer);
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
          updated_at integer, primary key(scope, key));
        create table if not exists folders(
          id text primary key,
          name text not null,
          color text not null,
          position integer not null default 0,
          created_at integer);
        """
    )
    return con


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
    if attrs == _INVALID:
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
def iter_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_NAMES]
        for f in sorted(filenames):
            if f in IGNORE_NAMES:
                continue
            p = os.path.join(dirpath, f)
            if is_reparse(p):
                continue
            yield os.path.relpath(p, root).replace(os.sep, "/"), p
        for d in list(dirnames):
            if is_reparse(os.path.join(dirpath, d)):
                dirnames.remove(d)


def sha_tree(root):
    h = hashlib.sha256()
    for rel, p in sorted(iter_files(root)):
        try:
            digest = hashlib.sha256(open(p, "rb").read()).hexdigest()
        except OSError:
            # 单文件被杀软锁定 / 权限拒绝 / 扫描间隙被删（TOCTOU）：跳过这一个，
            # 别让整个 rescan / list_boards 毁掉（与 dir_stats 的容错同口径）
            continue
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(digest.encode("ascii"))
        h.update(b"\0")
    return h.hexdigest()


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
        for _rel, p in iter_files(path):
            count += 1
            try:
                size += os.path.getsize(p)
            except OSError:
                pass
    return count, size


def tree_stats(path):
    """目录总体积（含空目录也无所谓，只按文件算）。"""
    return dir_stats(path)[1]


def has_skill_md(p):
    """目录里有没有 SKILL.md —— 独有检测与中央仓库（store_skill_dirs）同一口径。"""
    return (os.path.isfile(os.path.join(p, "SKILL.md"))
            or os.path.isfile(os.path.join(p, "skill.md")))


def file_list(path):
    out = []
    if os.path.isdir(path):
        for rel, p in iter_files(path):
            try:
                size = os.path.getsize(p)
            except OSError:
                size = 0
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
    """sha_tree 带备忘：同一次看板快照里独有检测与外部来源统计会各算一遍。"""
    if cache is None:
        return sha_tree(path)
    key = norm_path(path)
    if key not in cache:
        try:
            cache[key] = sha_tree(path)
        except OSError:
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
        con.execute("alter table skill_meta add column folder_id text")
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
