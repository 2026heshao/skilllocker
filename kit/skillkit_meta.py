#!/usr/bin/env python3
"""skillkit 元数据层：skill_meta 表（显示名 / 备注 / 分类）的预置播种、
读写、清理与导出。scope('shared'|'unique') + key 承载两类 skill；
共有 key = 目录名，独有 key = tool + '/' + 目录名。
seed 列记录首次预置分类，用来判定「预置分类不算人工编辑」。
"""
import datetime
import json
import re

from skillkit_base import APP_VERSION, log_event, now_ms  # noqa: F401
from skillkit_boards import list_boards
from skillkit_store import unique_entries


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
    """给还没有 meta 的 skill / 独有条目预置分类与归属夹。

    幂等：known 集合里已有的（scope,key）不动，只补磁盘上新增的 —— 独有
    看板的行会随磁盘 / adapters 变化增长，所以不能只播种一次。force=True
    时先清空再重建（仅显式重建用；「清空我的编辑」走 reseed_meta，不清表）。

    shared 新行：folder_id = 预置夹（夹被删过则留空 = 未归档），categories
    写 '[]'（多选分类已废弃，列保留只为旧代码回滚可读）。unique 新行
    folder 恒为 NULL（独有技能不参与归档）。
    """
    if force:
        con.execute("delete from skill_meta")
        con.commit()
    known = {(r["scope"], r["key"]) for r in con.execute("select scope,key from skill_meta")}
    folder_ids = {r["id"] for r in con.execute("select id from folders")}
    rows = []
    for r in con.execute("select name from skills order by name"):
        if ("shared", r["name"]) in known:
            continue
        seed = seed_category(r["name"])
        rows.append(("shared", r["name"], "", "", "", "[]", seed,
                     seed if seed in folder_ids else None, 0))
    for u in unique_entries(con, cfg):
        key = u["tool"] + "/" + u["name"]
        if ("unique", key) in known:
            continue
        seed = "doc" if u["sameName"] else "eng"
        rows.append(("unique", key, u["tool"], "", "", json.dumps([seed]), seed, None, 0))
    for row in rows:
        con.execute("insert or replace into skill_meta"
                    "(scope,key,tool,display_name,note,categories,seed,folder_id,updated_at) "
                    "values(?,?,?,?,?,?,?,?,?)", row)
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
            "folder": r["folder_id"] or "",
            "seed": r["seed"] or "",
            "scope": r["scope"],
            "tool": r["tool"] or "",
            "updated": int(r["updated_at"] or 0),
        }
    return out


def set_meta(con, scope, key, payload):
    """写一条人工编辑。payload: {display, note, folder?, tool?}

    folder 仅 shared 生效：'' = 移到未归档；字段缺省 = 保留原归属（旧前端 /
    旧脚本不带 folder 时不会误清归属）；不存在的夹 id 直接抛错，宁可失败也
    不静默改错地方。unique 行 folder 恒为 NULL，传了也忽略。
    categories 列不再接收写入 —— 读旧值原样写回，回滚到旧代码后仍能读到
    迁移前的多选分类。
    """
    if scope not in ("shared", "unique"):
        raise RuntimeError("未知 scope: %s" % scope)
    payload = payload or {}
    tool = payload.get("tool") or (key.split("/", 1)[0] if scope == "unique" else "")
    display = str(payload.get("display") or "").strip()[:48]
    note = str(payload.get("note") or "")[:400]
    old = con.execute("select seed, categories, folder_id from skill_meta "
                      "where scope=? and key=?", (scope, key)).fetchone()
    seed = old["seed"] if old else ""
    cats_json = (old["categories"] if old is not None else None) or "[]"
    if scope == "shared":
        if "folder" in payload:
            folder = str(payload.get("folder") or "")
            if folder and not con.execute("select 1 from folders where id=?",
                                          (folder,)).fetchone():
                raise RuntimeError("分类文件夹不存在: %s" % folder)
        else:
            folder = (old["folder_id"] if old is not None else "") or ""
    else:
        folder = ""
    con.execute("insert or replace into skill_meta"
                "(scope,key,tool,display_name,note,categories,seed,folder_id,updated_at) "
                "values(?,?,?,?,?,?,?,?,?)",
                (scope, key, tool, display, note, cats_json, seed, folder or None,
                 now_ms()))
    con.commit()
    return {"scope": scope, "key": key, "tool": tool, "display": display,
            "note": note, "folder": folder, "seed": seed, "updated": now_ms()}


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
    """设置 →「清空我的编辑」：显示名 / 备注清空，shared 行归属拉回预置夹。

    不再清表重建（旧实现 force 重建会把用户自建文件夹一起抹掉）：folders
    表不动，用户自建夹保留；shared 行的归属统一回到 seed_category 的预置
    夹，预置夹已被删掉时归属留空 = 未归档。categories 列同样不动（回滚
    兼容）。返回影响行数。
    """
    folder_ids = {r["id"] for r in con.execute("select id from folders")}
    rows = con.execute("select scope, key from skill_meta").fetchall()
    for r in rows:
        folder = ""
        if r["scope"] == "shared":
            seed = seed_category(r["key"])
            folder = seed if seed in folder_ids else ""
        con.execute("update skill_meta set display_name='', note='', folder_id=?, "
                    "updated_at=0 where scope=? and key=?",
                    (folder or None, r["scope"], r["key"]))
    con.commit()
    return len(rows)


def prune_meta(con, boards):
    """磁盘上已经不存在的 skill，把对应 meta 行一起清掉，避免"已编辑"计数虚高。

    快照带 scanDegraded 标记（某个扫描根目录临时不可枚举）时跳过本轮：
    把「暂时看不见」当「已删除」会永久清掉用户的改名 / 备注 / 分类，
    宁可等下一轮扫描完整时再清。
    """
    if boards["meta"].get("scanDegraded"):
        return 0
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
