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
  python skilllocker.py contract [--fix]     检查/修复身份契约（目录名 == 包内声明名）
  python skilllocker.py sync [--tool k] [--fix]  建立/修复分发
  python skilllocker.py import <dir> [--name 名字]  从目录导入
  python skilllocker.py remove <name>        删分发 + 删实体（永久，不入回收站）
  python skilllocker.py seed [--dry]         首启自动建库（--dry 只打印将收的名字与来源）
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
    p.add_argument("cmd", choices=["scan", "status", "verify", "contract", "sync", "import",
                                   "remove", "detect", "seed", "search", "fetch"])
    p.add_argument("arg", nargs="?", help="import 用目录 / remove 用 skill 名 / search 用关键词 / fetch 用 owner/repo/skill")
    p.add_argument("--tool", action="append")
    p.add_argument("--name")
    p.add_argument("--branch")
    p.add_argument("--fix", action="store_true")
    p.add_argument("--dry", action="store_true", help="seed 用：只打印将收入的名字与来源")
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
        before = {r["name"] for r in con.execute("select name from skills")}
        count = rescan(con, cfg)
        after = {r["name"] for r in con.execute("select name from skills")}
        new = sorted(after - before)
        dist = auto_distribute(con, cfg, new)
        print("中央仓库 skill 数:", count)
        if new:
            print("新增 %d 个：%s" % (len(new), "、".join(new)))
        if dist["tools"]:
            print("自动分发 %d 处链接到 %d 个开着的 agent%s" % (
                dist["changed"], len(dist["tools"]),
                "（%d 个 agent 失败，见 events / skilllocker.log）"
                % len(dist["errors"]) if dist["errors"] else ""))
    elif a.cmd == "seed":
        sync_tools_from_adapters(con, cfg)
        if a.dry:
            pre = seed_preview(con, cfg)
            print("候选 %d 行 · 贡献 agent %s · 阈值 %d%s" % (
                pre["candidates"], "/".join(pre["contributors"]) or "无",
                pre["min_agents"],
                "（单 agent 退化）" if pre["single_agent"]
                else ("（无同名交叉，全收）" if pre.get("fallback_all") else "")))
            if pre["blocked"]:
                print("实际不会执行（%s）；下面是按规则会收入的名字："
                      % ", ".join(pre["blocked"]))
            for g in pre["groups"]:
                flag = "将收入" if g["name"] in pre["seeded"] else "跳过（同名不同内容/不可读）"
                print("  %-8s %-32s %s" % (flag, g["name"], "/".join(g["tools"])))
                for it in g["instances"]:
                    print("           - %s: %s" % (it["tool"], it["path"]))
            if not pre["groups"]:
                print("没有可自动收入的候选（各 agent 主目录里没有可收的技能）")
        else:
            got = seed_shared_store(con, cfg)
            if got is None:
                print("未执行自动建库：已跑过 / seed_auto=false / 仓库非空或不可用 / "
                      "反推得出已有仓库（应先接管）/ 没有可收候选")
            else:
                print(seed_note(got))
                print("收入 %d / 跳过 %d / 候选 %d / %dms" % (
                    len(got["seeded"]), len(got["skipped"]),
                    got["candidates"], got["ms"]))
                for s in got["skipped"][:20]:
                    print("  跳过 %s：%s" % (s.get("name"), s.get("reason")))
    
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
        # 链接在不在只说明「分发过」；包内声明名不等于目录名时，agent 注册的
        # 名字和用户看到的就是两套，链接再绿也用不上，所以漂移之外还要报契约。
        cr = contract_rows(con, cfg)
        print("身份契约违规 %d 个（skilllocker contract --fix 可对齐到目录名）" % len(cr))
        for c in cr[:80]:
            print("  %-34s %s" % (c["name"], c["detail"]))
        orph = orphan_links(con, cfg)
        print("孤儿条目 %d 个（agent 目录里存在但加载不了；只报告，本工具不删别人的东西）" % len(orph))
        for c in orph[:80]:
            print("  %-16s %-28s %s" % (c["tool"], c["name"], c["detail"]))
    elif a.cmd == "contract":
        if a.fix:
            got = repair_contract(con, cfg, [a.arg] if a.arg else None, apply_changes=True)
            done = [g for g in got if g["done"]]
            print("已对齐 %d 个包的声明名" % len(done))
            for g in got:
                if g.get("error"):
                    print("  失败 %-32s %s" % (g["skill"], g["error"]))
            left = contract_rows(con, cfg)
            print("剩余违规 %d 个" % len(left))
        else:
            plan = repair_contract(con, cfg, [a.arg] if a.arg else None, apply_changes=False)
            print("违规 %d 个（这是预览，加 --fix 才真正改 SKILL.md）" % len(plan))
            for g in plan:
                print("  %-34s %s" % (g["skill"], g["detail"]))
                if g["shadowed_by"]:
                    print("  %-34s 与 %s 声明同名，按名注册的 agent 里会互相遮蔽"
                          % ("", "/".join(g["shadowed_by"])))
            if not plan:
                print("中央仓库全部满足「目录名 == 包内声明名」")
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
        name = import_dir(con, cfg, a.arg, a.name)
        # 与桌面端同一语义：导入即按各 agent 当前分发开关补齐链接
        dist = auto_distribute(con, cfg, [name])
        print("导入为:", name)
        if dist["tools"]:
            print("已分发 %d 处链接到 %d 个开着的 agent%s" % (
                dist["changed"], len(dist["tools"]),
                "（%d 个 agent 失败，见 events / skilllocker.log）"
                % len(dist["errors"]) if dist["errors"] else ""))
        else:
            print("没有开着的 agent，暂不分发（到智能体页打开分发开关即可）")
    elif a.cmd == "remove":
        if not a.arg:
            raise SystemExit("remove 需要 skill 名")
        print("清掉链接:", remove_skill(con, cfg, a.arg))
    con.close()


if __name__ == "__main__":
    main()
