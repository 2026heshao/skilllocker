#!/usr/bin/env python3
"""skillkit 桌面端无人值守自检（desktop.py --selftest 的执行体）。

离屏建一个隐藏窗口，跑完整条 UI 链路（bridge / 原生窗口 chrome / 拖拽缩放 /
写库与删除安全兜底），逐项断言后输出 SELFTEST n/n 并退出进程。
要借 desktop 的原生窗口辅助函数；desktop.main 只在 --selftest 分支懒加载
本模块，所以不存在循环导入。产物：控制台或 pythonw 下的 kit/selftest.log。
"""
import ctypes
import ctypes.wintypes  # noqa: F401 - GetWindowLongW 等需要 wintypes 结构体
import json
import os
import shutil
import sys
import time

import skillkit

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
        if window.evaluate_js("!!window.%s" % key):
            break
        time.sleep(0.1)
    raw = window.evaluate_js("JSON.stringify(window.%s||null)" % key)
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
    results.append(("任务栏小图标=skillkit.ico", bool(expect_small) and got_small == expect_small,
                    "got=%r expect=%r" % (got_small, expect_small)))
    results.append(("任务栏大图标=skillkit.ico", bool(expect_big) and got_big == expect_big,
                    "got=%r expect=%r" % (got_big, expect_big)))
    results.append(("任务栏小图标2=skillkit.ico", bool(expect_small) and got_small2 == expect_small,
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
    con = skillkit.db()
    try:
        boards = skillkit.list_boards(con, skillkit.load_config())
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
    store_path = skillkit.load_config()["store"].replace("/", "\\")
    con = skillkit.db()
    try:
        meta_map = skillkit.get_meta(con)
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
    con = skillkit.db()
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
    con = skillkit.db()
    try:
        tf = skillkit.folder_create(con, "自检夹", "teal")
        con.commit()
        skillkit.set_meta(con, "shared", "pdf", {"folder": tf["id"]})
    finally:
        con.close()
    _refresh_ui()
    time.sleep(0.3)
    _check(window, results, "建夹后卡片数=%d" % (folder_total + 1),
           "document.querySelectorAll('.folder-card:not(.ghost)').length", folder_total + 1)
    moved = 0
    pdf_row = None
    con = skillkit.db()
    try:
        moved = skillkit.folder_delete(con, tf["id"])["moved"]
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
    con = skillkit.db()
    try:
        skillkit.set_meta(con, "shared", "pdf", {"folder": pdf_old_folder})
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
    con = skillkit.db()
    try:
        skillkit.set_meta(con, "shared", "pdf", {"folder": f0["id"]})
        v1 = con.execute(
            "select folder_id from skill_meta where scope='shared' and key='pdf'").fetchone()
        skillkit.set_meta(con, "shared", "pdf", {"folder": ""})
        v2 = con.execute(
            "select folder_id from skill_meta where scope='shared' and key='pdf'").fetchone()
        ok6 = bool(v1) and v1["folder_id"] == f0["id"] and (v2["folder_id"] or "") == ""
    finally:
        con.close()
    results.append(("set_meta 归属落库", ok6,
                    "%r -> %r" % (v1["folder_id"] if v1 else None,
                                  v2["folder_id"] if v2 else None)))
    # #6 把 pdf 归属写成了 ''，这里还原原归属，别吃掉用户的真实数据
    con = skillkit.db()
    try:
        skillkit.set_meta(con, "shared", "pdf", {"folder": pdf_old_folder})
    finally:
        con.close()

    # 8) 迁移幂等：全新库（升级前 schema）回填一次，二次迁移不再动任何行
    import skillkit_base as sk_base
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
        n1 = sk_base.migrate_folders(fcon, skillkit.load_config())
        r1 = fcon.execute("select folder_id from skill_meta where key='__selftest_mig__'").fetchone()["folder_id"]
        n2 = sk_base.migrate_folders(fcon, skillkit.load_config())
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

    # 差异弹窗（走 Python conflict_detail）：是否有「同名副本」由后端现算——
    # 冲突可能只挂在独有板（如 Trae 内置 skill-creator 与中央同名不同内容），
    # 也可能同时是共有 skill 的实体冲突（如当年的 workbuddy/pdf）。
    # 共有板分支只在 find_conflict_tool 现查到实体冲突时才测，其余走独有板分支。
    if conflict:
        c_names = {f["name"] for f in conflict["central"]["files"]}
        c_names |= {f["name"] for f in conflict["local"]["files"]}
        header = "CONFLICT · %s / %s" % (conflict["tool"], conflict["skill"])
        con2 = skillkit.db()
        try:
            shared_conflict = skillkit.find_conflict_tool(
                con2, skillkit.load_config(), conflict["skill"]) is not None
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
    con = skillkit.db()
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
    con = skillkit.db()
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
    cfg = skillkit.load_config()
    temp_dir = os.path.join(cfg["store"], temp_name)
    try:
        shutil.rmtree(temp_dir, ignore_errors=True)
        os.makedirs(temp_dir, exist_ok=True)
        with open(os.path.join(temp_dir, "SKILL.md"), "w", encoding="utf-8") as fh:
            fh.write("# selftest delete\n")
        con = skillkit.db()
        try:
            skillkit.rescan(con, cfg)
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
        con = skillkit.db()
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

    # 智能体看板：三态来自 skillkit.list_boards，开关按真实磁盘链接计数
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

    # 最大化态拖标题栏：应先还原再继续位移，不能静默放弃（老实现直接 endMove）
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
        if not _wait_ready(window):
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
