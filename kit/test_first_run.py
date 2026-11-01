#!/usr/bin/env python3
"""首次启动（干净机器）全链路回归 · FIRST-RUN-FIX-PLAN.md §5.1。

隔离仿真一台「全新机器」：临时目录假 HOME（装 2 个 agent）+ 假中央仓库 +
无 config.json / skillkit.db，走 bootstrap → 开关 → 坏盘符 → 坏 adapters 的
真实代码路径（desktop.Api 桥方法，非绕过）。补丁注意：skillkit_store.HOME
与 skillkit_boards.HOME 是 from-import 值绑定，只 patch base 不生效。

python test_first_run.py（无第三方依赖；junction 用例仅 Windows）
"""
import contextlib
import json
import logging
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import skillkit  # noqa: E402
import skillkit_base  # noqa: E402
import skillkit_boards  # noqa: E402
import skillkit_store  # noqa: E402

# 桌面端 _guard 的异常日志没装配 handler 时会打到 stderr（lastResort），
# 坏路径用例特意触发它：这里静音，别把测试输出淹了
logging.getLogger("skillkit").addHandler(logging.NullHandler())

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
    olds = (skillkit_base.DB_FILE, skillkit_base.CONFIG_FILE,
            skillkit_base.ADAPTERS_FILE, skillkit_base.DEFAULT_STORE,
            skillkit_base.HOME, skillkit_store.HOME, skillkit_boards.HOME)
    skillkit_base.DB_FILE = os.path.join(td, "skillkit.db")
    skillkit_base.CONFIG_FILE = os.path.join(td, "config.json")
    skillkit_base.ADAPTERS_FILE = os.path.join(td, "adapters.json")
    skillkit_base.DEFAULT_STORE = store
    skillkit_base.HOME = home
    skillkit_store.HOME = home   # from-import 值绑定，必须单独补
    skillkit_boards.HOME = home  # 同上
    if adapters is not None:  # 传字符串 = 原样写入（构造损坏文件用例）
        write(skillkit_base.ADAPTERS_FILE,
              adapters if isinstance(adapters, str) else json.dumps(adapters))
    if config is not None:
        write(skillkit_base.CONFIG_FILE, json.dumps(config))
    try:
        yield home, store
    finally:
        (skillkit_base.DB_FILE, skillkit_base.CONFIG_FILE,
         skillkit_base.ADAPTERS_FILE, skillkit_base.DEFAULT_STORE,
         skillkit_base.HOME, skillkit_store.HOME, skillkit_boards.HOME) = olds


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
            assert len(res["data"]["folders"]) == len(skillkit.FOLDER_PRESETS)
            con = skillkit.db()
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
            con = skillkit.db()
            try:
                cfg = skillkit.load_config()
                srow = con.execute("select * from skills where name='demo-skill'").fetchone()
                trow = con.execute("select * from tools where key='claude_code'").fetchone()
                state, _ = skillkit.target_state(
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
    bad = "%s:/skillkit/store" % absent_drive()
    with tempfile.TemporaryDirectory() as td:
        with sandbox(td, store=bad, config={"store": bad}):
            cfg = skillkit.load_config()  # 不抛即过
            assert cfg["store"] == bad, cfg
            assert "不可创建" in cfg.get("_load_error", ""), cfg


@case("first_run: bootstrap 对坏 store 返回 ok:false（错误页数据源）")
def _():
    bad = "%s:/skillkit/store" % absent_drive()
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
            lg = logging.getLogger("skillkit")
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
            con = skillkit.db()
            try:
                try:
                    skillkit.sync_tools_from_adapters(con, skillkit.load_config())
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
            con = skillkit.db()
            try:
                # 旧库里的行：enabled=1（老版首次入库语义），链接可能存在
                con.execute("insert into tools(key,label,skills_dir,detect_dir,detected,enabled,mode) "
                            "values('claude_code','Claude Code',?, '.claude',1,1,'junction')",
                            (os.path.join(home, ".claude", "skills").replace(os.sep, "/"),))
                con.commit()
                cfg = skillkit.load_config()
                skillkit.sync_tools_from_adapters(con, cfg)
                rows = {r["key"]: (r["detected"], r["enabled"])
                        for r in con.execute("select key,detected,enabled from tools")}
                # 老行 enabled 保持 1（update 分支不碰），新行默认 0
                assert rows["claude_code"] == (1, 1), rows
                assert rows["cursor"] == (1, 0), rows
            finally:
                con.close()


@case("first_run: DEFAULT_STORE 以安装目录为基（无 config.json 时）")
def _():
    install_root = os.path.dirname(os.path.abspath(skillkit_base.KIT))  # kit/ 上一级
    want = os.path.normpath(os.path.join(install_root, "store")).replace(os.sep, "/")
    assert skillkit_base.DEFAULT_STORE == want, (
        skillkit_base.DEFAULT_STORE, want)
    assert skillkit_base.DEFAULT_STORE.endswith("/store")


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
