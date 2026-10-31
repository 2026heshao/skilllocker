#!/usr/bin/env python3
"""skillkit — 本地 Agent Skills 管理器（自研，无第三方依赖）

扫描 SKILL.md → 收进中央仓库 → 用 junction / copy 分发到各 agent 的全局 skills 目录。
Windows 下所有删除都先判断 reparse point，绝不顺着链接递归删实体。

CLI:
  python skillkit.py scan                 重新扫描中央仓库，刷新注册表
  python skillkit.py status               打印 skill x tool 分发矩阵摘要
  python skillkit.py verify               检查漂移（只报告，不改）
  python skillkit.py sync [--tool k] [--fix]  建立/修复分发
  python skillkit.py import <dir> [--name 名字]  从目录导入
  python skillkit.py remove <name>        删分发 + 删实体（永久，不入回收站）
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time

KIT = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(KIT, "config.json")
ADAPTERS_FILE = os.path.join(KIT, "adapters.json")
DB_FILE = os.path.join(KIT, "skillkit.db")
HOME = os.path.expanduser("~")
IGNORE_NAMES = {".git", ".DS_Store", "Thumbs.db", ".gitignore"}
DEFAULT_STORE = "D:/skillkit/store"
APP_VERSION = "0.1.0"

# UI 上的两字母标记；不在表里的工具用 label 首字母推导
TOOL_MARKS = {
    "claude_code": "CC", "codex": "CX", "deepseek_harness": "DS", "qoder": "QD",
    "trae_cn": "TR", "workbuddy": "WB", "zcode": "ZC", "codebuddy": "CB",
    "github_copilot": "GH", "pi": "PI",
}


# ---------------------------------------------------------------- 基础设施
def load_config():
    cfg = {"store": DEFAULT_STORE}
    if os.path.isfile(CONFIG_FILE):
        cfg.update(json.load(open(CONFIG_FILE, encoding="utf-8")))
    cfg["store"] = os.path.normpath(cfg["store"]).replace(os.sep, "/")
    os.makedirs(cfg["store"], exist_ok=True)
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
        """
    )
    return con


def log_event(con, kind, detail):
    con.execute("insert into events(ts,kind,detail) values(?,?,?)",
                (int(time.time() * 1000), kind, detail))


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
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(hashlib.sha256(open(p, "rb").read()).hexdigest().encode("ascii"))
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


def read_text(path, limit=400000):
    try:
        data = open(path, "rb").read(limit)
        return data.decode("utf-8", "replace")
    except OSError:
        return ""


# ---------------------------------------------------------------- 工具目录
def sync_tools_from_adapters(con, cfg):
    adapters = json.load(open(ADAPTERS_FILE, encoding="utf-8"))
    rows = []
    for a in adapters:
        skills_dir = os.path.join(HOME, *a["skills_dir"].split("/"))
        detect = os.path.join(HOME, *a["detect_dir"].split("/"))
        detected = 1 if os.path.isdir(detect) else 0
        rows.append((a["key"], a["label"], skills_dir.replace(os.sep, "/"),
                     a["detect_dir"], detected))
    for key, label, sd, dd, detected in rows:
        cur = con.execute("select key from tools where key=?", (key,))
        if cur.fetchone():
            con.execute("update tools set label=?, skills_dir=?, detect_dir=?, detected=? where key=?",
                        (label, sd, dd, detected, key))
        else:
            # 首次入库：检测到的工具默认开启分发，没检测到的默认关
            con.execute("insert into tools(key,label,skills_dir,detect_dir,detected,enabled,mode) "
                        "values(?,?,?,?,?,?, 'junction')",
                        (key, label, sd, dd, detected, detected))
    con.commit()
    return [r for r in rows if r[4]]


def store_skill_dirs(cfg):
    root = cfg["store"]
    out = []
    if not os.path.isdir(root):
        return out
    for n in sorted(os.listdir(root)):
        p = os.path.join(root, n)
        if not os.path.isdir(p) or is_reparse(p) or n in IGNORE_NAMES:
            continue
        if os.path.isfile(os.path.join(p, "SKILL.md")) or os.path.isfile(os.path.join(p, "skill.md")):
            out.append((n, p))
    return out


def rescan(con, cfg):
    rows = store_skill_dirs(cfg)
    present = set()
    for name, path in rows:
        md = os.path.join(path, "SKILL.md")
        if not os.path.isfile(md):
            md = os.path.join(path, "skill.md")
        fm = parse_frontmatter(md)
        chash = sha_tree(path)
        label = fm.get("name") or name
        desc = (fm.get("description") or "").strip()
        present.add(name)
        old = con.execute("select content_hash from skills where name=?", (name,)).fetchone()
        if old is None:
            con.execute("insert into skills(name,label,description,content_hash,enabled,origin,added_at,updated_at) "
                        "values(?,?,?,?,1,'scan',?,?)",
                        (name, label, desc, chash, now_ms(), now_ms()))
            log_event(con, "add", name)
        elif old["content_hash"] != chash:
            con.execute("update skills set label=?,description=?,content_hash=?,updated_at=? where name=?",
                        (label, desc, chash, now_ms(), name))
            log_event(con, "content-changed", name)
        else:
            con.execute("update skills set label=?,description=? where name=?", (label, desc, name))
    for r in con.execute("select name from skills"):
        if r["name"] not in present:
            con.execute("delete from skills where name=?", (r["name"],))
            log_event(con, "drop-missing", r["name"])
    log_event(con, "scan", "扫描 %d 个 skill" % len(rows))
    con.commit()
    return len(rows)


# ---------------------------------------------------------------- 分发状态
def target_state(link, cfg, skill_row, tool_row):
    """返回 (状态, 说明)。状态：ok-link / ok-copy / missing / foreign-link /
    conflict-dir / broken-link / off"""
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
        actual = sha_tree(path)
        if tool_row["mode"] == "copy":
            return ("ok-copy" if actual == skill_row["content_hash"] else "stale-copy"), path
        # junction 模式下出现实体目录：内容一致就接管，不一致就是冲突
        if actual == skill_row["content_hash"]:
            return "takeover", path
        return "conflict-dir", path
    return "conflict-file", path


