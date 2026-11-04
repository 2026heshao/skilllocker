#!/usr/bin/env python3
"""首启引导（中央仓库为空）UI 回归 · 真实后端回包 + Playwright DOM 走查。

后端在临时沙箱里真跑一遍 bootstrap / adopt_store / adopt_local_skills，回包
原样喂给注入的假 pywebview 桥；界面以 file://?desktop=1&noboot=1 加载，断言
① 面板渲染与内容区收起 ② 确认弹窗文案 ③ 执行后面板消失、共有看板出现、
toast 汇总 ④ 全程 0 条 JS 错误。

python kit/test_firstrun_ui.py（需 playwright + 本机 Edge；junction 仅 Windows）
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import skilllocker  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from test_first_run import (  # noqa: E402
    ADAPTERS, ADAPTERS_WITH_EXTRA, empty_store, sandbox, skill_dir, write)

UI = "file:///" + os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ui",
                 "index.html")).replace("\\", "/") + "?desktop=1&noboot=1"
SHOTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".screenshots")
results = []


def check(name, ok, value=""):
    results.append((name, bool(ok), value))
    print("  [%s] %-34s %s" % ("PASS" if ok else "FAIL", name, value))


def _jsonable(obj):
    return json.loads(json.dumps(obj, default=str))


def capture_candidate():
    """沙箱①：各 agent 的 junction 汇聚到仓库外的 central/，另有 1 个本地实体。

    seed_auto=false 保留手动引导面板，专门测「接管该仓库」按钮链路。
    """
    with tempfile.TemporaryDirectory() as td:
        store = empty_store(td)
        central = os.path.join(td, "central")
        with sandbox(td, store=store, adapters=ADAPTERS,
                     config={"seed_auto": False}) as (home, _s):
            for n in ("alpha", "beta", "gamma"):
                skill_dir(central, n)
                for tool_dir in (".claude", ".cursor"):
                    skilllocker.make_junction(
                        os.path.join(home, tool_dir, "skills", n),
                        os.path.join(central, n))
            skill_dir(os.path.join(home, ".claude", "skills"), "local-only")
            import desktop
            api = desktop.Api()
            boot = api.bootstrap()
            assert boot.get("ok"), boot
            assert boot["data"]["firstRun"]["candidate"], "沙箱没造出候选仓库"
            act = api.adopt_store(central)
            assert act.get("ok"), act
            return {"bootstrap": _jsonable(boot), "adopt_store": _jsonable(act)}


def capture_auto_adopt():
    """沙箱①b：同候选仓库，但保留自动接管 —— 首屏直接是 3 共有 / 1 独有。"""
    with tempfile.TemporaryDirectory() as td:
        store = empty_store(td)
        central = os.path.join(td, "central")
        with sandbox(td, store=store, adapters=ADAPTERS) as (home, _s):
            for n in ("alpha", "beta", "gamma"):
                skill_dir(central, n)
                for tool_dir in (".claude", ".cursor"):
                    skilllocker.make_junction(
                        os.path.join(home, tool_dir, "skills", n),
                        os.path.join(central, n))
            skill_dir(os.path.join(home, ".claude", "skills"), "local-only")
            import desktop
            boot = desktop.Api().bootstrap()
            assert boot.get("ok"), boot
            fr = boot["data"]["firstRun"]
            assert fr and fr["strip"] and not fr["storeEmpty"], fr
            assert len(boot["data"]["skills"]) == 3, boot["data"]["skills"]
            assert "已接管本机已有的中央仓库" in (boot["data"]["meta"].get("seedNote") or ""), \
                boot["data"]["meta"].get("seedNote")
            return {"bootstrap": _jsonable(boot)}


def capture_adoptable():
    """沙箱②：反推不出仓库，只有 agent 主目录里的实体技能（外部目录另有 1 个）。

    seed_auto=false 保留手动面板；自动建库链路由沙箱④/⑦覆盖。
    """
    with tempfile.TemporaryDirectory() as td:
        store = empty_store(td)
        with sandbox(td, store=store, adapters=ADAPTERS_WITH_EXTRA,
                     config={"seed_auto": False}) as (home, _s):
            skill_dir(os.path.join(home, ".claude", "skills"), "one")
            skill_dir(os.path.join(home, ".claude", "skills"), "two")
            skill_dir(os.path.join(home, ".cursor", "skills"), "three")
            skill_dir(os.path.join(home, ".agents", "skills"), "ext")
            import desktop
            api = desktop.Api()
            boot = api.bootstrap()
            assert boot.get("ok"), boot
            fr = boot["data"]["firstRun"]
            assert not fr["candidate"] and len(fr["adoptable"]) == 3, fr
            act = api.adopt_local_skills()
            assert act.get("ok"), act
            return {"bootstrap": _jsonable(boot), "adopt_local_skills": _jsonable(act)}


def capture_bare():
    """沙箱③：既没有可接管的仓库也没有可收编的实体 —— 只剩导入出口。"""
    with tempfile.TemporaryDirectory() as td:
        store = empty_store(td)
        with sandbox(td, store=store, adapters=ADAPTERS):
            import desktop
            boot = desktop.Api().bootstrap()
            assert boot.get("ok"), boot
            return {"bootstrap": _jsonable(boot)}


def capture_seeded():
    """沙箱④：空仓库 + 两个 agent 有共同技能 —— 首启自动建库的直接回包。"""
    with tempfile.TemporaryDirectory() as td:
        store = empty_store(td)
        with sandbox(td, store=store, adapters=ADAPTERS) as (home, _s):
            skill_dir(os.path.join(home, ".claude", "skills"), "alpha")
            skill_dir(os.path.join(home, ".cursor", "skills"), "alpha")
            skill_dir(os.path.join(home, ".claude", "skills"), "only-cl")
            skill_dir(os.path.join(home, ".cursor", "skills"), "only-cu")
            import desktop
            boot = desktop.Api().bootstrap()
            assert boot.get("ok"), boot
            assert boot["data"]["meta"].get("seedNote"), "沙箱没触发自动建库"
            return {"bootstrap": _jsonable(boot)}


def capture_single_seed():
    """沙箱⑤：只有一个 agent，退化规则把它的 3 个技能全收进初始库。"""
    with tempfile.TemporaryDirectory() as td:
        store = empty_store(td)
        with sandbox(td, store=store, adapters=[ADAPTERS[0]]) as (home, _s):
            import shutil
            shutil.rmtree(os.path.join(home, ".cursor"), ignore_errors=True)
            for n in ("s1", "s2", "s3"):
                skill_dir(os.path.join(home, ".claude", "skills"), n)
            import desktop
            boot = desktop.Api().bootstrap()
            assert boot.get("ok"), boot
            return {"bootstrap": _jsonable(boot)}


def capture_disjoint():
    """沙箱⑦：两个 agent 技能完全不重叠 —— 首启也应自动建库，共有不再为 0。"""
    with tempfile.TemporaryDirectory() as td:
        store = empty_store(td)
        with sandbox(td, store=store, adapters=ADAPTERS) as (home, _s):
            skill_dir(os.path.join(home, ".claude", "skills"), "only-cl")
            skill_dir(os.path.join(home, ".cursor", "skills"), "only-cu")
            import desktop
            boot = desktop.Api().bootstrap()
            assert boot.get("ok"), boot
            assert sorted(s["name"] for s in boot["data"]["skills"]) == \
                ["only-cl", "only-cu"], boot["data"]["skills"]
            return {"bootstrap": _jsonable(boot)}


def capture_dupe_panel():
    """沙箱⑧：seed_auto=false + 两个 agent 各一份同内容 brandkit + 一个独有。

    面板必须按「技能」计数（2 个技能 / 3 份副本），独有看板按内容合并
    （2 个逻辑技能 / 3 个实例）。
    """
    with tempfile.TemporaryDirectory() as td:
        store = empty_store(td)
        with sandbox(td, store=store, adapters=ADAPTERS,
                     config={"seed_auto": False}) as (home, _s):
            skill_dir(os.path.join(home, ".claude", "skills"), "brandkit")
            skill_dir(os.path.join(home, ".cursor", "skills"), "brandkit")
            skill_dir(os.path.join(home, ".claude", "skills"), "only-cl")
            import desktop
            boot = desktop.Api().bootstrap()
            assert boot.get("ok"), boot
            return {"bootstrap": _jsonable(boot)}


def capture_grouped():
    """沙箱⑥：仓库非空（demo-skill），agent 目录里有多实例 / 同名不同内容。"""
    with tempfile.TemporaryDirectory() as td:
        store = empty_store(td)
        with sandbox(td, store=store, adapters=ADAPTERS) as (home, _s):
            write(os.path.join(store.replace("/", os.sep), "demo-skill", "SKILL.md"),
                  "---\nname: demo-skill\ndescription: demo\n---\nbody\n")
            cl = os.path.join(home, ".claude", "skills")
            cu = os.path.join(home, ".cursor", "skills")
            skill_dir(cl, "brandkit")
            skill_dir(cu, "brandkit")           # 同名同内容 → 1 组 2 实例
            skill_dir(cl, "pdf", body="A")
            skill_dir(cu, "pdf", body="B")      # 同名不同内容 → 2 组不合并
            import desktop
            boot = desktop.Api().bootstrap()
            assert boot.get("ok"), boot
            assert boot["data"]["meta"]["uniqueLogical"] == 3, boot["data"]["meta"]
            return {"bootstrap": _jsonable(boot)}


def api_names():
    """假桥的方法名单直接取桌面桥的公开方法，桥面变了测试自动跟上。"""
    import desktop
    return sorted(n for n in dir(desktop.Api)
                  if not n.startswith("_") and callable(getattr(desktop.Api, n)))


JS_STUB = """
(function () {
  var api = {};
  (window.__SK_API_NAMES || []).forEach(function (name) {
    api[name] = function () {
      var res = window.__SK_FIXTURES[name];
      if (res === undefined) res = {ok: true, data: null, meta: {}};
      return Promise.resolve(res);
    };
  });
  window.pywebview = {api: api};
})();
"""


def stub_js(fixtures):
    """假桥回包：真实后端沙箱抓来的 JSON + 未覆盖接口一律给空成功回包。

    用普通对象而不是 Proxy：Proxy 的 get 每次返回新函数，与 pywebview 注入
    的调用约定不一致，app.js 的启动链路会停在未 settle 的 Promise 上。
    """
    return ("window.__SK_FIXTURES = " + json.dumps(fixtures, ensure_ascii=True) + ";"
            + JS_STUB)



def open_app(browser, fixtures, wait_first=False):
    """假桥必须在 bridge.js 的 6s 注入兜底之前就位，所以用 add_init_script。"""
    page = browser.new_page(viewport={"width": 1200, "height": 760})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script("window.__SK_API_NAMES = %s;" % json.dumps(api_names()))
    page.add_init_script(stub_js(fixtures))
    page.goto(UI)
    page.wait_for_function("window.__SK_READY === true", timeout=30000)
    if wait_first:
        page.wait_for_selector("#first-run:not([hidden])", timeout=15000)
    return page, errors


def open_first_run(browser, fixtures):
    return open_app(browser, fixtures, wait_first=True)


def panel_state(page):
    return page.evaluate("""(function () {
      var fr = document.getElementById('first-run');
      return {
        shown: !fr.hidden,
        opts: Array.prototype.map.call(
          fr.querySelectorAll('.fr-opt-main b'), function (b) { return b.textContent; }),
        text: fr.textContent.replace(/\\s+/g, ' '),
        listHidden: document.getElementById('list-card').hidden,
        gridHidden: document.getElementById('grid-root').hidden,
        rows: document.querySelectorAll('.row, .file-card').length
      };
    })()""")


def click_through(page, button_id, expect_modal):
    """点引导按钮 → 确认弹窗 → 执行 → 等面板消失（后端回包驱动界面）。"""
    page.click(button_id)
    page.wait_for_selector("#modal-root .modal", timeout=5000)
    modal = page.text_content("#modal-root .modal").replace("\n", " ")
    assert expect_modal in modal, modal
    page.click("#confirm-ok")
    page.wait_for_timeout(80)
    busy = page.evaluate(
        "(id) => { var b = document.getElementById(id);"
        " return {disabled: b.disabled, label: b.textContent}; }",
        button_id.lstrip("#"))
    check("执行中：%s 禁用并显示「处理中…」" % button_id,
          busy["disabled"] and busy["label"] == "处理中…", json.dumps(busy))
    page.wait_for_selector("#first-run", state="hidden", timeout=20000)
    return modal


def toast_text(page):
    el = page.query_selector(".toast")
    return el.text_content().replace("\n", " ").strip() if el else ""


def main():
    os.makedirs(SHOTS, exist_ok=True)
    cand, able, bare = capture_candidate(), capture_adoptable(), capture_bare()
    auto = capture_auto_adopt()
    seeded = capture_seeded()
    single = capture_single_seed()
    grouped = capture_grouped()
    disjoint = capture_disjoint()
    dupe_panel = capture_dupe_panel()
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge")

        # ---------- ① 接管已有仓库 ----------
        page, errs = open_first_run(browser, cand)
        st = panel_state(page)
        check("候选：面板出现且收起列表/网格",
              st["shown"] and st["listHidden"] and st["gridHidden"] and st["rows"] == 0, st)
        able_n = len({a["name"] for a in
                      cand["bootstrap"]["data"]["firstRun"]["adoptable"]})
        check("候选：三个出口（接管 / 收编 / 导入）", len(st["opts"]) == 3 and
              st["opts"][0] == "接管本机已有的中央仓库" and
              st["opts"][1].startswith("一键收编本机 %d" % able_n), " | ".join(st["opts"]))
        check("候选：说明含仓库路径与两处计数",
              "central" in st["text"] and "3 个技能" in st["text"] and
              ("一键收编本机 %d 个技能" % able_n) in st["text"], st["text"][:140])
        page.screenshot(path=os.path.join(SHOTS, "firstrun-candidate.png"))
        modal = click_through(page, "#fr-adopt-store", "接管后「共有技能」")
        check("候选：确认文案给出可逆说明", "config.json" in modal and "不搬移" in modal, modal[:90])
        after = page.evaluate("""(function () {
            return {shared: document.getElementById('tab-shared-num').textContent,
                    unique: document.getElementById('tab-unique-num').textContent,
                    cards: document.querySelectorAll('.folder-card, .file-card, .row').length};
          })()""")
        check("候选：接管后共有=3 且 7 条独有降到 1",
              after["shared"] == "3" and after["unique"] == "1", after)
        # 剩下的那 1 条必须是中央仓库里确实没有的本地实体（local-only）：
        # 接管只把「已有链接指向的那批」变成共有，不虚报清零
        page.click('[data-view-mode="list"]')
        page.wait_for_timeout(250)
        shared_rows = page.evaluate(
            "document.getElementById('list-body').textContent.replace(/\\s+/g,' ')")
        check("候选：共有看板是被接管的 3 个技能",
              "alpha" in shared_rows and "gamma" in shared_rows
              and "local-only" not in shared_rows, shared_rows[:80])
        page.click('.board-tab[data-board="unique"]')
        page.wait_for_timeout(400)
        ugroups = page.evaluate(
            "[...document.querySelectorAll('#grid-root .folder-card')].map(function (c) {"
            " return c.querySelector('.fc-name').textContent + '|' + c.querySelector('.fc-meta').textContent; })")
        check("候选：残留独有=1 个 agent 夹里的 local-only",
              ugroups == ["Claude Code|1 个 skill"], json.dumps(ugroups))
        first_sect = page.evaluate(
            "document.querySelector('#grid-root .grid-sect b').textContent")
        check("候选：无合并时先显示 Agent 文件夹", first_sect == "Agent 文件夹", first_sect)
        check("候选：toast 汇报接管结果", "已接管中央仓库" in toast_text(page), toast_text(page))
        check("候选：0 条 JS 错误", not errs, "; ".join(errs[:3]))
        page.screenshot(path=os.path.join(SHOTS, "firstrun-adopted.png"))
        page.close()

        # ---------- ①b 首启自动接管：无面板、共有直接有内容 ----------
        page, errs_auto = open_app(browser, auto)
        auto_st = page.evaluate("""(function () {
            var fr = document.getElementById('first-run');
            return {shown: !fr.hidden, strip: fr.classList.contains('is-strip'),
                    opts: fr.querySelectorAll('.fr-opt').length,
                    text: fr.textContent.replace(/\\s+/g, ' ')};
          })()""")
        check("自动接管：空库面板降级为提示条",
              auto_st["shown"] and auto_st["strip"] and auto_st["opts"] == 0, auto_st)
        check("自动接管：提示条说明接管结果",
              "已接管本机已有的中央仓库" in auto_st["text"], auto_st["text"][:120])
        counts = page.evaluate("""(function () {
            return {shared: document.getElementById('tab-shared-num').textContent,
                    unique: document.getElementById('tab-unique-num').textContent};
          })()""")
        check("自动接管：共有=3、独有=1", counts == {"shared": "3", "unique": "1"}, counts)
        check("自动接管：toast 说明接管的仓库与数量",
              "已接管本机已有的中央仓库" in toast_text(page), toast_text(page))
        check("自动接管：0 条 JS 错误", not errs_auto, "; ".join(errs_auto[:3]))
        page.screenshot(path=os.path.join(SHOTS, "firstrun-auto-adopt.png"))
        page.close()

        # ---------- ② 一键收编 ----------
        page, errs2 = open_first_run(browser, able)
        st = panel_state(page)
        check("收编：主按钮是收编（无候选仓库）",
              st["shown"] and len(st["opts"]) == 2 and
              st["opts"][0].startswith("一键收编本机 3"), " | ".join(st["opts"]))
        check("收编：说明点明源目录保留", "源目录保留" in st["text"], st["text"][:120])
        page.screenshot(path=os.path.join(SHOTS, "firstrun-adoptable.png"))
        modal = click_through(page, "#fr-adopt-local", "拷贝一份进仓库")
        after = page.evaluate("""(function () {
            return {shared: document.getElementById('tab-shared-num').textContent,
                    unique: document.getElementById('tab-unique-num').textContent};
          })()""")
        check("收编：共有=3、外部目录 1 个仍是独有",
              after["shared"] == "3" and after["unique"] == "1", after)
        check("收编：toast 汇报收编数", "已收编 3 个技能" in toast_text(page), toast_text(page))
        check("收编：0 条 JS 错误", not errs2, "; ".join(errs2[:3]))
        page.close()

        # ---------- ③ 既无候选也无实体 ----------
        page, errs3 = open_first_run(browser, bare)
        st = panel_state(page)
        check("空机器：只剩导入出口且不谎报可收编",
              st["shown"] and st["opts"] == ["自己挑一个目录导入"], " | ".join(st["opts"]))
        check("空机器：标题说明共有为 0 的原因",
              "中央仓库还是空的" in st["text"], st["text"][:100])
        page.evaluate("document.getElementById('fr-dismiss').click()")
        page.wait_for_timeout(120)
        check("空机器：「以后再说」收起面板", page.evaluate("document.getElementById('first-run').hidden"))
        check("空机器：0 条 JS 错误", not errs3, "; ".join(errs3[:3]))
        page.close()

        # ---------- ③b 多 agent 技能不重叠：自动全收，不再出现共有 0 ----------
        page, errs3b = open_app(browser, disjoint)
        counts = page.evaluate("""(function () {
            return {fr: document.getElementById('first-run').hidden,
                    shared: document.getElementById('tab-shared-num').textContent,
                    unique: document.getElementById('tab-unique-num').textContent};
          })()""")
        check("不重叠：自动建库，共有=2、独有=0、面板不出现",
              counts == {"fr": True, "shared": "2", "unique": "0"}, counts)
        check("不重叠：toast 说明无同名交叉",
              "没有同名交叉" in toast_text(page), toast_text(page))
        check("不重叠：0 条 JS 错误", not errs3b, "; ".join(errs3b[:3]))
        page.close()

        # ---------- ③c 同名副本：面板按技能计数，独有按内容合并 ----------
        page, errs3c = open_first_run(browser, dupe_panel)
        st = panel_state(page)
        counts = page.evaluate("""(function () {
            return {logical: document.getElementById('tab-unique-num').textContent,
                    em: document.getElementById('tab-unique-em').textContent};
          })()""")
        check("同名副本：面板按技能计数（2 个技能，不是 3 份副本）",
              any(x.startswith("一键收编本机 2 个技能") for x in st["opts"]) and
              "3 个" not in " ".join(st["opts"]), " | ".join(st["opts"]))
        check("同名副本：面板给出 2 个逻辑技能 / 3 个实例",
              "2 个逻辑独有技能（3 个实例）" in st["text"], st["text"][:160])
        check("同名副本：侧栏独有=2（合并后），副标题给 3 个实例",
              counts == {"logical": "2", "em": "2 个 agent 文件夹 · 3 个实例"}, counts)
        check("同名副本：0 条 JS 错误", not errs3c, "; ".join(errs3c[:3]))
        page.close()

        # ---------- ④ 自动建库后：共有看板直接用，只剩单行提示条 ----------
        page, errs4 = open_first_run(browser, seeded)
        st = panel_state(page)
        strip = page.evaluate(
            "document.getElementById('first-run').classList.contains('is-strip')")
        counts = page.evaluate("""(function () {
            return {shared: document.getElementById('tab-shared-num').textContent,
                    unique: document.getElementById('tab-unique-num').textContent};
          })()""")
        check("自动建库：面板降级为单行提示条",
              st["shown"] and strip and st["opts"] == [], st)
        check("自动建库：共有看板直接有 1 个技能",
              counts["shared"] == "1" and counts["unique"] == "2", counts)
        check("自动建库：toast 说明来源与数量",
              "已自动建立中央仓库" in toast_text(page), toast_text(page))
        page.screenshot(path=os.path.join(SHOTS, "firstrun-seeded-strip.png"))
        page.evaluate("document.getElementById('fr-dismiss').click()")
        page.wait_for_timeout(140)
        check("自动建库：提示条可收起",
              page.evaluate("document.getElementById('first-run').hidden"))
        check("自动建库：0 条 JS 错误", not errs4, "; ".join(errs4[:3]))
        page.close()

        # ---------- ⑤ 单 agent 退化：全收，无面板 ----------
        page, errs5 = open_app(browser, single)
        counts = page.evaluate("""(function () {
            return {fr: document.getElementById('first-run').hidden,
                    shared: document.getElementById('tab-shared-num').textContent,
                    unique: document.getElementById('tab-unique-num').textContent};
          })()""")
        check("单 agent：自动收全部 3 个且不出面板",
              counts == {"fr": True, "shared": "3", "unique": "0"}, counts)
        check("单 agent：toast 如实说明退化规则",
              "只检测到 1 个 agent" in toast_text(page), toast_text(page))
        check("单 agent：0 条 JS 错误", not errs5, "; ".join(errs5[:3]))
        page.close()

        # ---------- ⑥ 独有看板按逻辑技能聚合 ----------
        page, errs6 = open_app(browser, grouped)
        page.evaluate("document.getElementById('nav-unique').click()")
        page.wait_for_timeout(350)
        grid = page.evaluate("""(function () {
            return {
              logical: [...document.querySelectorAll('#grid-root .group-card')].map(function (c) {
                return c.querySelector('.fc-main b').textContent + '|' +
                       c.querySelector('.fc-main span').textContent; }),
              agents: document.querySelectorAll('#grid-root [data-folder-card]').length,
              sub: document.getElementById('page-sub').textContent
            };
          })()""")
        check("聚合：3 个逻辑技能卡 + 2 个 agent 文件夹",
              len(grid["logical"]) == 3 and grid["agents"] == 2, grid)
        first_sect = page.evaluate(
            "document.querySelector('#grid-root .grid-sect b').textContent")
        check("聚合：有合并时逻辑技能排第一段", first_sect == "逻辑技能", first_sect)
        check("聚合：brandkit 副标题说明 2 个 agent 内容一致",
              any(x.startswith("brandkit|2 个 agent 各有一份") for x in grid["logical"]),
              grid["logical"])
        check("聚合：页面标题给出逻辑数 / 实例数",
              "3 个逻辑技能（4 个实例）" in grid["sub"], grid["sub"])
        page.evaluate(
            "(function(){var b=document.querySelector(\"#view-seg [data-view-mode='list']\");"
            "if(b)b.click()})()")
        page.wait_for_timeout(250)
        rows = page.evaluate("""[...document.querySelectorAll('.row.group-row')].map(function (r) {
            return r.getAttribute('data-key') + '|' + r.querySelector('.row-name').textContent; })""")
        check("聚合：列表 3 行且键带 g# 前缀",
              len(rows) == 3 and all(x.startswith("g#") for x in rows), rows)
        page.evaluate("""(function () {
            var r = [...document.querySelectorAll('.row.group-row')].find(function (x) {
              return x.querySelector('.row-name').textContent === 'brandkit'; });
            if (r) r.click();
          })()""")
        page.wait_for_timeout(250)
        inst = page.evaluate("""(function () {
            var b = document.querySelector('#ins-body');
            return {buttons: document.querySelectorAll('[data-open-instance]').length,
                    text: b ? b.textContent.replace(/\\s+/g, ' ') : ''};
          })()""")
        check("聚合：组详情列出 2 个实例入口",
              inst["buttons"] == 2 and "实例" in inst["text"], inst)
        page.evaluate("document.querySelector('[data-open-instance]').click()")
        page.wait_for_timeout(300)
        jumped = page.evaluate("""(function () {
            var s = window.__SK_APP.state;
            var r = document.querySelector('.row.is-selected');
            return {folder: s.folder, selected: s.selected,
                    rowKey: r ? r.getAttribute('data-key') : null,
                    title: document.getElementById('page-title').textContent};
          })()""")
        check("聚合：查看实例进入 agent 夹并选中实例行",
              jumped["folder"] and jumped["selected"] == jumped["rowKey"], jumped)
        page.screenshot(path=os.path.join(SHOTS, "unique-groups.png"))
        check("聚合：0 条 JS 错误", not errs6, "; ".join(errs6[:3]))
        page.close()

        browser.close()
    failed = [n for n, ok, _ in results if not ok]
    print("=== FIRSTRUN-UI %d/%d ===" % (len(results) - len(failed), len(results)))
    if failed:
        print("failed:", "; ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
