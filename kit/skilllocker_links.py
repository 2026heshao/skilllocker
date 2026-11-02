#!/usr/bin/env python3
"""skilllocker 分发层：分发矩阵与执行（matrix / do_sync）、导入 / 撤链 / 删除 /
独有转共有（promote_unique）、导入预检（import_preflight）、同名冲突处置
（resolve_conflict）与在线检索 / 下载（online_search / online_fetch，skills.sh
+ GitHub 下载到系统临时目录）。

依赖：skilllocker_base（链接原语与状态机）、skilllocker_store（import / 处置后
重扫中央仓库）、skilllocker_meta（remove_unique 清独有 meta / promote 迁移
显示名备注）。全部单向。
"""
import hashlib
import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import skilllocker_base as _base  # noqa: F401 - 统一从门面取常量时走这里
from skilllocker_base import (  # noqa: F401 - 部分名字经 skilllocker 门面再导出
    IGNORE_NAMES, db, delete_tree, dir_stats, has_skill_md, is_reparse, link_target,
    log_event, make_junction, norm_path, parse_frontmatter, remove_link, sha_tree,
    target_state)
from skilllocker_meta import clear_meta, set_meta
from skilllocker_store import find_unique_row, rescan


# ---------------------------------------------------------------- 分发状态
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
                copy_tree_atomic(central, path)
            log_event(con, "sync", "%s/%s %s" % (t["key"], s["name"], action or state))
            entry["done"] = action
    if apply_changes:
        con.commit()
    return plan


# ---------------------------------------------------------------- 增删
def check_skill_name(name):
    """skill 名 = 中央仓库 / skills_dir 的直属目录名，所有删除与导入入口共用。

    - 路径分隔符 / . .. ：copytree、delete_tree 会写到目录树之外
    - Windows 保留名（CON/NUL/COM1...）：copytree 失败或产出无法访问的目录
    - 尾点 / 尾空格：NTFS 静默吞掉，注册名与磁盘名不一致
    - 冒号：NTFS 备用数据流语法，不是目录名
    """
    if not name or name in (".", "..") or os.path.basename(name) != name:
        raise RuntimeError("非法 skill 名: %s（不能含路径分隔符）" % name)
    if re.match(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])$", name.lower()):
        raise RuntimeError("非法 skill 名: %s（Windows 保留名）" % name)
    if name != name.rstrip(". ") or ":" in name:
        raise RuntimeError("非法 skill 名: %s（不能以点/空格结尾或含冒号）" % name)
    return name


def copy_tree_atomic(src, dst):
    """copytree 的原子版：先拷到同盘同级的 .skpart-* 临时名，再 os.replace 翻正。

    直接 copytree 中途失败（断电 / 杀软拦截）会留下半成品实体目录，下一轮
    扫描把它判成 conflict-dir，用户看到的是莫名的冲突。调用方保证 dst 不存在
    （import / resolve 都先查过或删过）；os.replace 同卷内对不存在的目标是
    一次原子改名。临时目录名以 .skpart- 开头：store_skill_dirs 会跳过，
    rescan 启动时顺手清理崩溃残留。
    """
    dst = os.path.normpath(dst)
    tmp = os.path.join(os.path.dirname(dst),
                       ".skpart-%d-%s" % (os.getpid(), os.path.basename(dst)))
    if os.path.lexists(tmp):
        if is_reparse(tmp) or os.path.islink(tmp):
            remove_link(tmp)
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(src, tmp, ignore=shutil.ignore_patterns(*sorted(IGNORE_NAMES)))
    try:
        _replace_dir(tmp, dst)
    except OSError:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


# Windows 上目录树内有任何文件被打开着句柄，rename 就报 WinError 5（拒绝访问）。
# 刚落盘的文件会被杀软 / 索引服务短暂握住句柄 —— 在线下载后立即导入时几乎必现
# （网络新内容首次落盘要全量扫描，本地旧文件早有缓存判定）。瞬时句柄退避重试
# 即可通过；拿不到具体的进程句柄，重试是唯一可靠解。单测把间隔 patch 成 0 加速。
RENAME_BACKOFF = (0.15, 0.4, 0.8, 1.5, 3.0)


def _replace_dir(tmp, dst):
    last = None
    for i, delay in enumerate(RENAME_BACKOFF):
        try:
            os.replace(tmp, dst)
            return
        except PermissionError as exc:
            last = exc
            if i < len(RENAME_BACKOFF) - 1:
                time.sleep(delay)
    raise last


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
    # 与 remove_unique / resolve_conflict 同一标准：skill 名只能是中央仓库的
    # 直属目录名，否则 --name ../../x 会让 copytree 写到 store 之外
    check_skill_name(name)
    central = os.path.join(cfg["store"], name)
    if os.path.normcase(central) == os.path.normcase(src):
        raise RuntimeError("源与中央仓库同名同路径：%s" % central)
    if os.path.lexists(central):
        raise RuntimeError("中央仓库已有同名 skill：%s（先改名或删掉再导入）" % name)
    copy_tree_atomic(src, central)
    rescan(con, cfg)
    log_event(con, "import", "%s <- %s" % (name, src))
    con.commit()
    return name


def _preflight_tree(base, cap=6):
    """预检报告的顶层结构 chips：目录给 `名/ ×N`（N = 夹内递归文件数），文件给
    文件名；SKILL.md 恒列首。只做展示，超过 cap 截断（全量文件树归差异弹窗管）。"""
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return []
    dirs, files = [], []
    for n in entries:
        if n in IGNORE_NAMES:
            continue
        p = os.path.join(base, n)
        if os.path.isdir(p) and not is_reparse(p):
            dirs.append(n)
        elif os.path.isfile(p):
            files.append(n)
    out = []
    if "SKILL.md" in files:
        out.append("SKILL.md")
        files.remove("SKILL.md")
    for n in dirs + files:
        if len(out) >= cap:
            break
        if n in dirs:
            cnt = dir_stats(os.path.join(base, n))[0]
            out.append("%s/ ×%d" % (n, cnt) if cnt else "%s/" % n)
        else:
            out.append(n)
    return out


