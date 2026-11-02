#!/usr/bin/env python3
"""skilllocker — 本地 Agent Skills 管理器（自研，无第三方依赖）

扫描 SKILL.md → 收进中央仓库 → 用 junction / copy 分发到各 agent 的全局 skills 目录。
Windows 下所有删除都先判断 reparse point，绝不顺着链接递归删实体。

模块地图（本文件是对外门面：`import skilllocker` 后所有名字仍从 skilllocker.X 取，
CLI 也仍是 `python skilllocker.py ...`；依赖全部单向，无环）：
  skilllocker_base   常量 / SQLite / config / Windows 链接语义 / 内容解析 / 统计 /
                  target_state 状态机基元
  skilllocker_store  工具表同步（adapters.json）+ 中央仓库 rescan + 独有技能与
                  agent 看板扫描
  skilllocker_links  分发矩阵与执行（matrix / do_sync）、导入 / 撤链 / 删除 /
                  独有转共有（promote_unique）/ 冲突处置
  skilllocker_meta   skill_meta（显示名 / 备注 / 分类）播种与读写
  skilllocker_boards list_boards 一次性快照与冲突对照

CLI:
  python skilllocker.py scan                 重新扫描中央仓库，刷新注册表
  python skilllocker.py status               打印 skill x tool 分发矩阵摘要
  python skilllocker.py verify               检查漂移（只报告，不改）
  python skilllocker.py sync [--tool k] [--fix]  建立/修复分发
  python skilllocker.py import <dir> [--name 名字]  从目录导入
  python skilllocker.py remove <name>        删分发 + 删实体（永久，不入回收站）
  python skilllocker.py search <关键词>       skills.sh 在线检索（只读，不碰库）
  python skilllocker.py fetch <owner/repo/skill 名> [--branch b]
                                          在线获取：下载到 %TEMP%/skilllocker-dl，
                                          不写仓库；随后对打印出的路径用 import 入库
"""
import argparse

from skilllocker_base import *  # noqa: F401,F403
from skilllocker_base import _file_digest, _hash_tree  # noqa: F401 - 单测 / 排障直接引用
from skilllocker_boards import *  # noqa: F401,F403
from skilllocker_links import *  # noqa: F401,F403
from skilllocker_meta import *  # noqa: F401,F403
from skilllocker_store import *  # noqa: F401,F403


def main():
    p = argparse.ArgumentParser(prog="skilllocker")
    p.add_argument("cmd", choices=["scan", "status", "verify", "sync", "import", "remove",
                                   "detect", "search", "fetch"])
    p.add_argument("arg", nargs="?", help="import 用目录 / remove 用 skill 名 / search 用关键词 / fetch 用 owner/repo/skill")
    p.add_argument("--tool", action="append")
    p.add_argument("--name")
    p.add_argument("--branch")
    p.add_argument("--fix", action="store_true")
    a = p.parse_args()
    # 在线检索 / 获取不碰本地库：不建连接、不读 config
    if a.cmd == "search":
        if not a.arg:
            raise SystemExit("search 需要关键词，如：skilllocker search pdf")
        rows = online_search(a.arg)
        if not rows:
            print("skills.sh 没有命中：%s" % a.arg)
        for r in rows:
            print("  %-28s %-36s %9s  %s" % (
                str(r["name"])[:28], str(r.get("source") or "-")[:36],
                r.get("installs") if r.get("installs") is not None else "-", r.get("page_url")))
        return
    if a.cmd == "fetch":
        if not a.arg:
            raise SystemExit("fetch 需要 owner/repo/skill 名，如："
                             "skilllocker fetch anthropics/skills/skill-creator")
        src = a.arg.strip().strip("/")
        name = a.name
        if not name:
            if src.count("/") < 2:
                raise SystemExit("fetch 需要 owner/repo/skill 名（或用 --name 单独给名字）")
            src, name = src.rsplit("/", 1)
        got = online_fetch(src, name, a.branch)
        print("已下载到临时目录（%s@%s，%s 清单，%d 个文件）：%s"
              % (got["source"], got["branch"], got["listing"], got["files"], got["path"]))
        print("入库：python skilllocker.py import \"%s\" --name %s" % (got["path"], got["name"]))
        return
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
