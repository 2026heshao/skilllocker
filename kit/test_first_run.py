#!/usr/bin/env python3
"""首次启动（干净机器）全链路回归 · FIRST-RUN-FIX-PLAN.md §5.1。

隔离仿真一台「全新机器」：临时目录假 HOME（装 2 个 agent）+ 假中央仓库 +
无 config.json / skilllocker.db，走 bootstrap → 开关 → 坏盘符 → 坏 adapters 的
真实代码路径（desktop.Api 桥方法，非绕过）。补丁注意：skilllocker_store.HOME
与 skilllocker_boards.HOME 是 from-import 值绑定，只 patch base 不生效。

python test_first_run.py（无第三方依赖；junction 用例仅 Windows）
"""
import contextlib
import json
import logging
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import skilllocker  # noqa: E402
import skilllocker_base  # noqa: E402
import skilllocker_boards  # noqa: E402
import skilllocker_store  # noqa: E402

# 桌面端 _guard 的异常日志没装配 handler 时会打到 stderr（lastResort），
# 坏路径用例特意触发它：这里静音，别把测试输出淹了
logging.getLogger("skilllocker").addHandler(logging.NullHandler())

CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


ADAPTERS = [
    {"key": "claude_code", "label": "Claude Code",
     "skills_dir": ".claude/skills", "detect_dir": ".claude"},
    {"key": "cursor", "label": "Cursor",
     "skills_dir": ".cursor/skills", "detect_dir": ".cursor"},
]

# 同 ADAPTERS，但 claude_code 还声明一个外部目录（~/.agents/skills）：
# 首启探测必须把它当「agent 自己管的地盘」排除，不能建议接管
ADAPTERS_WITH_EXTRA = [
    {"key": "claude_code", "label": "Claude Code",
     "skills_dir": ".claude/skills", "detect_dir": ".claude",
     "extra_dirs": [".agents/skills"]},
    {"key": "cursor", "label": "Cursor",
     "skills_dir": ".cursor/skills", "detect_dir": ".cursor"},
]


def skill_dir(root, name, body="body"):
    """造一个真的 skill 目录（含 SKILL.md），返回路径。"""
    p = os.path.join(root, name)
    write(os.path.join(p, "SKILL.md"),
          "---\nname: %s\ndescription: %s\n---\n%s\n" % (name, name, body))
    return p


def empty_store(td, name="store"):
    """安装版首启的真实形态：仓库目录存在但一个技能也没有。"""
    p = os.path.normpath(os.path.join(td, name)).replace(os.sep, "/")
    os.makedirs(p.replace("/", os.sep), exist_ok=True)
    return p


def absent_drive():
    """找一个本机不存在的盘符构造「坏 store」用例；全占满时让用例显式失败。"""
    d = next((d for d in "QRSTUVWXYZ" if not os.path.exists(d + ":")), None)
    assert d, "找不到不存在的盘符来构造用例"
    return d


@contextlib.contextmanager
def sandbox(td, home=None, store=None, adapters=None, config=None):
    """全套路径补丁 + 可选 fixtures。store 传字符串可指向坏路径（不创建）；
    adapters/config 传 dict|list 则写入文件，None 则文件缺失。"""
    home = home or os.path.join(td, "home")
    if store is None:
        store = os.path.normpath(os.path.join(td, "store")).replace(os.sep, "/")
        os.makedirs(store, exist_ok=True)
        write(os.path.join(store, "demo-skill", "SKILL.md"),
              "---\nname: demo-skill\ndescription: demo\n---\nbody\n")
    for root, sub in ((".claude", "skills"), (".cursor", "skills")):
        os.makedirs(os.path.join(home, root, sub), exist_ok=True)
    olds = (skilllocker_base.DB_FILE, skilllocker_base.CONFIG_FILE,
            skilllocker_base.ADAPTERS_FILE, skilllocker_base.DEFAULT_STORE,
            skilllocker_base.HOME, skilllocker_store.HOME, skilllocker_boards.HOME)
    skilllocker_base.DB_FILE = os.path.join(td, "skilllocker.db")
    skilllocker_base.CONFIG_FILE = os.path.join(td, "config.json")
    skilllocker_base.ADAPTERS_FILE = os.path.join(td, "adapters.json")
    skilllocker_base.DEFAULT_STORE = store
    skilllocker_base.HOME = home
    skilllocker_store.HOME = home   # from-import 值绑定，必须单独补
    skilllocker_boards.HOME = home  # 同上
    if adapters is not None:  # 传字符串 = 原样写入（构造损坏文件用例）
        write(skilllocker_base.ADAPTERS_FILE,
              adapters if isinstance(adapters, str) else json.dumps(adapters))
    if config is not None:
        write(skilllocker_base.CONFIG_FILE, json.dumps(config))
    try:
        yield home, store
    finally:
        (skilllocker_base.DB_FILE, skilllocker_base.CONFIG_FILE,
         skilllocker_base.ADAPTERS_FILE, skilllocker_base.DEFAULT_STORE,
         skilllocker_base.HOME, skilllocker_store.HOME, skilllocker_boards.HOME) = olds


# ---------------------------------------------------------------- 核心链路
@case("first_run: 全新库 bootstrap 即识别 agent 与 skill，且 enabled=0")
def _():
    if sys.platform != "win32":
        raise AssertionError("仅 Windows（需要 junction）")
    with tempfile.TemporaryDirectory() as td:
        with sandbox(td, adapters=ADAPTERS):
            import desktop
            res = desktop.Api().bootstrap()
            assert res.get("ok"), res
            agents, skills = res["data"]["agents"], res["data"]["skills"]
            assert len(agents) == 2, [a["key"] for a in agents]
            assert all(a["status"] == "off" and not a["enabled"] for a in agents), agents
            assert [s["name"] for s in skills] == ["demo-skill"], skills
            assert len(res["data"]["folders"]) == len(skilllocker.FOLDER_PRESETS)
            # 仓库有内容 → 不该出现首启引导
            assert res["data"].get("firstRun") is None, res["data"].get("firstRun")
            con = skilllocker.db()
            try:
                rows = {r["key"]: r["enabled"] for r in con.execute("select key,enabled from tools")}
                assert rows == {"claude_code": 0, "cursor": 0}, rows
            finally:
                con.close()


@case("first_run: 开关 ON 建 junction 且 target_state=ok-link")
def _():
    if sys.platform != "win32":
        raise AssertionError("仅 Windows（需要 junction）")
    with tempfile.TemporaryDirectory() as td:
        with sandbox(td, adapters=ADAPTERS) as (home, store):
            import desktop
            api = desktop.Api()
            assert api.bootstrap()["ok"]
            res = api.set_agent("claude_code", True)
            assert res.get("ok"), res
            assert res["action"]["changed"] >= 1, res["action"]
            link = os.path.join(home, ".claude", "skills", "demo-skill")
            assert os.path.isdir(link), link
            con = skilllocker.db()
            try:
                cfg = skilllocker.load_config()
                srow = con.execute("select * from skills where name='demo-skill'").fetchone()
                trow = con.execute("select * from tools where key='claude_code'").fetchone()
                state, _ = skilllocker.target_state(
                    os.path.join(trow["skills_dir"], srow["name"]).replace(os.sep, "/"),
                    cfg, srow, trow)
                assert state == "ok-link", state
            finally:
                con.close()
            connected = [a for a in res["data"]["agents"] if a["key"] == "claude_code"][0]
            assert connected["status"] == "connected" and connected["linked"] >= 1, connected


# ---------------------------------------------------------------- 坏路径容错
@case("first_run: store 指向不存在盘符时 load_config 不抛、留痕")
def _():
    bad = "%s:/skilllocker/store" % absent_drive()
    with tempfile.TemporaryDirectory() as td:
        with sandbox(td, store=bad, config={"store": bad}):
            cfg = skilllocker.load_config()  # 不抛即过
            assert cfg["store"] == bad, cfg
            assert "不可创建" in cfg.get("_load_error", ""), cfg


@case("first_run: bootstrap 对坏 store 返回 ok:false（错误页数据源）")
def _():
    bad = "%s:/skilllocker/store" % absent_drive()
    with tempfile.TemporaryDirectory() as td:
        with sandbox(td, store=bad, adapters=ADAPTERS, config={"store": bad}):
            import desktop
            res = desktop.Api().bootstrap()
            assert res.get("ok") is False, res
            assert "中央仓库目录不存在" in res.get("error", ""), res