def import_preflight(cfg, src, name=None):
    """导入前预检（只读）：路径归一化 → junction 解析 → 名称/冲突判定 →
    SKILL.md 解析 → 规模统计。import_dir 的全部防线在这里以「报告」而非
    「异常」的形式预演，前端拿 dict 直接渲染（SPEC-skill-import.md §6.1）。

    致命问题（不是目录 / 链接解析不到 SKILL.md）以 ok=False + reason 返回，
    不抛异常；名字非法只记 name_error 不挡报告（导入按钮由前端 disabled，
    import_dir 运行时仍会再拦一次）。同名时才做一次 sha_tree 同内容比对
    （大目录的主要开销），其余统计只计数不哈希。
    """
    out = {"ok": False, "src": "", "resolved": None, "name": "", "name_error": "",
           "has_skill_md": False, "fm": None, "files": 0, "bytes": 0,
           "tree": [], "conflict": "none"}
    src = os.path.normpath(str(src or "")).replace(os.sep, "/")
    out["src"] = src
    # 链接判定放在 isdir 之前：死链（目标已消失）的 isdir 为假，但「链接解析
    # 不到 SKILL.md」比「不是目录」更接近用户能动手的原因。与 import_dir 同一
    # 防线：链接源必须解析到带 SKILL.md 的真实目录
    if src and is_reparse(src):
        tgt = link_target(src)
        real = os.path.normpath(tgt).replace(os.sep, "/") if tgt else None
        if not real or not os.path.isfile(os.path.join(real, "SKILL.md")):
            out["reason"] = "源是链接但解析不到 SKILL.md：%s -> %s" % (src, tgt)
            return out
        out["resolved"] = real
        src = real
    if not src or not os.path.isdir(src):
        out["reason"] = "不是目录：%s" % (src or "（空路径）")
        return out
    name = name or os.path.basename(src)
    out["name"] = name
    try:
        check_skill_name(name)
    except RuntimeError as exc:
        # 与 import_dir 同句（check_skill_name 单一出处），前端复用同一文案
        out["name_error"] = str(exc)
    md = os.path.join(src, "SKILL.md")
    if not os.path.isfile(md):
        # has_skill_md 同口径：中央仓库注册认 SKILL.md / skill.md 两种大小写
        alt = os.path.join(src, "skill.md")
        md = alt if os.path.isfile(alt) else None
    out["has_skill_md"] = bool(md)
    if md:
        fm = parse_frontmatter(md)
        if fm:
            out["fm"] = {"name": fm.get("name") or "",
                         "description": fm.get("description") or ""}
    out["files"], out["bytes"] = dir_stats(src)
    out["tree"] = _preflight_tree(src)
    if not out["name_error"]:
        central = os.path.join(cfg["store"], name)
        if os.path.lexists(central):
            con = db()
            try:
                row = con.execute(
                    "select content_hash from skills where name=? collate nocase",
                    (name,)).fetchone()
            finally:
                con.close()
            if row and row["content_hash"]:
                try:
                    same = sha_tree(src) == row["content_hash"]
                except (OSError, RuntimeError):
                    same = False  # 不可枚举：证明不了同内容，按同名冲突处理
                if same:
                    out["conflict"] = "same-content"
                    out["hash8"] = row["content_hash"][:8]
                    out["ok"] = True
                    return out
            out["conflict"] = "same-name"
    out["ok"] = True
    return out


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
        try:
            same = sha_tree(path) == row["content_hash"]
        except (OSError, RuntimeError):
            same = False  # 不可枚举（sha_tree 严格模式）：拿不到证据就不删
        if same:
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
            if row:
                try:
                    same = sha_tree(path) == row["content_hash"]
                except (OSError, RuntimeError):
                    same = False  # 不可枚举（sha_tree 严格模式）：拿不到证据就不删
                if same:
                    delete_tree(path)
                    removed_links.append(path.replace(os.sep, "/"))
    if purge:
        central = os.path.join(cfg["store"], name)
        if os.path.isdir(central):
            delete_tree(central)
    con.execute("delete from skills where name=?", (name,))
    # 残留的忽略标记会让重建的同名 skill 静默继承「冲突已忽略」；meta 行同理
    con.execute("delete from suppressed where skill=?", (name,))
    con.execute("delete from skill_meta where scope='shared' and key=?", (name,))
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
    check_skill_name(name)
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


def uninstall_unique(con, cfg, tool_key, name):
    """卸载独有 skill——不受 deletable 限制，覆盖 remove_unique 管不到的三类行：
      foreign-link        只摘链接（真身在别处，绝不顺着链接删实体）
      容器子目录 / extra   永久删除该目录（delete_tree 自带 reparse 防护）
      扫描后被换成链接的   只摘链接
    main 直属实体仍走 remove_unique（delete_skill 原路径，防线不动）。
    路径一律来自 find_unique_row 现查，拒绝中央仓库内的任何路径。
    """
    u = find_unique_row(con, cfg, tool_key, name)
    if not u:
        raise RuntimeError("独有 skill 不存在（或 agent 未检测到）：%s/%s" % (tool_key, name))
    path = os.path.normpath(u["path"])
    if not os.path.lexists(path):
        raise RuntimeError("路径已不存在：%s" % path)
    store_np = norm_path(cfg["store"])
    if norm_path(path) == store_np or norm_path(path).startswith(store_np + "/"):
        raise RuntimeError("拒绝卸载中央仓库内的路径：%s" % path)
    if u["kind"] == "foreign-link" or is_reparse(path) or os.path.islink(path):
        # 外部链接，或扫描后目录被替换成了链接：只摘链接本身
        if not (is_reparse(path) or os.path.islink(path)):
            raise RuntimeError("foreign-link 行的路径已不是链接：%s" % path)
        remove_link(path)
        files = size = 0
    else:
        if not has_skill_md(path):
            raise RuntimeError("目标不是 skill 目录（缺 SKILL.md）：%s" % path)
        files, size = dir_stats(path)
        delete_tree(path)
    clear_meta(con, "unique", tool_key + "/" + name)
    log_event(con, "uninstall-unique", "%s/%s（%s，%d 文件）" % (
        tool_key, name, u.get("src") or u["kind"], files))
    con.commit()
    return {"scope": "unique", "key": tool_key + "/" + name,
            "path": path.replace(os.sep, "\\"), "files": files, "bytes": size or 0,
            "links": 0, "central": False, "kind": u["kind"], "src": u.get("src") or ""}


