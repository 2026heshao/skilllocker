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
    FIRST_RUN_ADOPT_EVENT, FIRST_RUN_SEED_EVENT, IGNORE_NAMES, adopt_declared_name,
    check_skill_name, contract_issue, contract_rows, contract_text, db, delete_tree,
    declared_name, dir_stats, has_first_run_action, has_skill_md, is_reparse,
    link_target, log_event, make_junction, migrate_folders, norm_path, ordered_tools,
    parse_frontmatter, path_under, remove_link, rewrite_declared_name, sha_tree,
    skill_md_of, store_location, target_state, write_config)
from skilllocker_meta import clear_meta, seed_meta, set_meta
from skilllocker_store import (
    adoptable_rows, agent_managed_roots, find_existing_store, find_unique_row,
    rescan, skill_dirs_under, store_skill_dirs, unique_entries)


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
    # 隔离模式（验收 / 冒烟）：只允许出计划。真建链、撤链、重拷都会往 agent
    # 目录写东西，而那正是用户真实的 agent 地盘（见 base.ISOLATION_MSG）。
    if apply_changes:
        _base.guard_agent_write(cfg, "分发同步")
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


def auto_distribute(con, cfg, names, skip_tools=(), apply_changes=True):
    """把刚入库的名字分发给「分发开关开着」的 agent。

    分发开关是持续状态：用户打开某个 agent 时授权的是「按中央仓库同步」，
    之后新入库的 skill 也属于这份授权。旧实现只有 set_agent 会调 do_sync，
    import / rescan 只登记不补链，于是每加一个技能都得手动重新开关一次。

    与 set_agent 走同一条 do_sync，不引入第二套链接语义：
      - 只处理 names（本次新登记 / 内容变更的名字），不碰存量；
      - 只选 detected=1 且 enabled=1 的工具，skip_tools 排除指定 agent
        （收编容器 / extra 来源时，源 agent 已有自己的加载路径，补主目录
        链接会让它从两个路径加载同名技能）；
      - 单个 agent 失败（权限 / 目录被占用）不影响其他 agent，也绝不把
        已成功的导入回滚；错误进 events，回包如实计数；
      - conflict / 不可读格口 do_sync 本来就不动手，这里不额外判定。
    返回 {skills, tools, changed, plan, errors}，调用方只读汇总。
    """
    names = sorted({str(n) for n in (names or []) if n})
    out = {"skills": names, "tools": [], "changed": 0, "plan": [], "errors": [],
           "isolated": False}
    if not names:
        return out
    # 隔离模式：这里**不能**抛错。刷新（bootstrap / rescan）会自动调用它，一抛就
    # 把整次刷新变成失败回包，用户看到的是「刷新报错」而不是「这次没补链」。
    # 降级成只登记不补链，并把 isolated 带回回包，界面如实说明。
    if apply_changes and _base.isolated(cfg):
        out["isolated"] = True
        log_event(con, "warn", "隔离模式：本次 %d 个新技能只登记不分发，未改动任何 agent 目录"
                  % len(names))
        con.commit()
        return out
    tools = [r["key"] for r in con.execute(
        "select key from tools where detected=1 and enabled=1 order by key")]
    skip = set(skip_tools or ())
    tools = [k for k in tools if k not in skip]
    out["tools"] = tools
    for key in tools:
        try:
            plan = do_sync(con, cfg, only_tools=[key], only_skills=names,
                           apply_changes=apply_changes)
        except Exception as exc:  # noqa: BLE001 - 单 agent 失败不能拖垮整批
            out["errors"].append({"tool": key, "error": str(exc)})
            log_event(con, "warn", "自动分发 %s 失败：%s" % (key, exc))
            try:
                con.commit()
            except Exception:  # noqa: BLE001 - 已写的事件尽力而为
                pass
            continue
        out["plan"].extend(plan)
    out["changed"] = sum(1 for p in out["plan"] if p.get("done"))
    if out["changed"] or out["errors"]:
        log_event(con, "auto-sync", "%d 个技能 → %d 个 agent，%d 处链接%s" % (
            len(names), len(tools), out["changed"],
            "，%d 个 agent 失败" % len(out["errors"]) if out["errors"] else ""))
        con.commit()
    return out


# ---------------------------------------------------------------- 增删
# check_skill_name 已上移到 skilllocker_base（身份契约原语与目录名校验必须同源，
# 否则 rescan / import / UI 各拿一套标准）；这里经门面 import 继续可用。


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
    # 身份契约入口归一化：第三方包的 frontmatter name 几乎必然不等于我们落盘
    # 的目录名（soft-skill 包声明 high-end-visual-design 就是这一类）。不在这
    # 里对齐，注册表就会存下 label != name 的行，UI 显示的名字在 agent 里不存在。
    adopt_declared_name(central)
    rescan(con, cfg)
    log_event(con, "import", "%s <- %s" % (name, src))
    con.commit()
    return name