@case("first_run: config store 空串退回默认仓库，留痕进 payload 与日志（M-4/L-3）")
def _():
    with tempfile.TemporaryDirectory() as td:
        with sandbox(td, adapters=ADAPTERS, config={"store": ""}):
            import desktop
            records = []
            h = logging.Handler()
            h.emit = lambda r: records.append(r.getMessage())  # noqa: E731
            lg = logging.getLogger("skilllocker")
            lg.addHandler(h)
            try:
                res = desktop.Api().bootstrap()
            finally:
                lg.removeHandler(h)
            assert res.get("ok"), res
            # store 已退回默认仓库：skill 照常登记，但留痕必须显性化
            assert [s["name"] for s in res["data"]["skills"]] == ["demo-skill"]
            load_error = res["data"]["meta"].get("loadError")
            assert load_error and "为空或相对根" in load_error, load_error
            assert any("为空或相对根" in m for m in records), records


@case("first_run: 空工具表 + adapters.json 损坏 → 显式报错")
def _():
    with tempfile.TemporaryDirectory() as td:
        with sandbox(td, adapters="{not json"):
            con = skilllocker.db()
            try:
                try:
                    skilllocker.sync_tools_from_adapters(con, skilllocker.load_config())
                    raise AssertionError("空表 + 坏 adapters 应抛 RuntimeError")
                except RuntimeError as exc:
                    assert "adapters.json" in str(exc), exc
            finally:
                con.close()


# ---------------------------------------------------------------- 升级与常量
@case("first_run: 老机器升级 sync_tools 不动 enabled（场景 D）")
def _():
    with tempfile.TemporaryDirectory() as td:
        with sandbox(td, adapters=ADAPTERS) as (home, _store):
            con = skilllocker.db()
            try:
                # 旧库里的行：enabled=1（老版首次入库语义），链接可能存在
                con.execute("insert into tools(key,label,skills_dir,detect_dir,detected,enabled,mode) "
                            "values('claude_code','Claude Code',?, '.claude',1,1,'junction')",
                            (os.path.join(home, ".claude", "skills").replace(os.sep, "/"),))
                con.commit()
                cfg = skilllocker.load_config()
                skilllocker.sync_tools_from_adapters(con, cfg)
                rows = {r["key"]: (r["detected"], r["enabled"])
                        for r in con.execute("select key,detected,enabled from tools")}
                # 老行 enabled 保持 1（update 分支不碰），新行默认 0
                assert rows["claude_code"] == (1, 1), rows
                assert rows["cursor"] == (1, 0), rows
            finally:
                con.close()


@case("first_run: DEFAULT_STORE 以安装目录为基（无 config.json 时）")
def _():
    install_root = os.path.dirname(os.path.abspath(skilllocker_base.KIT))  # kit/ 上一级
    want = os.path.normpath(os.path.join(install_root, "store")).replace(os.sep, "/")
    assert skilllocker_base.DEFAULT_STORE == want, (
        skilllocker_base.DEFAULT_STORE, want)
    assert skilllocker_base.DEFAULT_STORE.endswith("/store")


@case("first_run: 缺 pywebview → main 弹错并退出码 2（场景 C）")
def _():
    import desktop
    reports = []
    old_webview, old_report, old_argv = (desktop.webview, desktop._report, sys.argv)
    desktop.webview = None  # 模拟未安装 pywebview
    desktop._report = reports.append
    sys.argv = ["desktop.py"]
    try:
        rc = desktop.main()
    finally:
        desktop.webview, desktop._report, sys.argv = old_webview, old_report, old_argv
    assert rc == 2, rc
    assert reports and "pywebview" in reports[0], reports


# ---------------------------------------------------------------- 首启接管 / 一键收编
@case("first_run: 自动接管已跑过（或已标记）时回退手动引导块")
def _():
    if sys.platform != "win32":
        raise AssertionError("仅 Windows（需要 junction）")
    with tempfile.TemporaryDirectory() as td:
        store = empty_store(td)
        central = os.path.join(td, "central")
        with sandbox(td, store=store, adapters=ADAPTERS) as (home, _s):
            skill_dir(central, "one")
            skill_dir(central, "two")
            for tool_dir in (".claude", ".cursor"):
                for n in ("one", "two"):
                    skilllocker.make_junction(
                        os.path.join(home, tool_dir, "skills", n),
                        os.path.join(central, n))
            skill_dir(os.path.join(home, ".claude", "skills"), "local-only")
            # 模拟「首启自动接管已经做过一次」：标记存在 → 即使仓库又被清空，
            # 也不再自动改指，把决策权交回用户。手动面板仍要完整可用。
            con = skilllocker.db()
            try:
                skilllocker.log_event(con, skilllocker.FIRST_RUN_ADOPT_EVENT, "test")
                con.commit()
            finally:
                con.close()
            import desktop
            res = desktop.Api().bootstrap()
            assert res.get("ok"), res
            assert res["data"]["skills"] == []  # 空仓库：共有 0（就是用户报的现象）
            fr = res["data"]["firstRun"]
            assert fr and fr["storeEmpty"] is True, fr
            assert fr["candidate"] and fr["candidate"]["skillCount"] == 2, fr
            # 已标记首启动作 → 不再自动接管/建库；收编清单仍含主目录实体与链接
            assert sorted(a["name"] for a in fr["adoptable"]) == \
                ["local-only", "one", "one", "two", "two"], fr["adoptable"]
            assert fr["adoptableAgents"] == ["Claude Code", "Cursor"], fr["adoptableAgents"]
            assert fr["seedBlocked"] == "candidate", fr.get("seedBlocked")
            # 空仓库首启是轻量快照：只登记名字/来源，不扫文件树、不哈希
            assert res["data"]["meta"]["uniqueLight"] is True, res["data"]["meta"]
            con = skilllocker.db()
            try:
                assert not skilllocker.has_first_run_seed(con), "不该再自动建库"
            finally:
                con.close()


@case("first_run: 首启自动接管已有仓库，共有不再是 0")
def _():
    if sys.platform != "win32":
        raise AssertionError("仅 Windows（需要 junction）")
    with tempfile.TemporaryDirectory() as td:
        store = empty_store(td)
        central = os.path.join(td, "central")
        with sandbox(td, store=store, adapters=ADAPTERS) as (home, _s):
            for n in ("one", "two"):
                skill_dir(central, n)
                for tool_dir in (".claude", ".cursor"):
                    skilllocker.make_junction(
                        os.path.join(home, tool_dir, "skills", n),
                        os.path.join(central, n))
            skill_dir(os.path.join(home, ".claude", "skills"), "local-only")
            import desktop
            api = desktop.Api()
            res = api.bootstrap()
            assert res.get("ok"), res
            names = [s["name"] for s in res["data"]["skills"]]
            assert names == ["one", "two"], names
            # 接管成功 → 不再有整块空库面板；还剩可收编的 local-only 时降级为提示条
            fr = res["data"]["firstRun"]
            assert fr and fr["strip"] and not fr["storeEmpty"], fr
            note = res["data"]["meta"].get("seedNote") or ""
            assert "已接管本机已有的中央仓库" in note and "2 个技能" in note, note
            # 接管的是安装目录外的自定义仓库，界面不能继续标「安装目录」
            assert res["data"]["meta"]["storeIn"] == "custom", res["data"]["meta"]
            with open(skilllocker_base.CONFIG_FILE, encoding="utf-8") as fh:
                assert skilllocker.norm_path(json.load(fh)["store"]) == \
                    skilllocker.norm_path(central), fh
            # 链接已认作本仓库分发结果：唯一剩下的独有是本地实体
            assert [u["name"] for u in res["data"]["unique"]] == ["local-only"], \
                res["data"]["unique"]
            con = skilllocker.db()
            try:
                kinds = [r["kind"] for r in con.execute(
                    "select kind from events where kind like 'first-run-%' order by id")]
                assert kinds == ["first-run-adopt"], kinds
            finally:
                con.close()
            # 第二次启动沿用接管结果，不再重复接管
            res2 = api.bootstrap()
            assert res2.get("ok"), res2
            assert [s["name"] for s in res2["data"]["skills"]] == ["one", "two"]
            con = skilllocker.db()
            try:
                n = con.execute("select count(*) from events where kind=?",
                                (skilllocker.FIRST_RUN_ADOPT_EVENT,)).fetchone()[0]
                assert n == 1, n
            finally:
                con.close()


