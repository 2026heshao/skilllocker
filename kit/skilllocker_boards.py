#!/usr/bin/env python3
"""skilllocker 快照层：list_boards 一次性给出 UI 需要的全部只读数据（字段与
ui/data.js 快照保持一致）；conflict_detail 同名副本文件对照；find_conflict_tool
定位共有 skill 的实体冲突所在 agent。只读，不改库、不改盘。"""
import datetime
import os
import sys

from skilllocker_base import (  # noqa: F401 - 部分名字经 skilllocker 门面再导出
    APP_VERSION, HOME, _file_digest, _tool_mark, file_list, fmt_bytes,
    folder_rows, last_scan_time, ordered_tools, target_state, tree_stats)
from skilllocker_store import (  # noqa: F401 - 门面再导出
    adoptable_rows, agent_entries, find_existing_store, group_unique, unique_entries)


def _db_path():
    """skilllocker.db 的展示路径。经 base 现读：单测打 DB_FILE 补丁也跟着走。"""
    import skilllocker_base as _base
    return _base.DB_FILE.replace("/", "\\")


def list_boards(con, cfg):
    """一次性给出 UI 需要的全部只读数据，字段与 ui/data.js 快照保持一致。"""
    tools = ordered_tools(con)
    enabled = [t for t in tools if t["enabled"]]
    skills = [dict(r) for r in con.execute("select * from skills order by name")]

    # 同一快照共享一份哈希备忘：state_map 与 agent_entries 会把同一个格口
    # 各查一遍，不贯通缓存就是每棵实体树全量哈希两遍
    # bootstrap 在 cfg 上挂了共享备忘（seed_meta 已扫过一遍）；其他桥调用没有，
    # 就用本次快照自己的缓存。
    hash_cache = cfg.get("_hash_cache")
    if not isinstance(hash_cache, dict):
        hash_cache = {}
    state_map = {}
    for t in enabled:
        for s in skills:
            if not s["enabled"]:
                state_map[(t["key"], s["name"])] = "disabled"
                continue
            state, _detail = target_state(
                os.path.join(t["skills_dir"], s["name"]).replace(os.sep, "/"),
                cfg, s, t, hash_cache)
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

    # 独有检测覆盖本机所有装了的 agent（含分发开关关闭的），不只 enabled；
    # 扫描根临时不可枚举时收集进 scan_errors，快照据此标记「降级」，
    # prune_meta 看到标记就跳过本轮（防把暂时看不见的 meta 当已删除清掉）
    scan_errors = []
    unique = unique_entries(con, cfg, tools, hash_cache, scan_errors)
    # 独有看板的真实单位是「逻辑技能」(名字, 内容哈希)，不是 (agent, 目录项)：
    # 同一技能被 7 个 agent 各装一份 = 1 个技能、7 个实例。实例行保留给
    # 删除 / 卸载等按行定位的操作，组行只用于展示与计数。
    unique_groups = group_unique(unique)
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

    # 首启引导：仓库为空时给「接管已有仓库 / 一键收编 / 导入」整块面板；
    # 自动建库只收走了共有那部分、还有可收行时给单行提示条（strip），
    # 不长期打扰。candidate 只在空库时计算（有内容时接管会被后端守卫拒绝）。
    first_run = None
    ready = adoptable_rows(unique, cfg)
    strip = bool(skill_out and cfg.get("_seed_note") and ready)
    if not skill_out or strip:
        cand = find_existing_store(con, cfg, tools) if not skill_out else None
        blocked = None
        if not skill_out:
            if not cfg.get("seed_auto", True):
                blocked = "disabled"
            elif cand:
                blocked = "candidate"
        first_run = {
            "storeEmpty": not skill_out,
            "strip": strip,
            "candidate": cand,
            "adoptable": [{"tool": u["tool"], "toolLabel": u["toolLabel"],
                           "mark": u["mark"], "name": u["name"],
                           "files": u["files"], "bytes": u["bytes"],
                           "kind": u["kind"]}
                          for u in ready],
            "adoptableAgents": sorted({u["toolLabel"] for u in ready}),
            "seedNote": cfg.get("_seed_note") or None,
            "seedBlocked": blocked,
            # 自动接管失败的原因：面板据此如实说明「为什么还得手动点一次」
            "autoAdoptError": cfg.get("_auto_adopt_error") or None,
        }

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
            "db": _db_path(),
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
            # uniqueCount 保持「实例数」语义（旧前端 / 脚本兼容）；
            # uniqueLogical 是去重后的逻辑技能数，界面默认展示后者。
            "uniqueCount": len(unique),
            "uniqueLogical": len(unique_groups),
            "storeIn": cfg.get("storeIn") or "install",
            # 空仓库首启的轻量快照：同名副本尚未按内容合并，文件数/字节未统计
            "uniqueLight": bool(cfg.get("_unique_light")),
            # 首启自动建库的说明（仅在真正跑过的那次 bootstrap 上有值）
            "seedNote": cfg.get("_seed_note") or None,
            # 非 None = 本轮有扫描根不可枚举（prune_meta 会跳过）；给排障留现场
            "scanDegraded": scan_errors or None,
            # config.json 损坏退回默认配置的留痕：前端 toast 显性化，别静默换仓库
            "loadError": cfg.get("_load_error") or None,
            # 首启自动接管失败（成功时为空）：状态栏/排障要能看到原因
            "autoAdoptError": cfg.get("_auto_adopt_error") or None,
        },
        "skills": skill_out,
        "tools": out_tools,
        "folders": folder_rows(con),
        "agents": agent_rows,
        "unique": unique,
        "unique_groups": unique_groups,
        "events": [],
        "conflict": conflict,
        "firstRun": first_run,
    }


def conflict_detail(con, cfg, tool_key, name):
    """中央 vs 本地同名副本的文件对照；只读，不合并、不写盘。

    公共文件先比大小，同大小再比内容哈希：只比大小会把「同大小不同内容」
    漏报成一致，误导 local-to-central 的回收决策。读不了内容的文件按一致算
    （拿不到证据不下结论）。"""
    trow = con.execute("select * from tools where key=?", (tool_key,)).fetchone()
    if not trow:
        raise RuntimeError("未知工具: %s" % tool_key)
    central = os.path.join(cfg["store"], name)
    local = os.path.join(trow["skills_dir"], name)
    c_files = file_list(central)
    l_files = file_list(local)
    c_map = {f["name"]: f["size"] for f in c_files}
    l_map = {f["name"]: f["size"] for f in l_files}
    common = set(c_map) & set(l_map)
    differs = {}
    diffs = 0
    for n in common:
        if c_map[n] != l_map[n]:
            differs[n] = True
        else:
            cd, ld = _file_digest(os.path.join(central, n)), _file_digest(os.path.join(local, n))
            differs[n] = cd is not None and ld is not None and cd != ld
        if differs[n]:
            diffs += 1
    for f in c_files:
        if f["name"] not in l_map:
            f["flag"] = "central-only"
        else:
            f["flag"] = "diff" if differs[f["name"]] else "same"
    for f in l_files:
        if f["name"] not in c_map:
            f["flag"] = "local-only"
        else:
            f["flag"] = "diff" if differs[f["name"]] else "same"
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
