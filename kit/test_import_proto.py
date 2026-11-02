#!/usr/bin/env python3
"""导入 skill 原型（design-skill-import.html · file://）Playwright 回归。

SPEC-skill-import.md §7.2 的固化：auto-open → demo 直达各预检态 → 冲突键入
焦点保持 → 同内容跳过 → 缺 SKILL.md → 全流程 done 计数 51→52 + .is-new →
失败演练 → 在线 fetch→review → 深色 → DataTransfer 合成拖拽全窗遮罩与预检
回填 → Esc 链。计数敏感的场景按「重载页复位」分段，互不污染。

python kit/test_import_proto.py（需 playwright + 本机 Edge/Chromium）
"""
import sys

from playwright.sync_api import sync_playwright

URL = "file:///D:/skilllocker/ui/design-skill-import.html"
results = []


def check(name, ok, value=""):
    results.append((name, bool(ok), value))
    print("  [%s] %-30s %s" % ("PASS" if ok else "FAIL", name, value))


def open_review(page, demo):
    """demo 直达预检：必要时重开弹窗 / 回来源页 → 点 chip → 等 620ms 扫描。"""
    page.evaluate(
        "(function(){"
        "if(!document.querySelector('.imp-modal'))"
        "document.getElementById('btn-import-side').click();"
        "var b=document.querySelector('[data-act=\"imp-back\"]');"
        "if(b)b.click();})()")
    page.wait_for_selector(".demo-chip[data-demo=%s]" % demo, timeout=3000)
    page.evaluate(
        "document.querySelector(\".demo-chip[data-demo=%s]\").click()" % demo)
    page.wait_for_selector(".imp-modal .rep", timeout=4000)