@case("first_run: config.json 显式配过 store 时不自动接管")
def _():
    if sys.platform != "win32":
        raise AssertionError("仅 Windows（需要 junction）")
    with tempfile.TemporaryDirectory() as td:
        store = empty_store(td)
        central = os.path.join(td, "central")
        with sandbox(td, store=store, adapters=ADAPTERS,
                     config={"store": store}) as (home, _s):
            for n in ("one", "two"):
                skill_dir(central, n)
                for tool_dir in (".claude", ".cursor"):
                    skilllocker.make_junction(
                        os.path.join(home, tool_dir, "skills", n),
                        os.path.join(central, n))
            import desktop
            res = desktop.Api().bootstrap()
            assert res.get("ok"), res
            assert res["data"]["skills"] == [], res["data"]["skills"]
            fr = res["data"]["firstRun"]
            assert fr and fr["candidate"] and fr["seedBlocked"] == "candidate", fr
            with open(skilllocker_base.CONFIG_FILE, encoding="utf-8") as fh:
                assert skilllocker.norm_path(json.load(fh)["store"]) == \
                    skilllocker.norm_path(store), fh


@case("first_run: 反推可接管仓库的边界（agent 地盘 / 链接太少都不给候选）")
def _():
    if sys.platform != "win32":
        raise AssertionError("仅 Windows（需要 junction）")
    with tempfile.TemporaryDirectory() as td:
        store = empty_store(td)
        with sandbox(td, store=store, adapters=ADAPTERS_WITH_EXTRA) as (home, _s):
            con = skilllocker.db()
            try:
                cfg = skilllocker.load_config()
                skilllocker.sync_tools_from_adapters(con, cfg)

                def cand():
                    return skilllocker.find_existing_store(con, cfg)

                assert cand() is None, cand()
                # 1) 只有一条链接 → 证据不足（min_links=2）
                one = skill_dir(os.path.join(td, "central"), "solo")
                skilllocker.make_junction(
                    os.path.join(home, ".claude", "skills", "solo"), one)
                assert cand() is None, "单条链接不该当仓库证据"
                # 2) 两条链接指向同一父目录 → 候选成立
                skill_dir(os.path.join(td, "central"), "second")
                skilllocker.make_junction(
                    os.path.join(home, ".cursor", "skills", "second"),
                    os.path.join(td, "central", "second"))
                c = cand()
                assert c and c["skillCount"] == 2 and c["linkCount"] == 2, c
                assert c["store"] == skilllocker.norm_path(os.path.join(td, "central")), c
                # 3) 链接汇聚点落在 agent 自己声明的外部目录 → 绝不接管
                agents_root = os.path.join(home, ".agents", "skills")
                for n in ("ext-a", "ext-b"):
                    skill_dir(agents_root, n)
                    skilllocker.make_junction(
                        os.path.join(home, ".claude", "skills", n),
                        os.path.join(agents_root, n))
                c2 = cand()
                assert c2["store"] != skilllocker.norm_path(agents_root), c2
            finally:
                con.close()


@case("first_run: adopt_store 改指 + 落盘 config + 登记技能，独有行归零")
def _():
    if sys.platform != "win32":
        raise AssertionError("仅 Windows（需要 junction）")
    with tempfile.TemporaryDirectory() as td:
        store = empty_store(td)
        # 手工接管入口：关掉首启自动接管，模拟用户明确要自己选仓库
        with sandbox(td, store=store, adapters=ADAPTERS,
                     config={"seed_auto": False}) as (home, _s):
            central = os.path.join(td, "central")
            for n in ("one", "two", "three"):
                skill_dir(central, n)
                for tool_dir in (".claude", ".cursor"):
                    skilllocker.make_junction(
                        os.path.join(home, tool_dir, "skills", n),
                        os.path.join(central, n))
            import desktop
            api = desktop.Api()
            assert api.bootstrap()["ok"]
            got = api.adopt_store(central)
            assert got.get("ok"), got
            res = api.bootstrap()  # 下一次启动必须沿用被接管的仓库
            assert res.get("ok"), res
            assert [s["name"] for s in res["data"]["skills"]] == ["one", "three", "two"], \
                [s["name"] for s in res["data"]["skills"]]
            assert res["data"]["unique"] == [], res["data"]["unique"]  # 链接已指向仓库
            assert res["data"]["firstRun"] is None, res["data"]["firstRun"]
            with open(skilllocker_base.CONFIG_FILE, encoding="utf-8") as fh:
                assert skilllocker.norm_path(json.load(fh)["store"]) == \
                    skilllocker.norm_path(central), fh
            con = skilllocker.db()
            try:
                kinds = {r["kind"] for r in
                         con.execute("select kind from events where kind in ('adopt-store','scan')")}
                assert "adopt-store" in kinds, kinds
            finally:
                con.close()


@case("first_run: adopt_store 守卫（非空仓库 / 无 SKILL.md / agent 目录 / 自己）")
def _():
    if sys.platform != "win32":
        raise AssertionError("仅 Windows（需要 junction）")
    with tempfile.TemporaryDirectory() as td:
        store = empty_store(td)
        with sandbox(td, store=store, adapters=ADAPTERS,
                     config={"seed_auto": False}) as (home, _s):
            central = os.path.join(td, "central")
            skill_dir(central, "one")
            skill_dir(central, "two")
            skilllocker.make_junction(
                os.path.join(home, ".claude", "skills", "one"),
                os.path.join(central, "one"))
            skilllocker.make_junction(
                os.path.join(home, ".cursor", "skills", "two"),
                os.path.join(central, "two"))
            import desktop
            api = desktop.Api()
            assert api.bootstrap()["ok"]
            bad = api.adopt_store(os.path.join(td, "not-here"))
            assert not bad.get("ok") and "不存在" in bad["error"], bad
            empty_dir = os.path.join(td, "nothing")
            os.makedirs(empty_dir, exist_ok=True)
            bad = api.adopt_store(empty_dir)
            assert not bad.get("ok") and "SKILL.md" in bad["error"], bad
            bad = api.adopt_store(os.path.join(home, ".claude", "skills"))
            assert not bad.get("ok") and "agent 自己的技能目录" in bad["error"], bad
            good = api.adopt_store(central)
            assert good.get("ok"), good
            bad = api.adopt_store(central)  # 已经是当前仓库
            assert not bad.get("ok") and "已经是当前中央仓库" in bad["error"], bad
            # 仓库已有内容后不再允许首启改指（防共有 meta 被 prune 误清）
            other = os.path.join(td, "another")
            skill_dir(other, "x")
            skill_dir(other, "y")
            bad = api.adopt_store(other)
            assert not bad.get("ok") and "空仓库" in bad["error"], bad


