#!/usr/bin/env python3
"""skillkit 扫描层：工具表同步（adapters.json）、中央仓库扫描（rescan）、
独有技能扫描（unique_entries，覆盖 main / extra / `根/**` / foreign-link 四类来源）
与智能体看板扫描（agent_entries）。

adapters.json 路径等「单测会打补丁」的模块级常量，一律经 skillkit_base 属性
在调用时现读（_base.ADAPTERS_FILE），不做 import 时的值绑定。
"""
import json
import os
import re
import shutil

import skillkit_base as _base
from skillkit_base import (  # noqa: F401 - 部分名字经 skillkit 门面再导出
    HOME, IGNORE_NAMES, _hash_tree, _tool_mark, dir_stats, has_skill_md,
    is_reparse, link_target, log_event, norm_path, now_ms, ordered_tools,
    parse_frontmatter, sha_tree, target_state)

AGENT_STATUS_LABEL = {"connected": "已连接", "off": "已关闭", "missing": "未安装"}


# ---------------------------------------------------------------- 工具目录
def sync_tools_from_adapters(con, cfg):
    try:
        with open(_base.ADAPTERS_FILE, encoding="utf-8") as fh:
            adapters = json.load(fh)
    except (OSError, ValueError) as exc:
        # adapters.json 损坏不该让 rescan 全盘失败：沿用库内工具表继续跑
        rows = [(r["key"], r["label"], r["skills_dir"], r["detect_dir"], 1)
                for r in con.execute(
                    "select key,label,skills_dir,detect_dir from tools where detected=1")]
        if not rows:
            # 全新机器 + adapters.json 不可读 = 无从探测，静默返回空表会让用户
            # 困在「永远 0 agent」里；宁可让本轮失败并如实报错（→ 启动错误页）
            raise RuntimeError("adapters.json 缺失或损坏，且工具表为空，无法探测任何 agent: %s" % exc)
        log_event(con, "warn", "adapters.json 解析失败，沿用库内工具表: %s" % exc)
        con.commit()
        return rows
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
            # 首次入库一律默认关闭分发（enabled=0）：探测到 ≠ 用户授权分发，
            # 链接由用户打开开关时经 set_agent → do_sync 显式建立。
            # 否则首屏全是「已连接」却 0 链接，且开关已 on 无法补链（no-op 死区）。
            # 老机器升级不受影响：update 分支不碰 enabled。
            con.execute("insert into tools(key,label,skills_dir,detect_dir,detected,enabled,mode) "
                        "values(?,?,?,?,?,?, 'junction')",
                        (key, label, sd, dd, detected, 0))
    con.commit()
    return [r for r in rows if r[4]]


def store_skill_dirs(cfg):
    """中央仓库里的 skill 目录清单。

    根目录不可枚举（U 盘未挂载 / 网络断连 / 杀软锁目录）时**抛错**而不是当成
    空仓库返回：rescan 拿到空清单会把所有 skill 当「已消失」从库里删掉，
    prune_meta 再把用户 meta 一起清掉 —— 一次刷新静默不可逆（见 rescan）。
    """
    root = cfg["store"]
    if not os.path.isdir(root):
        raise RuntimeError("中央仓库目录不存在：%s" % root)
    try:
        names = sorted(os.listdir(root))
    except OSError as exc:
        raise RuntimeError("中央仓库目录不可读，已放弃本轮扫描: %s" % exc)
    out = []
    for n in names:
        if n.startswith(".") or ".skpart-" in n:
            # 隐藏目录；以及 copy_tree_atomic 崩溃残留的 .skpart-* 半成品
            continue
        p = os.path.join(root, n)
        if not os.path.isdir(p) or is_reparse(p) or n in IGNORE_NAMES:
            continue
        if has_skill_md(p):
            out.append((n, p))
    return out


def rescan(con, cfg):
    # 先清掉上次原子拷贝可能留下的 .skpart-* 临时目录（正常流程已被翻正/删除）
    try:
        for n in os.listdir(cfg["store"]):
            if n.startswith(".skpart-"):
                shutil.rmtree(os.path.join(cfg["store"], n), ignore_errors=True)
    except OSError:
        pass
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


