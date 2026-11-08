#!/usr/bin/env python3
"""原型（file://）boot 专项回归 · SPEC-boot-animation.md §7 的 6 项 + noboot 行为。

python kit/test_boot_proto.py（需 playwright + 本机 Edge/Chromium）
"""
import json
import sys
import time

from playwright.sync_api import sync_playwright

URL = "file:///D:/skilllocker/ui/index.html"
results = []


def check(name, ok, value=""):
    results.append((name, bool(ok), value))
    print("  [%s] %-28s %s" % ("PASS" if ok else "FAIL", name, value))


with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge")

    # ---------- 1) file:// 满速完整编排 ----------
    page = browser.new_page(viewport={"width": 1200, "height": 760})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    t0 = time.time()
    page.goto(URL)
    page.wait_for_selector("#boot.run", state="attached", timeout=5000)
    # chips 峰值 ≤ 14（.out 的那个正在退场，不计）：异步采样，别阻塞编排时钟
    peak = page.evaluate("""new Promise(function (res) {
        var n = 0;
        var iv = setInterval(function () {
            n = Math.max(n, document.querySelectorAll('.boot-chip:not(.out)').length);
        }, 30);
        setTimeout(function () { clearInterval(iv); res(n); }, 2700);
    })""")
    check("chips 上限 14", 0 < peak <= 14, "peak=%d" % peak)
    # 计数器与 agent 点亮终值 = 快照真实值
    page.wait_for_selector("#boot.ok", timeout=8000)
    cnt = page.text_content(".boot-cnt").strip()
    check("计数器终值=sharedCount", cnt == "51 / 51", cnt)
    on_n = page.eval_on_selector_all(".boot-agent.on", "els => els.length")
    total_n = page.eval_on_selector_all(".boot-agent", "els => els.length")
    check("agent 点亮数=7", on_n == 7 and total_n == 7, "%d/%d" % (on_n, total_n))
    ph = page.text_content(".boot-ph").strip()
    line = page.text_content(".boot-line").strip()
    check("就绪相位文案", ph == "就绪" and "51 个技能就绪" in line and "7 个 agent" in line,
          "%s | %s" % (ph, line))
    # reveal：done + ready，随后 hidden；file:// 满速 → done 应在 ~4.94s 出现。
    # state=attached：done 后 320ms overlay 即转 hidden，快机器上等 visible 会错过窗口
    page.wait_for_selector("#boot.done", state="attached", timeout=8000)
    t_done = time.time() - t0
    ready_cls = page.evaluate("document.getElementById('app-window').classList.contains('ready')")
    check("reveal 级联 done+ready", ready_cls, "t=%.2fs" % t_done)
    check("file:// 满速时长≈4.94s", 4.4 <= t_done <= 6.0, "t=%.2fs" % t_done)
    page.wait_for_selector("#boot", state="hidden", timeout=4000)
    check("overlay 最终 hidden", page.is_hidden("#boot"))
    # 视图无关计数（网格根级是文件夹卡 + 未归档文件卡，列表才是 .row）
    rows = page.eval_on_selector_all(".row, .file-card, .folder-card:not(.ghost)", "els => els.length")
    check("原型主界面可渲染", rows > 0, "cards/rows=%d" % rows)
    check("原型 0 条 JS 错误", not errors, "; ".join(errors[:3]))
    # 深色切换：overlay 背景 token 自动跟随
    page.evaluate("document.body.classList.add('dark')")
    bg = page.evaluate("getComputedStyle(document.getElementById('boot')).backgroundColor")
    check("深色下 overlay 背景跟随", bg == "rgb(8, 11, 18)", bg)
    page.close()

    # ---------- 2) ?noboot=1：与 v0.2.0 行为一致 ----------
    page2 = browser.new_page(viewport={"width": 1180, "height": 720})
    errors2 = []
    page2.on("pageerror", lambda e: errors2.append(str(e)))
    page2.goto(URL + "?noboot=1")
    page2.wait_for_function("window.__SK_READY === true", timeout=8000)
    boot_state = page2.evaluate("""(function(){
        var b = document.getElementById('boot');
        return { hidden: !!b && b.hidden, run: !!b && b.classList.contains('run'),
                 winBoot: document.getElementById('app-window').classList.contains('booting') };
    })()""")
    check("noboot: overlay 立即回收", boot_state["hidden"] and not boot_state["run"]
          and not boot_state["winBoot"], json.dumps(boot_state))
    sw = page2.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    check("noboot: 1180×720 无横向溢出", sw)
    rows2 = page2.eval_on_selector_all(".row, .file-card, .folder-card:not(.ghost)", "els => els.length")
    # 期望值与正常页对齐（noboot 只影响 overlay，不影响渲染内容），不写死视图口径
    check("noboot: 主界面行为不变", rows2 > 0 and rows2 == rows, "cards/rows=%d (normal page=%d)" % (rows2, rows))
    check("noboot: 0 条 JS 错误", not errors2, "; ".join(errors2[:3]))
    page2.close()

    # ---------- 3) 桌面壳 bootstrap 失败 → 启动错误页（FIRST-RUN-FIX-PLAN FIX-04）----------
    # file:// + ?desktop=1，注入假 pywebview api（bootstrap 永远 reject）：
    # 必须渲染错误页，绝不退回内置演示快照
    page3 = browser.new_page(viewport={"width": 1200, "height": 760})
    errors3 = []
    page3.on("pageerror", lambda e: errors3.append(str(e)))
    page3.add_init_script("""
      window.pywebview = { api: {
        bootstrap: function () {
          return Promise.reject(
            new Error('RuntimeError: 中央仓库目录不存在：Q:/skilllocker/store'));
        }
      } };
    """)
    page3.goto(URL + "?desktop=1")
    page3.wait_for_selector("#boot-error", timeout=8000)
    check("错误页出现", page3.is_visible("#boot-error"))
    title = page3.text_content("#boot-error h1").strip()
    detail = page3.text_content(".boot-error-detail").strip()
    check("错误页标题与详情", title == "后端启动失败" and "中央仓库目录不存在" in detail,
          "%s | %s" % (title, detail[:40]))
    check("重试按钮存在", page3.is_visible("#boot-error-retry"))
    hint = page3.text_content(".boot-error-hint")
    check("日志提示行", "skilllocker.log" in hint, hint.strip()[:50])
    mode = page3.evaluate("window.__SK_MODE")
    snap_rows = page3.eval_on_selector_all(".row, .file-card, .folder-card:not(.ghost)",
                                           "els => els.length")
    check("进入 error 模式且无演示数据", mode == "error" and snap_rows == 0,
          "mode=%s rows=%d" % (mode, snap_rows))
    check("错误页 0 条 JS 错误", not errors3, "; ".join(errors3[:3]))
    page3.close()

    # ---------- 4) 桌面壳内注入超时 → 启动错误页（评审 M-2 / M-1）----------
    # file:// + ?desktop=1 但不注入 pywebview：boot() 必须走错误页，绝不退回内置
    # 演示快照。注入窗口分两轮（bridge.js）：首轮 20s 给足冷启动耐心（首装文件
    # 刚落地时 WebView2 + 杀软扫描会拖很久），判死后 boot() 自动整页重载一次
    # （bridgeRetry=1），重试轮进程已热，只给 8s —— 别再让人对着动画白等。
    # 实测三轮 28.00 / 28.43 / 28.44s（改两轮窗口之前是 40.05~40.14s）。
    page4 = browser.new_page(viewport={"width": 1200, "height": 760})
    errors4, navs4 = [], []
    page4.on("pageerror", lambda e: errors4.append(str(e)))
    page4.on("framenavigated", lambda f: navs4.append(f.url))
    page4.goto(URL + "?desktop=1")
    t4 = time.time()

    # 桥没接上时不许 reveal 空壳：硬上限到点应改停在「等待桌面端」并报真实秒数。
    # 采样点落在首轮 20s 窗口内（8s 硬上限之后、20s 判死之前）。
    def sample():
        return page4.evaluate("""() => {
          const boot = document.getElementById('boot'), win = document.getElementById('app-window');
          const q = (s) => { const e = boot && boot.querySelector(s); return e ? (e.textContent || '').trim() : null; };
          return {hidden: !boot || boot.hidden === true, booting: !!win && win.classList.contains('booting'),
                  phase: q('.boot-ph'), line: q('.boot-line'),
                  rows: document.querySelectorAll('.row, .file-card, .folder-card:not(.ghost)').length};
        }""")

    page4.wait_for_timeout(10000)
    s1 = sample()
    check("桥未落地时不 reveal 空壳（t≈10s）",
          not s1["hidden"] and s1["booting"] and s1["rows"] == 0,
          "hidden=%s booting=%s rows=%s" % (s1["hidden"], s1["booting"], s1["rows"]))
    check("停在等待相位并报真实秒数",
          "等待桌面端" in (s1["phase"] or "") and "正在连接桌面端" in (s1["line"] or "")
          and "已等待" in (s1["line"] or ""),
          "%s | %s" % (s1["phase"], s1["line"]))
    page4.wait_for_timeout(7000)
    s2 = sample()
    check("等待文案按秒数推进（t≈17s）",
          "已等待" in (s2["line"] or "") and (s2["phase"] or "") == s1["phase"]
          and not s2["hidden"], "%s" % s2["line"])
    page4.wait_for_selector("#boot-error", timeout=60000)
    t_err = time.time() - t4
    check("注入超时走错误页", page4.is_visible("#boot-error"), "t=%.2fs" % t_err)
    check("判死=首轮 20s + 重试轮 8s", 26.0 <= t_err <= 34.0, "t=%.2fs" % t_err)
    # 自动重载只能有一次：多了就是重载风暴，用户永远看不到错误页
    reloads = [u for u in navs4 if "bridgeRetry=1" in u]
    check("自动重载恰好一次", len(reloads) == 1, "%d 次 · %s"
          % (len(reloads), [u.split("index.html")[-1] for u in navs4]))
    check("重载后 URL 带 bridgeRetry=1", "bridgeRetry=1" in page4.url, page4.url.split("index.html")[-1])
    title4 = page4.text_content("#boot-error h1").strip()
    detail4 = page4.text_content(".boot-error-detail").strip()
    check("注入超时错误详情", title4 == "后端启动失败" and "注入超时" in detail4,
          "%s | %s" % (title4, detail4[:40]))
    # 用户在这页之前已经等了 28 秒，文案必须交代「重试过、两轮各等多久」，
    # 否则看起来就是程序卡死
    check("错误详情交代自动重试与两轮时长",
          "已自动重试 1 次" in detail4 and "20 秒" in detail4 and "8 秒" in detail4,
          detail4[:76])
    check("重试按钮存在", page4.is_visible("#boot-error-retry"))
    mode4 = page4.evaluate("window.__SK_MODE")
    dm4 = page4.evaluate("document.documentElement.getAttribute('data-mode')")
    snap4 = page4.eval_on_selector_all(".row, .file-card, .folder-card:not(.ghost)",
                                       "els => els.length")
    check("进入 error 模式且无演示数据", mode4 == "error" and dm4 == "error"
          and snap4 == 0, "mode=%s data-mode=%s rows=%d" % (mode4, dm4, snap4))
    page4.wait_for_selector("#boot", state="hidden", timeout=8000)
    check("boot overlay 已收尾", page4.is_hidden("#boot"))
    check("注入超时页 0 条 JS 错误", not errors4, "; ".join(errors4[:3]))
    page4.close()

    # ---------- 5) 桌面壳里桥正常接上 → 编排照常收尾（回归防线）----------
    # 等待态（BootFX.awaitBridge）只该在桥没落地时接管硬上限；桥按时接上就必须
    # 走正常首屏：不进「等待桌面端」，reveal 照常完成。这条是那次改动的回归护栏。
    page5 = browser.new_page(viewport={"width": 1200, "height": 760})
    errors5 = []
    page5.on("pageerror", lambda e: errors5.append(str(e)))
    page5.add_init_script("""
      (function () {
        window.pywebview = { api: {
          // 300ms 后成功返回：模拟真实桌面壳里桥按时接上、bootstrap 正常回包
          bootstrap: function () {
            return new Promise(function (resolve) {
              setTimeout(function () {
                resolve({ ok: true, data: window.SKILLLOCKER_DATA, meta: {} });
              }, 300);
            });
          },
          log_ui: function () { return Promise.resolve({ ok: true }); }
        } };
      })();
    """)
    page5.goto(URL + "?desktop=1")
    page5.wait_for_timeout(3000)
    s5a = page5.evaluate("""() => {
      const boot = document.getElementById('boot');
      const q = (s) => { const e = boot && boot.querySelector(s); return e ? (e.textContent || '').trim() : null; };
      return {phase: q('.boot-ph'), line: q('.boot-line')};
    }""")
    check("桥按时接上时不进等待态",
          "等待桌面端" not in (s5a["phase"] or "") and "正在连接桌面端" not in (s5a["line"] or ""),
          "%s | %s" % (s5a["phase"], s5a["line"]))
    page5.wait_for_selector("#boot", state="hidden", timeout=10000)
    mode5 = page5.evaluate("window.__SK_MODE")
    rows5 = page5.eval_on_selector_all(".row, .file-card, .folder-card:not(.ghost)",
                                       "els => els.length")
    booting5 = page5.evaluate("document.getElementById('app-window').classList.contains('booting')")
    check("桥正常：主界面照常 reveal", mode5 == "db" and rows5 > 0 and not booting5,
          "mode=%s rows=%d booting=%s" % (mode5, rows5, booting5))
    check("桥正常路径 0 条 JS 错误", not errors5, "; ".join(errors5[:3]))
    page5.close()
    browser.close()

fails = [r for r in results if not r[1]]
print("=== PROTO-BOOT %d/%d ===" % (len(results) - len(fails), len(results)))
sys.exit(1 if fails else 0)