def repair_contract(con, cfg, names=None, apply_changes=True):
    """把「目录名 != 包内声明名」的中央仓库包对齐到目录名。

    这是 soft-skill 那类「分发 7/7 全绿但 agent 里叫不出名字」的修复入口。
    只改 SKILL.md frontmatter 的 name 一行，正文、description、其余键一字不动；
    改完 rescan，label 回到与 name 一致，agent 侧因为本来就用目录名，链接与
    注册名都不受影响 —— 所以对齐方向是「文件跟目录走」，不是重命名目录：
    后者要重建 7 个 agent 的 junction、改 skills 主键、迁移使用统计，且会让
    用户已经习惯的名字全部失效。

    默认预览（apply_changes=False 只报将改什么），与 do_sync 的 --fix 同一语义。
    """
    only = set(names or [])
    plan = []
    for row in contract_rows(con, cfg):
        name = row["name"]
        if only and name not in only:
            continue
        central = os.path.join(cfg["store"], name)
        md = skill_md_of(central)
        entry = {"skill": name, "declared": row["declared"], "kind": row["kind"],
                 "detail": row["detail"], "shadowed_by": row["shadowed_by"],
                 "path": md.replace(os.sep, "/") if md else "",
                 "done": False}
        if not md:
            entry["error"] = "找不到 SKILL.md（目录不是合法技能包）"
        elif not apply_changes:
            entry["action"] = "rewrite-name"
        else:
            try:
                entry["done"] = rewrite_declared_name(md, name)
                entry["action"] = "rewritten"
            except RuntimeError as exc:
                entry["error"] = str(exc)
        plan.append(entry)
    done = [p for p in plan if p["done"]]
    if done:
        rescan(con, cfg)
        log_event(con, "contract-fix", "对齐 %d 个包的声明名：%s"
                  % (len(done), ", ".join(p["skill"] for p in done)))
        con.commit()
    return plan