def matrix(con, cfg, only_tools=None, only_skills=None):
    tools = [dict(r) for r in con.execute("select * from tools where detected=1 and enabled=1")]
    if only_tools:
        tools = [t for t in tools if t["key"] in only_tools]
    skills = [dict(r) for r in con.execute(
        "select * from skills order by name")]
    if only_skills:
        skills = [s for s in skills if s["name"] in only_skills]
    out = []
    supp = {(r["tool"], r["skill"]) for r in con.execute("select tool,skill from suppressed")}
    for t in tools:
        for s in skills:
            if not s["enabled"]:
                state, detail = "disabled", "skill 已停用"
            else:
                state, detail = target_state(
                    os.path.join(t["skills_dir"], s["name"]).replace(os.sep, "/"), cfg, s, t)
            if state.startswith("conflict") and (t["key"], s["name"]) in supp:
                state, detail = "suppressed", "两份都保留（已忽略）"
            out.append({"tool": t["key"], "tool_label": t["label"],
                        "skill": s["name"], "state": state, "detail": detail})
    return out


def do_sync(con, cfg, only_tools=None, only_skills=None, apply_changes=True):
    plan = []
    tools = {r["key"]: dict(r) for r in con.execute(
        "select * from tools where detected=1 and enabled=1")}
    if only_tools:
        tools = {k: v for k, v in tools.items() if k in only_tools}
    skills = [dict(r) for r in con.execute("select * from skills order by name")]
    if only_skills:
        skills = [s for s in skills if s["name"] in only_skills]
    for t in tools.values():
        os.makedirs(t["skills_dir"], exist_ok=True)
        for s in skills:
            if not s["enabled"]:
                continue
            central = os.path.join(cfg["store"], s["name"])
            path = os.path.join(t["skills_dir"], s["name"])
            state, detail = target_state(path, cfg, s, t)
            action = None
            if state == "ok-link":
                pass
            elif state == "missing":
                action = "junction" if t["mode"] == "junction" else "copy"
            elif state in ("foreign-link", "broken-link", "takeover"):
                action = "relink"
            elif state == "stale-copy":
                action = "recopy"
            elif state in ("ok-copy", "off"):
                pass
            elif state in ("conflict-dir", "conflict-file"):
                action = None
            else:
                action = None
            entry = {"tool": t["key"], "skill": s["name"], "state": state,
                     "action": action, "path": path.replace(os.sep, "/")}
            plan.append(entry)
            if not apply_changes or action is None:
                continue
            if action == "relink":
                if is_reparse(path) or os.path.islink(path):
                    remove_link(path)
                else:
                    delete_tree(path)
            if action in ("junction", "relink") and t["mode"] == "junction":
                make_junction(path, central)
            elif action in ("copy", "recopy") or (action == "relink" and t["mode"] == "copy"):
                if os.path.lexists(path):
                    if is_reparse(path) or os.path.islink(path):
                        remove_link(path)
                    else:
                        delete_tree(path)
                shutil.copytree(central, path, ignore=shutil.ignore_patterns(*sorted(IGNORE_NAMES)))
            log_event(con, "sync", "%s/%s %s" % (t["key"], s["name"], action or state))
            entry["done"] = action
    if apply_changes:
        con.commit()
    return plan


# ---------------------------------------------------------------- 增删
def import_dir(con, cfg, src, name=None):
    src = os.path.normpath(src).replace(os.sep, "/")
    if not os.path.isdir(src):
        raise RuntimeError("不是目录：%s" % src)
    if is_reparse(src):
        tgt = link_target(src)
        real = os.path.normpath(tgt).replace(os.sep, "/") if tgt else None
        if not real or not os.path.isfile(os.path.join(real, "SKILL.md")):
            raise RuntimeError("源是链接但解析不到 SKILL.md：%s -> %s" % (src, tgt))
        src = real
    name = name or os.path.basename(src)
    central = os.path.join(cfg["store"], name)
    if os.path.normcase(central) == os.path.normcase(src):
        raise RuntimeError("源与中央仓库同名同路径：%s" % central)
    if os.path.lexists(central):
        raise RuntimeError("中央仓库已有同名 skill：%s（先改名或删掉再导入）" % name)
    shutil.copytree(src, central, ignore=shutil.ignore_patterns(*sorted(IGNORE_NAMES)))
    rescan(con, cfg)
    log_event(con, "import", "%s <- %s" % (name, src))
    con.commit()
    return name


def _unlink_one(con, cfg, name, tool_row):
    """撤下单个格口：只删指向中央副本的链接；实体拷贝仅在内容一致时删除。"""
    central = norm_path(os.path.join(cfg["store"], name))
    row = con.execute("select content_hash from skills where name=?", (name,)).fetchone()
    path = os.path.join(tool_row["skills_dir"], name)
    if os.path.lexists(path) and (is_reparse(path) or os.path.islink(path)):
        if link_target(path) == central:
            remove_link(path)
            return [path.replace(os.sep, "/")]
        return []
    if os.path.isdir(path) and not is_reparse(path) and row:
        if sha_tree(path) == row["content_hash"]:
            delete_tree(path)
            return [path.replace(os.sep, "/")]
    return []


def unlink_skill(con, cfg, name, tool_key=None):
    """撤下某个 skill 的全部分发（或仅某个工具）。"""
    if tool_key is None:
        tools = con.execute("select * from tools where detected=1").fetchall()
    else:
        tools = con.execute("select * from tools where detected=1 and key=?", (tool_key,)).fetchall()
    removed = []
    for t in tools:
        removed += _unlink_one(con, cfg, name, t)
    log_event(con, "unlink", "%s（%d 处）" % (name, len(removed)))
    con.commit()
    return removed


def unlink_tool(con, cfg, key):
    """撤下某个工具的全部分发。"""
    t = con.execute("select * from tools where detected=1 and key=?", (key,)).fetchone()
    if not t:
        return []
    removed = []
    for s in con.execute("select name from skills order by name").fetchall():
        removed += _unlink_one(con, cfg, s["name"], t)
    log_event(con, "unlink_tool", "%s（%d 处）" % (key, len(removed)))
    con.commit()
    return removed