def promote_unique(con, cfg, tool_key, name):
    """独有技能一键转共有：把真身目录拷进中央仓库，纳入统一分发。

    收编范围（右侧栏「转为共有技能」按钮的落点，前端只做守卫，这里把关）：
      - kind=unique 的实体目录（main / extra / 容器子目录 / 扫描变体）可收。
        store 目录名取真身目录名：容器行（tool/container/sub）与扫描变体行
        （name（段名））的 name 不是合法的仓库直属目录名，真身目录名一定合法。
      - 同名覆盖行（overridden）不收：中央已有同名副本，「谁覆盖谁」是冲突
        处置的决策（diff / resolve_conflict），一键覆盖太危险。
      - foreign-link 不收：真身在别处、不归本工具管理。

    源副本一律保留（非破坏）：拷贝完成后真身与中央内容一致，独有行自动从
    看板消失（_unique_item 按内容哈希判定）。main 直属行且该 agent 分发开关
    开着时，顺手 do_sync 把源副本对齐分发——junction 模式下 takeover →
    relink 成指向中央的链接，agent 无感续用；copy 模式内容一致 ok-copy 不动。
    容器 / 变体行不走这段：真身不在 skills_dir 直属位，补链接会让 agent 从
    两个路径加载到同名的两份。独有 meta 里写过的显示名 / 备注迁给共有行
    （归属夹本就不参与；独有 meta 行随后由 prune_meta 按行消失自动清理）。
    """
    u = find_unique_row(con, cfg, tool_key, name)
    if not u:
        raise RuntimeError("独有 skill 不存在（或 agent 未检测到）：%s/%s" % (tool_key, name))
    if u["kind"] == "foreign-link":
        raise RuntimeError("这是指向 agent 外部目录的链接，真身不归本工具管理，无法收编；"
                           "确要入库请在中央仓库手动导入 %s" % u["path"])
    if u["sameName"]:
        raise RuntimeError("中央仓库已有同名但内容不同的副本，一键收编会覆盖它；"
                           "请先在详情里「查看文件差异」处置冲突")
    path = os.path.normpath(u["path"])
    if not os.path.isdir(path):
        raise RuntimeError("路径已不存在：%s" % path)
    store_name = os.path.basename(path)
    if store_name in ("", ".", ".."):
        raise RuntimeError("无法从真身路径推导 skill 名：%s" % path)
    ukey = tool_key + "/" + u["name"]
    src_main = u.get("src") == "main" and u["name"] == store_name
    import_dir(con, cfg, path, store_name)
    files, size = dir_stats(os.path.join(cfg["store"], store_name))
    um = con.execute("select display_name, note from skill_meta "
                     "where scope='unique' and key=?", (ukey,)).fetchone()
    if um and (um["display_name"] or um["note"]):
        set_meta(con, "shared", store_name, {"display": um["display_name"] or "",
                                             "note": um["note"] or ""})
    relinked = 0
    if src_main:
        trow = con.execute("select * from tools where key=?", (tool_key,)).fetchone()
        if trow and trow["detected"] and trow["enabled"]:
            plan = do_sync(con, cfg, only_tools=[tool_key], only_skills=[store_name],
                           apply_changes=True)
            relinked = sum(1 for p in plan if p.get("done") == "relink")
    log_event(con, "promote", "%s/%s -> 共有 %s（%d 文件，%s）" % (
        tool_key, u["name"], store_name, files,
        "源副本已转链接" if relinked else "源副本保留"))
    con.commit()
    return {"scope": "unique", "key": ukey, "tool": tool_key, "name": store_name,
            "path": path.replace(os.sep, "\\"), "files": files, "bytes": size,
            "relinked": relinked}


def resolve_conflict(con, cfg, tool_key, name, direction):
    """同名不同版的人工处置。
    local-to-central: 把工具目录里那份收进中央仓库（覆盖中央，之后统一分发）
    central-to-local: 用中央仓库那份覆盖工具目录里那份（转成 junction）
    ignore: 保持现状并记入 suppressed，不再报警
    """
    trow = con.execute("select * from tools where key=?", (tool_key,)).fetchone()
    if not trow:
        raise RuntimeError("未知工具: %s" % tool_key)
    # 与 remove_unique / import_dir 同一标准：name 只能是 skills_dir / store 的
    # 直属目录名。否则 name=".." 时 central-to-local 会 delete_tree 到父目录。
    name = str(name or "")
    check_skill_name(name)
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
        copy_tree_atomic(local, central)
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


# ---------------------------------------------------------------- 在线检索与下载
# skills.sh 社区索引 + GitHub 下载，可靠性配方来自实践（github-sandbox-download 技能）：
#   清单：GitHub git tree 为准（权威；jsDelivr 的分支快照有幻影文件 / 陈旧 size，
#   只在 api.github.com 不可用时兜底）；分支解析成 commit sha 把下载钉到不可变快照；
#   文件：jsDelivr 镜像按实测健康度排序（gcore/testingcf 对冷文件直接 200；
#   cdn/fastly/quantil 可能 301 回 raw，raw 在部分网络下不可达），raw 兜底；
#   >300KB 走 HTTP Range 分块；文件级并发；每次请求独立重试，单次 fetch 有总预算。
#   传输完整性用响应自己的 Content-Length / Content-Range 校验（防半截传输，
#   清单 size 只作规模参考不硬对）；GitHub tree 的 blob sha 下载后再校验一次，
#   CDN 陈旧版本 / 镜像篡改会被识别并换源。路径越界 / Windows 保留名 / Git LFS 一律拒绝。
_DEFAULT_JSDELIVR_HOSTS = ("gcore.jsdelivr.net", "testingcf.jsdelivr.net",
                           "cdn.jsdelivr.net", "fastly.jsdelivr.net",
                           "quantil.jsdelivr.net")


def _read_net_config():
    """读 config.json 的可选 network 段；只读，不建目录、不写库，坏配置回退默认。
    在线检索 / 获取刻意不依赖 DB，所以这里直接读同目录 config.json。
    配置改了下次启动生效。"""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "config.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        net = data.get("network") if isinstance(data, dict) else None
        return net if isinstance(net, dict) else {}
    except (OSError, ValueError):
        return {}


_net_cfg = _read_net_config()