@case("first_run: adopt_local_skills 收 main 实体与真身可读链接，extra 不收")
def _():
    if sys.platform != "win32":
        raise AssertionError("仅 Windows（需要 junction）")
    with tempfile.TemporaryDirectory() as td:
        store = empty_store(td)
        with sandbox(td, store=store, adapters=ADAPTERS_WITH_EXTRA,
                     config={"seed_auto": False}) as (home, _s):
            cl = os.path.join(home, ".claude", "skills")
            cu = os.path.join(home, ".cursor", "skills")
            skill_dir(cl, "alpha")
            skill_dir(cu, "alpha")               # 同名同内容：只收一份
            skill_dir(cl, "beta")
            write(os.path.join(skill_dir(cu, "beta"), "SKILL.md"),
                  "---\nname: beta\n---\n另一版\n")  # 同名不同内容：跳过
            skill_dir(os.path.join(home, ".agents", "skills"), "ext-skill")
            outside = skill_dir(os.path.join(td, "elsewhere"), "linked-skill")
            skilllocker.make_junction(os.path.join(cl, "linked-skill"), outside)
            import desktop
            api = desktop.Api()
            boot = api.bootstrap()
            assert boot.get("ok"), boot
            assert sorted(a["name"] for a in boot["data"]["firstRun"]["adoptable"]) == \
                ["alpha", "alpha", "beta", "beta", "linked-skill"], \
                boot["data"]["firstRun"]["adoptable"]
            res = api.adopt_local_skills()
            assert res.get("ok"), res
            act = res["action"]
            assert sorted(act["adopted"]) == ["alpha", "beta", "linked-skill"], act
            assert sorted(s["name"] for s in res["data"]["skills"]) == \
                ["alpha", "beta", "linked-skill"], res["data"]["skills"]
            assert act["skipped"] and "内容不同" in act["skipped"][0]["reason"], act["skipped"]
            names = {u["name"] for u in res["data"]["unique"]}
            assert names == {"beta", "ext-skill"}, names
            kinds = {(u["src"], u["kind"]) for u in res["data"]["unique"]}
            assert ("main", "unique") not in kinds, kinds          # 可收编实体已全部进仓库
            assert (".agents/skills", "unique") in kinds, kinds    # 外部目录只展示不收
            assert ("main", "foreign-link") not in kinds, kinds    # 真身可读的链接已按实体收
            assert ("main", "overridden") in kinds, kinds          # 同名不同内容留给人工处置
            assert res["data"]["firstRun"] is None, res["data"]["firstRun"]
            # 非破坏：源副本 / 第三方真身都还在（两个 agent 的分发开关都是关的）
            assert os.path.isdir(os.path.join(cl, "alpha")) and not skilllocker.is_reparse(
                os.path.join(cl, "alpha")), "开关关着不该改写 agent 目录"
            assert os.path.isdir(os.path.join(cu, "alpha"))
            assert os.path.isfile(os.path.join(outside, "SKILL.md")), "第三方真身不得改动"
            assert os.path.isfile(os.path.join(store.replace("/", os.sep), "beta", "SKILL.md"))


@case("first_run: 收编迁移改名备注，并对开着分发的 agent 对齐成链接")
def _():
    if sys.platform != "win32":
        raise AssertionError("仅 Windows（需要 junction）")
    with tempfile.TemporaryDirectory() as td:
        store = empty_store(td)
        with sandbox(td, store=store, adapters=ADAPTERS,
                     config={"seed_auto": False}) as (home, _s):
            cl = os.path.join(home, ".claude", "skills")
            cu = os.path.join(home, ".cursor", "skills")
            skill_dir(cl, "alpha")
            skill_dir(cl, "beta")
            skill_dir(cu, "gamma")
            import desktop
            api = desktop.Api()
            assert api.bootstrap()["ok"]
            con = skilllocker.db()
            try:
                skilllocker.set_meta(con, "unique", "claude_code/beta",
                                     {"display": "我的小技能", "note": "备注保留"})
                con.execute("update tools set enabled=1 where key='claude_code'")
                con.commit()
            finally:
                con.close()
            res = api.adopt_local_skills()
            assert res.get("ok"), res
            act = res["action"]
            assert sorted(act["adopted"]) == ["alpha", "beta", "gamma"], act
            assert act["metaMoved"] == 1, act
            assert res["meta"].get("beta", {}).get("display") == "我的小技能", res["meta"]
            assert act["relinked"] == 2, act     # claude 开着：它自己那两份同名实体转成链接
            assert act["linked"] == 0, act       # gamma 不是 claude 贡献的：收编不越权替它分发
            for n in ("alpha", "beta"):
                p = os.path.join(cl, n)
                assert skilllocker.is_reparse(p), p
                assert skilllocker.link_target(p) == skilllocker.norm_path(
                    os.path.join(store, n)), p
            assert os.path.isdir(os.path.join(cu, "gamma")) and not skilllocker.is_reparse(
                os.path.join(cu, "gamma")), "cursor 开关关着，源副本必须保留"
            assert not os.path.lexists(os.path.join(cl, "gamma")),                 "claude 没贡献 gamma，收编不该给它新建链接（分发是开关的事）"
            assert res["data"]["unique"] == [], res["data"]["unique"]


@case("first_run: 容器展开行不进批量收编，也不会被补成重复加载路径")
def _():
    if sys.platform != "win32":
        raise AssertionError("仅 Windows（需要 junction）")
    with tempfile.TemporaryDirectory() as td:
        store = empty_store(td)
        with sandbox(td, store=store, adapters=ADAPTERS,
                     config={"seed_auto": False}) as (home, _s):
            cl = os.path.join(home, ".claude", "skills")
            skill_dir(os.path.join(cl, ".system"), "nested")    # 容器子目录（非直属）
            skill_dir(cl, "plain")                              # 主目录直属
            import desktop
            api = desktop.Api()
            assert api.bootstrap()["ok"]
            boot = api.bootstrap()
            names = sorted(a["name"] for a in boot["data"]["firstRun"]["adoptable"])
            assert names == ["plain"], boot["data"]["firstRun"]["adoptable"]
            con = skilllocker.db()
            try:
                con.execute("update tools set enabled=1 where key='claude_code'")
                con.commit()
            finally:
                con.close()
            res = api.adopt_local_skills()
            assert res.get("ok"), res
            act = res["action"]
            assert act["adopted"] == ["plain"], act
            assert act["relinked"] == 1 and act["linked"] == 0, act
            p = os.path.join(cl, "plain")
            assert skilllocker.is_reparse(p), p
            assert skilllocker.link_target(p) == skilllocker.norm_path(os.path.join(store, "plain")), p
            # 容器那份：不入库、不新建 skills_dir/nested（否则同一 agent 两份同名）
            assert not os.path.lexists(os.path.join(cl, "nested")), \
                "容器行不该被补成直属链接，会让 Claude Code 从两个路径加载 nested"
            assert os.path.isdir(os.path.join(cl, ".system", "nested"))
            uniq = [u["name"] for u in res["data"]["unique"]]
            assert uniq == [".system/nested"], uniq


# ---------------------------------------------------------------- 首启自动建库
@case("seed: 空仓库首启自动收入两个 agent 的共有技能，源目录不动且幂等")
def _():
    if sys.platform != "win32":
        raise AssertionError("仅 Windows（需要 junction）")
    with tempfile.TemporaryDirectory() as td:
        store = empty_store(td)
        with sandbox(td, store=store, adapters=ADAPTERS) as (home, _s):
            cl = os.path.join(home, ".claude", "skills")
            cu = os.path.join(home, ".cursor", "skills")
            skill_dir(cl, "alpha")
            skill_dir(cu, "alpha")
            skill_dir(cl, "only-cl")
            skill_dir(cu, "only-cu")
            import desktop
            api = desktop.Api()
            res = api.bootstrap()
            assert res.get("ok"), res
            data = res["data"]
            assert [s["name"] for s in data["skills"]] == ["alpha"], data["skills"]
            assert {u["name"] for u in data["unique"]} == {"only-cl", "only-cu"}, data["unique"]
            assert data["meta"]["uniqueLogical"] == 2, data["meta"]
            assert data["meta"]["uniqueCount"] == 2, data["meta"]
            note = data["meta"].get("seedNote") or ""
            assert "已自动建立中央仓库" in note and "1 个共有技能" in note, note
            assert data["firstRun"] and data["firstRun"]["strip"] is True, data["firstRun"]
            assert not skilllocker.is_reparse(os.path.join(cl, "alpha")), \
                "首启开关全关，不得改写 agent 目录"
            con = skilllocker.db()
            try:
                rows = con.execute("select detail from events where kind='first-run-seed'").fetchall()
                assert len(rows) == 1, rows
                assert "收入 1" in rows[0]["detail"], rows[0]["detail"]
            finally:
                con.close()
            # 第二次启动不重跑、不重复提示
            res2 = api.bootstrap()
            assert res2["data"]["meta"].get("seedNote") is None, res2["data"]["meta"]
            con = skilllocker.db()
            try:
                assert con.execute("select count(*) from events where kind='first-run-seed'"
                                   ).fetchone()[0] == 1
            finally:
                con.close()