def remove_skill(con, cfg, name, purge=True):
    removed_links = []
    for t in con.execute("select * from tools where detected=1"):
        path = os.path.join(t["skills_dir"], name)
        if os.path.lexists(path) and (is_reparse(path) or os.path.islink(path)):
            remove_link(path)
            removed_links.append(path.replace(os.sep, "/"))
        elif os.path.isdir(path) and not is_reparse(path):
            # 实体拷贝：只删内容与我方一致的那些，避免误删用户手改的版本
            row = con.execute("select content_hash from skills where name=?", (name,)).fetchone()
            if row and sha_tree(path) == row["content_hash"]:
                delete_tree(path)
                removed_links.append(path.replace(os.sep, "/"))
    if purge:
        central = os.path.join(cfg["store"], name)
        if os.path.isdir(central):
            delete_tree(central)
    con.execute("delete from skills where name=?", (name,))
    log_event(con, "remove", name + (" purged" if purge else " registry-only"))
    con.commit()
    return removed_links


def remove_unique(con, cfg, tool_key, name):
    """彻底删除某个 agent 目录下的独有 skill 副本；中央仓库实体不受影响。

    只允许删 skills_dir 的直属子目录，且拒绝链接 / 目录外路径，避免误删。
    """
    row = con.execute("select * from tools where key=?", (tool_key,)).fetchone()
    if not row:
        raise RuntimeError("未知 agent: %s" % tool_key)
    if not row["detected"]:
        raise RuntimeError("这个 agent 没有装在本机")
    name = str(name or "")
    if not name or name in (".", "..") or os.path.basename(name) != name:
        raise RuntimeError("非法 skill 名: %s" % name)
    base = os.path.normpath(row["skills_dir"])
    path = os.path.normpath(os.path.join(base, name))
    if os.path.normcase(os.path.dirname(path)) != os.path.normcase(base):
        raise RuntimeError("拒绝删除 skills 目录外的路径: %s" % path)
    if not os.path.isdir(path):
        raise RuntimeError("目录不存在：%s" % path)
    if is_reparse(path) or os.path.islink(path):
        raise RuntimeError("拒绝删除链接：%s" % path)
    files, size = dir_stats(path)
    delete_tree(path)
    clear_meta(con, "unique", tool_key + "/" + name)
    log_event(con, "remove-unique", "%s/%s（%d 文件）" % (tool_key, name, files))
    con.commit()
    return {"scope": "unique", "key": tool_key + "/" + name,
            "path": path.replace(os.sep, "\\"), "files": files, "bytes": size,
            "links": 0, "central": False}


def resolve_conflict(con, cfg, tool_key, name, direction):
    """同名不同版的人工处置。
    local-to-central: 把工具目录里那份收进中央仓库（覆盖中央，之后统一分发）
    central-to-local: 用中央仓库那份覆盖工具目录里那份（转成 junction）
    ignore: 保持现状并记入 suppressed，不再报警
    """
    trow = con.execute("select * from tools where key=?", (tool_key,)).fetchone()
    if not trow:
        raise RuntimeError("未知工具: %s" % tool_key)
    local = os.path.join(trow["skills_dir"], name)
    central = os.path.join(cfg["store"], name)
    if direction == "local-to-central":
        if not os.path.isdir(local) or is_reparse(local):
            raise RuntimeError("本地那份不是可读取的实体目录")
        new_hash = sha_tree(local)
        old = con.execute("select content_hash from skills where name=?", (name,)).fetchone()
        if old and old["content_hash"] == new_hash:
            raise RuntimeError("内容与中央仓库一致，无需回收")
        delete_tree(central)
        shutil.copytree(local, central, ignore=shutil.ignore_patterns(*sorted(IGNORE_NAMES)))
        remove_link(local) if (is_reparse(local) or os.path.islink(local)) else delete_tree(local)
        rescan(con, cfg)
        log_event(con, "resolve", "%s: 本地版收进中央仓库" % name)
    elif direction == "central-to-local":
        if os.path.lexists(local):
            if is_reparse(local) or os.path.islink(local):
                remove_link(local)
            else:
                delete_tree(local)
        make_junction(local, central)
        log_event(con, "resolve", "%s/%s: 中央版覆盖本地并转 junction" % (tool_key, name))
    elif direction == "ignore":
        con.execute("insert or replace into suppressed(tool,skill,kind,note) values(?,?,?,?)",
                    (tool_key, name, "conflict", "人工选择保留两份"))
        log_event(con, "resolve", "%s/%s: 冲突已忽略" % (tool_key, name))
    else:
        raise RuntimeError("未知处置方向: %s" % direction)
    con.execute("delete from suppressed where tool=? and skill=?", (tool_key, name)) if direction != "ignore" else None
    con.commit()
    return {"tool": tool_key, "skill": name, "direction": direction}


def toggle(con, table, ident, value, column="enabled", id_col="key"):
    con.execute("update %s set %s=? where %s=?" % (table, column, id_col), (value, ident))
    con.commit()


def online_search(query, limit=20):
    import urllib.request
    from urllib.parse import quote
    url = "https://skills.sh/api/search?q=%s&limit=%d" % (quote(query), limit)
    req = urllib.request.Request(url, headers={"User-Agent": "skillkit"})
    with urllib.request.urlopen(req, timeout=20) as f:
        data = json.load(f)
    out = []
    for s in data.get("skills", []):
        source = s.get("source") or ""
        name = s.get("name")
        sid = s.get("id") or ("%s/%s" % (source, name) if source else name)
        item = {"name": name, "installs": s.get("installs"), "source": source, "id": sid,
                "page_url": "https://www.skills.sh/%s" % sid}
        # skills.sh 的官方安装命令面向 GitHub 源；非 GitHub 源只给来源与页面
        if "/" in source:
            item["source_url"] = "https://github.com/%s" % source
            item["install"] = "npx skills add https://github.com/%s --skill %s" % (source, name)
        out.append(item)
    return out