with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge")
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(URL)
    # ---------- 1) auto-open ----------
    page.wait_for_selector(".imp-modal", timeout=5000)
    step0 = page.text_content(".imp-stp.cur")
    check("auto-open 步骤条=选择来源", "选择来源" in step0, step0)
    # ---------- 2) demo 直达标准 skill ----------
    open_review(page, "'ok'")
    start_on = page.evaluate(
        "var b=document.querySelector('[data-act=\"imp-start\"]');!!b && !b.disabled")
    md_row = page.evaluate(
        "!!document.querySelector('.rep .r-mono') "
        "&& document.querySelector('.rep').textContent.indexOf('frontmatter')>-1")
    check("标准 skill 预检报告+可导入", start_on and md_row,
          "start=%s md=%s" % (start_on, md_row))
    # 返回来源页
    page.evaluate("document.querySelector('[data-act=\"imp-back\"]').click()")
    # ---------- 3) 同名冲突 + 键入焦点保持 ----------
    open_review(page, "'dup'")
    pv = page.eval_on_selector(
        "[data-role=\"panel-conflict\"] .pv-name", "e => e.textContent")
    check("同名冲突面板 pv-name=pdf", pv == "pdf", pv)
    page.evaluate(
        "var n=document.getElementById('imp-name');n.value='pdf-2';"
        "n.dispatchEvent(new Event('input',{bubbles:true}))")
    hidden_after = page.evaluate(
        "document.querySelector('[data-role=\"panel-conflict\"]').hidden")
    focus_kept = page.evaluate("document.activeElement.id") == "imp-name"
    start_on2 = page.evaluate(
        "var b=document.querySelector('[data-act=\"imp-start\"]');!!b && !b.disabled")
    # 注意：.panel-t 是 flex 容器，innerText 会在图标与 <span> 间插换行，
    # 断言只取 pv-name 的 textContent，不用跨元素连续子串
    check("改键入后面板隐藏+焦点保持+可导入",
          hidden_after and focus_kept and start_on2,
          "hidden=%s focus=%s start=%s" % (hidden_after, focus_kept, start_on2))
    # ---------- 4) 同内容：same 面板 + 跳过 ----------
    open_review(page, "'same'")
    same_on = page.evaluate(
        "var p=document.querySelector('[data-role=\"panel-same\"]');"
        "!!p && !p.hidden")
    start_off = page.evaluate(
        "var b=document.querySelector('[data-act=\"imp-start\"]');!!b && b.disabled")
    page.evaluate("document.querySelector('[data-act=\"imp-skip\"]').click()")
    closed = page.evaluate("!document.querySelector('.imp-modal')")
    check("同内容面板+禁导入+跳过关闭", same_on and start_off and closed,
          "same=%s off=%s closed=%s" % (same_on, start_off, closed))
    # ---------- 5) 缺 SKILL.md：警示不阻断 ----------
    open_review(page, "'warn'")
    warn = page.evaluate(
        "!!document.querySelector('.note-bar.warn') "
        "&& document.querySelector('.note-bar.warn').textContent.indexOf('未找到 SKILL.md')>-1")
    start_on3 = page.evaluate(
        "var b=document.querySelector('[data-act=\"imp-start\"]');!!b && !b.disabled")
    check("缺 SKILL.md 警示仍可导入", warn and start_on3,
          "warn=%s start=%s" % (warn, start_on3))
    # ---------- 8) 在线 fetch → review（演示步） ----------
    page.evaluate("document.querySelector('[data-act=\"imp-back\"]').click()")
    page.evaluate(
        "document.querySelector('[data-act=\"imp-tab\"][data-tab=\"online\"]').click()")
    page.evaluate("document.querySelector('[data-act=\"imp-fetch\"]').click()")
    page.wait_for_selector(".imp-modal .rep", timeout=4000)
    fetch_ok = page.evaluate(
        "document.getElementById('imp-name').value.indexOf('skill-creator')>-1")
    check("在线获取→预检回填", fetch_ok, "name=%r" % page.evaluate(
        "document.getElementById('imp-name').value"))
    # ---------- 9) 深色 ----------
    page.evaluate("document.getElementById('theme-toggle').click()")
    dark = page.evaluate("document.getElementById('sk-app').classList.contains('theme-dark')")
    check("深色模式切换", dark)
    # ---------- 10) 合成拖拽：全窗遮罩 + 预检回填 ----------
    page.reload()
    page.wait_for_selector("#sk-app", timeout=5000)
    page.wait_for_timeout(600)  # 420ms auto-open；先关掉再测全窗拖拽
    page.evaluate(
        "if(document.querySelector('.imp-modal'))"
        "document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))")
    drag = page.evaluate("""(function () {
      var dt = new DataTransfer();
      dt.items.add(new File([''], 'my-skill', { type: '' }));
      var ev = new DragEvent('dragenter', { bubbles: true, cancelable: true, dataTransfer: dt });
      document.dispatchEvent(ev);
      return { types: Array.prototype.indexOf.call(dt.types, 'Files') > -1,
               ov: !!document.querySelector('.drop-ov') };
    })()""")
    page.wait_for_timeout(120)
    ov = page.evaluate("!!document.querySelector('.drop-ov')")
    dropped = page.evaluate("""(function () {
      var dt = new DataTransfer();
      dt.items.add(new File([''], 'my-skill', { type: '' }));
      var ev = new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer: dt });
      document.dispatchEvent(ev);
      return true;
    })()""")
    page.wait_for_selector(".imp-modal .rep", timeout=4000)
    name_filled = page.evaluate(
        "document.getElementById('imp-name').value.indexOf('my-skill')>-1")
    check("合成拖拽遮罩+预检回填",
          drag["types"] and drag["ov"] and ov and dropped and name_filled,
          "enter=%s ov=%s drop=%s name=%s" % (drag["types"], drag["ov"], dropped,
                                              name_filled))
    # ---------- 6) 全流程 done：计数 51→52 + .is-new ----------
    page.evaluate("document.querySelector('[data-act=\"imp-start\"]').click()")
    page.wait_for_selector(".imp-modal .hero.ok", timeout=6000)
    cnt = page.text_content("#store-count").strip()
    is_new = page.evaluate("!!document.querySelector('.file-card.is-new')")
    check("全流程 done 计数 52+is-new", cnt == "52" and is_new,
          "cnt=%s is_new=%s" % (cnt, is_new))
    # 去分发（原型 toast + 关闭）
    page.evaluate("document.querySelector('[data-act=\"imp-finish\"]').click()")
    check("完成页关闭", page.evaluate("!document.querySelector('.imp-modal')"))
    # ---------- 7) 失败演练 ----------
    page.evaluate("document.getElementById('btn-import-side').click()")
    open_review(page, "'fail'")
    page.evaluate("document.querySelector('[data-act=\"imp-start\"]').click()")
    page.wait_for_selector(".imp-modal .hero.bad", timeout=6000)
    bad = page.evaluate(
        "document.querySelector('.hero.bad').textContent.indexOf('写入中央仓库失败')>-1")
    check("失败演练 hero.bad", bad)
    page.evaluate("document.querySelector('[data-act=\"imp-back\"]').click()")
    back = page.evaluate(
        "var s=document.querySelector('.imp-stp.cur');!!s && s.textContent.indexOf('选择来源')>-1")
    check("失败后返回修改", back)
    # ---------- 11) Esc 链：弹窗 → 清搜索 ----------
    page.evaluate("document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))")
    page.evaluate("document.getElementById('btn-import-top').click()")
    open_m = page.evaluate("!!document.querySelector('.imp-modal')")
    page.evaluate("document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))")
    esc_closed = page.evaluate("!document.querySelector('.imp-modal')")
    check("Esc 关导入弹窗", open_m and esc_closed, "open=%s closed=%s" % (open_m, esc_closed))
    check("原型 0 条 JS 错误", not errors, "; ".join(errors[:3]))
    browser.close()

fails = [n for n, ok, _v in results if not ok]
print("=== IMPORT-PROTO %d/%d ===" % (len(results) - len(fails), len(results)))
if fails:
    print("failed:", "; ".join(fails))
sys.exit(1 if fails else 0)