@case("seed: 单 agent 退化收全部实体，文案如实说明")
def _():
    if sys.platform != "win32":
        raise AssertionError("仅 Windows（需要 junction）")
    with tempfile.TemporaryDirectory() as td:
        store = empty_store(td)
        with sandbox(td, store=store, adapters=[ADAPTERS[0]]) as (home, _s):
            import shutil
            shutil.rmtree(os.path.join(home, ".cursor"), ignore_errors=True)
            cl = os.path.join(home, ".claude", "skills")
            for n in ("s1", "s2", "s3"):
                skill_dir(cl, n)
            import desktop
            res = desktop.Api().bootstrap()
            assert res.get("ok"), res
            assert sorted(s["name"] for s in res["data"]["skills"]) == ["s1", "s2", "s3"], \
                res["data"]["skills"]
            assert res["data"]["unique"] == [], res["data"]["unique"]
            note = res["data"]["meta"].get("seedNote") or ""
            assert "只检测到 1 个 agent" in note, note
            assert not skilllocker.is_reparse(os.path.join(cl, "s1"))


@case("seed: 同名不同内容不选边，跳过并留标记")
def _():
    with tempfile.TemporaryDirectory() as td:
        store = empty_store(td)
        with sandbox(td, store=store, adapters=ADAPTERS) as (home, _s):
            cl = os.path.join(home, ".claude", "skills")
            cu = os.path.join(home, ".cursor", "skills")
            skill_dir(cl, "pdf", body="A")
            skill_dir(cu, "pdf", body="B")
            import desktop
            res = desktop.Api().bootstrap()
            assert res.get("ok"), res
            assert res["data"]["skills"] == [], res["data"]["skills"]
            groups = res["data"]["unique_groups"]
            assert len(groups) == 2 and all(g["name"] == "pdf" for g in groups), groups
            assert res["data"]["meta"]["uniqueLogical"] == 2, res["data"]["meta"]
            note = res["data"]["meta"].get("seedNote") or ""
            assert "同名副本内容不同" in note and "一键收编" in note, note
            con = skilllocker.db()
            try:
                row = con.execute("select detail from events where kind='first-run-seed'").fetchone()
                assert row and "跳过" in row["detail"], row
            finally:
                con.close()


@case("seed: 多 agent 技能完全不重叠时退化为全收，共有不再为 0")
def _():
    with tempfile.TemporaryDirectory() as td:
        store = empty_store(td)
        with sandbox(td, store=store, adapters=ADAPTERS) as (home, _s):
            skill_dir(os.path.join(home, ".claude", "skills"), "only-a")
            skill_dir(os.path.join(home, ".cursor", "skills"), "only-b")
            import desktop
            res = desktop.Api().bootstrap()
            assert res.get("ok"), res
            assert sorted(s["name"] for s in res["data"]["skills"]) == \
                ["only-a", "only-b"], res["data"]["skills"]
            assert res["data"]["unique"] == [], res["data"]["unique"]
            assert res["data"]["firstRun"] is None, res["data"]["firstRun"]
            note = res["data"]["meta"].get("seedNote") or ""
            assert "没有同名交叉" in note and "2 个技能" in note, note


@case("seed: 清空仓库后重启不灌回（once-only）")
def _():
    with tempfile.TemporaryDirectory() as td:
        store = empty_store(td)
        with sandbox(td, store=store, adapters=ADAPTERS) as (home, _s):
            cl = os.path.join(home, ".claude", "skills")
            cu = os.path.join(home, ".cursor", "skills")
            skill_dir(cl, "alpha")
            skill_dir(cu, "alpha")
            import shutil
            import desktop
            api = desktop.Api()
            assert api.bootstrap()["ok"]
            shutil.rmtree(os.path.join(store.replace("/", os.sep), "alpha"))
            con = skilllocker.db()
            try:
                con.execute("delete from skills")
                con.commit()
            finally:
                con.close()
            res = api.bootstrap()
            assert res.get("ok"), res
            assert res["data"]["skills"] == [], res["data"]["skills"]
            assert res["data"]["meta"].get("seedNote") is None, res["data"]["meta"]
            con = skilllocker.db()
            try:
                assert con.execute("select count(*) from events where kind='first-run-seed'"
                                   ).fetchone()[0] == 1
            finally:
                con.close()


@case("seed: 数量上限触顶仍打标记，剩余数写进提示")
def _():
    with tempfile.TemporaryDirectory() as td:
        store = empty_store(td)
        with sandbox(td, store=store, adapters=[ADAPTERS[0]],
                     config={"seed_max_skills": 2}) as (home, _s):
            import shutil
            shutil.rmtree(os.path.join(home, ".cursor"), ignore_errors=True)
            cl = os.path.join(home, ".claude", "skills")
            for n in ("s1", "s2", "s3"):
                skill_dir(cl, n)
            import desktop
            res = desktop.Api().bootstrap()
            assert res.get("ok"), res
            assert len(res["data"]["skills"]) == 2, res["data"]["skills"]
            note = res["data"]["meta"].get("seedNote") or ""
            assert "数量上限" in note and "剩余 1" in note, note
            con = skilllocker.db()
            try:
                row = con.execute("select detail from events where kind='first-run-seed'").fetchone()
                assert row and "截断" in row["detail"], row
            finally:
                con.close()


@case("seed: seed_auto=false 退回引导面板且不写标记")
def _():
    with tempfile.TemporaryDirectory() as td:
        store = empty_store(td)
        with sandbox(td, store=store, adapters=ADAPTERS,
                     config={"seed_auto": False}) as (home, _s):
            skill_dir(os.path.join(home, ".claude", "skills"), "alpha")
            skill_dir(os.path.join(home, ".cursor", "skills"), "alpha")
            import desktop
            res = desktop.Api().bootstrap()
            assert res.get("ok"), res
            assert res["data"]["skills"] == [], res["data"]["skills"]
            fr = res["data"]["firstRun"]
            assert fr and fr["storeEmpty"] and fr["seedBlocked"] == "disabled", fr
            con = skilllocker.db()
            try:
                assert not skilllocker.has_first_run_seed(con)
            finally:
                con.close()


@case("seed: extra / 容器行不入选，foreign-link 按真身入种子")
def _():
    if sys.platform != "win32":
        raise AssertionError("仅 Windows（需要 junction）")
    with tempfile.TemporaryDirectory() as td:
        store = empty_store(td)
        with sandbox(td, store=store, adapters=ADAPTERS_WITH_EXTRA) as (home, _s):
            cl = os.path.join(home, ".claude", "skills")
            cu = os.path.join(home, ".cursor", "skills")
            skill_dir(cl, "alpha")
            skill_dir(cu, "alpha")
            skill_dir(os.path.join(home, ".agents", "skills"), "ext-skill")
            skill_dir(os.path.join(cl, ".system"), "nested")
            outside = skill_dir(os.path.join(td, "elsewhere"), "linked-skill")
            skilllocker.make_junction(os.path.join(cl, "linked-skill"), outside)
            skilllocker.make_junction(os.path.join(cu, "linked-skill"), outside)
            import desktop
            res = desktop.Api().bootstrap()
            assert res.get("ok"), res
            assert sorted(s["name"] for s in res["data"]["skills"]) == \
                ["alpha", "linked-skill"], res["data"]["skills"]
            assert {u["name"] for u in res["data"]["unique"]} == \
                {".system/nested", "ext-skill"}, res["data"]["unique"]
            with open(os.path.join(store.replace("/", os.sep), "linked-skill", "SKILL.md"),
                      encoding="utf-8") as fh:
                assert "name: linked-skill" in fh.read()
            # 开关全关：两个 agent 的链接仍指向第三方真身，真身不被改动
            for root in (cl, cu):
                p = os.path.join(root, "linked-skill")
                assert skilllocker.is_reparse(p), p
                assert skilllocker.link_target(p) == skilllocker.norm_path(outside), p
            assert os.path.isfile(os.path.join(outside, "SKILL.md"))