# ---------------------------------------------------------------- 元数据（桌面端编辑）
# 说明：skill_meta 用 scope('shared'|'unique') + key 承载两类 skill；
# 共有 key = 目录名，独有 key = tool + '/' + 目录名。
# seed 列记录首次预置分类，用来判定"预置分类不算人工编辑"。

def seed_category(name):
    """与 ui/app.js 的 seedCat() 保持同一套规则，避免两端口径漂移。"""
    n = (name or "").lower()
    if re.search(r"remotion|video", n):
        return "video"
    if re.search(r"mnemosyne|memory", n):
        return "memory"
    if re.search(r"aihot|ai hot", n):
        return "search"
    if re.search(r"pdf|docx|xlsx|pptx", n):
        return "doc"
    if re.search(r"^web-|webapp", n):
        return "frontend"
    if re.search(r"design|taste|stitch|minimalist|brutalist|brandkit|imagegen"
                 r"|image-to-code|redesign|ui-new|ui-design|soft-skill", n):
        return "design"
    return "eng"


def seed_meta(con, cfg, force=False):
    """给还没有 meta 的 skill / 独有条目预置分类。

    幂等：known 集合里已有的（scope,key）不动，只补磁盘上新增的 —— 独有
    看板的行会随磁盘 / adapters 变化增长，所以不能只播种一次。force=True
    时先清空再重建（reseed_meta 用）。
    """
    if force:
        con.execute("delete from skill_meta")
        con.commit()
    known = {(r["scope"], r["key"]) for r in con.execute("select scope,key from skill_meta")}
    rows = []
    for r in con.execute("select name from skills order by name"):
        if ("shared", r["name"]) in known:
            continue
        seed = seed_category(r["name"])
        rows.append(("shared", r["name"], "", "", "", json.dumps([seed]), seed, 0))
    for u in unique_entries(con, cfg):
        key = u["tool"] + "/" + u["name"]
        if ("unique", key) in known:
            continue
        seed = "doc" if u["sameName"] else "eng"
        rows.append(("unique", key, u["tool"], "", "", json.dumps([seed]), seed, 0))
    for row in rows:
        con.execute("insert or replace into skill_meta"
                    "(scope,key,tool,display_name,note,categories,seed,updated_at) "
                    "values(?,?,?,?,?,?,?,?)", row)
    if rows:
        log_event(con, "meta-seed", "预置分类 %d 条" % len(rows))
        con.commit()
    return len(rows)


def get_meta(con):
    out = {}
    for r in con.execute("select * from skill_meta"):
        try:
            cats = json.loads(r["categories"] or "[]")
        except ValueError:
            cats = []
        out[r["key"]] = {
            "display": r["display_name"] or "",
            "note": r["note"] or "",
            "cats": cats if isinstance(cats, list) else [],
            "seed": r["seed"] or "",
            "scope": r["scope"],
            "tool": r["tool"] or "",
            "updated": int(r["updated_at"] or 0),
        }
    return out


def set_meta(con, scope, key, payload):
    """写一条人工编辑。payload: {display, note, cats, tool?}"""
    if scope not in ("shared", "unique"):
        raise RuntimeError("未知 scope: %s" % scope)
    payload = payload or {}
    tool = payload.get("tool") or (key.split("/", 1)[0] if scope == "unique" else "")
    display = str(payload.get("display") or "").strip()[:48]
    note = str(payload.get("note") or "")[:400]
    cats = [str(c)[:24] for c in (payload.get("cats") or [])][:12]
    old = con.execute("select seed from skill_meta where scope=? and key=?", (scope, key)).fetchone()
    seed = old["seed"] if old else ""
    con.execute("insert or replace into skill_meta"
                "(scope,key,tool,display_name,note,categories,seed,updated_at) "
                "values(?,?,?,?,?,?,?,?)",
                (scope, key, tool, display, note, json.dumps(cats, ensure_ascii=False),
                 seed, now_ms()))
    con.commit()
    return {"scope": scope, "key": key, "tool": tool, "display": display,
            "note": note, "cats": cats, "seed": seed, "updated": now_ms()}


def clear_meta(con, scope=None, key=None):
    if scope and key:
        cur = con.execute("delete from skill_meta where scope=? and key=?", (scope, key))
    elif scope:
        cur = con.execute("delete from skill_meta where scope=?", (scope,))
    else:
        cur = con.execute("delete from skill_meta")
    con.commit()
    return cur.rowcount


def reseed_meta(con, cfg):
    con.execute("delete from skill_meta")
    con.commit()
    return seed_meta(con, cfg, force=True)


def prune_meta(con, boards):
    """磁盘上已经不存在的 skill，把对应 meta 行一起清掉，避免"已编辑"计数虚高。"""
    valid = {("shared", s["name"]) for s in boards["skills"]}
    valid |= {("unique", u["tool"] + "/" + u["name"]) for u in boards["unique"]}
    dead = [(r["scope"], r["key"]) for r in con.execute("select scope,key from skill_meta")
            if (r["scope"], r["key"]) not in valid]
    for scope, key in dead:
        con.execute("delete from skill_meta where scope=? and key=?", (scope, key))
    if dead:
        con.commit()
    return len(dead)


def export_meta(con, cfg):
    boards = list_boards(con, cfg)
    return {
        "exportedAt": datetime.datetime.now().isoformat(timespec="seconds"),
        "version": APP_VERSION,
        "store": boards["meta"]["store"],
        "meta": get_meta(con),
        "skillCount": boards["meta"]["skillCount"],
        "uniqueCount": boards["meta"]["uniqueCount"],
    }


# ---------------------------------------------------------------- 看板快照（只读）
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


AGENT_STATUS_LABEL = {"connected": "已连接", "off": "已关闭", "missing": "未安装"}


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