def _cfg_num(key, default, lo, hi, cast=int):
    try:
        return max(lo, min(hi, cast(_net_cfg.get(key, default))))
    except (TypeError, ValueError):
        return default


_mirrors = _net_cfg.get("mirrors")
if isinstance(_mirrors, list):
    _mirrors = [str(h).strip() for h in _mirrors
                if re.match(r"^[A-Za-z0-9.-]+$", str(h).strip())]
_JSDELIVR_HOSTS = tuple(_mirrors or _DEFAULT_JSDELIVR_HOSTS)

NET_RETRIES = _cfg_num("retries", 3, 1, 10)                # 单请求最多尝试次数
NET_BACKOFF = _cfg_num("backoff", 1.0, 0.0, 30.0, float)   # 退避基数（秒）
NET_BACKOFF_MAX = _cfg_num("backoff_max", 4.0, 0.0, 60.0, float)
NET_TIMEOUT = _cfg_num("timeout", 15, 3, 120, float)       # 单次请求超时（秒）
NET_DEADLINE = _cfg_num("deadline", 180, 30, 900, float)   # 单次 fetch 下载预算（秒）
NET_PARALLEL = _cfg_num("parallel", 4, 1, 12)              # 文件级并发数
NET_LIST_TTL = _cfg_num("list_ttl", 60, 0, 600, float)     # 清单缓存 TTL（秒）
NET_CHUNK = _cfg_num("chunk_kb", 1024, 8, 8192) * 1024     # Range 分块大小
NET_BIGFILE = _cfg_num("bigfile_kb", 300, 1, 65536) * 1024  # 超过该大小走 Range
NET_MAX_READ = 128 * 1024 * 1024     # 单文件读取上限
MAX_SKILL_FILES = 2000               # 单个 skill 文件数上限
MAX_SKILL_BYTES = 64 * 1024 * 1024   # 单个 skill 总大小上限
_UA = {"User-Agent": "skilllocker"}
_NET_NO_RETRY = (400, 401, 403, 404, 405, 410)  # 确定性失败：换源，不重试
_WIN_RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\.|$)", re.I)
_LFS_MAGIC = b"version https://git-lfs"
_BAD_REDIRECT_HOSTS = {"raw.githubusercontent.com"}


class _NetFatal(Exception):
    """确定性网络失败（404 / 源未缓存等）：重试无意义，调用方换下载源。"""


