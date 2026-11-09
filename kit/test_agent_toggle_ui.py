#!/usr/bin/env python3
"""智能体分发开关 · 连开多个的并发回归（真实后端回包 + Playwright DOM 走查）。

复现并守住这条用户反馈：连着打开多个智能体，几秒后界面自动刷新只剩一个「已连接」，
剩下的还得重新打开。根因两层：

1. 每个 set_agent 回包都是「它自己那一刻」的整套看板快照。第一份回包落地并重绘时，
   后面几个请求还在后端排队 —— 它们此刻的 enabled 仍是 0，于是用户刚拨开的开关被
   界面自己弹回「已关闭」。
2. applyBootstrap 谁后到谁生效，不看快照新旧。乱序到达时旧快照会把已连接的状态整体
   刷回去 —— 这才是「只剩一个」。

沙箱真跑 bootstrap / 逐个 set_agent 抓到 5 份回包（后端 OPS_LOCK 会把并发请求排成队，
所以第 k 份回包正好只包含前 k 个 agent 已连接，与真实连点时逐笔返回的内容一致），
再用可控到达顺序的假桥喂给界面。断言：

①点了开关立刻发请求（不再先空等 650ms 动画）
②在途开关保持用户拨开的状态、标「连接中」并禁用（不重复工）
③第一份回包落地不许把其余在途开关刷回「已关闭」
④乱序到达：旧快照被忽略，最终 5 个全部已连接
⑤开关回包不重扫独有：独有计数沿用上一轮，不刷成 0
⑥⑦⑧关闭方向：在途不弹回「已开」、失败回落到后端数据、成功只动那一个 agent
⑨全程 0 条 JS 错误

python kit/test_agent_toggle_ui.py（需 playwright + 本机 Edge；junction 仅 Windows）
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import skilllocker  # noqa: E402,F401  (沙箱补丁经由 test_first_run)
from playwright.sync_api import sync_playwright  # noqa: E402

from test_first_run import sandbox, skill_dir  # noqa: E402

UI = "file:///" + os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "ui", "index.html"
)).replace("\\", "/") + "?desktop=1&noboot=1&board=agents"
SHOTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".screenshots")

AGENTS = ["ag0", "ag1", "ag2", "ag3", "ag4"]
ADAPTERS = [{"key": k, "label": "Agent " + k[-1],
             "skills_dir": ".%s/skills" % k, "detect_dir": ".%s" % k} for k in AGENTS]
N_SKILLS = 12
results = []


def check(name, ok, value=""):
    results.append((name, bool(ok), value))
    print("  [%s] %-44s %s" % ("PASS" if ok else "FAIL", name, str(value)[:170]))


def _jsonable(obj):
    return json.loads(json.dumps(obj, default=str))


def capture():
    """沙箱里真跑：5 个 agent 全关 → 逐个开，抓 5 份真实回包。"""
    with tempfile.TemporaryDirectory() as td:
        store = os.path.normpath(os.path.join(td, "store")).replace(os.sep, "/")
        for n in range(N_SKILLS):
            skill_dir(store, "skill-%02d" % n)
        home = os.path.join(td, "home")
        for k in AGENTS:
            os.makedirs(os.path.join(home, "." + k, "skills"), exist_ok=True)
        # 独有看板要有数据：某 agent 目录里放一份仓库没有的实体技能，用来验证
        # 「开关回包不扫独有」时界面沿用上一轮、独有计数不刷成 0
        skill_dir(os.path.join(home, ".ag0", "skills"), "local-only")
        with sandbox(td, home=home, store=store, adapters=ADAPTERS) as (_h, _s):
            import desktop
            api = desktop.Api()
            boot = api.bootstrap()
            assert boot.get("ok"), boot
            assert boot["data"]["meta"]["uniqueCount"] >= 1, boot["data"]["meta"]
            # 场景 B 要用的一对：只有 ag1 开着 → 关掉 ag1（其余状态不变，回包自洽）
            b_on = _jsonable(api.set_agent("ag1", True))
            b_off = _jsonable(api.set_agent("ag1", False))
            assert b_on.get("ok") and b_off.get("ok"), (b_on, b_off)
            payloads = {}
            for k in AGENTS:
                r = api.set_agent(k, True)
                assert r.get("ok"), r
                assert r["action"]["changed"] >= N_SKILLS, r["action"]
                payloads[k] = _jsonable(r)
            return {"bootstrap": _jsonable(boot), "set_agent": payloads,
                    "b_on": b_on, "b_off": b_off}


def api_names():
    import desktop
    return sorted(n for n in dir(desktop.Api)
                  if not n.startswith("_") and callable(getattr(desktop.Api, n)))


# 假桥：set_agent 不自动回话，把 resolve 攒进 __SK_HANDS，由测试决定到达顺序；
# 同时记下每笔请求的发出时刻（测「点了就发」）。其余接口与 test_firstrun_ui 同口径。
STUB = """
(function () {
  window.__SK_HANDS = [];
  window.__SK_TS = [];
  var api = {};
  (window.__SK_API_NAMES || []).forEach(function (name) {
    api[name] = function () {
      if (name === 'set_agent') {
        window.__SK_TS.push(Date.now());
        return new Promise(function (resolve) { window.__SK_HANDS.push(resolve); });
      }
      var res = window.__SK_FIXTURES[name];
      if (res === undefined) res = {ok: true, data: null, meta: {}};
      return Promise.resolve(res);
    };
  });
  window.pywebview = {api: api};
})();
"""

DUMP = """(function () {
  var out = {};
  document.querySelectorAll('.agent-row [data-agent-switch]').forEach(function (l) {
    var k = l.getAttribute('data-agent-switch');
    var row = l.closest('.agent-row');
    var st = row ? row.querySelector('.agent-state') : null;
    var inp = l.querySelector('input');
    out[k] = {
      checked: !!(inp && inp.checked),
      disabled: !!(inp && inp.disabled),
      pending: l.getAttribute('data-agent-pending') === '1',
      status: st ? st.textContent.replace(/\\s+/g, ' ').trim() : ''
    };
  });
  var u = document.getElementById('tab-unique-num');
  out.__unique = u ? u.textContent.trim() : '';
  var sub = document.getElementById('page-sub');
  out.__sub = sub ? sub.textContent.replace(/\\s+/g, ' ').trim() : '';
  out.__hands = window.__SK_HANDS.length;
  return out;
})()"""


def open_app(browser, fixtures):
    """假桥必须在 bridge.js 的注入兜底之前就位，所以用 add_init_script。"""
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script("window.__SK_API_NAMES = %s;" % json.dumps(api_names()))
    page.add_init_script("window.__SK_FIXTURES = %s;%s" % (
        json.dumps(fixtures, ensure_ascii=True), STUB))
    page.goto(UI)
    page.wait_for_function("window.__SK_READY === true", timeout=30000)
    return page, errors


def flip(page, keys, on=True):
    """连点：一次 JS 调用里把多个开关拨到目标状态（行内的开关，不含检查器副本）。"""
    page.evaluate(
        "(function (a) { a.forEach(function (one) {"
        "var i = document.querySelector('.agent-row [data-agent-switch=\"' + one[0] + '\"] input');"
        "if (i && i.checked !== one[1]) i.click(); }); })",
        [[k, on] for k in keys])


def deliver(page, payload):
    page.evaluate("(p) => window.__SK_HANDS.shift()(p)", payload)


def flip_off(page, key):
    """关 = 二次确认后才发请求（UI-SPEC §分发开关），这里替用户点掉确认框。"""
    flip(page, [key], on=False)
    page.wait_for_selector("#modal-root .modal", timeout=8000)
    page.click("#confirm-ok")


def states(page):
    return page.evaluate(DUMP)


def settle(page, ms=300):
    """回包是同步注入的，留一拍让 app.js 把重绘走完再读 DOM。"""
    page.wait_for_timeout(ms)


def pending(page, n):
    """等到第 n 笔请求真的发出去（假桥记下调用时刻）。"""
    page.wait_for_function("window.__SK_HANDS.length >= %d" % n, timeout=8000)


def main():
    fx = capture()
    print("沙箱回包就绪：agent %d 个，set_agent 回包 %d 份 + 撤链接回包 1 份" % (
        len(fx["bootstrap"]["data"]["agents"]), len(fx["set_agent"])))
    os.makedirs(SHOTS, exist_ok=True)
    fixtures = {"bootstrap": fx["bootstrap"]}
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge")

        # ============ 场景 A：连着打开多个（用户报的那条） ============
        page, errs = open_app(browser, fixtures)
        st0 = states(page)
        check("起点：5 个开关全关、独有看板非空",
              all(not st0[k]["checked"] for k in AGENTS) and
              st0["__unique"] not in ("", "0"), st0)
        uniq0 = st0["__unique"]

        # ① 连点 5 个：请求立刻发出，不先空等进度条
        t0 = page.evaluate("Date.now()")
        flip(page, AGENTS)
        pending(page, 5)
        first_ms = page.evaluate("window.__SK_TS[0] - %d" % t0)
        check("连点后首笔请求 <200ms 发出（不空等进度条）", first_ms < 200, first_ms)
        st = states(page)
        check("5 个开关都保持用户拨开的状态", all(st[k]["checked"] for k in AGENTS), st)

        # ②③ 第一份回包落地：只该宣告 ag0 连上，不许把在途的 4 个刷回「已关闭」
        deliver(page, fx["set_agent"]["ag0"])
        settle(page)
        st = states(page)
        check("ag0 落地后显示已连接", st["ag0"]["status"].startswith("已连接"), st["ag0"])
        check("其余 4 个在途：开关不弹回、标「连接中」",
              all(st[k]["checked"] and st[k]["pending"] for k in AGENTS[1:]),
              {k: st[k] for k in AGENTS[1:]})
        check("在途开关禁用，挡掉重复工",
              all(st[k]["disabled"] for k in AGENTS[1:]),
              {k: st[k]["disabled"] for k in AGENTS[1:]})
        check("汇总文案说得出几个还在途", st["__sub"].count("连接中") == 1 and
              "4 连接中" in st["__sub"], st["__sub"])

        # ④ 乱序到达：ag4 的回包先到，其后是更旧的四份 —— 旧快照不得覆盖新状态
        for k in ["ag4", "ag1", "ag3", "ag2"]:
            deliver(page, fx["set_agent"][k])
        settle(page)
        st = states(page)
        check("乱序回包后 5 个全部已连接（旧快照被忽略）",
              all(st[k]["checked"] and not st[k]["pending"] and
                  st[k]["status"].startswith("已连接") for k in AGENTS), st)
        check("开关回包沿用上一轮独有计数（不重扫独有也不刷成 0）",
              st["__unique"] == uniq0, "before=%s after=%s" % (uniq0, st["__unique"]))
        check("全部落定后汇总不再挂「连接中」", "连接中" not in st["__sub"], st["__sub"])
        page.screenshot(path=os.path.join(SHOTS, "agent-toggle-multi-on.png"))
        page.close()
        check("场景 A 全程 0 条 JS 错误", not errs, errs[:3])

        # ============ 场景 B：撤链接方向的在途与失败回落 ============
        # 这份 fixture 的真实状态就是「只有 ag1 开着」，回包内容与界面数据自洽
        page, errs = open_app(browser, {"bootstrap": fx["b_on"]})
        settle(page)
        st = states(page)
        check("场景 B 起点：ag1 已连接", st["ag1"]["status"].startswith("已连接"), st["ag1"])

        # ⑥ 关闭在途：开关保持用户拨下的「关」，不弹回「已开」
        page.evaluate("window.__SK_HANDS = []")
        flip_off(page, "ag1")
        pending(page, 1)
        st = states(page)
        check("关闭在途：开关不弹回、标「连接中」并禁用",
              not st["ag1"]["checked"] and st["ag1"]["pending"] and st["ag1"]["disabled"],
              st["ag1"])

        # ⑤ 该笔失败：回落到后端数据（仍已连接），且不留「连接中」
        deliver(page, {"ok": False, "error": "模拟撤链接失败"})
        settle(page)
        st = states(page)
        check("失败后 ag1 回落到后端数据（仍已连接）且无在途残留",
              st["ag1"]["checked"] and not st["ag1"]["pending"] and
              not st["ag1"]["disabled"] and st["ag1"]["status"].startswith("已连接"),
              st["ag1"])

        # ⑦ 关闭成功：ag1 变已关闭，其余 agent 不受这份快照影响
        page.evaluate("window.__SK_HANDS = []")
        flip_off(page, "ag1")
        pending(page, 1)
        deliver(page, fx["b_off"])
        settle(page)
        st = states(page)
        check("关闭成功后 ag1 已关闭、无在途残留",
              not st["ag1"]["checked"] and not st["ag1"]["pending"] and
              st["ag1"]["status"].startswith("已关闭"), st["ag1"])
        page.screenshot(path=os.path.join(SHOTS, "agent-toggle-off.png"))
        page.close()
        check("场景 B 全程 0 条 JS 错误", not errs, errs[:3])

        browser.close()

    bad = [n for n, ok, _ in results if not ok]
    print("\n%d/%d 通过" % (len(results) - len(bad), len(results)))
    if bad:
        print("失败：" + " / ".join(bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