@case("SEED-08: 冻结版仓库在安装目录；不可写时空库回退 LOCALAPPDATA")
def _():
    install = os.path.join("D:\\", "Apps", "SkillLocker")
    res_dir = os.path.join(install, "_internal")
    assert skilllocker_base.default_store_for(res_dir, True) == \
        os.path.normpath(os.path.join(install, "store")).replace(os.sep, "/")
    assert skilllocker_base.default_store_for(skilllocker_base.KIT, False) == \
        os.path.normpath(os.path.join(os.path.dirname(skilllocker_base.KIT), "store")
                         ).replace(os.sep, "/")
    # 安装目录不可写（不存在盘符）+ 没有 config.json + 空库 → 回退 DATA_DIR\store
    old_default, old_data = skilllocker_base.DEFAULT_STORE, skilllocker_base.DATA_DIR
    old_cfg = skilllocker_base.CONFIG_FILE
    with tempfile.TemporaryDirectory() as td:
        bad = "%s:/SkillLocker/store" % absent_drive()
        skilllocker_base.DEFAULT_STORE = bad
        skilllocker_base.DATA_DIR = os.path.join(td, "data")
        skilllocker_base.CONFIG_FILE = os.path.join(td, "no-config.json")
        try:
            cfg = skilllocker.load_config()
        finally:
            skilllocker_base.DEFAULT_STORE = old_default
            skilllocker_base.DATA_DIR = old_data
            skilllocker_base.CONFIG_FILE = old_cfg
        want = os.path.normpath(os.path.join(td, "data", "store")).replace(os.sep, "/")
        assert cfg["store"] == want, cfg
        assert cfg["storeIn"] == "localappdata", cfg
        assert "不可写" in cfg.get("_load_error", ""), cfg


@case("SEED-09: 同名同内容聚合为一组，同名不同内容分组不合并")
def _():
    with tempfile.TemporaryDirectory() as td:
        store = empty_store(td)
        with sandbox(td, store=store, adapters=ADAPTERS,
                     config={"seed_auto": False}) as (home, _s):
            # 仓库非空（正常库形态）才会走完整扫描 / 精确聚合；空仓库首启是
            # 轻量快照（不统计文件、不哈希），见 bootstrap 的 uniqueLight。
            write(os.path.join(store.replace("/", os.sep), "demo-skill", "SKILL.md"),
                  "---\nname: demo-skill\n---\nbody\n")
            cl = os.path.join(home, ".claude", "skills")
            cu = os.path.join(home, ".cursor", "skills")
            skill_dir(cl, "brandkit")
            skill_dir(cu, "brandkit")
            skill_dir(cl, "pdf", body="A")
            skill_dir(cu, "pdf", body="B")
            import desktop
            res = desktop.Api().bootstrap()
            assert res.get("ok"), res
            groups = res["data"]["unique_groups"]
            by = {}
            for g in groups:
                by.setdefault(g["name"], []).append(g)
            assert len(by["brandkit"]) == 1 and by["brandkit"][0]["count"] == 2, by
            assert len(by["pdf"]) == 2 and all(g["count"] == 1 for g in by["pdf"]), by
            assert res["data"]["meta"]["uniqueLogical"] == 3, res["data"]["meta"]
            assert res["data"]["meta"]["uniqueCount"] == 4, res["data"]["meta"]
            keys = [g["key"] for g in groups]
            assert len(keys) == len(set(keys)), keys


@case("SEED-10: 收编真身判定（断链 / 仓库内 / 非链接 / 缺 SKILL.md）")
def _():
    if sys.platform != "win32":
        raise AssertionError("仅 Windows（需要 junction）")
    with tempfile.TemporaryDirectory() as td:
        store = os.path.join(td, "store")
        os.makedirs(os.path.join(store, "inside"))
        write(os.path.join(store, "inside", "SKILL.md"), "---\nname: inside\n---\n")
        agent = os.path.join(td, "agent")
        os.makedirs(agent)
        inside_link = os.path.join(agent, "inside")
        skilllocker.make_junction(inside_link, os.path.join(store, "inside"))
        broken = os.path.join(agent, "broken")
        gone = skill_dir(os.path.join(td, "gone"), "broken")
        skilllocker.make_junction(broken, gone)
        import shutil
        shutil.rmtree(gone)
        no_md = os.path.join(agent, "no-md")
        os.makedirs(no_md)
        cfg = {"store": store.replace(os.sep, "/")}
        cases = [
            ({"kind": "foreign-link", "name": "inside",
              "path": inside_link.replace(os.sep, "\\")}, "本仓库内"),
            ({"kind": "foreign-link", "name": "broken",
              "path": broken.replace(os.sep, "\\")}, "断链"),
            ({"kind": "foreign-link", "name": "no-md",
              "path": no_md.replace(os.sep, "\\")}, "实体目录"),
            ({"kind": "unique", "name": "no-md",
              "path": no_md.replace(os.sep, "\\")}, "缺 SKILL.md"),
        ]
        for row, frag in cases:
            try:
                skilllocker.resolve_adopt_target(None, cfg, row)
                raise AssertionError("%r 应被拒绝" % (row.get("name"),))
            except RuntimeError as exc:
                assert frag in str(exc), (row, exc)


# ---------------------------------------------------------------- 本轮 review 回归
@case("bootstrap: 扫描降级时保留 scanDegraded，不误清暂时看不见的 meta")
def _():
    with tempfile.TemporaryDirectory() as td:
        with sandbox(td, adapters=ADAPTERS) as (home, _s):
            cl = os.path.join(home, ".claude", "skills")
            cu = os.path.join(home, ".cursor", "skills")
            skill_dir(cl, "alpha")
            skill_dir(cu, "beta")
            import desktop
            api = desktop.Api()
            first = api.bootstrap()
            assert first.get("ok"), first
            assert not first["data"]["meta"].get("scanDegraded"), first["data"]["meta"]
            saved = api.save_meta("unique", "cursor/beta",
                                  {"display": "我的 beta", "note": "keep me"})
            assert saved.get("ok"), saved
            # cursor 的 skills 目录暂时不可见（可移动盘拔出 / 网络目录掉线的同构场景）
            hidden = cu + "-hidden"
            os.rename(cu, hidden)
            try:
                second = api.bootstrap()
            finally:
                os.rename(hidden, cu)
            assert second.get("ok"), second
            # 复用了 seed_meta 的扫描结果，但扫描错误必须一起带回来
            assert second["data"]["meta"].get("scanDegraded"), \
                "bootstrap 丢了扫描错误，prune_meta 会把暂时看不见的行当已删除"
            con = skilllocker.db()
            try:
                row = con.execute(
                    "select display_name, note from skill_meta "
                    "where scope='unique' and key='cursor/beta'").fetchone()
            finally:
                con.close()
            assert row and row["display_name"] == "我的 beta" and row["note"] == "keep me", row


@case("seed: 全部拷入失败不写一次性标记，下次启动重试")
def _():
    with tempfile.TemporaryDirectory() as td:
        store = empty_store(td)
        with sandbox(td, store=store, adapters=ADAPTERS) as (home, _s):
            skill_dir(os.path.join(home, ".claude", "skills"), "alpha")
            skill_dir(os.path.join(home, ".cursor", "skills"), "alpha")
            import desktop
            import skilllocker_links
            orig = skilllocker_links.copy_tree_atomic

            def boom(src, dst):
                raise OSError(13, "Access is denied (simulated)")

            skilllocker_links.copy_tree_atomic = boom
            try:
                first = desktop.Api().bootstrap()
            finally:
                skilllocker_links.copy_tree_atomic = orig
            assert first.get("ok"), first
            assert first["data"]["skills"] == [], first["data"]["skills"]
            note = first["data"]["meta"].get("seedNote") or ""
            assert "建库失败" in note and "没有找到" not in note, note
            con = skilllocker.db()
            try:
                assert not skilllocker.has_first_run_seed(con), "全失败不该烧一次性标记"
            finally:
                con.close()
            second = desktop.Api().bootstrap()
            assert second.get("ok"), second
            assert [s["name"] for s in second["data"]["skills"]] == ["alpha"], \
                second["data"]["skills"]