def orphan_links(con, cfg):
    """agent 的 skills 目录里「条目在、但任何 agent 都加载不了」的孤儿。

    为什么 matrix 抓不到：matrix 只遍历中央仓库注册表（skills 表），仓库之外的
    条目根本不占格口，于是「目录里明明有个 tabbit，agent 里却查无此技能」这种
    状态在分发矩阵里是完全隐形的 —— 而它和用户报的「7/7 全绿但用不上」是同一
    类问题。这里补上另一半视野：
      - dead-link：链接指向的目标已经不存在（卸载 / 搬迁后留下的 junction）；
      - no-skill-md：目录里没有 SKILL.md，不算技能包。
    只报告不删：agent 目录里可能有第三方安装器自己建的条目（tabbit 就是），
    归属没确认前本工具不该动别人的地盘；要清理由用户点名后自行处理。
    已在注册表里的名字交给 matrix 的 broken-link / conflict-dir 报，避免双报。
    """
    known = {r["name"] for r in con.execute("select name from skills")}
    out = []
    for t in ordered_tools(con):
        if t["mode"] == "off" or not t["skills_dir"]:
            continue
        try:
            names = sorted(os.listdir(t["skills_dir"]))
        except OSError:
            continue  # 目录读不了（权限 / 盘没挂）：不下结论，交给 tools 检测
        for n in names:
            if n in known or n in IGNORE_NAMES or n.startswith(".") or ".skpart-" in n:
                continue
            p = os.path.join(t["skills_dir"], n)
            if not os.path.lexists(p):
                continue  # 连条目都不在了（枚举间隙被删），不是孤儿
            if os.path.isfile(p):
                continue  # 散文件是 agent 自己的状态文件（如 _bm_skillid_migration.json）
            # 判据必须与 target_state 同一口径用 has_skill_md：死 junction 的
            # os.path.isdir / isfile 全都返回 False，用它们当「是不是目录」的门槛
            # 会让死链在第一步就被 continue 掉，正好漏掉本函数唯一要抓的东西。
            if is_reparse(p) or os.path.islink(p):
                if not has_skill_md(p):
                    out.append({"tool": t["key"], "tool_label": t["label"], "name": n,
                                "kind": "dead-link",
                                "detail": "%s -> %s（目标不存在或已无 SKILL.md）" % (
                                    p.replace(os.sep, "/"), link_target(p))})
                # 能解析到 SKILL.md 的链接是 foreign-link，已由独有看板报，不双报
                continue
            if not has_skill_md(p):
                out.append({"tool": t["key"], "tool_label": t["label"], "name": n,
                            "kind": "no-skill-md",
                            "detail": "%s（目录里没有 SKILL.md）" % p.replace(os.sep, "/")})
    return out


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
    _base.guard_agent_write(cfg, "撤下某 agent 的全部分发")
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
    _base.guard_agent_write(cfg, "删除共有技能")
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
    _base.guard_agent_write(cfg, "删除 agent 本地副本")
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
    _base.guard_agent_write(cfg, "卸载独有技能")
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
      - foreign-link 按真身收：解析 junction 指向的目录（含 SKILL.md、不在本
        仓库内）拷进仓库；真身归第三方就原样保留，开关开着时链接改指本仓库，
        关着时保留原链接并如实说明。

    源副本一律保留（非破坏）：拷贝完成后真身与中央内容一致，独有行自动从
    看板消失（_unique_item 按内容哈希判定）。收编即共有，随后按分发开关
    自动补链（auto_distribute）：main 直属行连同源 agent 一起对齐——junction
    模式下 takeover → relink 成指向中央的链接，agent 无感续用；copy 模式内容
    一致 ok-copy 不动。容器 / 变体 / extra 行的真身不在 skills_dir 直属位，
    给源 agent 补主目录链接会让它从两个路径加载到同名的两份，因此只分发给
    其他开着的 agent，源目录原样保留。独有 meta 里写过的显示名 / 备注迁给
    共有行（归属夹本就不参与；独有 meta 行随后由 prune_meta 按行消失自动清理）。
    """
    _base.guard_agent_write(cfg, "独有转共有")
    u = find_unique_row(con, cfg, tool_key, name)
    if not u:
        raise RuntimeError("独有 skill 不存在（或 agent 未检测到）：%s/%s" % (tool_key, name))
    # 实体行直接收；foreign-link 先解析真身再收（真身可读且不在本仓库内）。
    # 同名覆盖仍拒绝：中央已有同名副本，「谁覆盖谁」走冲突处置。
    source, store_name, via = resolve_adopt_target(con, cfg, u)
    path = os.path.normpath(u["path"])
    ukey = tool_key + "/" + u["name"]
    src_main = u.get("src") == "main" and u["name"] == store_name
    import_dir(con, cfg, source, store_name)
    files, size = dir_stats(os.path.join(cfg["store"], store_name))
    um = con.execute("select display_name, note from skill_meta "
                     "where scope='unique' and key=?", (ukey,)).fetchone()
    if um and (um["display_name"] or um["note"]):
        set_meta(con, "shared", store_name, {"display": um["display_name"] or "",
                                             "note": um["note"] or ""})
    # 收编即共有：分发开关开着的 agent 都该收到。主目录直属行 / 可解析真身的
    # 链接（src=main）连同源 agent 一起对齐；容器子目录与 extra 外部目录的
    # 真身不在 skills_dir 直属位，给源 agent 补主目录链接会让它重复加载同名
    # 技能 —— 只分发给其他开着的 agent，源目录原样保留。
    skip = () if src_main else (tool_key,)
    dist = auto_distribute(con, cfg, [store_name], skip_tools=skip)
    relinked = sum(1 for p in dist["plan"] if p.get("done") == "relink")
    linked = sum(1 for p in dist["plan"] if p.get("done") in ("junction", "copy"))
    log_event(con, "promote", "%s/%s -> 共有 %s（%d 文件，%s%s%s）" % (
        tool_key, u["name"], store_name, files,
        "按真身收编，" if via == "link" else "",
        "源副本已转链接" if relinked else "源副本保留",
        "，分发 %d 处" % linked if linked else ""))
    con.commit()
    return {"scope": "unique", "key": ukey, "tool": tool_key, "name": store_name,
            "path": path.replace(os.sep, "\\"), "files": files, "bytes": size,
            "relinked": relinked, "linked": linked, "via": via,
            "source": source.replace(os.sep, "\\")}


# ---------------------------------------------------------------- 首启接管 / 一键收编
def adopt_store(con, cfg, path):
    """首启接管：把本机其实已经存在的中央仓库目录指为本工具的中心。

    只做三件事：校验 → 写 config.json → 重扫登记。不搬技能、不建链接，所以
    完全可逆（删掉 config.json 就回到默认仓库）。没有这一步，安装版首启永远
    是「共有 0 / 全是独有」——用户看到的不是检测错，是空仓库。

    守卫（任一不满足就抛错，让前端如实提示，绝不静默换仓库）：
      - 当前中央仓库还没有可登记的技能：仓库有内容时改指，prune_meta 会把旧
        仓库那批共有 meta（改名 / 备注 / 归属夹）当「行已消失」清掉；
      - 目标目录真实存在、可枚举，且至少含 1 个带 SKILL.md 的子目录；
      - 目标不在 agent 自己管的目录里（skills_dir / extra_dirs），也不与它们
        互相包含 —— 把 agent 的读取目录当仓库会让分发链接绕回原地；
      - 目标不是当前仓库自己。
    """
    if _base.isolated(cfg):
        raise RuntimeError("接管已有中央仓库被拒绝了：%s；接管会把仓库指针改到别处，"
                         "之后的分发就会落到真实库上" % _base.ISOLATION_MSG)
    target = os.path.normpath(str(path or "").strip().strip('"')).replace(os.sep, "/")
    if not target or target in (".", ".."):
        raise RuntimeError("要接管的中央仓库路径为空")
    if not os.path.isdir(target):
        raise RuntimeError("要接管的目录不存在：%s" % target.replace("/", "\\"))
    tp = norm_path(target)
    if tp == norm_path(cfg["store"]):
        raise RuntimeError("这个目录已经是当前中央仓库：%s" % target.replace("/", "\\"))
    for m in agent_managed_roots(con):
        if path_under(tp, m) or path_under(m, tp):
            raise RuntimeError("要接管的目录落在 agent 自己的技能目录里，不能当中央仓库用：%s"
                               % target.replace("/", "\\"))
    try:
        present = store_skill_dirs(cfg)
    except RuntimeError:
        present = []  # 当前仓库根不可读：不该拦住接管，下面 rescan 会给出真实错误
    if present:
        raise RuntimeError("当前中央仓库已有 %d 个技能，为避免丢失它们的改名与归档，"
                           "首启接管只允许在空仓库时使用；退出后手动改 config.json 的 store 也可以"
                           % len(present))
    try:
        skills = skill_dirs_under(target)
    except RuntimeError as exc:
        raise RuntimeError("要接管的目录不可用：%s" % exc)
    if not skills:
        raise RuntimeError("要接管的目录里没有带 SKILL.md 的技能，不像中央仓库：%s"
                           % target.replace("/", "\\"))
    old = cfg["store"]
    cfg["store"] = tp
    cfg["storeIn"] = store_location(tp)  # 界面如实显示「自定义目录」，别继续标安装目录
    try:
        write_config(cfg)
    except RuntimeError:
        cfg["store"] = old
        cfg["storeIn"] = store_location(old)
        raise
    count = rescan(con, cfg)
    log_event(con, "adopt-store", "接管已有中央仓库 %s（原 %s，登记 %d 个技能）"
              % (tp, old, count))
    con.commit()
    return {"store": tp, "path": tp.replace("/", "\\"),
            "previous": norm_path(old).replace("/", "\\"),
            "skills": count, "dirCount": len(skills)}


def auto_adopt_store(con, cfg, tools=None):
    """首启自动接管：从各 agent 已有 junction 反推出真仓库后，直接指过去。

    v0.3.2 的自动建库在「本机已有仓库」时会主动退让，只留一次点击的接管面板。
    但安装版的新用户绝大多数属于这种形态（别的管理器 / 老版本早就把 agent
    目录链到同一仓库），于是首屏长期停在「共有 0 / 全是独有」，看起来像没修。

    这里把「一次点击」变成自动动作，仍然复用 adopt_store 的完整守卫：
      - 只有仓库为空、没登记过技能、没做过首启决策时才跑；
      - seed_auto=false 或 config.json 显式写过 store 时不动用户选择；
      - 目标必须在 agent 地盘外、含 SKILL.md、当前仓库可写；
      - 只在候选成立（≥2 条链接 + ≥2 个技能）时接管，宁缺勿滥。

    失败不抛：错误写进 cfg["_auto_adopt_error"]，bootstrap 照常出首屏并回退到
    手动引导面板；成功则写 config.json + rescan，并打 FIRST_RUN_ADOPT_EVENT
    一次性标记（之后清空仓库也不会再自动改指）。
    """
    if not cfg.get("seed_auto", True):
        return None
    if cfg.get("_store_explicit"):
        return None  # 用户自己配过仓库，不替他把指针改走
    if has_first_run_action(con):
        return None
    if not os.path.isdir(cfg["store"]):
        return None
    try:
        if store_skill_dirs(cfg):
            return None  # 仓库非空：只服务真正的空库首启
    except RuntimeError:
        return None
    cand = find_existing_store(con, cfg, tools)
    if not cand:
        return None
    try:
        got = adopt_store(con, cfg, cand["store"])
    except (OSError, RuntimeError) as exc:
        cfg["_auto_adopt_error"] = str(exc)
        log_event(con, "warn", "首启自动接管失败：%s" % exc)
        con.commit()
        return None
    log_event(con, FIRST_RUN_ADOPT_EVENT,
              "首启自动接管 %s（%d 个技能，%d 条链接 / %d 个 agent）"
              % (got["path"], got["skills"], cand["linkCount"], len(cand["tools"])))
    con.commit()
    got["candidate"] = cand
    got["note"] = ("已接管本机已有的中央仓库：%s（%d 个技能，%d 条现有链接）"
                   % (got["path"], got["skills"], cand["linkCount"]))
    return got


def resolve_adopt_target(con, cfg, row):
    """把一条独有行的「可拷贝真身」解析出来：返回 (源目录, 入库名, via)。

    单行 promote_unique 与批量 _adopt_groups / 首启建库共用这一套判定，
    不再各写一份（旧实现 foreign-link 被整体排除，收编规则不完整）。
    拒绝时抛 RuntimeError，原因直接给前端。
    """
    if not row:
        raise RuntimeError("独有 skill 不存在（或 agent 未检测到）")
    if row.get("sameName"):
        raise RuntimeError("中央仓库已有同名但内容不同的副本，一键收编会覆盖它；"
                           "请先在详情里「查看文件差异」处置冲突")
    kind = row.get("kind")
    path = os.path.normpath(str(row.get("path") or ""))
    name = str(row.get("name") or "")
    if kind == "unique":
        # 实体行按真身目录名入库：容器行 / 扫描变体的行名不是合法的仓库直属
        # 目录名（`容器/子目录`、`name（段名）`），真身目录名才是。
        store_name = os.path.basename(path)
        if store_name in ("", ".", ".."):
            raise RuntimeError("无法从真身路径推导 skill 名：%s" % path)
        check_skill_name(store_name)
        if not os.path.isdir(path):
            raise RuntimeError("路径已不存在：%s" % path)
        if is_reparse(path) or os.path.islink(path):
            raise RuntimeError("这条实体行已经变成链接，请刷新看板后重试")
        if not has_skill_md(path):
            raise RuntimeError("目标不是 skill 目录（缺 SKILL.md）：%s" % path)
        return path, store_name, "dir"
    check_skill_name(name)
    if kind == "foreign-link":
        if not (is_reparse(path) or os.path.islink(path)):
            raise RuntimeError("这条外部链接已经变成实体目录，请刷新看板后重试")
        real = link_target(path)
        if not real:
            raise RuntimeError("链接已断开，解析不到真身：%s" % path)
        real = os.path.normpath(real)
        if not os.path.isdir(real):
            raise RuntimeError("链接真身目录不存在（断链）：%s" % real)
        if not has_skill_md(real):
            raise RuntimeError("链接真身不是 skill 目录（缺 SKILL.md）：%s" % real)
        store = norm_path(cfg["store"])
        rp = norm_path(real)
        if rp == store or rp.startswith(store + "/"):
            raise RuntimeError("链接真身已经在本仓库内，无需收编")
        return real, name, "link"
    raise RuntimeError("这种行形态不支持收编：%s（同名覆盖请走冲突处置）" % kind)


def _adopt_groups(con, cfg, rows, budget=None, pick_first_on_diff=True,
                  sync_links=True):
    """收编内核：分组 → 同名哈希判定 → 原子拷贝 → meta 迁移 →（可选）链接对齐。

    rows 为 adoptable_rows 过滤后的独有行；返回
    {adopted, skipped, metaMoved, relinked, linked, files, bytes,
     truncated, truncReason, remaining, failed, failure, direct}。
    failed 只统计「本可以收、但读不了 / 拷不进去」的硬失败（不含同名冲突等
    语义跳过）：调用方据此决定能不能算「首启已完成」，见 seed_shared_store。
    budget=(截止 monotonic 时刻, 技能上限, 字节上限)，任一触顶就停止并置
    truncated=True；已拷的每一项都是原子目录，保留。
    pick_first_on_diff=False（首启建库）时，同名但内容不同的组整组不收，
    绝不自动选边；True（用户显式收编）时收第一份、其余跳过并说明。
    """
    rows = rows or []
    direct = {}                 # tool -> {name}：主目录直属行，只有这些能补 / 对齐链接
    groups, index, affected = [], {}, set()
    skipped = []
    failed, failure = 0, ""

    def _fail(reason):
        nonlocal failed, failure
        failed += 1
        if not failure:
            failure = reason

    for u in rows:
        try:
            source, name, via = resolve_adopt_target(con, cfg, u)
        except RuntimeError as exc:
            _fail(str(exc))
            skipped.append({"name": u.get("name"), "tool": u.get("tool"),
                            "reason": str(exc)})
            continue
        direct.setdefault(u["tool"], set()).add(name)
        if name in index:
            groups[index[name]][1].append((u["tool"], source, via))
        else:
            index[name] = len(groups)
            groups.append((name, [(u["tool"], source, via)]))

    deadline, max_skills, max_bytes = budget or (None, None, None)
    adopted, truncated, trunc_reason, remaining = [], False, "", 0
    total_files = total_bytes = 0
    meta_moved = 0
    for gi, (name, items) in enumerate(groups):
        if max_skills is not None and len(adopted) >= max_skills:
            truncated, trunc_reason, remaining = True, "数量上限", len(groups) - gi
            break
        if deadline is not None and time.monotonic() > deadline:
            truncated, trunc_reason, remaining = True, "时间预算", len(groups) - gi
            break
        central = os.path.join(cfg["store"], name)
        if os.path.lexists(central):
            skipped.append({"name": name, "reason": "中央仓库已有同名条目，不覆盖"})
            continue
        base = None
        diffs = []
        for tool, source, via in items:
            try:
                h = sha_tree(source)
            except (OSError, RuntimeError):
                _fail("目录不可读，已跳过")
                skipped.append({"name": name, "tool": tool, "reason": "目录不可读，已跳过"})
                continue
            if base is None:
                base = (source, h, tool, via)
            elif h != base[1]:
                diffs.append((tool, source, via))
            else:
                affected.add(tool)  # 同一份内容：收一次就够，其余源副本保留
        if base is None:
            continue
        if diffs and not pick_first_on_diff:
            skipped.append({"name": name, "reason": "同名技能内容不同，未自动选边"})
            for tool, _source, _via in diffs:
                skipped.append({"name": name, "tool": tool,
                                "reason": "同名技能内容不同，未自动选边"})
            continue
        # 预算：拷前按源目录大小预估；这一项装不下就停，已收的保留
        _files, size = dir_stats(base[0])
        if max_bytes is not None and total_bytes + size > max_bytes:
            truncated, trunc_reason, remaining = True, "总量上限", len(groups) - gi
            break
        try:
            copy_tree_atomic(base[0], central)
        except (OSError, RuntimeError) as exc:
            _fail("拷入中央仓库失败：%s" % exc)
            skipped.append({"name": name, "reason": "拷入中央仓库失败：%s" % exc})
            continue
        adopt_declared_name(central)  # 与 import_dir 同一契约，收编路径不能漏
        affected.add(base[2])
        for tool, _source, _via in diffs:
            skipped.append({"name": name, "tool": tool,
                            "reason": "与已收编的同名技能内容不同，请到独有看板单独处置"})
        for tool, _source, _via in items:
            um = con.execute("select display_name, note from skill_meta "
                             "where scope='unique' and key=?",
                             (tool + "/" + name,)).fetchone()
            if not um or not (um["display_name"] or um["note"]):
                continue
            if con.execute("select 1 from skill_meta where scope='shared' and key=?",
                           (name,)).fetchone():
                continue  # 已有共有 meta（用户手动写过）：不覆盖
            set_meta(con, "shared", name, {"display": um["display_name"] or "",
                                           "note": um["note"] or ""})
            meta_moved += 1
        adopted.append(name)
        f, s = dir_stats(central)
        total_files += f
        total_bytes += s
        log_event(con, "adopt", "收编 %s ← %s%s" % (
            name, base[0].replace(os.sep, "\\"),
            "（按真身）" if base[3] == "link" else ""))

    if adopted:
        # 链接对齐依赖 skills 表里的新行（do_sync 按名字查中央内容哈希），
        # 所以先登记再对齐；调用方只需补 folders / meta 播种。
        rescan(con, cfg)
    relinked = linked = 0
    if sync_links and adopted:
        enabled = {r["key"] for r in con.execute(
            "select key from tools where detected=1 and enabled=1")}
        touched = sorted(affected & enabled)
        # 逐个 agent 只对齐它主目录直属的那批名字：容器行不补链接（否则同一个
        # agent 会从「容器/子目录」和「子目录」两个路径加载到同名技能）
        plan = []
        for tool in touched:
            names = [n for n in adopted if n in direct.get(tool, set())]
            if not names:
                continue
            plan += do_sync(con, cfg, only_tools=[tool], only_skills=names,
                            apply_changes=True)
        relinked = sum(1 for p in plan if p.get("done") in ("relink", "recopy"))
        linked = sum(1 for p in plan if p.get("done") in ("junction", "copy"))
    return {"adopted": adopted, "skipped": skipped, "metaMoved": meta_moved,
            "relinked": relinked, "linked": linked,
            "files": total_files, "bytes": total_bytes,
            "truncated": truncated, "truncReason": trunc_reason,
            "remaining": remaining, "failed": failed, "failure": failure,
            "direct": direct}


def adopt_local_skills(con, cfg, unique=None):
    """首启一键收编：把各 agent 主目录直属的可收技能收进中央仓库。

    范围由 store.adoptable_rows 把关（src=main 的实体行 + 可解析真身的
    foreign-link；extra 外部目录、容器展开行、同名不同内容的覆盖行不碰）。
    语义与单行收编 promote_unique 一致，共用 _adopt_groups：
      1. 同名同内容只收一份，同名不同内容收第一份、其余跳过（用户显式点收编）；
      2. copy_tree_atomic 拷进仓库；源目录保留；
      3. 独有行上的人工改名 / 备注迁给共有行；
      4. 一次 rescan + seed_meta（新共有行按预置规则归档）；
      5. 只对分发开关开着的 agent、且只对主目录直属名字做 do_sync 对齐。
    """
    _base.guard_agent_write(cfg, "一键收编 agent 技能")
    if not os.path.isdir(cfg["store"]):
        raise RuntimeError("中央仓库目录不可用：%s" % cfg["store"].replace("/", "\\"))
    rows = adoptable_rows(unique if unique is not None else unique_entries(con, cfg), cfg)
    res = _adopt_groups(con, cfg, rows, pick_first_on_diff=True, sync_links=True)
    if res["adopted"]:
        migrate_folders(con, cfg)
        seed_meta(con, cfg)
    log_event(con, "adopt", "一键收编：进仓库 %d 个，跳过 %d 个，对齐链接 %d 处，补链 %d 处"
              % (len(res["adopted"]), len(res["skipped"]), res["relinked"], res["linked"]))
    con.commit()
    return {"adopted": res["adopted"], "skipped": res["skipped"],
            "relinked": res["relinked"], "linked": res["linked"],
            "files": res["files"], "bytes": res["bytes"],
            "metaMoved": res["metaMoved"],
            "store": norm_path(cfg["store"]).replace("/", "\\")}


# ---------------------------------------------------------------- 首启自动建库
def _seed_candidates(con, cfg, tools=None):
    """首启建库的候选与共有判定（只读）。

    候选 = adoptable_rows(unique_entries(...))；按入库名分组，shared(name) =
    出现在多少个不同 agent 的主目录里。种子优先取 shared >= seed_min_agents；
    没有任何名字达到阈值时退化为「全部候选」：
      - 只有一个贡献 agent → 单 agent 用户也要能建库（single_agent）；
      - 多个 agent 但技能各装各的、没有同名交叉 → 新用户不会看到空库
        （fallback_all）。同名但内容不同的组仍由 _adopt_groups 整组跳过。
    """
    tools = tools if tools is not None else ordered_tools(con)
    rows = adoptable_rows(unique_entries(con, cfg, tools), cfg)
    groups, order = {}, []
    for u in rows:
        name = u["name"]
        if name not in groups:
            groups[name] = []
            order.append(name)
        groups[name].append(u)
    contributors = {u["tool"] for u in rows}
    shared = {name: len({u["tool"] for u in groups[name]}) for name in order}
    min_agents = cfg.get("seed_min_agents", 2)
    seeds = [name for name in order if shared[name] >= min_agents]
    single, fallback_all = False, False
    if not seeds and order:
        seeds = list(order)
        single = len(contributors) <= 1
        fallback_all = not single
    return {"rows": rows, "groups": groups, "order": order, "seeds": seeds,
            "shared": shared, "contributors": contributors,
            "single_agent": single, "fallback_all": fallback_all}


def seed_preview(con, cfg, tools=None):
    """CLI seed --dry：只报告将会收哪些名字、来自谁，不写盘、不改库。"""
    plan = _seed_candidates(con, cfg, tools)
    out = []
    for name in plan["seeds"]:
        items = plan["groups"][name]
        hashes, read_errors = [], []
        for u in items:
            try:
                source, _n, _v = resolve_adopt_target(con, cfg, u)
                hashes.append(sha_tree(source))
            except (OSError, RuntimeError) as exc:
                read_errors.append({"tool": u["tool"], "reason": str(exc)})
        out.append({
            "name": name,
            "tools": sorted({u["tool"] for u in items}),
            "shared": plan["shared"].get(name, 0),
            "consistent": len(set(hashes)) <= 1,
            "instances": [{"tool": u["tool"], "path": u["path"], "kind": u["kind"]}
                          for u in items],
            "errors": read_errors,
        })
    # 与 seed_shared_store 同一组守卫：dry 也要说清「实际会不会执行」
    blocked = []
    if not cfg.get("seed_auto", True):
        blocked.append("seed_auto=false")
    if has_first_run_action(con):
        blocked.append("first-run-done")
    try:
        if store_skill_dirs(cfg):
            blocked.append("store-not-empty")
    except RuntimeError:
        blocked.append("store-unreadable")
    if find_existing_store(con, cfg, tools):
        blocked.append("candidate-store")
    return {
        "seeded": [g["name"] for g in out
                   if g["consistent"] and not g["errors"] and not blocked],
        "groups": out,
        "candidates": len(plan["rows"]),
        "single_agent": plan["single_agent"],
        "fallback_all": plan["fallback_all"],
        "min_agents": cfg.get("seed_min_agents", 2),
        "contributors": sorted(plan["contributors"]),
        "blocked": blocked,
    }


def seed_note(res):
    """首启建库结果的一句话（进 meta.seedNote，toast + 状态栏留痕）。"""
    n = len(res.get("seeded") or [])
    agents = len(res.get("byAgent") or {})
    skipped = len(res.get("skipped") or [])
    failed = int(res.get("failed") or 0)
    if res.get("error"):
        return res["error"]
    if res.get("truncated"):
        return ("已收入 %d 个技能后停止（%s），剩余 %d 个请用「一键收编」手动处理"
                % (n, res.get("truncReason") or "上限", res.get("remaining") or 0))
    if not n and failed:
        return ("首次启动建库失败：%d 个候选未能拷入中央仓库（%s）；"
                "没有写入一次性标记，下次启动会自动重试"
                % (failed, (res.get("failure") or "原因见日志")[:160]))
    if res.get("single_agent"):
        return "本机只检测到 1 个 agent，已把它目录里的 %d 个技能作为初始中央仓库" % n
    if res.get("fallback_all") and n:
        return ("本机各 agent 的技能没有同名交叉，已把 %d 个技能全部作为初始仓库"
                "（来自 %d 个 agent）" % (n, agents))
    if res.get("conflict_only"):
        return ("首次启动检测到技能，但同名副本内容不同，未自动选边；"
                "请在独有看板或「一键收编」里决定保留哪份")
    if n:
        text = "已自动建立中央仓库：收入 %d 个共有技能（来自 %d 个 agent）" % (n, agents)
        if skipped:
            text += "，跳过 %d 个" % skipped
            text += ("（其中 %d 个拷入失败）" % failed) if failed else "（同名不同内容等）"
        return text
    return "首次启动检查完成：没有找到可自动收入的共有技能，可在独有看板一键收编"


def seed_shared_store(con, cfg, tools=None):
    """首启自动建库：把各 agent 的共有技能拷进空仓库，返回结果或 None。

    守卫（任一不满足返回 None，不抛错，让 bootstrap 正常出首屏）：
    seed_auto 关 / 首启已决策（建库、自动或手动接管，任一留痕）/
    仓库根不可用 / 反推得出已有仓库（此时应引导接管，不能造第二份仓库）。
    没有任何可收候选时返回 None 且不写标记 —— 等用户以后装了 agent 再跑。
    全部候选都硬失败（目录读不了 / 拷不进去）时也不写标记：这不是「没有
    候选」，下次启动要能重试，原因通过 seedNote 如实回给前端。
    """
    started = time.monotonic()
    if not cfg.get("seed_auto", True):
        return None
    if has_first_run_action(con):
        return None
    if not os.path.isdir(cfg["store"]):
        return None
    try:
        if store_skill_dirs(cfg):
            return None  # ① 中央仓库非空：只服务真正的空库首启
    except RuntimeError:
        return None
    if find_existing_store(con, cfg, tools):
        return None
    plan = _seed_candidates(con, cfg, tools)
    if not plan["rows"]:
        return None
    seed_rows = []
    for name in plan["seeds"]:
        seed_rows.extend(plan["groups"][name])
    deadline = started + cfg["seed_budget_s"]
    res = _adopt_groups(
        con, cfg, seed_rows,
        budget=(deadline, cfg["seed_max_skills"], cfg["seed_max_bytes"]),
        pick_first_on_diff=False, sync_links=False)
    if res["adopted"]:
        migrate_folders(con, cfg)
        seed_meta(con, cfg)
    by_agent = {}
    for name in res["adopted"]:
        for u in plan["groups"][name]:
            by_agent[u["tool"]] = by_agent.get(u["tool"], 0) + 1
    ms = int((time.monotonic() - started) * 1000)
    out = {
        "seeded": res["adopted"], "skipped": res["skipped"],
        "byAgent": by_agent,
        "shared": {n: plan["shared"].get(n, 0) for n in plan["seeds"]},
        "truncated": res["truncated"], "truncReason": res["truncReason"],
        "remaining": res["remaining"], "bytes": res["bytes"],
        "files": res["files"], "ms": ms, "single_agent": plan["single_agent"],
        "fallback_all": plan["fallback_all"],
        "candidates": len(seed_rows),
        "contributors": sorted(plan["contributors"]),
        "failed": res.get("failed") or 0,
        "failure": res.get("failure") or "",
    }
    # 有候选但一个都没收进来、也没有硬失败：只可能是同名内容不同的整组跳过。
    # 给前端一句准确的原因，别让用户以为「没检测到技能」。
    out["conflict_only"] = bool(
        seed_rows and not res["adopted"] and not out["failed"]
        and any("同名技能内容不同" in str(s.get("reason") or "")
                for s in res["skipped"]))
    if not res["adopted"] and out["failed"]:
        # 一个都没收进来、且失败都是硬失败（权限 / 磁盘满 / 目录读取失败）：
        # 这轮不算「首启已完成」。不写 first-run-seed，下次启动重试；
        # 真实原因带给前端，别再显示「没有找到可自动收入的共有技能」。
        reason = (out["failure"] or "原因见日志")[:160]
        out["error"] = ("首启建库失败：%d 个候选未能拷入中央仓库（%s）；"
                        "下次启动会自动重试" % (out["failed"], reason))
        log_event(con, "warn", out["error"])
        con.commit()
        return out
    detail = "收入 %d / 跳过 %d / 候选 %d / %dms%s" % (
        len(res["adopted"]), len(res["skipped"]), len(seed_rows), ms,
        (" / 截断：%s，剩余 %d" % (res["truncReason"], res["remaining"]))
        if res["truncated"] else "")
    log_event(con, FIRST_RUN_SEED_EVENT, detail)
    con.commit()
    return out


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
        adopt_declared_name(central)  # 回收本地版同样要满足目录名 == 声明名
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