def _extra_sources(t, extra_dirs, skills, hash_cache=None):
    """agent 额外读取的技能目录（本工具只检测展示，不分发、不删除）。

    Trae CN / ZCode 这类 agent 除了自己的 skills 目录，还会读
    `~/.agents/skills`（ClawHub / npx skills add 装进去的真身）。开关只管
    skills_dir，撤链接后这些实体仍会被 agent 加载 —— 界面必须如实说明，
    否则「已关闭却还能调用」看起来像 bug。这里只统计与中央仓库同名的实体，
    别的名字归「独有」看板（见 unique_entries 的 extra 来源）。
    """
    by_name = {s["name"]: s for s in skills}
    out = []
    for rel in extra_dirs or []:
        root = _resolve_extra_root(rel)
        if not root or not os.path.isdir(root):
            continue
        hits = same = diff = 0
        names = []
        for n in sorted(os.listdir(root)):
            p = os.path.join(root, n)
            if n in IGNORE_NAMES or not os.path.isdir(p):
                continue
            row = by_name.get(n)
            if row is None:
                continue
            if is_reparse(p) or os.path.islink(p):
                continue  # 指向别处的链接不是「另一份真身」
            hits += 1
            names.append(n)
            h = _hash_tree(p, hash_cache)
            if h is not None and h == row["content_hash"]:
                same += 1
            else:
                diff += 1
        if hits:
            out.append({
                "rel": rel, "path": root.replace(os.sep, "\\"),
                "count": hits, "same": same, "diff": diff,
                "names": names[:20], "more": max(0, hits - 20),
            })
    return out


def agent_entries(con, cfg, tools=None, skills=None, hash_cache=None):
    """智能体看板：本机所有已知 agent 的三态 + 实际链接数。

    status:
      connected = detected=1 且 enabled=1（分发开关打开）
      off       = detected=1 但 enabled=0（本机有目录，分发已关闭）
      missing   = detect_dir 不存在（未安装）
    linked 只统计真正生效的格口（ok-link / ok-copy）；冲突实体目录单独计数，
    保证界面上的数字来自磁盘而不是开关本身。
    """
    tools = tools if tools is not None else [
        dict(r) for r in con.execute("select * from tools order by key")]
    skills = skills if skills is not None else [
        dict(r) for r in con.execute("select * from skills order by name")]
    try:
        extra_map = {a.get("key"): a.get("extra_dirs") or []
                     for a in json.load(open(ADAPTERS_FILE, encoding="utf-8"))}
    except (OSError, ValueError):
        extra_map = {}
    out = []
    for t in tools:
        detected = bool(t["detected"])
        enabled = bool(t["enabled"])
        mode = t["mode"] or "junction"
        linked = 0
        conflicts = 0
        dir_exists = bool(t["skills_dir"]) and os.path.isdir(t["skills_dir"])
        if detected and dir_exists:
            for s in skills:
                state, _detail = target_state(
                    os.path.join(t["skills_dir"], s["name"]).replace(os.sep, "/"), cfg, s, t)
                if state in ("ok-link", "ok-copy"):
                    linked += 1
                elif state in ("conflict-dir", "conflict-file"):
                    conflicts += 1
        status = "missing" if not detected else ("off" if not enabled else "connected")
        rel = os.path.relpath(t["skills_dir"], HOME).replace(os.sep, "/") if t["skills_dir"] else ""
        detect_rel = t["detect_dir"] or ""
        detect_dir = (os.path.join(HOME, *detect_rel.split("/")).replace(os.sep, "\\")
                      if detect_rel else "")
        out.append({
            "key": t["key"], "label": t["label"], "mark": _tool_mark(t),
            "sub": rel, "dir": (t["skills_dir"] or "").replace("/", "\\"),
            "detect": detect_rel, "detectDir": detect_dir,
            "detected": detected, "enabled": enabled, "mode": mode,
            "status": status, "statusText": AGENT_STATUS_LABEL[status],
            "linked": linked, "conflicts": conflicts,
            "skillsTotal": len(skills), "dirExists": dir_exists,
            "external": _extra_sources(t, extra_map.get(t["key"]), skills, hash_cache)
                        if detected else [],
        })

    rank = {"connected": 0, "off": 1, "missing": 2}
    out.sort(key=lambda a: (rank[a["status"]], (a["label"] or a["key"]).lower()))
    return out


def extra_dirs_map(con):
    """adapters.json 的 extra_dirs 汇总，只留本机装了的 agent。返回两个表：

    tool_rels = {agent key: [相对目录]} —— unique_entries 按它扫各 agent 的外部目录；
    rel_tools = {相对目录: [agent key]} —— 用来算「还有哪些 agent 共读同一目录」
    （sharedWith）。`~/.agents/skills` 这类跨 agent 共享目录会出现在多个声明里，
    独有看板按路径去重归属，行归第一个读取它的 agent。
    """
    try:
        adapters = json.load(open(ADAPTERS_FILE, encoding="utf-8"))
    except (OSError, ValueError):
        return {}, {}
    detected = {r["key"] for r in con.execute("select key from tools where detected=1")}
    tool_rels, rel_tools = {}, {}
    for a in adapters:
        key = a.get("key")
        rels = a.get("extra_dirs") or []
        if key not in detected or not rels:
            continue
        tool_rels[key] = list(rels)
        for rel in rels:
            rel_tools.setdefault(rel, [])
            if key not in rel_tools[rel]:
                rel_tools[rel].append(key)
    return tool_rels, rel_tools


def has_skill_md(p):
    """目录里有没有 SKILL.md —— 独有检测与中央仓库（store_skill_dirs）同一口径。"""
    return (os.path.isfile(os.path.join(p, "SKILL.md"))
            or os.path.isfile(os.path.join(p, "skill.md")))


def _unique_item(t, name, p, src, deletable, shared_with, agent_enabled,
                 kind, same, skills, cfg, store_cache, hash_cache):
    """拼一条独有行；与中央同名且内容一致时返回 None（那是共有技能，不算独有）。"""
    row = skills.get(name)
    if row is not None and kind != "foreign-link":
        h = _hash_tree(p, hash_cache)
        if h is not None and h == row["content_hash"]:
            return None
        kind, same = "overridden", True
    count, size = dir_stats(p)
    item = {
        "name": name, "tool": t["key"], "toolLabel": t["label"],
        "mark": _tool_mark(t), "path": os.path.normpath(p).replace(os.sep, "\\"),
        "files": count, "bytes": size, "sameName": same, "kind": kind,
        "src": src, "deletable": deletable,
        "sharedWith": shared_with, "agentEnabled": bool(agent_enabled),
    }
    if same:
        if name not in store_cache:
            store_cache[name] = dir_stats(os.path.join(cfg["store"], name))
        item["storeFiles"], item["storeBytes"] = store_cache[name]
    return item


