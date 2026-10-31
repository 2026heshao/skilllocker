#!/usr/bin/env python3
"""skillkit 分发层：分发矩阵与执行（matrix / do_sync）、导入与增删
（import_dir / unlink_* / remove_skill / remove_unique）、同名冲突处置
（resolve_conflict）与 CLI 辅助（toggle / online_search）。

依赖：skillkit_base（链接原语与状态机）、skillkit_store（import / 处置后
重扫中央仓库）、skillkit_meta（remove_unique 清独有 meta）。全部单向。
"""
import json
import os
import shutil

import skillkit_base as _base  # noqa: F401 - 统一从门面取常量时走这里
from skillkit_base import (  # noqa: F401 - 部分名字经 skillkit 门面再导出
    IGNORE_NAMES, delete_tree, dir_stats, has_skill_md, is_reparse, link_target,
    log_event, make_junction, norm_path, remove_link, sha_tree, target_state)
from skillkit_meta import clear_meta
from skillkit_store import find_unique_row, rescan


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
        os.replace(tmp, dst)
    except OSError:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


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
    # 与 remove_unique 同一标准：skill 名只能是中央仓库的直属目录名。
    # 否则 --name ../../x 会让 copytree 写到 store 之外，--name a/b 写出的
    # 嵌套目录永远不会被注册成 skill。
    if not name or name in (".", "..") or os.path.basename(name) != name:
        raise RuntimeError("非法 skill 名: %s（不能含路径分隔符）" % name)
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