@case("first_run: 自动接管后清空仓库不会被自动建库灌回")
def _():
    if sys.platform != "win32":
        raise AssertionError("仅 Windows（需要 junction）")
    with tempfile.TemporaryDirectory() as td:
        store = empty_store(td)
        central = os.path.join(td, "central")
        with sandbox(td, store=store, adapters=ADAPTERS) as (home, _s):
            for n in ("one", "two"):
                skill_dir(central, n)
                for d in (".claude", ".cursor"):
                    skilllocker.make_junction(
                        os.path.join(home, d, "skills", n), os.path.join(central, n))
            # 接管之后 agent 里仍有实体 foo：清库后它还是「两个 agent 都有」的候选
            for d in (".claude", ".cursor"):
                skill_dir(os.path.join(home, d, "skills"), "foo")
            import shutil
            import desktop
            api = desktop.Api()
            first = api.bootstrap()
            assert first.get("ok"), first
            assert sorted(s["name"] for s in first["data"]["skills"]) == ["one", "two"], \
                first["data"]["skills"]
            # 模拟用户在界面里删光共有技能（摘链接 + 删中央实体），foo 保留
            for n in ("one", "two"):
                for d in (".claude", ".cursor"):
                    p = os.path.join(home, d, "skills", n)
                    if os.path.lexists(p):
                        os.rmdir(p)
                shutil.rmtree(os.path.join(central, n), ignore_errors=True)
            con = skilllocker.db()
            try:
                con.execute("delete from skills")
                con.commit()
            finally:
                con.close()
            second = api.bootstrap()
            assert second.get("ok"), second
            assert second["data"]["skills"] == [], "接管后清库不该再被自动建库灌回"
            con = skilllocker.db()
            try:
                kinds = [r["kind"] for r in con.execute(
                    "select kind from events where kind like 'first-run-%' order by id")]
            finally:
                con.close()
            assert kinds == [skilllocker.FIRST_RUN_ADOPT_EVENT], kinds


@case("first_run: 轻量快照只用于首次启动，空仓库后续启动走完整扫描")
def _():
    with tempfile.TemporaryDirectory() as td:
        store = empty_store(td)
        with sandbox(td, store=store, adapters=ADAPTERS,
                     config={"seed_auto": False}) as (home, _s):
            for d in (".claude", ".cursor"):
                skill_dir(os.path.join(home, d, "skills"), "brandkit")
            import desktop
            api = desktop.Api()
            first = api.bootstrap()
            assert first.get("ok"), first
            m1 = first["data"]["meta"]
            assert m1["uniqueLight"] is True, m1
            assert m1["uniqueLogical"] == 1, m1      # 轻量也按内容合并重名副本
            assert m1["uniqueCount"] == 2, m1
            second = api.bootstrap()
            assert second.get("ok"), second
            m2 = second["data"]["meta"]
            assert m2["uniqueLight"] is False, m2
            assert m2["uniqueLogical"] == 1 and m2["uniqueCount"] == 2, m2
            assert all(u["files"] > 0 for u in second["data"]["unique"]), \
                second["data"]["unique"]


# ---------------------------------------------------------------- 新技能自动分发
# 用户语义：分发开关是持续状态，不是一次性动作。开关开着的 agent 应该收到
# 之后新入库的 skill；开关关着的原样不动。旧实现 import / rescan 只登记不
# 分发，必须手动点同步或重新开关一次，新技能才会出现在 agent 目录里。
@case("import: 新 skill 自动分发给开关开着的 agent，关着的不动")
def _():
    if sys.platform != "win32":
        raise AssertionError("仅 Windows（需要 junction）")
    with tempfile.TemporaryDirectory() as td:
        store = empty_store(td)
        with sandbox(td, store=store, adapters=ADAPTERS,
                     config={"seed_auto": False}) as (home, _s):
            import desktop
            api = desktop.Api()
            assert api.bootstrap()["ok"]
            con = skilllocker.db()
            try:
                con.execute("update tools set enabled=1 where key='claude_code'")
                con.commit()
            finally:
                con.close()
            src = skill_dir(os.path.join(td, "incoming"), "newbie")
            res = api.import_skill(src, "newbie", "")
            assert res.get("ok"), res
            act = res.get("action") or {}
            assert act.get("distributed") == 1, act
            link = os.path.join(home, ".claude", "skills", "newbie")
            assert skilllocker.is_reparse(link), "开关开着的 agent 应自动收到链接"
            assert skilllocker.link_target(link) == skilllocker.norm_path(
                os.path.join(store.replace("/", os.sep), "newbie")), \
                skilllocker.link_target(link)
            assert not os.path.lexists(os.path.join(home, ".cursor", "skills", "newbie")), \
                "开关关着的 agent 不得被写入"


@case("rescan: store 外部新增的 skill 登记后自动分发给开关开着的 agent")
def _():
    if sys.platform != "win32":
        raise AssertionError("仅 Windows（需要 junction）")
    with tempfile.TemporaryDirectory() as td:
        store = empty_store(td)
        with sandbox(td, store=store, adapters=ADAPTERS,
                     config={"seed_auto": False}) as (home, _s):
            import desktop
            api = desktop.Api()
            assert api.bootstrap()["ok"]
            con = skilllocker.db()
            try:
                con.execute("update tools set enabled=1 where key='claude_code'")
                con.commit()
            finally:
                con.close()
            # 用户把技能目录直接放进中央仓库（拖拽 / 别的管理器 / 解压）
            skill_dir(store, "dropped")
            res = api.rescan()
            assert res.get("ok"), res
            dist = res.get("distributed") or {}
            assert dist.get("skills") == ["dropped"], res
            assert dist.get("changed") == 1, res
            link = os.path.join(home, ".claude", "skills", "dropped")
            assert skilllocker.is_reparse(link), "扫描登记后应自动补链"
            assert not os.path.lexists(os.path.join(home, ".cursor", "skills", "dropped"))


@case("bootstrap: 启动时发现仓库新增目录，登记并自动分发")
def _():
    if sys.platform != "win32":
        raise AssertionError("仅 Windows（需要 junction）")
    with tempfile.TemporaryDirectory() as td:
        store = empty_store(td)
        skill_dir(store, "seed-skill")
        with sandbox(td, store=store, adapters=ADAPTERS,
                     config={"seed_auto": False}) as (home, _s):
            import desktop
            api = desktop.Api()
            first = api.bootstrap()
            assert first.get("ok"), first
            assert [s["name"] for s in first["data"]["skills"]] == ["seed-skill"], first
            con = skilllocker.db()
            try:
                con.execute("update tools set enabled=1 where key='claude_code'")
                con.commit()
            finally:
                con.close()
            # 应用关着时往仓库里放了新技能：下一次启动就该被登记并分发，
            # 不能要求用户先点一次「刷新扫描」。
            skill_dir(store, "late-skill")
            second = api.bootstrap()
            assert second.get("ok"), second
            assert "late-skill" in [s["name"] for s in second["data"]["skills"]], second
            dist = second.get("distributed") or {}
            assert dist.get("skills") == ["late-skill"], second
            link = os.path.join(home, ".claude", "skills", "late-skill")
            assert skilllocker.is_reparse(link), "启动时也应自动补链"


# ---------------------------------------------------------------- 两段式刷新
_VOLATILE = ("generated", "scanAt", "eventsTotal", "db")


def _snapshot(payload):
    """归一化到可比较的快照：时间戳类字段与「本次分发了什么」都不参与判等。"""
    p = json.loads(json.dumps(payload, ensure_ascii=False))
    for f in _VOLATILE:
        if f in p["data"]["meta"]:
            p["data"]["meta"][f] = "<v>"
    p["data"]["events"] = []
    p.pop("distributed", None)
    p.pop("scanned", None)
    return p


def _merge_two_phase(first, second):
    """与 ui/app.js applyUniqueSnapshot 同一口径：第二段只并独有相关字段。"""
    out = json.loads(json.dumps(first, ensure_ascii=False))
    d2 = second["data"]
    for k in ("unique", "unique_groups", "conflict", "firstRun"):
        out["data"][k] = d2[k]
    for k in ("uniqueCount", "uniqueLogical", "uniqueLight", "scanDegraded",
              "uniquePending"):
        out["data"]["meta"][k] = d2["meta"][k]
    out["meta"] = second["meta"]
    return out


