#!/usr/bin/env python3
"""skillkit — 本地 Agent Skills 管理器（自研，无第三方依赖）

扫描 SKILL.md → 收进中央仓库 → 用 junction / copy 分发到各 agent 的全局 skills 目录。
Windows 下所有删除都先判断 reparse point，绝不顺着链接递归删实体。

模块地图（本文件是对外门面：`import skillkit` 后所有名字仍从 skillkit.X 取，
CLI 也仍是 `python skillkit.py ...`；依赖全部单向，无环）：
  skillkit_base   常量 / SQLite / config / Windows 链接语义 / 内容解析 / 统计 /
                  target_state 状态机基元
  skillkit_store  工具表同步（adapters.json）+ 中央仓库 rescan + 独有技能与
                  agent 看板扫描
  skillkit_links  分发矩阵与执行（matrix / do_sync）、导入 / 撤链 / 删除 /
                  冲突处置
  skillkit_meta   skill_meta（显示名 / 备注 / 分类）播种与读写
  skillkit_boards list_boards 一次性快照与冲突对照

CLI:
  python skillkit.py scan                 重新扫描中央仓库，刷新注册表
  python skillkit.py status               打印 skill x tool 分发矩阵摘要
  python skillkit.py verify               检查漂移（只报告，不改）
  python skillkit.py sync [--tool k] [--fix]  建立/修复分发
  python skillkit.py import <dir> [--name 名字]  从目录导入
  python skillkit.py remove <name>        删分发 + 删实体（永久，不入回收站）
"""
import argparse

from skillkit_base import *  # noqa: F401,F403
from skillkit_base import _file_digest, _hash_tree  # noqa: F401 - 单测 / 排障直接引用
from skillkit_boards import *  # noqa: F401,F403
from skillkit_links import *  # noqa: F401,F403
from skillkit_meta import *  # noqa: F401,F403
from skillkit_store import *  # noqa: F401,F403


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