def _scan_unique_root(t, root, src, deletable, shared_with, agent_enabled,
                      skills, cfg, store_cache, hash_cache, out):
    """扫一个技能目录（agent 主目录或 extra 外部目录），独有条目追加进 out。

    - 指向中央仓库的链接是我们分发的 junction，不算独有；指向别处的链接
      是 agent 能加载的真身，以 foreign-link 展示（只读：实体不归本工具管）。
    - 目录必须含 SKILL.md 才算 skill；顶层没有但子目录有的容器（如 codex
      的 .system）往下钻一层，skill 名记作「容器/子目录」，同样只读。
    """
    if not root or not os.path.isdir(root):
        return
    for n in sorted(os.listdir(root)):
        p = os.path.join(root, n)
        if n in IGNORE_NAMES or not os.path.isdir(p):
            continue
        if is_reparse(p) or os.path.islink(p):
            tgt = link_target(p)
            if tgt and tgt == norm_path(os.path.join(cfg["store"], n)):
                continue  # 指向中央仓库：本工具建立的分发链接
            real = tgt or p
            if not os.path.isdir(real) or not has_skill_md(real):
                continue  # 断链 / 不是 skill 目录
            out.append(_unique_item(t, n, p, src, False, shared_with, agent_enabled,
                                    "foreign-link", False, skills, cfg,
                                    store_cache, hash_cache))
            continue
        if has_skill_md(p):
            out.append(_unique_item(t, n, p, src, deletable, shared_with, agent_enabled,
                                    "unique", False, skills, cfg,
                                    store_cache, hash_cache))
            continue
        # 容器：顶层没有 SKILL.md，但里面装着 skill —— 展开一层收进来
        for sub in sorted(os.listdir(p)):
            sp = os.path.join(p, sub)
            if sub in IGNORE_NAMES or not os.path.isdir(sp):
                continue
            if is_reparse(sp) or os.path.islink(sp) or not has_skill_md(sp):
                continue
            out.append(_unique_item(t, n + "/" + sub, sp, src, False, shared_with,
                                    agent_enabled, "unique", False, skills, cfg,
                                    store_cache, hash_cache))


def _resolve_extra_root(rel):
    """extra_dirs 的根路径：`~/` 按 HOME 展开；盘符开头视为绝对路径（应用可能
    装在 D:\\ 之类的自定义位置）；其余相对 HOME。"""
    rel = str(rel or "").replace("\\", "/").strip().rstrip("/")
    if not rel:
        return ""
    if rel.startswith("~/"):
        return os.path.join(HOME, *rel[2:].split("/"))
    if len(rel) >= 2 and rel[1] == ":":
        return os.path.normpath(rel)
    return os.path.join(HOME, *rel.split("/"))


def _scan_unique_sweep(t, root, src, shared_with, agent_enabled,
                       skills, cfg, store_cache, hash_cache, out):
    """extra_dirs 的 `根/**` 写法：递归扫插件缓存 / 应用内置这类目录树。

    只认「父目录叫 skills 且自己含 SKILL.md」的目录，技能名取 SKILL.md 的
    name 字段（fallback 目录名）。同名条目按路径分两种情况：
      - 首个不同的路径段是版本号（<插件>/<版本>/skills/<技能>，缓存里新旧
        版本并存而 agent 只加载最新装的）→ 只留 mtime 最新的那份；
      - 是普通目录名（如 Qoder 的 computerUseWindows/MacOS/Linux 三个
        OS 变体）→ 都保留，名字加「（段名）」区分。
    链接不跟进，防止 node_modules 式依赖链接把扫描带飞。
    """
    cands = {}  # name -> [(mtime, rel_under_root, path)]
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in IGNORE_NAMES
                       and not is_reparse(os.path.join(dirpath, d))
                       and not os.path.islink(os.path.join(dirpath, d))]
        if os.path.basename(dirpath) != "skills":
            continue
        for n in sorted(os.listdir(dirpath)):
            p = os.path.join(dirpath, n)
            if n in IGNORE_NAMES or not os.path.isdir(p):
                continue
            if is_reparse(p) or os.path.islink(p):
                continue
            md = os.path.join(p, "SKILL.md")
            if not os.path.isfile(md):
                continue
            name = (parse_frontmatter(md).get("name") or "").strip() or n
            try:
                mt = os.path.getmtime(md)
            except OSError:
                mt = 0
            rel = os.path.relpath(p, root).replace(os.sep, "/")
            cands.setdefault(name, []).append((mt, rel, p))

    ver_re = re.compile(r"^\d+(\.\d+)+$")
    for name, group in cands.items():
        # 第一步：版本折叠。把路径里的版本号段去掉得到变体键（<插件>/<版本>/
        # skills/<技能> 的新旧版本并存而 agent 只加载最新装的），同键留最新。
        by_var = {}
        for mt, rel, p in group:
            vk = "/".join(s for s in rel.split("/") if not ver_re.match(s))
            prev = by_var.get(vk)
            if prev is None or mt > prev[0]:
                by_var[vk] = (mt, rel, p)
        kept = list(by_var.values())
        if len(kept) > 1:
            # 第二步：变体并存（如 Qoder 的 computerUseWindows/MacOS/Linux
            # 三个 OS 变体）→ 都保留，名字加「（首次分歧的段名）」区分。
            rels = [g[1] for g in kept]
            depth = min(len(r.split("/")) for r in rels)
            idx = 0
            while idx < depth and len({r.split("/")[idx] for r in rels}) == 1:
                idx += 1
            for mt, rel, p in kept:
                seg = rel.split("/")[idx] if idx < len(rel.split("/")) else rel
                item = _unique_item(t, "%s（%s）" % (name, seg), p, src, False,
                                    shared_with, agent_enabled, "unique", False,
                                    skills, cfg, store_cache, hash_cache)
                if item:
                    out.append(item)
            continue
        for mt, rel, p in kept:
            item = _unique_item(t, name, p, src, False, shared_with, agent_enabled,
                                "unique", False, skills, cfg, store_cache, hash_cache)
            if item:
                out.append(item)


