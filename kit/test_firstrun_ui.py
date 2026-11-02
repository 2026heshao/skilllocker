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
    ADAPTERS, ADAPTERS_WITH_EXTRA, empty_store, sandbox, skill_dir)

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
    """沙箱①：各 agent 的 junction 汇聚到仓库外的 central/，另有 1 个本地实体。"""
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
            api = desktop.Api()
            boot = api.bootstrap()
            assert boot.get("ok"), boot
            assert boot["data"]["firstRun"]["candidate"], "沙箱没造出候选仓库"
            act = api.adopt_store(central)
            assert act.get("ok"), act
            return {"bootstrap": _jsonable(boot), "adopt_store": _jsonable(act)}


def capture_adoptable():
    """沙箱②：反推不出仓库，只有 agent 主目录里的实体技能（外部目录另有 1 个）。"""
    with tempfile.TemporaryDirectory() as td:
        store = empty_store(td)
        with sandbox(td, store=store, adapters=ADAPTERS_WITH_EXTRA) as (home, _s):
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



def open_first_run(browser, fixtures):
    """假桥必须在 bridge.js 的 6s 注入兜底之前就位，所以用 add_init_script。"""
    page = browser.new_page(viewport={"width": 1200, "height": 760})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script("window.__SK_API_NAMES = %s;" % json.dumps(api_names()))
    page.add_init_script(stub_js(fixtures))
    page.goto(UI)
    page.wait_for_function("window.__SK_READY === true", timeout=30000)
    page.wait_for_selector("#first-run:not([hidden])", timeout=15000)
    return page, errors


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
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge")

        # ---------- ① 接管已有仓库 ----------
        page, errs = open_first_run(browser, cand)
        st = panel_state(page)
        check("候选：面板出现且收起列表/网格",
              st["shown"] and st["listHidden"] and st["gridHidden"] and st["rows"] == 0, st)
        check("候选：三个出口（接管 / 收编 / 导入）", len(st["opts"]) == 3 and
              st["opts"][0] == "接管本机已有的中央仓库" and
              st["opts"][1].startswith("一键收编本机"), " | ".join(st["opts"]))
        check("候选：说明含仓库路径与两处计数",
              "central" in st["text"] and "3 个技能" in st["text"] and
              "一键收编本机 1 个技能" in st["text"], st["text"][:140])
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
        check("候选：toast 汇报接管结果", "已接管中央仓库" in toast_text(page), toast_text(page))
        check("候选：0 条 JS 错误", not errs, "; ".join(errs[:3]))
        page.screenshot(path=os.path.join(SHOTS, "firstrun-adopted.png"))
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

        browser.close()
    failed = [n for n, ok, _ in results if not ok]
    print("=== FIRSTRUN-UI %d/%d ===" % (len(results) - len(failed), len(results)))
    if failed:
        print("failed:", "; ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