@case("刷新第一段：不扫独有、打 uniquePending，且绝不修剪独有 meta（防丢备注）")
def _():
    if sys.platform != "win32":
        raise AssertionError("仅 Windows（需要 junction）")
    with tempfile.TemporaryDirectory() as td:
        with sandbox(td, adapters=ADAPTERS) as (home, _store):
            import desktop
            api = desktop.Api()
            assert api.bootstrap()["ok"]
            # agent 目录里一份仓库没有的实体技能 → 独有行；用户在上面写了备注
            skill_dir(os.path.join(home, ".claude", "skills"), "agent-only")
            con = skilllocker.db()
            try:
                con.execute("insert or replace into skill_meta(scope,key,note,updated_at) "
                            "values('unique','claude_code/agent-only','手工备注',1)")
                con.commit()
            finally:
                con.close()
            res = api.rescan_shared()
            assert res.get("ok"), res
            m = res["data"]["meta"]
            assert m.get("uniquePending") is True, "第一段必须标独有待补，前端据此沿用上一轮"
            assert res["data"]["unique"] is None, "第一段带了独有行 = 独有扫描没被跳过"
            assert res["data"]["skills"], "共有看板仍要照常给出"
            con = skilllocker.db()
            try:
                # 这一段如果把 scope='unique' 当已删除剪掉，用户备注就永久丢了
                row = con.execute("select note from skill_meta where scope='unique' "
                                  "and key='claude_code/agent-only'").fetchone()
                assert row and row["note"] == "手工备注", "第一段误剪独有 meta（数据丢失）"
            finally:
                con.close()


@case("刷新第二段：只并独有字段，合并结果与一次性 rescan 逐字段一致")
def _():
    if sys.platform != "win32":
        raise AssertionError("仅 Windows（需要 junction）")
    with tempfile.TemporaryDirectory() as td:
        with sandbox(td, adapters=ADAPTERS) as (home, _store):
            import desktop
            api = desktop.Api()
            assert api.bootstrap()["ok"]
            skill_dir(os.path.join(home, ".claude", "skills"), "agent-only")
            skill_dir(os.path.join(home, ".cursor", "skills"), "agent-two")
            first = api.rescan_shared()
            second = api.rescan_unique()
            assert first.get("ok") and second.get("ok"), (first, second)
            # 第二段不许把共有 / 智能体看板再算一遍（那是第一段的活）
            assert set(second["data"]) == {"unique", "unique_groups", "conflict",
                                           "firstRun", "meta"}, sorted(second["data"])
            assert second["data"]["meta"]["uniquePending"] is False
            full = api.rescan()
            assert full.get("ok"), full
            merged = _snapshot(_merge_two_phase(first, second))
            once = _snapshot(full)
            assert merged == once, "两段合并与一次性快照不一致"


@case("storeSize：用 rescan 顺路存的统计，与全树遍历逐字节一致且随内容更新")
def _():
    with tempfile.TemporaryDirectory() as td:
        with sandbox(td, adapters=ADAPTERS) as (home, store):
            import desktop
            api = desktop.Api()
            assert api.bootstrap()["ok"]
            # 仓库根上的散装文件：不属于任何已登记技能。统计列只覆盖技能目录，
            # 少了这一步补偿，storeSize 就会比真实体积小一截
            write(os.path.join(store, "loose.txt"), "y" * 1000)
            con = skilllocker.db()
            try:
                cfg = skilllocker.load_config()
                got = skilllocker.store_size(con, cfg)
                assert got == skilllocker.tree_stats(store), \
                    "统计列口径与全树遍历不一致（仓库根下的散装内容漏计？）"
                # 反向自测：技能包里加一个文件
                write(os.path.join(store, "demo-skill", "extra.bin"), "x" * 4096)
                # 只查库不重扫时，统计还没更新 —— 数字应当如实停在旧值
                assert skilllocker.store_size(con, skilllocker.load_config()) == got
                skilllocker.rescan(con, skilllocker.load_config())
                grew = skilllocker.store_size(con, skilllocker.load_config())
                assert grew == got + 4096, (got, grew)
                assert grew == skilllocker.tree_stats(store)
            finally:
                con.close()


# ---------------------------------------------------------------- 隔离模式
@case("隔离模式：开分发被拒绝，agent 目录零写入，开关不卡在已启用")
def _():
    if sys.platform != "win32":
        raise AssertionError("仅 Windows（需要 junction）")
    with tempfile.TemporaryDirectory() as td:
        with sandbox(td, adapters=ADAPTERS, config={"isolated": True}) as (home, store):
            import desktop
            api = desktop.Api()
            res = api.bootstrap()
            assert res.get("ok"), res
            assert res["data"]["meta"].get("isolated") is True, "回包必须带隔离标记给角标用"
            r = api.set_agent("claude_code", True)
            assert not r.get("ok"), r
            assert "隔离模式" in (r.get("error") or ""), r
            d = os.path.join(home, ".claude", "skills")
            assert os.listdir(d) == [], "隔离模式绝不能往 agent 目录写链接：%s" % os.listdir(d)
            con = skilllocker.db()
            try:
                en = con.execute("select enabled from tools where key='claude_code'").fetchone()["enabled"]
            finally:
                con.close()
            assert not en, "拒绝后 enabled 要回写 0，否则界面卡在「开关开着、链接没建」"


@case("隔离模式：刷新照常成功，新技能只登记不分发")
def _():
    if sys.platform != "win32":
        raise AssertionError("仅 Windows（需要 junction）")
    with tempfile.TemporaryDirectory() as td:
        with sandbox(td, adapters=ADAPTERS, config={"isolated": True}) as (home, store):
            import desktop
            api = desktop.Api()
            assert api.bootstrap()["ok"]
            # 直接把开关置开（绕过被闸门挡住的 set_agent），模拟「验收实例本来就想分发」
            con = skilllocker.db()
            try:
                con.execute("update tools set enabled=1 where key='claude_code'")
                con.commit()
            finally:
                con.close()
            skill_dir(store, "dropped")
            res = api.rescan_shared()
            assert res.get("ok"), "隔离模式不能让刷新变成失败回包：%s" % res
            dist = res.get("distributed") or {}
            assert dist.get("skills") == ["dropped"], dist
            assert dist.get("isolated") is True, dist
            assert dist.get("changed") == 0, dist
            assert os.listdir(os.path.join(home, ".claude", "skills")) == []
            assert api.rescan_unique().get("ok"), "第二段也必须照常回来"


@case("隔离模式：删除 / 卸载 / 转共有 / 接管仓库全部被拒绝且盘上不动")
def _():
    with tempfile.TemporaryDirectory() as td:
        with sandbox(td, adapters=ADAPTERS, config={"isolated": True}) as (home, store):
            import desktop
            assert desktop.Api().bootstrap()["ok"]
            agent_only = skill_dir(os.path.join(home, ".cursor", "skills"), "agent-only")
            cfg = skilllocker.load_config()
            con = skilllocker.db()
            try:
                calls = [
                    ("删除共有技能", skilllocker.remove_skill, (con, cfg, "demo-skill")),
                    ("卸载独有技能", skilllocker.uninstall_unique, (con, cfg, "cursor", "agent-only")),
                    ("独有转共有", skilllocker.promote_unique, (con, cfg, "cursor", "agent-only")),
                    ("接管已有中央仓库", skilllocker.adopt_store, (con, cfg, store)),
                    ("撤下某 agent 全部分发", skilllocker.unlink_tool, (con, cfg, "cursor")),
                ]
                for label, fn, args in calls:
                    try:
                        fn(*args)
                        raise AssertionError("%s 在隔离模式下竟然执行了" % label)
                    except AssertionError:
                        raise
                    except RuntimeError as exc:
                        assert "隔离模式" in str(exc), "%s 文案不对：%s" % (label, exc)
            finally:
                con.close()
            assert os.path.isdir(os.path.join(store, "demo-skill")), "仓库实体被删了"
            assert os.path.isdir(agent_only), "agent 目录副本被动过"


def main():
    failed = []
    for name, fn in CASES:
        try:
            fn()
            print("  [PASS] %s" % name)
        except AssertionError as exc:
            failed.append(name)
            print("  [FAIL] %s: %s" % (name, exc))
        except Exception as exc:  # noqa: BLE001
            failed.append(name)
            print("  [FAIL] %s: %s: %s" % (name, type(exc).__name__, exc))
    print("=== FIRST-RUN %d/%d ===" % (len(CASES) - len(failed), len(CASES)))
    if failed:
        print("failed:", "; ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