# ---------------------------------------------------------------- 独有技能扫描
def extra_dirs_map(con):
    """adapters.json 的 extra_dirs 汇总，只留本机装了的 agent。返回两个表：

    tool_rels = {agent key: [相对目录]} —— unique_entries 按它扫各 agent 的外部目录；
    rel_tools = {相对目录: [agent key]} —— 用来算「还有哪些 agent 共读同一目录」
    （sharedWith）。`~/.agents/skills` 这类跨 agent 共享目录会出现在多个声明里，
    独有看板按路径去重归属，行归第一个读取它的 agent。
    """
    try:
        with open(_base.ADAPTERS_FILE, encoding="utf-8") as fh:
            adapters = json.load(fh)
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
                      skills, cfg, store_cache, hash_cache, out, errors):
    """扫一个技能目录（agent 主目录或 extra 外部目录），独有条目追加进 out。

    - 指向中央仓库的链接是我们分发的 junction，不算独有；指向别处的链接
      是 agent 能加载的真身，以 foreign-link 展示（只读：实体不归本工具管）。
    - 目录必须含 SKILL.md 才算 skill；顶层没有但子目录有的容器（如 codex
      的 .system）往下钻一层，skill 名记作「容器/子目录」，同样只读。
    - 根目录临时不可枚举时记入 errors 并跳过：调用方据此让 prune_meta
      跳过本轮，避免把「暂时看不见」当「已删除」清掉用户 meta。
    """
    if not root:
        return
    if not os.path.isdir(root):
        # 根整个看不见（可移动盘拔出 / 盘符暂缺 / 目录被临时移走）与「不可枚举」
        # 同等对待：记进 errors 让 prune_meta 跳过本轮，别把看不见的行当已删除清 meta
        errors.append("%s: 目录不存在（跳过本轮 meta 修剪）" % root)
        return
    try:
        names = sorted(os.listdir(root))
    except OSError as exc:
        errors.append("%s: %s" % (root, exc))
        return
    for n in names:
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
        try:
            subs = sorted(os.listdir(p))
        except OSError as exc:
            errors.append("%s: %s" % (p, exc))
            continue
        for sub in subs:
            sp = os.path.join(p, sub)
            if sub in IGNORE_NAMES or not os.path.isdir(sp):
                continue
            if is_reparse(sp) or os.path.islink(sp) or not has_skill_md(sp):
                continue
            out.append(_unique_item(t, n + "/" + sub, sp, src, False, shared_with,
                                    agent_enabled, "unique", False, skills, cfg,
                                    store_cache, hash_cache))


def _scan_unique_sweep(t, root, src, shared_with, agent_enabled,
                       skills, cfg, store_cache, hash_cache, out, errors):
    """extra_dirs 的 `根/**` 写法：递归扫插件缓存 / 应用内置这类目录树。

    只认「父目录叫 skills 且自己含 SKILL.md」的目录，技能名取 SKILL.md 的
    name 字段（fallback 目录名）。同名条目按路径分两种情况：
      - 首个不同的路径段是版本号（<插件>/<版本>/skills/<技能>，缓存里新旧
        版本并存而 agent 只加载最新装的）→ 只留 mtime 最新的那份；
      - 是普通目录名（如 Qoder 的 computerUseWindows/MacOS/Linux 三个
        OS 变体）→ 都保留，名字加「（段名）」区分。
    链接不跟进，防止 node_modules 式依赖链接把扫描带飞。
    根目录不可枚举时记入 errors（prune_meta 据此跳过本轮）。
    """
    cands = {}  # name -> [(mtime, rel_under_root, path)]

    def _walk_err(exc):
        errors.append("%s: %s" % (getattr(exc, "filename", None) or root, exc))

    for dirpath, dirnames, filenames in os.walk(root, onerror=_walk_err):
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
        try:
            root_names = sorted(os.listdir(root))
        except OSError:
            # 外部目录临时不可读：跳过这个来源，别让整张智能体看板失败
            continue
        for n in root_names:
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


def unique_entries(con, cfg, tools=None, hash_cache=None, errors=None):
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

    errors：传入列表时收集「根目录临时不可枚举」的路径，list_boards 据此
    给快照打 scanDegraded 标记，prune_meta 跳过本轮（防误清 meta）。
    """
    if tools is None:
        tools = ordered_tools(con)
    skills = {r["name"]: dict(r) for r in con.execute("select * from skills")}
    store_cache = {}
    hash_cache = {} if hash_cache is None else hash_cache
    if errors is None:
        errors = []
    tool_rels, rel_tools = extra_dirs_map(con)
    seen_extra = set()
    out = []
    for t in tools:
        _scan_unique_root(t, t["skills_dir"], "main", True, [], t["enabled"],
                          skills, cfg, store_cache, hash_cache, out, errors)
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
                                   skills, cfg, store_cache, hash_cache, out, errors)
            else:
                _scan_unique_root(t, root, rel, False, shared, t["enabled"],
                                  skills, cfg, store_cache, hash_cache, out, errors)
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


# ---------------------------------------------------------------- agent 看板
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
        with open(_base.ADAPTERS_FILE, encoding="utf-8") as fh:
            adapters = json.load(fh)
        extra_map = {a.get("key"): a.get("extra_dirs") or [] for a in adapters}
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
                    os.path.join(t["skills_dir"], s["name"]).replace(os.sep, "/"),
                    cfg, s, t, hash_cache)
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