def unique_entries(con, cfg, tools=None, hash_cache=None):
    """独有技能：agent 能加载、但中央仓库没有（或同名但内容不同）的 skill。

    覆盖三类来源，行的 src 字段标注：
      main         agent 自己 skills_dir 下的实体目录（可删副本）
      extra        agent 额外读取的外部目录（adapters.json 的 extra_dirs，
                   如 ~/.agents/skills；只展示，不删除、不分发）。
                   写法支持 `根/**` 递归扫插件缓存（见 _scan_unique_sweep）
      foreign-link 指向中央仓库以外的 junction（真身在别处，只展示）
    与中央仓库同名且内容一致的实体一律不算独有；同名不同内容记 overridden。
    本机装了但分发开关关闭的 agent 也扫 —— 开关只管本工具建立的链接，
    agent 自己装的 skill 关了开关仍会被它加载，不能从看板上消失。
    多个 agent 声明了同一个外部目录时按路径去重，行归 adapters.json 里
    先声明的 agent（固定归属：开关切换会改变 ordered_tools 的排序，归属
    若跟着排序走，行键就会漂移、用户在行上写的 meta 会被误清），其余
    读取者放进 sharedWith。
    """
    if tools is None:
        tools = ordered_tools(con)
    skills = {r["name"]: dict(r) for r in con.execute("select * from skills")}
    store_cache = {}
    hash_cache = {} if hash_cache is None else hash_cache
    tool_rels, rel_tools = extra_dirs_map(con)
    seen_extra = set()
    out = []
    for t in tools:
        _scan_unique_root(t, t["skills_dir"], "main", True, [], t["enabled"],
                          skills, cfg, store_cache, hash_cache, out)
        for rel in tool_rels.get(t["key"]) or []:
            owner = (rel_tools.get(rel) or [t["key"]])[0]
            if owner != t["key"]:
                continue  # 共享目录：由 adapters 里先声明的 agent 收进看板
            sweep = rel.endswith("/**")
            root_rel = rel[:-3].rstrip("/") if sweep else rel
            root = _resolve_extra_root(root_rel)
            np = norm_path(root)
            if np in seen_extra:
                continue  # 同一路径不会出两行
            seen_extra.add(np)
            shared = [k for k in rel_tools[rel] if k != t["key"]]
            if sweep:
                _scan_unique_sweep(t, root, root_rel, shared, t["enabled"],
                                   skills, cfg, store_cache, hash_cache, out)
            else:
                _scan_unique_root(t, root, rel, False, shared, t["enabled"],
                                  skills, cfg, store_cache, hash_cache, out)
    # 同一 agent 下多个来源扫出同名技能时（如插件缓存与安装目录里的包源是
    # 同一份），保留先扫到的那个 —— extra_dirs 的声明顺序即优先级
    deduped, seen_names = [], set()
    for u in out:
        if not u:
            continue
        k = (u["tool"], u["name"])
        if k in seen_names:
            continue
        seen_names.add(k)
        deduped.append(u)
    return deduped


def find_unique_row(con, cfg, tool_key, name):
    """按 key（tool/name）现查独有行，拿真实路径与 deletable；查不到返回 None。

    desktop 的 open_dir / delete 都从这里取路径，不在两处各自拼。
    """
    trow = con.execute("select * from tools where key=?", (tool_key,)).fetchone()
    if not trow or not trow["detected"]:
        return None
    for u in unique_entries(con, cfg, [dict(trow)]):
        if u["tool"] == tool_key and u["name"] == name:
            return u
    return None


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


def conflict_detail(con, cfg, tool_key, name):
    """中央 vs 本地同名副本的文件对照；只读，不合并、不写盘。"""
    trow = con.execute("select * from tools where key=?", (tool_key,)).fetchone()
    if not trow:
        raise RuntimeError("未知工具: %s" % tool_key)
    central = os.path.join(cfg["store"], name)
    local = os.path.join(trow["skills_dir"], name)
    c_files = file_list(central)
    l_files = file_list(local)
    c_map = {f["name"]: f["size"] for f in c_files}
    l_map = {f["name"]: f["size"] for f in l_files}
    for f in c_files:
        f["flag"] = ("diff" if f["name"] in l_map and l_map[f["name"]] != f["size"]
                     else "central-only")
    for f in l_files:
        f["flag"] = ("diff" if f["name"] in c_map and c_map[f["name"]] != f["size"]
                     else "local-only")
    common = set(c_map) & set(l_map)
    diffs = sum(1 for n in common if c_map[n] != l_map[n])
    c_bytes = sum(c_map.values())
    l_bytes = sum(l_map.values())
    return {
        "skill": name, "tool": tool_key,
        "central": {"path": central.replace(os.sep, "\\"), "count": len(c_files),
                    "bytes": c_bytes, "files": c_files, "size": fmt_bytes(c_bytes)},
        "local": {"path": local.replace(os.sep, "\\"), "count": len(l_files),
                  "bytes": l_bytes, "files": l_files, "size": fmt_bytes(l_bytes)},
        "commonDiffs": diffs,
    }


def find_conflict_tool(con, cfg, name):
    """共有 skill 在哪个已启用工具里是实体冲突目录；没有则返回 None。"""
    skill_row = con.execute("select * from skills where name=?", (name,)).fetchone()
    if not skill_row:
        return None
    for t in ordered_tools(con):
        if not t["enabled"]:
            continue
        state, _detail = target_state(
            os.path.join(t["skills_dir"], name).replace(os.sep, "/"), cfg, skill_row, t)
        if state in ("conflict-dir", "conflict-file"):
            return t["key"]
    return None


def last_scan_time(con):
    row = con.execute("select ts from events where kind='scan' order by id desc limit 1").fetchone()
    if row and row["ts"]:
        return datetime.datetime.fromtimestamp(row["ts"] / 1000.0)
    try:
        return datetime.datetime.fromtimestamp(os.path.getmtime(DB_FILE))
    except OSError:
        return datetime.datetime.now()


