#!/usr/bin/env python3
"""skilllocker 桌面端无人值守自检（desktop.py --selftest 的执行体）。

离屏建一个隐藏窗口，跑完整条 UI 链路（bridge / 原生窗口 chrome / 拖拽缩放 /
写库与删除安全兜底），逐项断言后输出 SELFTEST n/n 并退出进程。
要借 desktop 的原生窗口辅助函数；desktop.main 只在 --selftest 分支懒加载
本模块，所以不存在循环导入。产物：控制台或 pythonw 下的 kit/selftest.log。
"""
import ctypes
import ctypes.wintypes  # noqa: F401 - GetWindowLongW 等需要 wintypes 结构体
import json
import os
import re
import shutil
import sys
import tempfile
import time

import skilllocker

from desktop import (INDEX, KIT, MIN_H, MIN_W, WIN_H, WIN_W, WS_MINIMIZEBOX,
                     _WINDOW_ICONS, _asset_version, _ensure_minimize_box,
                     _hwnd_by_title, _refresh_window_chrome, _window_icon)


def _wait_ready(window, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if window.evaluate_js("!!window.__SK_READY"):
                return True
        except Exception:  # noqa: BLE001 - 页面还在加载
            pass
        time.sleep(0.1)
    return False


def _check(window, results, name, code, expect=None):
    try:
        value = window.evaluate_js(code)
    except Exception as exc:  # noqa: BLE001
        results.append((name, False, "抛出异常 %r" % (exc,)))
        return None
    ok = value == expect if expect is not None else bool(value)
    results.append((name, ok, repr(value)))
    return value


def _await_js(window, key, timeout=15.0):
    """等一个 JS promise 把结果塞到 window.<key>，再取回解析后的对象。

    bootstrap 现在要扫插件缓存（秒级），隐藏窗口还会节流定时器，5 秒不够。
    就绪即返回，超时只是兜底。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if window.evaluate_js("!!window.%s" % key):
                break
        except Exception:  # noqa: BLE001 - 页面加载期 evaluate_js 可能抛错（与 _poll_js 同口径）
            pass
        time.sleep(0.1)
    try:
        raw = window.evaluate_js("JSON.stringify(window.%s||null)" % key)
    except Exception:  # noqa: BLE001
        return None
    return json.loads(raw) if raw else None


def _poll_js(window, code, timeout=6.0):
    """轮询一个 JS 表达式直到为真；页面导航期 evaluate_js 可能抛错，按假处理。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if window.evaluate_js(code):
                return True
        except Exception:  # noqa: BLE001 - 页面还在加载
            pass
        time.sleep(0.1)
    return False


def _selftest_native_window(results):
    """自检原生窗口：任务栏图标位 + 最小化位（以及被 WinForms 抹掉后能补回）。"""
    if sys.platform != "win32":
        results.append(("任务栏原生窗口", True, "非 Windows，跳过"))
        return
    hwnd = _refresh_window_chrome()
    if not hwnd:
        results.append(("任务栏原生窗口", False, "找不到主窗口"))
        return
    style = ctypes.windll.user32.GetWindowLongW(ctypes.wintypes.HWND(hwnd), -16)
    results.append(("任务栏最小化位", bool(style & WS_MINIMIZEBOX), hex(style & 0xffffffff)))
    small_px = ctypes.windll.user32.GetSystemMetrics(49) or 32
    expect_small = _WINDOW_ICONS.get((0, small_px))
    expect_big = _WINDOW_ICONS.get((1, ctypes.windll.user32.GetSystemMetrics(11) or 32))
    got_small = _window_icon(hwnd, 0)
    got_big = _window_icon(hwnd, 1)
    got_small2 = _window_icon(hwnd, 2)
    results.append(("任务栏小图标=skilllocker.ico", bool(expect_small) and got_small == expect_small,
                    "got=%r expect=%r" % (got_small, expect_small)))
    results.append(("任务栏大图标=skilllocker.ico", bool(expect_big) and got_big == expect_big,
                    "got=%r expect=%r" % (got_big, expect_big)))
    results.append(("任务栏小图标2=skilllocker.ico", bool(expect_small) and got_small2 == expect_small,
                    "got=%r expect=%r" % (got_small2, expect_small)))
    # 模拟 WinForms 在状态变化时抹掉最小化位，确保兜底函数能补回
    ctypes.windll.user32.SetWindowLongW(ctypes.wintypes.HWND(hwnd), -16,
                                       style & ~WS_MINIMIZEBOX)
    _ensure_minimize_box(hwnd)
    style2 = ctypes.windll.user32.GetWindowLongW(ctypes.wintypes.HWND(hwnd), -16)
    results.append(("最小化位可自动补回", bool(style2 & WS_MINIMIZEBOX), hex(style2 & 0xffffffff)))


def _edited_count(meta_map):
    """与 app.js `editedCount()` 同一规则：只有显示名 / 备注算编辑（归属夹不算）。"""
    total = 0
    for m in meta_map.values():
        if m.get("display") or m.get("note"):
            total += 1
    return total


def _selftest_body(window, results):
    _selftest_native_window(results)
    # 期望值一律取自真实后端，不写死数字：store 里增删 skill 后自检仍应成立
    con = skilllocker.db()
    try:
        boards = skilllocker.list_boards(con, skilllocker.load_config())
    finally:
        con.close()
    sh_total = len(boards["skills"])
    ag_rows = boards["agents"]
    # 独有 / 冲突 / agent 计数同样现算：库里增删 skill、冲突收口后自检仍应成立
    un_rows = boards.get("unique") or []
    un_total = len(un_rows)
    un_keys = sorted("%s/%s" % (u["tool"], u["name"]) for u in un_rows)
    conflict = boards.get("conflict")
    ag_connected = sum(1 for a in ag_rows if a["status"] == "connected")
    ag_missing = [a["key"] for a in ag_rows if a["status"] == "missing"]
    # 分类夹数与未归档数一律后端现算：folders 增删后自检仍应成立
    folders_rows = boards.get("folders") or []
    folder_total = len(folders_rows)
    store_path = skilllocker.load_config()["store"].replace("/", "\\")
    con = skilllocker.db()
    try:
        meta_map = skilllocker.get_meta(con)
    finally:
        con.close()
    unfiled_total = sum(
        1 for s in boards["skills"] if not (meta_map.get(s["name"]) or {}).get("folder"))
    edited0 = _edited_count(meta_map)

    # 上次运行可能停留在「智能体」看板或网格视图，自检统一从共有技能页 +
    # 列表视图 + pdf 详情开始（网格下没有 .row）
    window.evaluate_js("document.getElementById('nav-shared').click()")
    time.sleep(0.3)
    window.evaluate_js(
        "(function(){var b=document.querySelector(\"#view-seg [data-view-mode='list']\");"
        "if(b)b.click()})()")
    time.sleep(0.3)
    window.evaluate_js("document.querySelector('.row[data-key=\"pdf\"]').click()")
    time.sleep(0.3)
    _check(window, results, "运行模式=db", "window.__SK_MODE", "db")
    _check(window, results, "无 JS 错误", "window.__SK_ERRORS.length", 0)
    _check(window, results, "共有行数=%d" % sh_total,
           "document.querySelectorAll('.row').length", sh_total)
    _check(window, results, "侧栏共有=%d" % sh_total,
           "document.getElementById('nav-shared').textContent", str(sh_total))
    _check(window, results, "侧栏独有=%d" % un_total,
           "document.getElementById('nav-unique').textContent", str(un_total))
    _check(window, results, "已编辑初始=%d" % edited0,
           "document.getElementById('stat-edited').textContent", str(edited0))
    _check(window, results, "分类夹数=%d" % folder_total,
           "document.getElementById('stat-folders').textContent", str(folder_total))
    _check(window, results, "未归档数=%d" % unfiled_total,
           "document.getElementById('stat-unfiled').textContent", str(unfiled_total))
    _check(window, results, "agent=%d" % ag_connected,
           "document.getElementById('stat-agents').textContent", str(ag_connected))
    _check(window, results, "路径来自 cfg", "document.getElementById('store-path').textContent",
           store_path)
    _check(window, results, "选中 pdf", "window.__SK_APP.state.selected", "pdf")
    _check(window, results, "列表上方筛选行已移除",
           "document.querySelectorAll('.filters,.fchip,#filters,#count-text').length", 0)
    # 序号列：按当前列表顺序 1..N，纯展示
    _check(window, results, "首行序号=1", "document.querySelector('.row .row-num').textContent", "1")
    _check(window, results, "序号连续 1..%d" % sh_total,
           "(function(){var n=[].map.call(document.querySelectorAll('.row .row-num'),"
           "function(e){return +e.textContent});return n.length===" + str(sh_total) +
           "&&n.every(function(v,i){return v===i+1})})()", True)
    # 两位数（第 10 行）仍要装得进 26×26 的格子里，不溢出
    _check(window, results, "序号格 26x26 不溢出",
           "(function(){var e=document.querySelectorAll('.row .row-num')[9];"
           "var r=e.getBoundingClientRect();"
           "return Math.round(r.width)+'x'+Math.round(r.height)+':'+e.textContent+':'"
           "+(e.scrollWidth<=e.clientWidth)})()", "26x26:10:true")
    _check(window, results, "详情栏有删除按钮",
           "document.querySelectorAll('[data-delete]').length", 1)

    # ------------------------------------------------ 分类文件夹（§7.1）
    def _refresh_ui():
        window.evaluate_js(
            "window.__SK_RF=0;window.pywebview.api.bootstrap().then(function(r){"
            "window.__SK_APP.refresh(r);window.__SK_RF=1})")
        _await_js(window, "__SK_RF")

    pdf_old_folder = (meta_map.get("pdf") or {}).get("folder") or ""

    # 1) 网格视图：文件夹卡片数 = 后端 folders 数（期望值现算）
    window.evaluate_js(
        "(function(){var b=document.querySelector(\"#view-seg [data-view-mode='grid']\");"
        "if(b)b.click()})()")
    time.sleep(0.4)
    _check(window, results, "网格视图卡片数=%d" % folder_total,
           "document.querySelectorAll('.folder-card:not(.ghost)').length", folder_total)
    # 2) 新建虚卡存在
    _check(window, results, "网格新建虚卡存在",
           "!!document.querySelector('#grid-new-folder')", True)

    # 4) 进入首个文件夹（data-folder-card）：标题 = 夹名，行数 = 后端夹内计数
    f0 = folders_rows[0]
    con = skilllocker.db()
    try:
        f0_count = con.execute(
            "select count(*) from skill_meta where scope='shared' and folder_id=?",
            (f0["id"],)).fetchone()[0]
    finally:
        con.close()
    window.evaluate_js("document.querySelectorAll('[data-folder-card]')[0].click()")
    time.sleep(0.4)
    _check(window, results, "文件夹标题=%s" % f0["name"],
           "document.getElementById('page-title').textContent", f0["name"])
    _check(window, results, "夹内行数=%d" % f0_count,
           "document.querySelectorAll('.row').length", f0_count)

    # 5) 面包屑返回根：prefs.view 仍是网格，.folder-grid 应重新出现
    window.evaluate_js("var b=document.querySelector('[data-crumb-root]');if(b)b.click()")
    time.sleep(0.4)
    _check(window, results, "面包屑返回根可回网格",
           "!!document.querySelector('.folder-grid')", True)

    # 7) 删夹回落：建临时夹 + 归入 pdf → 卡片数 +1；删除 → 卡片数复原、
    #    夹内行 folder_id 置空、moved 计数正确
    con = skilllocker.db()
    try:
        tf = skilllocker.folder_create(con, "自检夹", "teal")
        con.commit()
        skilllocker.set_meta(con, "shared", "pdf", {"folder": tf["id"]})
    finally:
        con.close()
    _refresh_ui()
    time.sleep(0.3)
    _check(window, results, "建夹后卡片数=%d" % (folder_total + 1),
           "document.querySelectorAll('.folder-card:not(.ghost)').length", folder_total + 1)
    moved = 0
    pdf_row = None
    con = skilllocker.db()
    try:
        moved = skilllocker.folder_delete(con, tf["id"])["moved"]
        con.commit()
        pdf_row = con.execute(
            "select folder_id from skill_meta where scope='shared' and key='pdf'").fetchone()
    finally:
        con.close()
    _refresh_ui()
    time.sleep(0.3)
    _check(window, results, "删夹后卡片数=%d" % folder_total,
           "document.querySelectorAll('.folder-card:not(.ghost)').length", folder_total)
    results.append(("删夹后行回到未归档",
                    moved == 1 and bool(pdf_row) and (pdf_row["folder_id"] or "") == "",
                    "moved=%s folder_id=%r" % (moved, pdf_row["folder_id"] if pdf_row else None)))
    # 还原 pdf 原归属（有无都如实写回），UI 与库重新对齐
    con = skilllocker.db()
    try:
        skilllocker.set_meta(con, "shared", "pdf", {"folder": pdf_old_folder})
    finally:
        con.close()
    _refresh_ui()
    time.sleep(0.3)

    # 3) 切列表视图：.row 数 = sh_total（并恢复列表视图供后续断言使用）
    window.evaluate_js(
        "(function(){var b=document.querySelector(\"#view-seg [data-view-mode='list']\");"
        "if(b)b.click()})()")
    time.sleep(0.4)
    _check(window, results, "切列表后行数=%d" % sh_total,
           "document.querySelectorAll('.row').length", sh_total)

    # 6) 归属落库（纯后端口径）：set_meta(folder) 写 folder_id，'' 复原
    v1 = v2 = None
    con = skilllocker.db()
    try:
        skilllocker.set_meta(con, "shared", "pdf", {"folder": f0["id"]})
        v1 = con.execute(
            "select folder_id from skill_meta where scope='shared' and key='pdf'").fetchone()
        skilllocker.set_meta(con, "shared", "pdf", {"folder": ""})
        v2 = con.execute(
            "select folder_id from skill_meta where scope='shared' and key='pdf'").fetchone()
        ok6 = bool(v1) and v1["folder_id"] == f0["id"] and (v2["folder_id"] or "") == ""
    finally:
        con.close()
    results.append(("set_meta 归属落库", ok6,
                    "%r -> %r" % (v1["folder_id"] if v1 else None,
                                  v2["folder_id"] if v2 else None)))
    # #6 把 pdf 归属写成了 ''，这里还原原归属，别吃掉用户的真实数据
    con = skilllocker.db()
    try:
        skilllocker.set_meta(con, "shared", "pdf", {"folder": pdf_old_folder})
    finally:
        con.close()

    # 8) 迁移幂等：全新库（升级前 schema）回填一次，二次迁移不再动任何行
    import skilllocker_base as sk_base
    tmp_db = os.path.join(KIT, "_selftest_mig.db")
    for suffix in ("", "-wal", "-shm"):
        p = tmp_db + suffix
        if os.path.exists(p):
            os.remove(p)
    old_db_file = sk_base.DB_FILE
    sk_base.DB_FILE = tmp_db
    try:
        fcon = sk_base.db()
        fcon.execute(
            "insert or replace into skill_meta(scope,key,tool,display_name,note,"
            "categories,seed,updated_at) "
            "values('shared','__selftest_mig__','','','','[\"video\"]','video',0)")
        fcon.commit()
        n1 = sk_base.migrate_folders(fcon, skilllocker.load_config())
        r1 = fcon.execute("select folder_id from skill_meta where key='__selftest_mig__'").fetchone()["folder_id"]
        n2 = sk_base.migrate_folders(fcon, skilllocker.load_config())
        r2 = fcon.execute("select folder_id from skill_meta where key='__selftest_mig__'").fetchone()["folder_id"]
        fcon.close()
        ok8 = n1 == 1 and r1 == "video" and n2 == 0 and r2 == "video"
    except Exception as exc:  # noqa: BLE001 - 断言现场进结果
        ok8 = False
        r1 = r2 = "异常 %r" % (exc,)
        n1 = n2 = -1
    finally:
        sk_base.DB_FILE = old_db_file
        for suffix in ("", "-wal", "-shm"):
            p = tmp_db + suffix
            if os.path.exists(p):
                os.remove(p)
    results.append(("迁移回填且幂等", ok8, "first=%s(%r) second=%s(%r)" % (n1, r1, n2, r2)))

    # 独有看板：网格 = agent 文件夹卡（一个 agent 一个分类夹，与共有看板同款视觉）；
    # 列表 = 行数 / 行键集合 / 序号连续 1..N。网格下没有 .row，行断言前先切列表
    # （与共有看板网格的处理一致）；期望值一律按后端现算，不写死。
    un_tools = []
    for u in un_rows:
        if u["tool"] not in un_tools:
            un_tools.append(u["tool"])
    window.evaluate_js("document.getElementById('nav-unique').click()")
    time.sleep(0.3)
    window.evaluate_js(
        "(function(){var b=document.querySelector(\"#view-seg [data-view-mode='grid']\");"
        "if(b)b.click()})()")
    time.sleep(0.3)
    _check(window, results, "独有网格 agent 卡数=%d" % len(un_tools),
           "document.querySelectorAll('.folder-card:not(.ghost)').length", len(un_tools))
    if un_tools:
        t0 = un_tools[0]
        t0_label = next((a["label"] for a in ag_rows if a["key"] == t0), t0)
        t0_count = sum(1 for u in un_rows if u["tool"] == t0)
        window.evaluate_js("document.querySelectorAll('[data-folder-card]')[0].click()")
        time.sleep(0.3)
        _check(window, results, "agent 夹标题=%s" % t0_label,
               "document.getElementById('page-title').textContent", t0_label)
        _check(window, results, "agent 夹内行数=%d" % t0_count,
               "document.querySelectorAll('.row').length", t0_count)
        window.evaluate_js("var b=document.querySelector('[data-crumb-root]');if(b)b.click()")
        time.sleep(0.3)
        _check(window, results, "独有面包屑返回可回网格",
               "!!document.querySelector('.folder-grid')", True)
    window.evaluate_js(
        "(function(){var b=document.querySelector(\"#view-seg [data-view-mode='list']\");"
        "if(b)b.click()})()")
    time.sleep(0.3)
    _check(window, results, "独有行数=%d" % un_total,
           "document.querySelectorAll('.row').length", un_total)
    _check(window, results, "独有行键=后端集合",
           "(function(){var a=[].map.call(document.querySelectorAll('.row'),"
           "function(e){return e.getAttribute('data-key')});a.sort();"
           "return JSON.stringify(a)})()",
           json.dumps(un_keys, ensure_ascii=False, separators=(",", ":")))
    _check(window, results, "独有序号连续 1..%d" % un_total,
           "(function(){var n=[].map.call(document.querySelectorAll('.row .row-num'),"
           "function(e){return +e.textContent});return n.length===" + str(un_total) +
           "&&n.every(function(v,i){return v===i+1})})()", True)

    # 独有看板的外部徽标：src != main 的行（extra 目录 / 容器展开 / 外部链接）
    # 都要带「外部」标记；这类行没有「删除副本」（deletable=false），但详情栏
    # 必须提供「卸载」入口（选中第一行核对：卸载按钮在、删除按钮不在）
    un_ext = sum(1 for u in un_rows if u.get("src") and u["src"] != "main")
    if un_ext:
        _check(window, results, "独有外部徽标=%d" % un_ext,
               "document.querySelectorAll('.row .mini-flag.ext').length", un_ext)
        u0 = next(u for u in un_rows if u.get("src") and u["src"] != "main")
        window.evaluate_js("document.querySelector('.row[data-key=%s]').click()"
                           % json.dumps("%s/%s" % (u0["tool"], u0["name"])))
        time.sleep(0.3)
        _check(window, results, "外部行详情无删除按钮",
               "document.querySelectorAll('[data-delete]').length", 0)
        _check(window, results, "外部行详情有卸载按钮",
               "document.querySelectorAll('[data-uninstall]').length", 1)

    # 一键转共有：实体独有行（kind=unique 且无同名覆盖）详情栏必须有 promote
    # 入口；同名覆盖 / foreign-link 行不该出现。只断言按钮在，绝不点它 ——
    # 自检跑的是真库，点了会把真实的独有技能拷进中央仓库。点击会换选中行，
    # 结束后恢复原选中：后面的差异段还在独有板上按选中行断言。
    prev_sel = window.evaluate_js(
        "(function(){var r=document.querySelector('.row.is-selected');"
        "return r?r.getAttribute('data-key'):null})()")
    un_prom = [u for u in un_rows if u.get("kind") == "unique" and not u.get("sameName")]
    if un_prom:
        p_key = "%s/%s" % (un_prom[0]["tool"], un_prom[0]["name"])
        window.evaluate_js("document.querySelector('.row[data-key=%s]').click()"
                           % json.dumps(p_key))
        time.sleep(0.3)
        _check(window, results, "可收编行详情有「转为共有」按钮",
               "document.querySelectorAll('[data-promote]').length", 1)
    un_blocked = [u for u in un_rows if u.get("sameName") or u.get("kind") == "foreign-link"]
    if un_blocked:
        b_key = "%s/%s" % (un_blocked[0]["tool"], un_blocked[0]["name"])
        window.evaluate_js("document.querySelector('.row[data-key=%s]').click()"
                           % json.dumps(b_key))
        time.sleep(0.3)
        _check(window, results, "覆盖/链接行详情无「转为共有」按钮",
               "document.querySelectorAll('[data-promote]').length", 0)
    if prev_sel:
        window.evaluate_js("var r=document.querySelector('.row[data-key=%s]');if(r)r.click()"
                           % json.dumps(prev_sel))
        time.sleep(0.3)

    # 差异弹窗（走 Python conflict_detail）：是否有「同名副本」由后端现算——
    # 冲突可能只挂在独有板（如 Trae 内置 skill-creator 与中央同名不同内容），
    # 也可能同时是共有 skill 的实体冲突（如当年的 workbuddy/pdf）。
    # 共有板分支在 find_conflict_tool 现查到实体冲突时测弹窗全链路；
    # 否则切到共有板核对「无实体冲突 → 无差异按钮」，再走独有板分支。
    if conflict:
        c_names = {f["name"] for f in conflict["central"]["files"]}
        c_names |= {f["name"] for f in conflict["local"]["files"]}
        header = "CONFLICT · %s / %s" % (conflict["tool"], conflict["skill"])
        con2 = skilllocker.db()
        try:
            shared_conflict = skilllocker.find_conflict_tool(
                con2, skilllocker.load_config(), conflict["skill"]) is not None
        finally:
            con2.close()
        if shared_conflict:
            window.evaluate_js("document.getElementById('nav-shared').click()")
            time.sleep(0.3)
            window.evaluate_js(
                "var r=document.querySelector('.row[data-key=%s]');if(r)r.click()"
                % json.dumps(conflict["skill"]))
            time.sleep(0.3)
            _check(window, results, "差异按钮存在",
                   "document.querySelectorAll('[data-diff]').length", 1)
            window.evaluate_js("var b=document.querySelector('[data-diff]');if(b)b.click()")
            time.sleep(0.5)
            _check(window, results, "差异弹窗出现", "!!document.querySelector('.diff-table')", True)
            _check(window, results, "差异行数=%d" % len(c_names),
                   "document.querySelectorAll('.diff-row').length", len(c_names))
            _check(window, results, "冲突表头=%s" % header,
                   "document.querySelector('.modal-eyebrow').textContent", header)
            window.evaluate_js("var b=document.querySelector('[data-close]');if(b)b.click()")
        else:
            # 实体冲突只挂在独有板（同名覆盖）时，共有板该 skill 的详情不应有
            # 差异按钮。旧版没切看板就断言，实际查的是独有板当前选中行 —— 上
            # 面任何一步恰好选中同名行就会误报。这里明确切到共有板选中本尊行。
            # （共有板此刻必为列表视图：§7.1 末尾已切回；.row 只在列表存在）
            window.evaluate_js("document.getElementById('nav-shared').click()")
            time.sleep(0.3)
            window.evaluate_js(
                "var r=document.querySelector('.row[data-key=%s]');if(r)r.click()"
                % json.dumps(conflict["skill"]))
            time.sleep(0.3)
            _check(window, results, "共有板无实体冲突时不显示差异按钮",
                   "document.querySelectorAll('[data-diff]').length", 0)

        # 独有看板的同名副本差异（scope=unique 路径），只有存在同名副本时才测
        u_same = [u for u in un_rows if u.get("sameName")]
        if u_same:
            u_key = "%s/%s" % (u_same[0]["tool"], u_same[0]["name"])
            window.evaluate_js("document.getElementById('nav-unique').click()")
            time.sleep(0.3)
            window.evaluate_js(
                "var r=document.querySelector('.row[data-key=%s]');if(r)r.click()"
                % json.dumps(u_key))
            time.sleep(0.3)
            window.evaluate_js("var b=document.querySelector('[data-diff]');if(b)b.click()")
            time.sleep(0.5)
            _check(window, results, "独有差异弹窗出现",
                   "!!document.querySelector('.diff-table')", True)
            _check(window, results, "独有差异表头",
                   "document.querySelector('.modal-eyebrow').textContent", header)
            window.evaluate_js("var b=document.querySelector('[data-close]');if(b)b.click()")
    else:
        _check(window, results, "无同名副本时详情栏无差异按钮",
               "document.querySelectorAll('[data-diff]').length", 0)
        window.evaluate_js(
            "window.__SK_DIFF=null;window.pywebview.api.diff('shared','pdf')"
            ".then(function(r){window.__SK_DIFF=r})")
        data_diff = _await_js(window, "__SK_DIFF")
        ok_diff = bool(data_diff) and data_diff.get("ok") is False \
            and "没有同名副本" in (data_diff.get("error") or "")
        results.append(("无同名副本时 diff 被拒", ok_diff,
                        json.dumps(data_diff, ensure_ascii=False)))

    window.evaluate_js("document.getElementById('nav-shared').click()")
    time.sleep(0.3)
    window.evaluate_js("document.querySelector('.row[data-key=\"pdf\"]').click()")
    time.sleep(0.3)

    # 写库：改名后落 skill_meta。目标选一条「当前没有人工改名 / 备注」的共有 skill，
    # 「已编辑」数量由 bootstrap 的 meta 现算；结束把目标元数据原样恢复，不吃用户编辑。
    rename_target = next(
        (s["name"] for s in boards["skills"]
         if not (meta_map.get(s["name"]) or {}).get("display")
         and not (meta_map.get(s["name"]) or {}).get("note")), "pdf")
    old_target = meta_map.get(rename_target)
    window.evaluate_js("document.querySelector('.row[data-key=%s]').click()"
                       % json.dumps(rename_target))
    time.sleep(0.3)
    window.evaluate_js(
        "var i=document.getElementById('ins-name');"
        "i.value='自检改名';i.dispatchEvent(new Event('input',{bubbles:true}))")
    time.sleep(1.2)
    con = skilllocker.db()
    row = con.execute("select display_name, updated_at from skill_meta "
                      "where scope='shared' and key=?", (rename_target,)).fetchone()
    ok = bool(row) and row["display_name"] == "自检改名" and (row["updated_at"] or 0) > 0
    results.append(("改名写入 skill_meta", ok, repr(dict(row) if row else None)))
    con.close()
    _check(window, results, "改名后已编辑=%d" % (edited0 + 1),
           "document.getElementById('stat-edited').textContent", str(edited0 + 1))
    window.evaluate_js(
        "var i=document.getElementById('ins-name');"
        "i.value='';i.dispatchEvent(new Event('input',{bubbles:true}))")
    time.sleep(0.8)
    # 清掉测试写入；原来有元数据就只还原显示名与时间戳（整行 insert-or-replace
    # 会把 folder_id 一并抹掉），原来没有就删掉这一行
    con = skilllocker.db()
    if old_target:
        con.execute(
            "update skill_meta set display_name=?, updated_at=? where scope='shared' and key=?",
            (old_target["display"], old_target["updated"], rename_target))
    else:
        con.execute("delete from skill_meta where scope='shared' and key=?", (rename_target,))
    con.commit()
    con.close()
    window.evaluate_js(
        "window.__SK_R3=0;window.pywebview.api.bootstrap().then(function(r){"
        "window.__SK_APP.refresh(r);window.__SK_R3=1})")
    _await_js(window, "__SK_R3")
    _check(window, results, "还原后已编辑=%d" % edited0,
           "document.getElementById('stat-edited').textContent", str(edited0))

    # 搜索：不写死命中条数（模糊匹配结果随库内数据变），只校验「命中 pdf + 列表确实收窄」
    window.evaluate_js(
        "var s=document.getElementById('gsearch');"
        "s.value='pdf';s.dispatchEvent(new Event('input',{bubbles:true}))")
    time.sleep(0.3)
    _check(window, results, "搜索 pdf 命中 pdf",
           "!!document.querySelector('.row[data-key=\"pdf\"]')", True)
    _check(window, results, "搜索 pdf 列表收窄",
           "(function(){var n=document.querySelectorAll('.row').length;"
           "return n>=1&&n<%d})()" % sh_total, True)
    window.evaluate_js(
        "var s=document.getElementById('gsearch');"
        "s.value='';s.dispatchEvent(new Event('input',{bubbles:true}))")
    time.sleep(0.3)
    _check(window, results, "清筛选后=%d" % sh_total,
           "document.querySelectorAll('.row').length", sh_total)

    # 删除接口的安全兜底：不存在的 key 必须被拒绝，不能删任何东西
    window.evaluate_js(
        "window.__SK_D=null;window.pywebview.api.delete_skill('shared','__no_such_skill__')"
        ".then(function(r){window.__SK_D=r})")
    data_d = _await_js(window, "__SK_D")
    ok_d = bool(data_d) and data_d.get("ok") is False and "不存在" in (data_d.get("error") or "")
    results.append(("不存在 skill 拒绝删除", ok_d, json.dumps(data_d, ensure_ascii=False)))

    # 真实删除：临时造一个 skill，从 UI 详情栏点「彻底删除」→ 确认，磁盘与列表都必须干净
    temp_name = "__selftest_delete__"
    cfg = skilllocker.load_config()
    temp_dir = os.path.join(cfg["store"], temp_name)
    try:
        shutil.rmtree(temp_dir, ignore_errors=True)
        os.makedirs(temp_dir, exist_ok=True)
        with open(os.path.join(temp_dir, "SKILL.md"), "w", encoding="utf-8") as fh:
            fh.write("# selftest delete\n")
        con = skilllocker.db()
        try:
            skilllocker.rescan(con, cfg)
        finally:
            con.close()
        window.evaluate_js(
            "window.__SK_R=0;window.__SK_R_ERR='';window.pywebview.api.bootstrap().then(function(r){"
            "try{window.__SK_APP.refresh(r)}catch(e){window.__SK_R_ERR=String(e&&e.message||e)}"
            "window.__SK_R=1})")
        _await_js(window, "__SK_R")
        # 隐藏窗口会节流定时器，渲染可能慢半拍：等行真的出现再点；等不到就留存现场
        row_sel = ".row[data-key=%s]" % json.dumps(temp_name)
        deadline = time.time() + 10
        while time.time() < deadline and not window.evaluate_js(
                "!!document.querySelector(%s)" % json.dumps(row_sel)):
            time.sleep(0.2)
        diag = window.evaluate_js(
            "JSON.stringify({rErr:window.__SK_R_ERR,rows:document.querySelectorAll('.row').length,"
            "nav:document.getElementById('nav-shared').textContent,"
            "board:window.__SK_APP&&window.__SK_APP.state.board,"
            "jsErr:window.__SK_ERRORS})")
        row_present = bool(window.evaluate_js(
            "!!document.querySelector(%s)" % json.dumps(row_sel)))
        window.evaluate_js(
            "var r=document.querySelector(%s);if(r)r.click()" % json.dumps(row_sel))
        time.sleep(0.2)
        window.evaluate_js("var b=document.querySelector('[data-delete]');if(b)b.click()")
        time.sleep(0.3)
        modal_ok = bool(window.evaluate_js("!!document.getElementById('confirm-ok')"))
        if modal_ok:
            window.evaluate_js("document.getElementById('confirm-ok').click()")
        deadline = time.time() + 8
        row_gone = False
        while time.time() < deadline and (os.path.isdir(temp_dir) or not row_gone):
            time.sleep(0.2)
            row_gone = bool(window.evaluate_js(
                "!document.querySelector('.row[data-key=%s]')" % json.dumps(temp_name)))
        selected = window.evaluate_js("window.__SK_APP.state.selected")
        ok_d2 = modal_ok and row_gone and not os.path.isdir(temp_dir) \
            and selected != temp_name
        results.append(("UI 点击彻底删除临时 skill", ok_d2,
                        "modal=%s row_gone=%s dir_gone=%s selected=%r diag=%s"
                        % (modal_ok, row_gone, not os.path.isdir(temp_dir),
                           selected, "" if row_present else diag)))
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        con = skilllocker.db()
        try:
            con.execute("delete from skills where name=?", (temp_name,))
            con.execute("delete from skill_meta where scope='shared' and key=?", (temp_name,))
            con.commit()
        finally:
            con.close()
        window.evaluate_js(
            "window.__SK_R2=0;window.pywebview.api.bootstrap().then(function(r){"
            "window.__SK_APP.refresh(r);window.__SK_R2=1})")
        _await_js(window, "__SK_R2")


    # ============ 导入 skill（SPEC-skill-import.md §7.1 之 6-10）============
    # tempfile 造源目录（不碰真实 agent 目录），弹窗走真桥预检与真库落库；
    # 结束按「临时 skill 删除」同一套模式清理磁盘、skills 与 skill_meta。
    cfg = skilllocker.load_config()
    imp_src = tempfile.mkdtemp(prefix="sk-imp-src-")
    imp_name = "sk-import-it"
    imp_store_dir = os.path.join(cfg["store"], imp_name)
    try:
        os.makedirs(os.path.join(imp_src, "scripts"), exist_ok=True)
        with open(os.path.join(imp_src, "SKILL.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nname: %s\ndescription: selftest import smoke.\n---\nbody\n"
                     % imp_name)
        with open(os.path.join(imp_src, "scripts", "a.py"), "w", encoding="utf-8") as fh:
            fh.write("x = 1\n")
        # 上次中断的残留先清掉，保证同名键入断言可复现
        shutil.rmtree(imp_store_dir, ignore_errors=True)
        con = skilllocker.db()
        try:
            con.execute("delete from skills where name=?", (imp_name,))
            con.execute("delete from skill_meta where scope='shared' and key=?",
                        (imp_name,))
            con.commit()
            row = con.execute("select name from skills where name<>? order by name "
                              "limit 1", (imp_name,)).fetchone()
            # 同名键入用例要的是「合法但被占用」的名字：首个合法名（AI HOT 这类
            # 带空格的会被前端 validName 先拦，冲突面板根本不出现）
            existing_name = ""
            for r in con.execute("select name from skills where name<>? order by name",
                                 (imp_name,)).fetchall():
                n = r["name"]
                if n and n not in (".", "..") and re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", n) \
                        and n == n.rstrip(". ") and ":" not in n:
                    existing_name = n
                    break
            frow = con.execute("select id from folders order by position limit 1").fetchone()
            folder_id = frow["id"] if frow else ""
        finally:
            con.close()
        window.evaluate_js(
            "window.__SK_B3=0;window.pywebview.api.bootstrap().then(function(r){"
            "window.__SK_APP.refresh(r);window.__SK_B3=1})")
        _await_js(window, "__SK_B3")

        # 8) 入口：点顶栏按钮 → 弹窗出现，步骤条 01=选择来源
        window.evaluate_js("document.getElementById('btn-import-top').click()")
        time.sleep(0.3)
        _check(window, results, "导入弹窗打开且步骤条=选择来源",
               "(function(){var m=document.querySelector('.imp-modal');"
               "var s=document.querySelector('.imp-stp.cur');"
               "return !!m && !!s && s.textContent.indexOf('选择来源')>-1})()", True)
        # 填路径 → 预检（db 模式走真桥 import_preflight）
        window.evaluate_js(
            "document.getElementById('imp-path').value=%s;"
            "document.querySelector('[data-act=\"imp-use-path\"]').click()"
            % json.dumps(imp_src))
        start_ready = False
        deadline = time.time() + 15
        while time.time() < deadline:
            if window.evaluate_js(
                    "var b=document.querySelector('#modal-root [data-act=\"imp-start\"]');"
                    "!!b && !b.disabled"):
                start_ready = True
                break
            time.sleep(0.2)
        results.append(("预检后 imp-start 可用", start_ready,
                        "ready=%s" % start_ready))
        _check(window, results, "步骤条=预检命名且报告卡存在",
               "(function(){var s=[].slice.call(document.querySelectorAll('.imp-stp'))[1];"
               "return !!s && s.classList.contains('cur')"
               "&& !!document.querySelector('.imp-modal .rep')})()", True)
        # 同名键入：按钮 disabled + 冲突面板显示（前端以 SKILLS 现值判定）
        if existing_name:
            window.evaluate_js(
                "var n=document.getElementById('imp-name');"
                "n.value=%s;n.dispatchEvent(new Event('input',{bubbles:true}))"
                % json.dumps(existing_name))
            time.sleep(0.2)
            _check(window, results, "键入同名后 disabled+冲突面板",
                   "(function(){var b=document.querySelector('#modal-root [data-act=\"imp-start\"]');"
                   "var p=document.querySelector('#modal-root [data-role=\"panel-conflict\"]');"
                   "return !!b && b.disabled && !!p && !p.hidden})()", True)
        # 键入目标名 → 恢复可用（隐藏窗口无键盘，直接 set value + input 事件）
        window.evaluate_js(
            "var n=document.getElementById('imp-name');"
            "n.value=%s;n.dispatchEvent(new Event('input',{bubbles:true}))"
            % json.dumps(imp_name))
        time.sleep(0.2)
        _check(window, results, "改回合法名后重新可用",
               "(function(){var b=document.querySelector('#modal-root [data-act=\"imp-start\"]');"
               "return !!b && !b.disabled"
               "&& !document.querySelector('#modal-root [data-role=\"panel-conflict\"]:not([hidden])')})()",
               True)
        # 选归档夹（仓库有夹才断言 meta 落库）
        if folder_id:
            window.evaluate_js(
                "var c=document.querySelector('.fold-chip[data-f=%s]');if(c)c.click()"
                % json.dumps(folder_id))
            time.sleep(0.2)
        # 9) 执行态不可关：X 不渲染、Esc 无效、弹窗仍在
        window.evaluate_js(
            "document.querySelector('#modal-root [data-act=\"imp-start\"]').click()")
        time.sleep(0.2)
        no_x = window.evaluate_js(
            "!!document.querySelector('.imp-modal')"
            "&& !document.querySelector('.imp-modal .modal-close')")
        window.evaluate_js(
            "document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))")
        time.sleep(0.2)
        esc_kept = window.evaluate_js("!!document.querySelector('.imp-modal')")
        results.append(("执行中 Esc 无法关闭", no_x and esc_kept,
                        "x_hidden=%s esc_kept=%s" % (no_x, esc_kept)))
        # 等完成页（import_skill 桥 180s 上限，自检给 90s 兜底）
        done_ok = False
        deadline = time.time() + 90
        while time.time() < deadline:
            if window.evaluate_js("!!document.querySelector('.imp-modal .hero.ok')"):
                done_ok = True
                break
            if window.evaluate_js("!!document.querySelector('.imp-modal .hero.bad')"):
                break
            time.sleep(0.3)
        results.append(("导入执行到完成页", done_ok, "hero_ok=%s" % done_ok))
        # 10) 完成联动：计数=后端现算、is-new 高亮（共有看板 + 网格视图）
        con = skilllocker.db()
        try:
            backend_count = con.execute("select count(*) from skills").fetchone()[0]
        finally:
            con.close()
        _check(window, results, "完成后 #store-count=后端现算值",
               "document.getElementById('store-count').textContent", str(backend_count))
        window.evaluate_js(
            "document.querySelector('[data-board=\"shared\"]').click();"
            "var g=document.querySelector('#view-seg [data-view-mode=\"grid\"]');"
            "if(g)g.click();"
            "var s=document.getElementById('gsearch');"
            "s.value=%s;s.dispatchEvent(new Event('input',{bubbles:true}))"
            % json.dumps(imp_name))
        time.sleep(0.4)
        # 网格根层只显示未归档卡：已归档的新卡用搜索结果流断言 is-new
        _check(window, results, "新导入卡带 is-new 高亮",
               "!!document.querySelector('.file-card.is-new')", True)
        # 清搜索并切回列表视图：后续既有断言（pdf 行点击等）依赖列表态 .row
        window.evaluate_js(
            "var s=document.getElementById('gsearch');s.value='';"
            "s.dispatchEvent(new Event('input',{bubbles:true}));"
            "var l=document.querySelector('#view-seg [data-view-mode=\"list\"]');"
            "if(l)l.click()")
        time.sleep(0.3)
        _check(window, results, "完成页位置指向新 skill",
               "(function(){var b=document.querySelector('.imp-modal .meta-row b');"
               "return !!b && b.textContent.indexOf(%s)>-1})()" % json.dumps(imp_name),
               True)
        # 6) 落库断言：skills 行 + meta.folder + 磁盘目录 + 关闭弹窗
        con = skilllocker.db()
        try:
            srow = con.execute("select name from skills where name=?",
                               (imp_name,)).fetchone()
            mrow = con.execute(
                "select folder_id from skill_meta where scope='shared' and key=?",
                (imp_name,)).fetchone()
        finally:
            con.close()
        results.append(("导入落库 skills+meta",
                        bool(srow)
                        and (not folder_id or (mrow and mrow["folder_id"] == folder_id))
                        and os.path.isfile(os.path.join(imp_store_dir, "SKILL.md")),
                        "row=%s meta=%s disk=%s folder=%s"
                        % (bool(srow), bool(mrow),
                           os.path.isfile(os.path.join(imp_store_dir, "SKILL.md")),
                           folder_id or "-")))
        window.evaluate_js(
            "document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))")
        time.sleep(0.2)
        _check(window, results, "完成态 Esc 可关闭弹窗",
               "!document.querySelector('.imp-modal')", True)
    finally:
        shutil.rmtree(imp_src, ignore_errors=True)
        shutil.rmtree(imp_store_dir, ignore_errors=True)
        con = skilllocker.db()
        try:
            con.execute("delete from skills where name=?", (imp_name,))
            con.execute("delete from skill_meta where scope='shared' and key=?",
                        (imp_name,))
            con.commit()
        finally:
            con.close()
        window.evaluate_js(
            "window.__SK_B4=0;window.pywebview.api.bootstrap().then(function(r){"
            "window.__SK_APP.refresh(r);window.__SK_B4=1})")
        _await_js(window, "__SK_B4")

    # ============ 在线检索 / 获取（SPEC-skill-import.md §10.1 已真实化）============
    # 全程假网络：patch 掉 online_search（桥走 skilllocker 门面引用，必须 patch 门面）
    # 与 skilllocker_links 的清单 / 下载两处；桥与 UI 走真实链路（检索渲染 → 获取
    # 下载到 %TEMP% → 标准预检回填），不写中央仓库；结束恢复补丁并清临时目录。
    import skilllocker_links as _lk
    _saved_net = (skilllocker.online_search, _lk._list_repo_files, _lk._net_bytes)
    _onl_dir = None
    try:
        skilllocker.online_search = lambda q, limit=20: [{  # noqa: E731
            "name": "online-selftest", "source": "u/online-selftest", "installs": 1234,
            "id": "u/online-selftest/online-selftest",
            "page_url": "https://www.skills.sh/u/online-selftest/online-selftest",
            "source_url": "https://github.com/u/online-selftest"}]
        _lk._list_repo_files = lambda s, b=None: ("main", [  # noqa: E731
            ("skills/online-selftest/SKILL.md", 60),
            ("skills/online-selftest/scripts/run.py", 6)], "github", "b" * 40)
        _lk._net_bytes = lambda url, expected_size=None, deadline=None, cancel=None: (  # noqa: E731
            b"---\nname: online-selftest\ndescription: selftest online fetch.\n---\nbody"
            if "SKILL.md" in url else b"x = 1\n")

        # 桥直调：获取 → 系统临时目录 + 标准预检可用
        window.evaluate_js(
            "window.__SK_ON1=null;window.pywebview.api.online_fetch("
            "'u/online-selftest','online-selftest').then(function(r){"
            "window.__SK_ON1=r})")
        onl = _await_js(window, "__SK_ON1") or {}
        got = onl.get("data") or {}
        _onl_dir = (got.get("path") or "").replace("/", os.sep)
        results.append(("在线获取(假网络)落临时目录",
                        bool(_onl_dir) and os.path.isdir(_onl_dir)
                        and got.get("files") == 2 and got.get("listing") == "github",
                        "ok=%s files=%s listing=%s dir=%s"
                        % (onl.get("ok"), got.get("files"), got.get("listing"),
                           bool(_onl_dir))))
        if _onl_dir:
            pre = skilllocker.import_preflight(skilllocker.load_config(), _onl_dir,
                                            got.get("name"))
            results.append(("在线获取结果可标准预检",
                            bool(pre.get("ok")) and pre.get("has_skill_md")
                            and pre.get("files") == 2
                            and (pre.get("fm") or {}).get("name") == "online-selftest",
                            "ok=%s md=%s fm=%r" % (pre.get("ok"), pre.get("has_skill_md"),
                                                   (pre.get("fm") or {}).get("name"))))
        # open_url 白名单：白名单外域名拒绝（不会真开浏览器）
        window.evaluate_js(
            "window.__SK_ON2=null;window.pywebview.api.open_url('https://evil.example/x')"
            ".then(function(r){window.__SK_ON2=r})")
        ou = _await_js(window, "__SK_ON2") or {}
        results.append(("open_url 白名单外拒绝",
                        ou.get("ok") is False and "不允许" in str(ou.get("error", "")),
                        json.dumps(ou, ensure_ascii=False)[:100]))

        # UI 链路：在线 tab 检索 → 行渲染 → 获取 → 预检回填
        window.evaluate_js(
            "document.getElementById('btn-import-top').click();"
            "document.querySelector('[data-act=\"imp-tab\"][data-tab=\"online\"]').click()")
        time.sleep(0.3)
        window.evaluate_js(
            "var q=document.getElementById('imp-q');q.value='selftest';"
            "q.dispatchEvent(new Event('input',{bubbles:true}));"
            "document.querySelector('[data-act=\"imp-search\"]').click()")
        row_hit = _poll_js(window,
                           "!!document.querySelector('#sr-list .sr-row') "
                           "&& document.querySelector('#sr-list .sr-row').textContent"
                           ".indexOf('online-selftest')>-1", 8)
        results.append(("UI 在线检索渲染假结果", bool(row_hit), "hit=%s" % row_hit))
        window.evaluate_js(
            "var fb=document.querySelector('#sr-list [data-act=\"imp-fetch\"]');"
            "if(fb)fb.click()")
        fill_ok = _poll_js(window,
                           "!!document.querySelector('.imp-modal .rep') "
                           "&& !!document.getElementById('imp-name') "
                           "&& document.getElementById('imp-name').value"
                           ".indexOf('online-selftest')>-1", 15)
        results.append(("UI 获取→预检回填", bool(fill_ok), "fill=%s" % fill_ok))
        window.evaluate_js(
            "document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))")
        time.sleep(0.2)
    finally:
        skilllocker.online_search, _lk._list_repo_files, _lk._net_bytes = _saved_net
        if _onl_dir:
            shutil.rmtree(_onl_dir, ignore_errors=True)
        # UI「获取」流程也会落一个临时目录（导入语义 = 复制进仓库后残留待清理），
        # 自检收尾把本次产生的 online-selftest-* 一并清掉
        try:
            for _n in os.listdir(_lk._dl_root()):
                if _n.startswith("online-selftest-"):
                    shutil.rmtree(os.path.join(_lk._dl_root(), _n), ignore_errors=True)
        except OSError:
            pass

    # 设置弹窗 + 主题
    window.evaluate_js("document.getElementById('open-settings').click()")
    time.sleep(0.3)
    _check(window, results, "设置弹窗出现", "!!document.getElementById('set-theme')", True)
    _check(window, results, "设置文案指向 skill_meta",
           "!!document.querySelector('.modal-body p').textContent.match('skill_meta')", True)
    window.evaluate_js("document.querySelector('[data-close]').click()")
    window.evaluate_js("document.documentElement.setAttribute('data-theme','dark')")
    _check(window, results, "深色主题属性生效",
           "document.documentElement.getAttribute('data-theme')", "dark")

    # 智能体看板：三态来自 skilllocker.list_boards，开关按真实磁盘链接计数
    ag_total = len(ag_rows)

    window.evaluate_js("document.getElementById('nav-agents').click()")
    time.sleep(0.4)
    _check(window, results, "智能体行数=%d" % ag_total,
           "document.querySelectorAll('.row[data-row-board=\"agents\"]').length", ag_total)
    _check(window, results, "侧栏智能体=已知数",
           "document.getElementById('nav-agents').textContent", str(ag_total))
    _check(window, results, "智能体 tab 已连接数",
           "document.getElementById('tab-agents-em').textContent",
           "%d 已连接" % ag_connected)
    _check(window, results, "智能体页标题",
           "document.getElementById('page-title').textContent", "智能体")
    _check(window, results, "智能体状态筛=4",
           "document.querySelectorAll('[data-agent-status]').length >= 4", True)
    _check(window, results, "每行都有开关",
           "document.querySelectorAll('.row[data-row-board=\"agents\"] .switch').length", ag_total)
    _check(window, results, "未安装开关禁用数",
           "document.querySelectorAll('.row[data-row-board=\"agents\"] .switch input:disabled').length",
           len(ag_missing))
    # 序号列：与共有 / 独有看板同款，按当前列表顺序 1..N，纯展示
    _check(window, results, "智能体首行序号=1",
           "document.querySelector('.row[data-row-board=\"agents\"] .row-num').textContent", "1")
    _check(window, results, "智能体序号连续 1..%d" % ag_total,
           "(function(){var n=[].map.call("
           "document.querySelectorAll('.row[data-row-board=\"agents\"] .row-num'),"
           "function(e){return +e.textContent});return n.length===" + str(ag_total) +
           "&&n.every(function(v,i){return v===i+1})})()", True)
    # 两位数（第 10 行）仍要装得进 26×26 的格子里；agent 不足 10 个时没有两位数，跳过
    if ag_total >= 10:
        _check(window, results, "智能体序号格 26x26 不溢出",
               "(function(){var e="
               "document.querySelectorAll('.row[data-row-board=\"agents\"] .row-num')[9];"
               "var r=e.getBoundingClientRect();"
               "return Math.round(r.width)+'x'+Math.round(r.height)+':'+e.textContent+':'"
               "+(e.scrollWidth<=e.clientWidth)})()", "26x26:10:true")

    # 外部来源（方案 A）：~/.agents/skills 这类 agent 自己安装/读取的目录只检测展示，
    # 开关不管它们。有与中央仓库同名的实体时，行上要有「外部 N」，详情里要说明。
    ag_ext = [a for a in ag_rows if a.get("external")]
    if ag_ext:
        a0 = ag_ext[0]
        e0 = a0["external"][0]
        e_sum = sum(e["count"] for e in a0["external"])
        _check(window, results, "%s 行有外部来源标记" % a0["key"],
               "!!document.querySelector('.row[data-key=\"%s\"] .ext-badge')" % a0["key"], True)
        _check(window, results, "外部标记数量=%d" % e_sum,
               "(function(){var b=document.querySelector('.row[data-key=\"%s\"] .ext-badge');"
               "return b?b.textContent.trim():''})()" % a0["key"],
               "外部 %d" % e_sum)
        window.evaluate_js(
            "var r=document.querySelector('.row[data-key=\"%s\"]');if(r)r.click()" % a0["key"])
        time.sleep(0.3)
        _check(window, results, "详情里有外部来源行",
               "(function(){var ok=false;"
               "Array.prototype.forEach.call(document.querySelectorAll('#ins-body .meta-row'),"
               "function(x){if(x.textContent.indexOf('外部来源')===0)ok=true});return ok})()", True)

    # 开关方向回归：点“开”着的开关必须先问「要不要关闭」，不能再走一次连接。
    # 老实现读 click 里的 !box.checked —— 点击冒到 document 时 checked 已被 Chromium
    # 翻转（label 还会把点击转给 input，一次按下两个 click），于是点开着反而又 open 一次。
    # 这里只走到确认框再取消，不产生任何磁盘写。
    ag_connected_rows = [a for a in ag_rows if a["status"] == "connected"]
    if ag_connected_rows:
        sw_key = ag_connected_rows[0]["key"]
        window.evaluate_js(
            "var h=document.createElement('div');h.id='__SK_SW';"
            "h.innerHTML='<label class=\"switch\" data-agent-switch=\"%s\">"
            "<input type=\"checkbox\" checked><i></i></label>';"
            "document.body.appendChild(h)" % sw_key)
        time.sleep(0.2)
        window.evaluate_js("document.querySelector('#__SK_SW label i').click()")
        time.sleep(0.4)
        _check(window, results, "开关点开→先问关闭",
               "(function(){var m=document.querySelector('#modal-root .modal');"
               "return !!m&&m.querySelector('h2').textContent.indexOf('关闭')===0})()", True)
        _check(window, results, "开关一次点击只弹一个确认框",
               "document.querySelectorAll('#modal-root .modal').length", 1)
        window.evaluate_js(
            "var b=document.querySelector('#modal-root .modal-foot [data-close]');"
            "if(b)b.click()")
        time.sleep(0.3)
        _check(window, results, "取消关闭→开关回到开",
               "document.querySelector('#__SK_SW input').checked", True)
        window.evaluate_js("document.getElementById('__SK_SW').remove()")

    _check(window, results, "窗口缩放 grip=8",
           "document.querySelectorAll('[data-resize]').length", 8)

    # 外框 API：grip 拖动以它为基准矩形；隐藏建窗的 --selftest 也能读写
    window.evaluate_js(
        "window.__SK_B=null;window.pywebview.api.win_bounds()"
        ".then(function(r){window.__SK_B=r})")
    deadline = time.time() + 5
    while time.time() < deadline and not window.evaluate_js("!!window.__SK_B"):
        time.sleep(0.1)
    raw = window.evaluate_js("JSON.stringify(window.__SK_B||null)")
    bounds = (json.loads(raw) if raw else None) or {}
    ok = bounds.get("ok") is True and abs((bounds.get("bounds") or {}).get("w", 0) - WIN_W) <= 40
    results.append(("窗口外框可读≈1200", ok, raw))

    # 标题栏拖动：合成 pointerdown/move/up，窗口外框应跟着位移（不依赖 pywebview drag-region）
    b0 = bounds.get("bounds") or {}
    window.evaluate_js(
        "var bar=document.querySelector('.titlebar');"
        "bar.dispatchEvent(new PointerEvent('pointerdown',{pointerId:91,button:0,bubbles:true,"
        "screenX:500,screenY:300,clientX:200,clientY:20}))")
    time.sleep(0.4)
    window.evaluate_js(
        "document.dispatchEvent(new PointerEvent('pointermove',{pointerId:91,bubbles:true,"
        "screenX:560,screenY:340}))")
    time.sleep(0.5)
    window.evaluate_js(
        "document.dispatchEvent(new PointerEvent('pointerup',{pointerId:91,bubbles:true,"
        "screenX:560,screenY:340}))")
    time.sleep(0.5)
    window.evaluate_js(
        "window.__SK_B4=null;window.pywebview.api.win_bounds()"
        ".then(function(r){window.__SK_B4=r})")
    b4 = _await_js(window, "__SK_B4") or {}
    nb = b4.get("bounds") or {}
    moved_ok = b4.get("ok") is True \
        and abs(nb.get("x", 0) - (b0.get("x", 0) + 60)) <= 4 \
        and abs(nb.get("y", 0) - (b0.get("y", 0) + 40)) <= 4
    results.append(("标题栏拖动窗口 (dx=60,dy=40)", moved_ok,
                    "from=(%s,%s) to=(%s,%s)" % (b0.get("x"), b0.get("y"),
                                                 nb.get("x"), nb.get("y"))))
    # 复位，避免影响后续尺寸断言
    window.evaluate_js(
        "window.__SK_B5=null;window.pywebview.api.win_set_bounds(%s,%s,%s,%s)"
        ".then(function(r){window.__SK_B5=r})"
        % (b0.get("x", 0), b0.get("y", 0), b0.get("w", WIN_W), b0.get("h", WIN_H)))
    _await_js(window, "__SK_B5")

    if ag_missing:
        key_js = json.dumps(ag_missing[0])
        window.evaluate_js(
            "window.__SK_T=null;window.pywebview.api.set_agent(%s,true)"
            ".then(function(r){window.__SK_T=r})" % key_js)
        deadline = time.time() + 5
        while time.time() < deadline:
            if window.evaluate_js("!!window.__SK_T"):
                break
            time.sleep(0.1)
        raw = window.evaluate_js("JSON.stringify(window.__SK_T||null)")
        data = json.loads(raw) if raw else None
        ok = bool(data) and data.get("ok") is False and "未安装" in (data.get("error") or "")
        results.append(("未安装 agent 无法开启", ok, raw))
    window.evaluate_js("document.getElementById('nav-shared').click()")
    time.sleep(0.4)
    window.evaluate_js("document.querySelector('.row[data-key=\"pdf\"]').click()")
    time.sleep(0.3)

    # 布局：建窗时已按 _nc_css() 补偿 frameless 去边框的差额，内容区应正好 1200x760；
    # 这里仍用区间断言，兼容小屏（fit_size 会收口到工作区，最小 1180x720）
    _check(window, results, "启动宽 1180~1200",
           "window.innerWidth>=%d && window.innerWidth<=%d" % (MIN_W, WIN_W), True)
    _check(window, results, "启动高 720~760",
           "window.innerHeight>=%d && window.innerHeight<=%d" % (MIN_H, WIN_H), True)
    _check(window, results, "启动宽≤工作区",
           "window.innerWidth<=screen.availWidth", True)
    _check(window, results, "启动高≤工作区",
           "window.innerHeight<=screen.availHeight", True)
    _check(window, results, "底部设置按钮完整可见",
           "(function(){var f=document.getElementById('open-settings').getBoundingClientRect();"
           "var a=document.querySelector('.app-body').getBoundingClientRect();"
           "return f.bottom<=a.bottom&&f.top>=a.top})()", True)
    _check(window, results, "版本号行可见",
           "(function(){var v=document.querySelector('.version').getBoundingClientRect();"
           "var a=document.querySelector('.app-body').getBoundingClientRect();"
           "return v.bottom<=a.bottom&&v.top>=a.top})()", True)
    _check(window, results, "详情栏完整可见",
           "Math.round(document.querySelector('.inspector').getBoundingClientRect().right)"
           "<=window.innerWidth", True)
    window.resize(MIN_W, MIN_H)
    time.sleep(0.8)
    _check(window, results, "1180 无横向溢出",
           "document.documentElement.scrollWidth<=window.innerWidth", True)
    _check(window, results, "1180 窗口宽=100vw",
           "Math.round(document.getElementById('app-window').getBoundingClientRect().width)", MIN_W)

    # 外框写入 + 最小尺寸兜底：grip 拖动最终落到 win_set_bounds，放在最后避免影响上面的内宽断言
    window.evaluate_js(
        "window.__SK_B2=null;window.pywebview.api.win_set_bounds(210,35,1240,780)"
        ".then(function(r){window.__SK_B2=r})")
    raw2 = _await_js(window, "__SK_B2")
    rect2 = ((raw2 or {}).get("bounds") or {})
    ok2 = (raw2 or {}).get("ok") is True and abs(rect2.get("w", 0) - 1240) <= 3 \
        and abs(rect2.get("h", 0) - 780) <= 3
    results.append(("外框写入 1240x780", ok2, json.dumps(raw2, ensure_ascii=False)))

    window.evaluate_js(
        "window.__SK_B3=null;window.pywebview.api.win_set_bounds(10,10,900,500)"
        ".then(function(r){window.__SK_B3=r})")
    raw3 = _await_js(window, "__SK_B3")
    rect3 = ((raw3 or {}).get("bounds") or {})
    ok3 = (raw3 or {}).get("ok") is True and abs(rect3.get("w", 0) - MIN_W) <= 3 \
        and abs(rect3.get("h", 0) - MIN_H) <= 3
    results.append(("外框最小尺寸兜底 1180x720", ok3, json.dumps(raw3, ensure_ascii=False)))

    # —— 窗口态测试（真实最大化 / 还原）必须放在最后：隐藏窗口的 WebView2 布局
    #    在真实最大化过一次后会卡在最大化尺寸（OS 外框已还原、innerWidth 仍 1536，
    #    也不再跟随后续 resize），上面的 innerWidth / 布局断言不能被它污染；
    #    末轮 boot 会重载页面，拿回干净布局。

    # 最大化态拖标题栏：位移越过阈值（>6px）才还原跟随，不能静默放弃（老实现直接 endMove）
    window.evaluate_js(
        "window.__SK_B9=null;window.pywebview.api.win_set_bounds(%s,%s,%s,%s)"
        ".then(function(r){window.__SK_B9=r})"
        % (b0.get("x", 0), b0.get("y", 0), b0.get("w", WIN_W), b0.get("h", WIN_H)))
    _await_js(window, "__SK_B9")
    time.sleep(0.3)
    window.evaluate_js(
        "window.__SK_MX=null;window.pywebview.api.win_max()"
        ".then(function(r){window.__SK_MX=r})")
    _await_js(window, "__SK_MX")
    time.sleep(0.5)
    window.evaluate_js(
        "var bar=document.querySelector('.titlebar');"
        "bar.dispatchEvent(new PointerEvent('pointerdown',{pointerId:92,button:0,bubbles:true,"
        "screenX:500,screenY:300,clientX:200,clientY:20}))")
    time.sleep(0.6)
    window.evaluate_js(
        "document.dispatchEvent(new PointerEvent('pointermove',{pointerId:92,bubbles:true,"
        "screenX:530,screenY:320}))")
    time.sleep(0.6)
    window.evaluate_js(
        "document.dispatchEvent(new PointerEvent('pointerup',{pointerId:92,bubbles:true,"
        "screenX:530,screenY:320}))")
    time.sleep(0.6)
    window.evaluate_js(
        "window.__SK_B6=null;window.pywebview.api.win_bounds()"
        ".then(function(r){window.__SK_B6=r})")
    b6 = _await_js(window, "__SK_B6") or {}
    nb6 = b6.get("bounds") or {}
    restored_ok = b6.get("ok") is True and b6.get("maximized") is False \
        and abs(nb6.get("w", 0) - b0.get("w", WIN_W)) <= 40 \
        and abs(nb6.get("x", 0) - (b0.get("x", 0) + 30)) <= 40
    results.append(("最大化拖动先还原再位移", restored_ok,
                    "from=(%s,%s,%s) to=(%s,%s,%s)" % (b0.get("x"), b0.get("y"), b0.get("w"),
                                                       nb6.get("x"), nb6.get("y"), nb6.get("w")))
                    )

    # 最大化态单击标题栏：不得还原（老实现基准矩形一回来就 restore，单击也缩窗闪屏）。
    # 缩小只认双击（win_max 切换）与越过阈值的真实拖动；松手位置带 1px 抖动也不算拖。
    window.evaluate_js(
        "window.__SK_MX2=null;window.pywebview.api.win_max()"
        ".then(function(r){window.__SK_MX2=r})")
    _await_js(window, "__SK_MX2")
    time.sleep(0.5)
    window.evaluate_js(
        "var bar=document.querySelector('.titlebar');"
        "bar.dispatchEvent(new PointerEvent('pointerdown',{pointerId:93,button:0,bubbles:true,"
        "screenX:500,screenY:300,clientX:200,clientY:20}))")
    time.sleep(0.5)
    window.evaluate_js(
        "document.dispatchEvent(new PointerEvent('pointerup',{pointerId:93,bubbles:true,"
        "screenX:501,screenY:301}))")
    time.sleep(0.5)
    window.evaluate_js(
        "window.__SK_B7=null;window.pywebview.api.win_bounds()"
        ".then(function(r){window.__SK_B7=r})")
    b7 = _await_js(window, "__SK_B7") or {}
    click_ok = b7.get("ok") is True and b7.get("maximized") is True
    results.append(("最大化单击不还原", click_ok,
                    "maximized=%s w=%s" % (b7.get("maximized"),
                                           (b7.get("bounds") or {}).get("w"))))
    # 复位：还原回基准外框，末轮 boot 重载后的断言在正常窗口态下进行
    window.evaluate_js(
        "window.__SK_MX3=null;window.pywebview.api.win_max()"
        ".then(function(r){window.__SK_MX3=r})")
    _await_js(window, "__SK_MX3")
    window.evaluate_js(
        "window.__SK_B8=null;window.pywebview.api.win_set_bounds(%s,%s,%s,%s)"
        ".then(function(r){window.__SK_B8=r})"
        % (b0.get("x", 0), b0.get("y", 0), b0.get("w", WIN_W), b0.get("h", WIN_H)))
    _await_js(window, "__SK_B8")
    time.sleep(0.3)


def _selftest_boot(window, results):
    """带 boot 启动一轮（SPEC-boot-animation.md §7）。

    主轮固定 ?noboot=1，与 v0.2.0 行为一致，boot 编排不干扰 52 项断言的时序；
    这里末轮重载不带 noboot 的页面，验证 overlay 全周期：首帧可见（booting 态）
    → start() 后 done + ready 级联 → 最终 hidden 回收，全程无 JS 错误。
    """
    window.load_url(INDEX + "?desktop=1&v=" + _asset_version())
    ok = _poll_js(window,
                  "!!document.getElementById('boot') && !document.getElementById('boot').hidden"
                  " && document.getElementById('app-window').classList.contains('booting')")
    results.append(("boot 首帧 overlay 可见", ok, "visible=%s" % ok))
    t_ready = time.time()
    # 隐藏窗口里 bootstrap 要扫插件缓存（秒级，见 _await_js 的 15s 注释），
    # 重载页同一待遇放宽到 20s，避免系统繁忙时把环境慢当断言失败
    if not _wait_ready(window, timeout=20.0):
        results.append(("boot 轮页面就绪", False, "__SK_READY 超时"))
        return
    t_r = time.time()
    ok = _poll_js(window,
                  "var b=document.getElementById('boot');"
                  "!!b && b.classList.contains('done')"
                  " && document.getElementById('app-window').classList.contains('ready')")
    # R→done 耗时只观测不断言：R 晚于 4.42s 时（隐藏窗口 bootstrap 慢）走 ~520ms 快车道，
    # 否则提速到 ~1.2s；写进断言值里供排查，硬性时长验收在可见窗口人工核对
    results.append(("boot 就绪后 done+ready 级联", ok,
                    "done+ready=%s R->done=%.2fs" % (ok, time.time() - t_r)))
    ok = _poll_js(window, "var b=document.getElementById('boot'); !!b && !!b.hidden")
    results.append(("boot overlay 最终 hidden 回收", ok, "hidden=%s" % ok))
    _check(window, results, "boot 轮无 JS 错误", "window.__SK_ERRORS.length", 0)


def run(window):
    results = []
    errors_repr = ""
    try:
        # 20s：首启 bootstrap 含全量哈希（~4s 级），默认 8s 在冷机/负载下会偶发超时
        if not _wait_ready(window, timeout=20.0):
            raise RuntimeError("页面未就绪（__SK_READY 未置位）")
        time.sleep(0.5)
        _selftest_body(window, results)
        _selftest_boot(window, results)
    except Exception as exc:  # noqa: BLE001
        results.append(("自检异常", False, "%s: %s" % (type(exc).__name__, exc)))
    finally:
        # __SK_ERRORS 要趁窗口还在时读；然后先销毁窗口再输出 —— pythonw 下
        # sys.stdout 是 None，print 会抛 AttributeError，放在 destroy 之前
        # 隐藏窗口就永远销毁不掉、进程挂死
        try:
            errors_repr = window.evaluate_js("JSON.stringify(window.__SK_ERRORS)")
        except Exception as exc:  # noqa: BLE001
            errors_repr = "<不可读 %r>" % (exc,)
        try:
            window.destroy()
        except Exception:  # noqa: BLE001
            pass
        _selftest_report(results, errors_repr)


def _selftest_report(results, errors_repr):
    """输出自检结果。有控制台走 stdout；pythonw（stdout=None）落盘 selftest.log。"""
    lines = ["",
             "=== SELFTEST %d/%d ===" % (sum(1 for _n, ok, _v in results if ok),
                                         len(results))]
    for name, ok, value in results:
        lines.append("  [%s] %-28s %s" % ("PASS" if ok else "FAIL", name, value))
    lines.append("ERRORS: %s" % errors_repr)
    text = "\n".join(lines)
    if sys.stdout is None:
        try:
            with open(os.path.join(KIT, "selftest.log"), "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
        except OSError:
            pass
        return
    print(text)