class _NoRawRedirect(urllib.request.HTTPRedirectHandler):
    """jsDelivr 对未缓存文件会 301 到 raw.githubusercontent.com。raw 在部分网络
    下不可达，跟随过去会把「换源」变成「等 NET_TIMEOUT 超时」。这里把落点为
    raw 的重定向直接判为确定性失败，调用方立刻换 gcore/testingcf。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        from urllib.parse import urlparse
        host = (urlparse(newurl).hostname or "").lower()
        if host in _BAD_REDIRECT_HOSTS:
            raise _NetFatal("源未缓存（重定向到 %s）" % host)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _make_opener():
    handlers = [_NoRawRedirect()]
    proxy = _net_cfg.get("proxy")
    proxy = proxy.strip() if isinstance(proxy, str) else ""
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(*handlers)


_OPENER = _make_opener()


class _FetchState:
    """单次 online_fetch 内的源健康记忆：成功源优先，连续失败 2 次本轮跳过。
    只作用于本次 fetch，不跨请求持久化（网络状况会变）。按 URL 主机名记账，
    不能按 "jsdelivr"/"raw" 标签记 —— 五个镜像必须各自独立。"""

    def __init__(self, deadline=None):
        self.deadline = deadline
        self.ok = {}
        self.fail = {}
        self.lock = threading.Lock()

    @staticmethod
    def _key(item):
        return urllib.parse.urlparse(item[1]).hostname or item[0]

    def order(self, urls):
        with self.lock:
            alive = [u for u in urls if self.fail.get(self._key(u), 0) < 2]
            pool = alive or list(urls)
            return sorted(pool, key=lambda u: (-self.ok.get(self._key(u), 0),
                                               self.fail.get(self._key(u), 0)))

    def mark(self, host, ok):
        with self.lock:
            if ok:
                self.ok[host] = self.ok.get(host, 0) + 1
                self.fail[host] = 0
            else:
                self.fail[host] = self.fail.get(host, 0) + 1


def _net_get(url, headers=None, rng=None, deadline=None, cancel=None):
    """单次 HTTP GET → (status, body, headers)。body 有 128MB 读取上限，
    防止清单被绕过后把超大文件整个读进内存；deadline 用于给单次请求收紧
    超时；cancel 在每次请求前检查，取消后最多等当前请求返回/超时。"""
    if cancel is not None and cancel.is_set():
        raise _NetFatal("已取消")
    hs = dict(_UA)
    if headers:
        hs.update(headers)
    if rng:
        hs["Range"] = rng
    timeout = float(NET_TIMEOUT)
    if deadline is not None:
        left = deadline - time.monotonic()
        if left <= 0:
            raise _NetFatal("下载总预算已耗尽")
        timeout = max(1.0, min(timeout, left))
    req = urllib.request.Request(url, headers=hs)
    with _OPENER.open(req, timeout=timeout) as f:
        data = f.read(NET_MAX_READ + 1)
        if len(data) > NET_MAX_READ:
            raise _NetFatal("单文件超过读取上限（%d MB）" % (NET_MAX_READ // 1048576))
        return f.status, data, f.headers


def _retry_after(exc, default):
    try:
        v = exc.headers.get("Retry-After") if getattr(exc, "headers", None) else None
        return max(0.0, min(15.0, float(v)))
    except (TypeError, ValueError):
        return default


def _net_retry(fn, what, deadline=None, cancel=None):
    """重试驱动：NET_RETRIES 次 / 指数退避（上限 NET_BACKOFF_MAX）。_NetFatal 与
    4xx 确定性错误立刻上抛（调用方换源）；网络类错误与不完整传输重试；deadline
    到期立即停止；cancel 在每次尝试前检查。"""
    import http.client
    import random
    last = None
    for attempt in range(NET_RETRIES):
        if cancel is not None and cancel.is_set():
            raise _NetFatal("已取消")
        if deadline is not None and time.monotonic() >= deadline:
            raise _NetFatal("%s：下载总预算耗尽" % what)
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, _NetFatal):
                raise
            if isinstance(exc, urllib.error.HTTPError):
                if exc.code in _NET_NO_RETRY:
                    raise _NetFatal("HTTP %d" % exc.code) from exc
                if exc.code == 429:
                    last = exc
                    if attempt < NET_RETRIES - 1:
                        wait = _retry_after(exc, NET_BACKOFF)
                        if deadline is not None:
                            left = deadline - time.monotonic()
                            if left <= 0:
                                raise _NetFatal("HTTP 429：下载总预算耗尽") from exc
                            wait = min(wait, max(0.0, left - 0.1))
                        if wait > 0:
                            time.sleep(wait)
                    continue
            if isinstance(exc, (OSError, http.client.HTTPException,
                                RuntimeError, ValueError)):
                last = exc
            else:
                raise
        if attempt < NET_RETRIES - 1:
            delay = min(NET_BACKOFF * (2 ** attempt), NET_BACKOFF_MAX)
            if delay and deadline is not None:
                delay = min(delay, max(0.0, deadline - time.monotonic() - 0.1))
            if delay > 0:
                time.sleep(delay + random.uniform(0, 0.15))
    raise RuntimeError("%s（尝试 %d 次）：%s" % (what, NET_RETRIES, last))


def _net_json(url, headers=None):
    """带重试的 JSON GET（在线检索 / 文件清单共用），支持 gzip 响应。"""
    def once():
        import gzip
        hs = {"Accept": "application/json", "Accept-Encoding": "gzip"}
        hs.update(headers or {})
        _st, body, rh = _net_get(url, headers=hs)
        if (rh.get("Content-Encoding") or "").lower() == "gzip":
            body = gzip.decompress(body)
        return json.loads(body.decode("utf-8"))

    return _net_retry(once, "JSON 请求失败")


def _net_bytes(url, expected_size=None, deadline=None, cancel=None):
    """下载一个文件到内存。清单 size 只作参考（jsDelivr 的清单快照与 CDN 缓存
    可能差几个提交，硬对会永远失败）：传输完整性用响应自己的 Content-Length /
    Content-Range 校验，半截响应与零字节冒充重试；大文件走 Range 分块，逐块
    重试，失败不再从 0 重来。cancel 在每次请求/重试前检查。"""
    if expected_size is not None and expected_size > NET_BIGFILE:
        return _net_bigfile(url, deadline=deadline, expected_size=expected_size,
                            cancel=cancel)

    def once():
        st, body, hs = _net_get(url, deadline=deadline, cancel=cancel)
        if st != 200:
            if 300 <= st < 400:
                raise _NetFatal("HTTP %s（源未就绪）" % st)
            raise RuntimeError("HTTP %s" % st)
        cl = hs.get("Content-Length")
        if cl and cl.isdigit() and int(cl) != len(body):
            raise RuntimeError("传输不完整：Content-Length %s，实得 %d" % (cl, len(body)))
        if not body and expected_size:
            raise RuntimeError("空响应（0 字节）")
        return body

    return _net_retry(once, "下载失败", deadline, cancel)


def _net_bigfile(url, deadline=None, expected_size=None, cancel=None):
    """大文件下载：首块直接取 0..NET_CHUNK-1（省掉 1 字节探测的 RTT），从
    Content-Range 拿总大小；后续按块拉取，每块独立重试，失败只重拉当前块。

    实测 jsDelivr 对个别文件会返回自相矛盾的 206：Content-Range 是陈旧快照
    （总长 84787），响应体却是当前整文件（346255，与 GitHub tree 的 size 一致）。
    这种响应不能按分块拼装，否则会截断成旧版本。若响应体长度恰好等于权威
    清单 size，按「服务器忽略 Range、整包返回」处理；否则拒绝并换源。"""
    chunk = max(1, int(NET_CHUNK))

    def probe():
        st, first, hs = _net_get(url, rng="bytes=0-%d" % (chunk - 1),
                                 deadline=deadline, cancel=cancel)
        if st == 200:
            # 服务器不支持 Range：这一发已经是整包
            cl = hs.get("Content-Length")
            if cl and cl.isdigit() and int(cl) != len(first):
                raise RuntimeError("传输不完整：Content-Length %s，实得 %d"
                                   % (cl, len(first)))
            return 200, first, None
        if st != 206:
            if 300 <= st < 400:
                raise _NetFatal("HTTP %s（源未就绪）" % st)
            raise RuntimeError("Range 请求返回 HTTP %s" % st)
        cr = (hs.get("Content-Range") or "").rsplit("/", 1)[-1].strip()
        if not cr.isdigit():
            raise RuntimeError("Content-Range 异常：%r" % (hs.get("Content-Range"),))
        total = int(cr)
        if total > NET_MAX_READ:
            raise _NetFatal("单文件超过读取上限（%d MB）" % (NET_MAX_READ // 1048576))
        want = min(chunk, total)
        if len(first) != want:
            # CDN 头陈旧但整包与 tree size 一致：按整包成功处理
            if expected_size and len(first) == expected_size:
                return 200, first, None
            raise RuntimeError("首块不完整：要 %d 字节，实得 %d" % (want, len(first)))
        return 206, first, total

    st, first, total = _net_retry(probe, "Range 首块失败", deadline, cancel)
    if st == 200:
        return first
    parts = [first]
    start = len(first)
    while start < total:
        end = min(start + chunk, total) - 1
        size = end - start + 1

        def get_chunk(s=start, e=end, n=size):
            st2, body, _hs = _net_get(url, rng="bytes=%d-%d" % (s, e),
                                      deadline=deadline, cancel=cancel)
            if st2 != 206:
                if 300 <= st2 < 400:
                    raise _NetFatal("HTTP %s（源未就绪）" % st2)
                raise RuntimeError("Range 请求返回 HTTP %s" % st2)
            if len(body) != n:
                raise RuntimeError("分块不完整：要 %d 字节，实得 %d" % (n, len(body)))
            return body

        parts.append(_net_retry(get_chunk, "分块下载失败", deadline, cancel))
        start = end + 1
    return b"".join(parts)


def _parse_source(source, branch=None):
    """归一化 GitHub 源，返回 (owner/repo, branch)。接受 owner/repo、
    owner/repo@branch、https://github.com/owner/repo[/tree/branch][.git]。
    owner/repo 只认 GitHub 的用户名 / 仓库名字符集，防止清单 URL 被注入。"""
    s = str(source or "").strip()
    if not s:
        raise RuntimeError("在线获取需要 GitHub 源（owner/repo）")
    if "://" in s:
        from urllib.parse import urlparse
        u = urlparse(s)
        if u.netloc.lower() not in ("github.com", "www.github.com"):
            raise RuntimeError("只支持 github.com 源：%s" % source)
        s = u.path.strip("/")
    if s.endswith(".git"):
        s = s[:-4]
    b = str(branch or "").strip()
    if not b and "@" in s:
        s, b = s.rsplit("@", 1)
    parts = [p for p in s.split("/") if p]
    if len(parts) > 2 and parts[2] == "tree":
        b = b or "/".join(parts[3:])
        parts = parts[:2]
    if len(parts) != 2:
        raise RuntimeError("GitHub 源格式应为 owner/repo：%s" % (source,))
    owner, repo = parts
    if not re.match(r"^[A-Za-z0-9-]+$", owner) or \
            not re.match(r"^[A-Za-z0-9._-]+$", repo) or repo in (".", ".."):
        raise RuntimeError("owner/repo 含非法字符：%s" % (source,))
    return owner + "/" + repo, b


def _jsdelivr_flat(source, branch):
    """jsDelivr flat 文件清单（api.github.com 不可用时的兜底）。不可用返回 None。
    注意：jsDelivr 对分支名的快照有漂移，实测能列出 HEAD 上已不存在的幻影文件、
    size 陈旧 —— 只在 GitHub tree 拿不到时才用它。"""
    from urllib.parse import quote
    url = "https://data.jsdelivr.com/v1/package/gh/%s@%s/flat" % (
        source, quote(str(branch), safe=""))
    try:
        data = _net_json(url)
    except (_NetFatal, RuntimeError):
        return None
    files = []
    for e in data.get("files") or []:
        if not isinstance(e, dict):
            continue
        p = str(e.get("name") or "").lstrip("/")
        if not p or "\\" in p:
            continue
        try:
            files.append((p, int(e.get("size") or 0), ""))  # jsDelivr 无 blob sha
        except (TypeError, ValueError):
            continue
    return files or None


def _github_headers(extra=None):
    """GitHub API 请求头。匿名限流 60/h；设置 SKILLLOCKER_GITHUB_TOKEN 或
    GITHUB_TOKEN 环境变量可提到 5000/h。Token 只进请求头，不写日志。"""
    hs = {"Accept": "application/vnd.github+json"}
    tok = (os.environ.get("SKILLLOCKER_GITHUB_TOKEN")
           or os.environ.get("GITHUB_TOKEN") or "").strip()
    if tok:
        hs["Authorization"] = "Bearer " + tok
    if extra:
        hs.update(extra)
    return hs


def _github_tree(source, branch):
    """GitHub git tree 清单（权威来源）。返回 [(path, size, blob_sha)]；不可用
    返回 None；树被截断则拒绝下载。blob_sha 用于下载后内容完整性校验。"""
    from urllib.parse import quote
    url = ("https://api.github.com/repos/%s/git/trees/%s?recursive=1"
           % (source, quote(str(branch), safe="")))
    try:
        data = _net_json(url, headers=_github_headers())
    except (_NetFatal, RuntimeError):
        return None
    if data.get("truncated"):
        raise RuntimeError("仓库 %s 的文件树过大，GitHub 截断了清单，无法安全下载" % source)
    files = []
    for e in data.get("tree") or []:
        if not isinstance(e, dict) or e.get("type") != "blob":
            continue
        p = str(e.get("path") or "")
        if not p or "\\" in p:
            continue
        files.append((p, int(e.get("size") or 0), str(e.get("sha") or "")))
    return files or None


def _resolve_commit(source, branch):
    """branch → commit sha：下载 URL 钉定到具体提交，避开 CDN 对分支名的快照
    漂移。失败返回 None（下载回退用分支名）。"""
    from urllib.parse import quote
    try:
        data = _net_json("https://api.github.com/repos/%s/git/refs/heads/%s"
                         % (source, quote(str(branch), safe="")),
                         headers=_github_headers())
        sha = str((data.get("object") or {}).get("sha") or "")
        return sha if re.match(r"^[0-9a-f]{40}$", sha) else None
    except (_NetFatal, RuntimeError):
        return None


_LIST_CACHE = {}


def _list_repo_files(source, branch=None):
    """定分支 + 拿全仓库文件清单 → (branch, [(path, size)], provider, sha)。
    清单一律先走 GitHub git tree（权威）；api.github.com 不可用（DNS 间歇抽风 /
    限流）才退 jsDelivr flat。GitHub 清单才解析 commit sha 钉定不可变快照；
    jsDelivr 兜底说明 GitHub API 多半不可用，再花一次重试解析没有意义。
    成功结果按 NET_LIST_TTL 缓存，重复「获取」同一仓库不再重拉。"""
    key = (source, branch or "")
    now = time.monotonic()
    if NET_LIST_TTL > 0:
        hit = _LIST_CACHE.get(key)
        if hit and now - hit[0] < NET_LIST_TTL:
            _ts, got = hit
            return got[0], list(got[1]), got[2], got[3]
    providers = ((_github_tree, "github"), (_jsdelivr_flat, "jsdelivr"))
    tried = [branch] if branch else ["main", "master"]
    for b in tried:
        for fn, provider in providers:
            files = fn(source, b)
            if files:
                sha = _resolve_commit(source, b) if provider == "github" else None
                got = (b, files, provider, sha)
                if NET_LIST_TTL > 0:
                    _LIST_CACHE[key] = (now, got)
                return got
    if not branch:
        try:
            info = _net_json("https://api.github.com/repos/" + source,
                             headers=_github_headers())
            db = str(info.get("default_branch") or "").strip()
            if db and db not in tried:
                for fn, provider in providers:
                    files = fn(source, db)
                    if files:
                        sha = _resolve_commit(source, db) if provider == "github" else None
                        got = (db, files, provider, sha)
                        if NET_LIST_TTL > 0:
                            _LIST_CACHE[key] = (now, got)
                        return got
        except (RuntimeError, _NetFatal):
            pass
    raise RuntimeError("拿不到 %s 的文件清单（分支 %s 不存在，或网络不通）"
                       % (source, branch or "main / master"))


def _find_skill_dir(files, name, source):
    """在仓库清单里定位 skill 目录：入口只认 SKILL.md / skill.md。
    目录名（或仓库根 = repo 名）与目标名相同的候选优先，同分取路径最短。"""
    name_cf = str(name or "").strip().casefold()
    repo_base = source.rsplit("/", 1)[-1].casefold()
    cands = []
    for entry in files:
        path = entry[0]
        segs = path.split("/")
        if segs[-1].lower() != "skill.md":
            continue
        d = "/".join(segs[:-1])
        score = 0
        if d:
            if d.rsplit("/", 1)[-1].casefold() == name_cf:
                score = 3
            elif name_cf and name_cf in (s.casefold() for s in d.split("/")):
                score = 2
        elif name_cf == repo_base:
            score = 3
        elif not name_cf:
            score = 2
        if score:
            cands.append((score, len(d), d))
    if not cands:
        found = sorted({("/".join(entry[0].split("/")[:-1]) or "（仓库根）")
                        for entry in files if entry[0].split("/")[-1].lower() == "skill.md"})
        hint = ("，仓库里的 skill 有：%s" % "、".join(found[:8])) if found \
            else "，仓库里没有任何 SKILL.md"
        raise RuntimeError("在 %s 里找不到名为 %s 的 skill%s" % (source, name or "（空）", hint))
    cands.sort(key=lambda t: (-t[0], t[1]))
    return cands[0][2]


def _files_under(files, skill_dir):
    """取 skill 目录下的文件 [(仓库路径, 相对路径, size, blob_sha)]，并套总量防线。
    清单项兼容 2 元组（测试 / 旧格式）与 3 元组（GitHub tree 带 blob sha）。"""
    prefix = skill_dir + "/" if skill_dir else ""
    out = []
    for entry in files:
        p = entry[0]
        if not p.startswith(prefix):
            continue
        blob = str(entry[2]) if len(entry) > 2 else ""
        out.append((p, p[len(prefix):], int(entry[1] or 0), blob))
    if not out:
        raise RuntimeError("skill 目录是空的：%s" % (skill_dir or "（仓库根）"))
    if len(out) > MAX_SKILL_FILES:
        raise RuntimeError("skill 文件数超上限（%d > %d），拒绝下载" % (len(out), MAX_SKILL_FILES))
    total = sum(s for _p, _r, s, _b in out)
    if total > MAX_SKILL_BYTES:
        raise RuntimeError("skill 总大小超上限（%.1f MB > %d MB），拒绝下载"
                           % (total / 1048576.0, MAX_SKILL_BYTES // 1048576))
    return out


def _safe_dest(base, rel):
    """仓库相对路径 → 临时目录下的安全绝对路径。清单来自网络不可信：
    绝对路径 / 盘符 / .. / Windows 保留名（con、aux、com1…含带扩展名的）一律拒绝。"""
    rel = str(rel).replace("\\", "/")
    if re.match(r"^[A-Za-z]:", rel) or rel.startswith("/"):
        raise RuntimeError("仓库清单里出现非法路径：%s" % rel)
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        raise RuntimeError("仓库清单里出现越界路径：%s" % rel)
    for p in parts:
        if _WIN_RESERVED.match(p):
            raise RuntimeError("仓库里有 Windows 无法创建的文件名：%s" % rel)
    dest = base
    for p in parts:
        dest = os.path.join(dest, p)
    base_abs = os.path.normcase(os.path.abspath(base))
    if not os.path.normcase(os.path.abspath(dest)).startswith(base_abs + os.sep):
        raise RuntimeError("路径逃逸出临时目录：%s" % rel)
    return dest


def _dl_root():
    import tempfile
    return os.path.join(tempfile.gettempdir(), "skilllocker-dl")


def _fresh_dl_dir(name):
    """新建下载临时目录（%TEMP%/skilllocker-dl/<安全名>-XXXX），顺手清掉遗留超过
    2 小时的旧目录（导入完成或放弃后都不再需要它们；best-effort 不抛错）。"""
    import tempfile
    import time
    root = _dl_root()
    os.makedirs(root, exist_ok=True)
    now = time.time()
    for n in os.listdir(root):
        p = os.path.join(root, n)
        try:
            if now - os.path.getmtime(p) > 2 * 3600 and not is_reparse(p):
                delete_tree(p)
        except Exception:  # noqa: BLE001 - 清理失败不影响本次下载
            continue
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(name or "skill")).strip("._-")[:60] or "skill"
    return tempfile.mkdtemp(prefix=safe + "-", dir=root)


def _blob_urls(source, branch, path, sha=None):
    """同一文件的下载地址候选，按序尝试。有 commit sha 时钉定 @sha（不可变快照，
    避开 jsDelivr 对分支名的缓存漂移），否则用分支名。镜像顺序按实测健康度：
    gcore/testingcf 对冷文件直接 200；cdn/fastly/quantil 可能 301 回 raw（由
    _NoRawRedirect 快速判失败）；raw 兜底 —— jsDelivr 未收录时它是唯一正源。"""
    from urllib.parse import quote
    q = quote(str(path))
    b = quote(str(sha or branch), safe="")
    urls = [("jsdelivr", "https://%s/gh/%s@%s/%s" % (h, source, b, q))
            for h in _JSDELIVR_HOSTS]
    urls.append(("raw", "https://raw.githubusercontent.com/%s/%s/%s" % (source, b, q)))
    return urls


def _git_blob_sha(body):
    """Git blob 对象的 SHA-1：sha1("blob <len>\\0" + body)。GitHub tree 的
    blob sha 是内容指纹，下载后校验可发现 CDN 陈旧版本 / 半截内容 / 镜像篡改。"""
    h = hashlib.sha1()
    h.update(b"blob %d\0" % len(body))
    h.update(body)
    return h.hexdigest()


def _download_blob(source, branch, path, size, sha=None, state=None, cancel=None,
                   blob_sha=None):
    """单个文件：按候选源顺序下载，任一源成功即返回。Git LFS 指针文件拒绝：
    下载到的是指针文本而非真内容，静默落库会得到坏 skill。
    state 非空时记录源健康度：成功源优先，连续失败 2 次的源本轮跳过。
    cancel 置位时直接上抛，不把「已取消」当成某个源失败继续换源。
    blob_sha 非空时校验 Git blob 内容指纹，不匹配视为该源失败并换源。"""
    errs = []
    urls = _blob_urls(source, branch, path, sha)
    if state is not None:
        urls = state.order(urls)
    for name, url in urls:
        host = state._key((name, url)) if state is not None else name
        try:
            body = _net_bytes(url, expected_size=size,
                              deadline=state.deadline if state is not None else None,
                              cancel=cancel)
        except (_NetFatal, RuntimeError) as exc:
            if cancel is not None and cancel.is_set():
                raise
            errs.append("%s: %s" % (name, exc))
            if state is not None:
                state.mark(host, False)
            continue
        if body[:len(_LFS_MAGIC)] == _LFS_MAGIC:
            raise RuntimeError("%s 是 Git LFS 指针文件，暂不支持下载该 skill" % path)
        if blob_sha and _git_blob_sha(body) != blob_sha:
            errs.append("%s: 内容校验失败（blob sha 不匹配）" % name)
            if state is not None:
                state.mark(host, False)
            continue
        if state is not None:
            state.mark(host, True)
        return body
    raise RuntimeError("下载 %s 失败（全部候选源都失败）：%s"
                       % (path, "；".join(errs[:3])))


def online_fetch(source, name, branch=None, progress=None, cancel=None):
    """在线获取：把 skills.sh 检索到的 skill（owner/repo + 名字）从 GitHub 下载到
    系统临时目录（%TEMP%/skilllocker-dl），不碰中央仓库 —— 调用方对返回的 path 走
    标准 import_preflight / import_dir（两段式：下载失败可重试，不写仓库）。

    文件级并发下载（NET_PARALLEL，大文件自动降并发控制内存峰值）；单次 fetch
    有 NET_DEADLINE 总预算；progress(done, total, bytes_done, path) 每完成一个
    文件回调一次；cancel 为 threading.Event，置位后每个请求前检查并尽快退出。
    返回 {name, source, branch, commit, path, files, bytes, listing, timings}；
    失败抛 RuntimeError，半途失败会清掉残缺的临时目录，仓库永远保持原样。
    """
    t_start = time.time()

    def tick(done, total, nbytes, path):
        if progress is None:
            return
        try:
            progress(done, total, nbytes, path)
        except Exception:  # noqa: BLE001 - 进度回调失败不能影响下载
            pass

    def stopped():
        return cancel is not None and cancel.is_set()

    if stopped():
        raise _NetFatal("已取消")
    source, branch = _parse_source(source, branch)
    name = str(name or "").strip()
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise RuntimeError("skill 名不合法：%r" % (name,))
    t_list = time.time()
    got_branch, files, provider, sha = _list_repo_files(source, branch or None)
    listing_ms = int((time.time() - t_list) * 1000)
    if stopped():
        raise _NetFatal("已取消")
    skill_dir = _find_skill_dir(files, name, source)
    picked = _files_under(files, skill_dir)
    dest = _fresh_dl_dir(name)
    state = _FetchState(deadline=time.monotonic() + NET_DEADLINE)
    workers = 1
    total_files = len(picked)
    counter_lock = threading.Lock()
    done = [0, 0]  # [文件数, 实际字节数]；并发任务用 lock 更新

    def one(item):
        repo_path, rel, size, blob_sha = item
        target = _safe_dest(dest, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        body = _download_blob(source, got_branch, repo_path, size, sha,
                              state=state, cancel=cancel, blob_sha=blob_sha)
        with open(target, "wb") as fh:
            fh.write(body)
        with counter_lock:
            done[0] += 1
            done[1] += len(body)
            snap = (done[0], done[1])
        tick(snap[0], total_files, snap[1], repo_path)

    tick(0, total_files, 0, "")
    t_dl = time.time()
    try:
        if len(picked) > 1:
            from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
            total = sum(s for _p, _r, s, _b in picked)
            workers = min(NET_PARALLEL, len(picked))
            if total > 24 * 1024 * 1024:
                # 下载结果在内存里落盘，大文件并发会把峰值放大
                workers = min(workers, 2)
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = [ex.submit(one, item) for item in picked]
                first_err = None
                for fut in as_completed(futs):
                    try:
                        exc = fut.exception()
                    except CancelledError:
                        # 取消时我们会对未开始的 future 调 cancel()，
                        # as_completed 仍可能把它交出来；不是下载错误
                        continue
                    if exc is not None and first_err is None:
                        first_err = exc
                        for f in futs:
                            f.cancel()
                if first_err is not None:
                    raise first_err
        else:
            one(picked[0])
    except Exception:
        try:
            delete_tree(dest)
        except Exception:  # noqa: BLE001 - 残留目录下次 fetch 顺手清理
            pass
        raise
    if stopped():
        try:
            delete_tree(dest)
        except Exception:  # noqa: BLE001
            pass
        raise _NetFatal("已取消")
    if not has_skill_md(dest):
        try:
            delete_tree(dest)
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError("下载完成但临时目录里没有 SKILL.md（清单口径异常）")
    download_ms = int((time.time() - t_dl) * 1000)
    n_files, n_bytes = dir_stats(dest)
    suggested = skill_dir.rsplit("/", 1)[-1] if skill_dir else name
    return {"name": suggested, "source": source, "branch": got_branch,
            "commit": (sha or "")[:10], "path": dest.replace(os.sep, "/"),
            "files": n_files, "bytes": n_bytes, "listing": provider,
            "timings": {"listing_ms": listing_ms, "download_ms": download_ms,
                        "total_ms": int((time.time() - t_start) * 1000),
                        "parallel": workers}}


def online_search(query, limit=20):
    """skills.sh 社区索引检索。返回 [{name, source, id, installs, page_url,
    source_url?, install?}]；installs 是原始数字，格式化交给前端。
    网络请求走 _net_retry（NET_RETRIES 次 / NET_BACKOFF 秒退避）。"""
    from urllib.parse import quote
    try:
        limit = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        limit = 20
    q = str(query or "").strip()
    if not q:
        return []
    url = "https://skills.sh/api/search?q=%s&limit=%d" % (quote(q), limit)
    data = _net_json(url)
    out = []
    for s in data.get("skills") or []:
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