def list_boards(con, cfg):
    """一次性给出 UI 需要的全部只读数据，字段与 ui/data.js 快照保持一致。"""
    tools = ordered_tools(con)
    enabled = [t for t in tools if t["enabled"]]
    skills = [dict(r) for r in con.execute("select * from skills order by name")]

    state_map = {}
    for t in enabled:
        for s in skills:
            if not s["enabled"]:
                state_map[(t["key"], s["name"])] = "disabled"
                continue
            state, _detail = target_state(
                os.path.join(t["skills_dir"], s["name"]).replace(os.sep, "/"), cfg, s, t)
            state_map[(t["key"], s["name"])] = state

    skill_out = []
    for s in skills:
        agents, real_in = [], []
        for t in enabled:
            state = state_map[(t["key"], s["name"])]
            if state in ("ok-link", "ok-copy", "takeover", "conflict-dir",
                         "conflict-file", "stale-copy"):
                agents.append(t["key"])
            if state in ("conflict-dir", "conflict-file", "takeover"):
                real_in.append(t["key"])
        skill_out.append({
            "name": s["name"], "label": s["label"] or s["name"],
            "description": s["description"] or "",
            "hash": (s["content_hash"] or "")[:12], "origin": s["origin"] or "scan",
            "agents": agents, "realIn": real_in,
        })

    hash_cache = {}
    # 独有检测覆盖本机所有装了的 agent（含分发开关关闭的），不只 enabled
    unique = unique_entries(con, cfg, tools, hash_cache)
    agent_rows = agent_entries(con, cfg, tools, skills, hash_cache)
    agent_counts = {k: sum(1 for a in agent_rows if a["status"] == k)
                    for k in ("connected", "off", "missing")}
    conflict = None
    for u in unique:
        if u["sameName"]:
            conflict = conflict_detail(con, cfg, u["tool"], u["name"])
            break

    scan_at = last_scan_time(con)
    generated = datetime.datetime.now()
    out_tools = []
    for t in tools:
        rel = ""
        if t["skills_dir"] and os.path.isdir(t["skills_dir"]):
            rel = os.path.relpath(t["skills_dir"], HOME).replace(os.sep, "/")
        out_tools.append({
            "key": t["key"], "label": t["label"], "mark": _tool_mark(t),
            "sub": rel, "dir": (t["skills_dir"] or "").replace("/", "\\"),
            "enabled": bool(t["enabled"]), "mode": t["mode"] or "junction",
            "detected": bool(t["detected"]),
        })

    return {
        "meta": {
            "generated": generated.strftime("%Y-%m-%d %H:%M"),
            "version": APP_VERSION,
            "python": "%d.%d.%d" % sys.version_info[:3],
            "store": cfg["store"].replace("/", "\\"),
            "storeSize": fmt_bytes(tree_stats(cfg["store"])),
            "db": DB_FILE.replace("/", "\\"),
            "scanAt": scan_at.strftime("%H:%M"),
            "eventsTotal": con.execute("select count(*) from events").fetchone()[0],
            "skillCount": len(skill_out),
            "agentCount": len(enabled),
            "agentTotal": len(agent_rows),
            "agentConnected": agent_counts["connected"],
            "agentOff": agent_counts["off"],
            "agentMissing": agent_counts["missing"],
            "disabledCount": len(tools) - len(enabled),
            "sharedCount": len(skill_out),
            "uniqueCount": len(unique),
        },
        "skills": skill_out,
        "tools": out_tools,
        "agents": agent_rows,
        "unique": unique,
        "events": [],
        "conflict": conflict,
    }


def main():
    p = argparse.ArgumentParser(prog="skillkit")
    p.add_argument("cmd", choices=["scan", "status", "verify", "sync", "import", "remove", "detect"])
    p.add_argument("arg", nargs="?", help="import 用目录 / remove 用 skill 名")
    p.add_argument("--tool", action="append")
    p.add_argument("--name")
    p.add_argument("--fix", action="store_true")
    a = p.parse_args()
    cfg = load_config()
    con = db()
    if a.cmd == "detect":
        rows = sync_tools_from_adapters(con, cfg)
        for k, label, sd, dd, det in rows:
            print("  %-18s %-22s %s" % (k, label, sd.replace(HOME, "~")))
        print("本机可用工具:", ", ".join(r[0] for r in rows))
    elif a.cmd == "scan":
        print("中央仓库 skill 数:", rescan(con, cfg))
    elif a.cmd == "status":
        st = matrix(con, cfg)
        counts = {}
        for c in st:
            counts[c["state"]] = counts.get(c["state"], 0) + 1
        print("格位 %d，状态分布:" % len(st), counts)
        for c in st:
            if c["state"] not in ("ok-link", "ok-copy", "disabled"):
                print("  %-16s %-34s %s" % (c["tool"], c["skill"], c["state"]))
    elif a.cmd == "verify":
        bad = [c for c in matrix(con, cfg)
               if c["state"] not in ("ok-link", "ok-copy", "disabled", "off")]
        print("漂移 %d 处" % len(bad))
        for c in bad[:80]:
            print("  %-16s %-34s %-13s %s" % (c["tool"], c["skill"], c["state"], c["detail"]))
    elif a.cmd == "sync":
        plan = do_sync(con, cfg, a.tool, apply_changes=a.fix)
        todo = [x for x in plan if x["action"]]
        print(("已执行 " if a.fix else "待执行 ") + "%d 处" % len(todo))
        for x in todo[:60]:
            print("  %-16s %-34s %-13s -> %s" % (x["tool"], x["skill"], x["state"], x["action"]))
        if not a.fix:
            print("（这是预览，加 --fix 才真正改动）")
    elif a.cmd == "import":
        if not a.arg:
            raise SystemExit("import 需要目录参数")
        print("导入为:", import_dir(con, cfg, a.arg, a.name))
    elif a.cmd == "remove":
        if not a.arg:
            raise SystemExit("remove 需要 skill 名")
        print("清掉链接:", remove_skill(con, cfg, a.arg))
    con.close()


if __name__ == "__main__":
    main()
