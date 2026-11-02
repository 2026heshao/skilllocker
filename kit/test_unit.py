#!/usr/bin/env python3
"""skilllocker 纯函数 / 库层单元测试（无第三方依赖，直接 python test_unit.py 运行）。

覆盖 CODE_REVIEW.md 第三批点名的表驱动对象：
  parse_frontmatter / seed_category / target_state 状态机 / prune_meta 降级 /
  meta 读写截断 / 原子拷贝 / 内容哈希冲突对照 / 删除清理 / import 名校验 /
  events 轮转 / hash 缓存。每个用例自建临时目录与临时 DB，互不污染。
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import skilllocker  # noqa: E402
import skilllocker_base  # noqa: E402
import skilllocker_links  # noqa: E402
import skilllocker_store  # noqa: E402

CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


def tmp_cfg(store):
    return {"store": os.path.normpath(store).replace(os.sep, "/")}


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def insert_tool(con, key, skills_dir, mode="junction", detected=1, enabled=1):
    con.execute(
        "insert or replace into tools(key,label,skills_dir,detect_dir,detected,enabled,mode) "
        "values(?,?,?,?,?,?,?)", (key, key.upper(), skills_dir.replace(os.sep, "/"),
                                  skills_dir, detected, enabled, mode))


# ---------------------------------------------------------------- 解析
@case("parse_frontmatter: 基本键值 / 引号 / 注释 / 缩进跳过")
def _():
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "SKILL.md")
        write(p, "---\n"
                 "# comment\n"
                 "name: foo\n"
                 'description: "my skill"\n'
                 "\n"
                 "  nested: skipped\n"
                 "---\nbody\n")
        fm = skilllocker.parse_frontmatter(p)
        assert fm == {"name": "foo", "description": "my skill"}, fm


@case("parse_frontmatter: > 折叠块与 | 保留块")
def _():
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "SKILL.md")
        write(p, "---\n"
                 "desc: >\n"
                 "  line one\n"
                 "  line two\n"
                 "note: |\n"
                 "  keep A\n"
                 "  keep B\n"
                 "---\n")
        fm = skilllocker.parse_frontmatter(p)
        assert fm["desc"] == "line one line two", fm
        assert fm["note"] == "keep A\nkeep B", fm


@case("parse_frontmatter: 无 frontmatter / 空文件 / BOM")
def _():
    with tempfile.TemporaryDirectory() as td:
        p1 = os.path.join(td, "a.md")
        write(p1, "# just markdown\n")
        p2 = os.path.join(td, "b.md")
        write(p2, "")
        p3 = os.path.join(td, "c.md")
        write(p3, "﻿---\nname: bom\n---\n")
        assert skilllocker.parse_frontmatter(p1) == {}
        assert skilllocker.parse_frontmatter(p2) == {}
        assert skilllocker.parse_frontmatter(p3).get("name") == "bom"


@case("seed_category: 与 app.js seedCat 同一套规则")
def _():
    table = {
        "remotion-best-practices": "video",
        "mnemosyne-os-memory": "memory",
        "aihot": "search",
        "pdf": "doc",
        "web-deploy-github": "frontend",
        "webapp-testing": "frontend",
        "brandkit": "design",
        "dev-expert": "eng",
        "skill-creator": "eng",
    }
    for name, want in table.items():
        got = skilllocker.seed_category(name)
        assert got == want, "%s -> %s (want %s)" % (name, got, want)


@case("fmt_bytes / norm_path 边界")
def _():
    assert skilllocker.fmt_bytes(0) == "0 B"
    assert skilllocker.fmt_bytes(512) == "512 B"
    assert skilllocker.fmt_bytes(2048) == "2.0 KB"
    assert skilllocker.fmt_bytes(20 * 1024) == "20 KB"
    assert skilllocker.fmt_bytes(2 * 1024 * 1024) == "2.0 MB"
    assert skilllocker.norm_path("C:\\Users\\x\\") == "c:/Users/x"  # 只小写盘符
    assert skilllocker.norm_path("\\\\?\\C:\\x") == "c:/x"
    assert skilllocker.norm_path("") == ""


# ---------------------------------------------------------------- 状态机
@case("target_state: 八态状态机（junction/copy/off）")
def _():
    if sys.platform != "win32":
        raise AssertionError("仅 Windows（需要 junction）")
    with tempfile.TemporaryDirectory() as td:
        store = os.path.join(td, "store")
        agent = os.path.join(td, "agent")
        os.makedirs(os.path.join(store, "c1"))
        write(os.path.join(store, "c1", "SKILL.md"), "c1\n")
        h1 = skilllocker.sha_tree(os.path.join(store, "c1"))
        os.makedirs(os.path.join(store, "c2"))  # 无 SKILL.md → 断链素材
        other = os.path.join(td, "other")
        os.makedirs(other)
        write(os.path.join(other, "SKILL.md"), "other\n")
        os.makedirs(agent)
        # ok-link / broken-link / foreign-link
        skilllocker.make_junction(os.path.join(agent, "c1"), os.path.join(store, "c1"))
        skilllocker.make_junction(os.path.join(agent, "c2"), os.path.join(store, "c2"))
        skilllocker.make_junction(os.path.join(agent, "c3"), other)
        # takeover / conflict-dir / conflict-file
        shutil.copytree(os.path.join(store, "c1"), os.path.join(agent, "c5"))
        os.makedirs(os.path.join(agent, "c4"))
        write(os.path.join(agent, "c4", "SKILL.md"), "changed\n")
        write(os.path.join(agent, "c8"), "i am a file")
        # ok-copy / stale-copy
        shutil.copytree(os.path.join(store, "c1"), os.path.join(agent, "c6"))
        os.makedirs(os.path.join(agent, "c7"))
        write(os.path.join(agent, "c7", "SKILL.md"), "stale\n")
        cfg = tmp_cfg(store)
        trow = {"mode": "junction", "skills_dir": agent.replace(os.sep, "/")}
        srow = lambda n, h: {"name": n, "content_hash": h}  # noqa: E731
        want = [
            ("c0", "missing"), ("c1", "ok-link"), ("c2", "broken-link"),
            ("c3", "foreign-link"), ("c4", "conflict-dir"),
            ("c5", "conflict-dir"),  # 哈希不匹配；takeover 在下方用真实哈希单测
            ("c8", "conflict-file"),
        ]
        for n, expect in want:
            state, _ = skilllocker.target_state(
                os.path.join(agent, n), cfg, srow(n, "x" * 64), trow)
            assert state == expect, "%s -> %s (want %s)" % (n, state, expect)
        assert skilllocker.target_state(
            os.path.join(agent, "c5"), cfg, srow("c5", h1), trow)[0] == "takeover"
        assert skilllocker.target_state(
            os.path.join(agent, "c5"), cfg, srow("c5", "x" * 64), trow)[0] == "conflict-dir"
        cpy = {"mode": "copy", "skills_dir": agent.replace(os.sep, "/")}
        assert skilllocker.target_state(
            os.path.join(agent, "c6"), cfg, srow("c6", h1), cpy)[0] == "ok-copy"
        assert skilllocker.target_state(
            os.path.join(agent, "c7"), cfg, srow("c7", h1), cpy)[0] == "stale-copy"
        off = {"mode": "off", "skills_dir": agent.replace(os.sep, "/")}
        assert skilllocker.target_state(
            os.path.join(agent, "c6"), cfg, srow("c6", h1), off)[0] == "off"


@case("target_state: hash_cache 贯通后命中缓存（M6）")
def _():
    with tempfile.TemporaryDirectory() as td:
        store = os.path.join(td, "store")
        agent = os.path.join(td, "agent")
        os.makedirs(os.path.join(store, "s1"))
        write(os.path.join(store, "s1", "SKILL.md"), "v1\n")
        shutil.copytree(os.path.join(store, "s1"), os.path.join(agent, "s1"))
        cfg = tmp_cfg(store)
        trow = {"mode": "junction", "skills_dir": agent.replace(os.sep, "/")}
        srow = {"name": "s1", "content_hash": "x" * 64}
        cache = {}
        h1 = skilllocker._hash_tree(os.path.join(agent, "s1"), cache)
        write(os.path.join(agent, "s1", "SKILL.md"), "v2\n")
        assert skilllocker._hash_tree(os.path.join(agent, "s1"), cache) == h1  # 缓存命中
        assert skilllocker._hash_tree(os.path.join(agent, "s1"), {}) != h1    # 新快照现算


# ---------------------------------------------------------------- prune / meta
def _temp_db(td):
    # DB_FILE 等常量归属于 skilllocker_base；打补丁必须打到所属模块，
    # 打到 skilllocker 门面上不会影响 db() 的实际读取
    old = skilllocker_base.DB_FILE
    skilllocker_base.DB_FILE = os.path.join(td, "test.db")
    con = skilllocker.db()
    return con, old


@case("prune_meta: 正常修剪 + scanDegraded 跳过（M3）")
def _():
    with tempfile.TemporaryDirectory() as td:
        con, old = _temp_db(td)
        try:
            for scope, key in (("shared", "a"), ("shared", "gone"),
                               ("unique", "t/u"), ("unique", "t/gone")):
                con.execute("insert into skill_meta(scope,key) values(?,?)", (scope, key))
            con.commit()
            boards = {"meta": {}, "skills": [{"name": "a"}],
                      "unique": [{"tool": "t", "name": "u"}]}
            assert skilllocker.prune_meta(con, boards) == 2
            left = {(r["scope"], r["key"]) for r in
                    con.execute("select scope,key from skill_meta")}
            assert left == {("shared", "a"), ("unique", "t/u")}, left
            # 降级快照：本轮绝不删
            con.execute("insert into skill_meta(scope,key) values('shared','gone2')")
            con.commit()
            degraded = {"meta": {"scanDegraded": ["C:\\x: 失败"]}, "skills": [], "unique": []}
            assert skilllocker.prune_meta(con, degraded) == 0
            assert con.execute(
                "select count(*) from skill_meta where key='gone2'").fetchone()[0] == 1
        finally:
            con.close()
            skilllocker_base.DB_FILE = old


@case("scan_unique: 根目录不存在也要记 scanDegraded（M3 残余）")
def _():
    with tempfile.TemporaryDirectory() as td:
        con, old = _temp_db(td)
        try:
            store = os.path.join(td, "store")
            agent = os.path.join(td, "agent", "skills")
            os.makedirs(store)
            os.makedirs(os.path.join(agent, "uniq1"))
            write(os.path.join(agent, "uniq1", "SKILL.md"), "---\nname: uniq1\n---\n")
            cfg = tmp_cfg(store)
            insert_tool(con, "t1", agent)
            skilllocker.rescan(con, cfg)
            con.execute("insert into skill_meta(scope,key) values('unique','t1/uniq1')")
            con.commit()
            trow = con.execute("select * from tools where key='t1'").fetchone()
            # 可移动盘拔出 / 盘符暂缺：根整个看不见
            gone = os.path.join(td, "drive_missing", "skills")
            out, errors = [], []
            skilllocker_store._scan_unique_root(
                trow, gone, "main", True, [], 1, {}, cfg, {}, {}, out, errors)
            assert errors, "目录不存在必须记进 errors"
            assert not out, "看不见的根不得产出独有行"
            boards = {"meta": {"scanDegraded": errors}, "skills": [], "unique": []}
            assert skilllocker.prune_meta(con, boards) == 0, "降级轮绝不清 meta"
            assert con.execute(
                "select count(*) from skill_meta where key='t1/uniq1'").fetchone()[0] == 1
        finally:
            con.close()
            skilllocker_base.DB_FILE = old


@case("set_meta / get_meta: 截断与回读")
def _():
    with tempfile.TemporaryDirectory() as td:
        con, old = _temp_db(td)
        try:
            skilllocker.migrate_folders(con, {"store": td})  # 建 folder_id 列 + 预置夹
            fid = skilllocker.folder_create(con, "测试夹", "teal")["id"]
            con.commit()
            row = skilllocker.set_meta(con, "shared", "k", {
                "display": "x" * 60, "note": "n" * 500, "folder": fid})
            assert len(row["display"]) == 48 and len(row["note"]) == 400
            assert row["folder"] == fid
            got = skilllocker.get_meta(con)["k"]
            assert got["display"] == "x" * 48 and got["folder"] == fid
            # 不带 folder 字段 = 保留原归属；categories 列按口径原样写回
            skilllocker.set_meta(con, "shared", "k", {"display": "y"})
            assert skilllocker.get_meta(con)["k"]["folder"] == fid
            skilllocker.set_meta(con, "unique", "t/k", {"display": "u", "folder": fid})
            uk = skilllocker.get_meta(con)["t/k"]
            assert uk["tool"] == "t" and uk["folder"] == ""
        finally:
            con.close()
            skilllocker_base.DB_FILE = old


@case("seed_meta: 共有/独有/同名覆盖三路播种")
def _():
    with tempfile.TemporaryDirectory() as td:
        store = os.path.join(td, "store")
        agent = os.path.join(td, "agent", "skills")
        os.makedirs(os.path.join(store, "shared1"))
        write(os.path.join(store, "shared1", "SKILL.md"), "---\nname: shared1\n---\n")
        os.makedirs(os.path.join(agent, "uniq1"))
        write(os.path.join(agent, "uniq1", "SKILL.md"), "---\nname: uniq1\n---\n")
        # 同名不同内容 → overridden，播种 doc
        os.makedirs(os.path.join(agent, "shared1"))
        write(os.path.join(agent, "shared1", "SKILL.md"), "different\n")
        con, old = _temp_db(td)
        try:
            cfg = tmp_cfg(store)
            insert_tool(con, "t1", agent)
            assert skilllocker.rescan(con, cfg) == 1
            assert skilllocker.migrate_folders(con, cfg) >= 0
            seeded = skilllocker.seed_meta(con, cfg)
            assert seeded == 3, seeded
            meta = skilllocker.get_meta(con)
            # shared 行：folder_id = 预置夹、categories 写 '[]'；unique 行 folder 恒空
            assert meta["shared1"]["folder"] == "eng"
            assert meta["shared1"]["cats"] == []
            assert meta["t1/uniq1"]["folder"] == ""
            assert meta["t1/uniq1"]["cats"] == ["eng"]
            assert meta["t1/shared1"]["folder"] == ""
            assert meta["t1/shared1"]["cats"] == ["doc"]
            uniq = skilllocker.unique_entries(con, cfg)
            kinds = {(u["name"]): u["kind"] for u in uniq}
            assert kinds == {"uniq1": "unique", "shared1": "overridden"}, kinds
        finally:
            con.close()
            skilllocker_base.DB_FILE = old


# ---------------------------------------------------------------- rescan / 拷贝
@case("rescan: 中央仓库不可读直接报错，不清库（M3）")
def _():
    with tempfile.TemporaryDirectory() as td:
        con, old = _temp_db(td)
        try:
            cfg = tmp_cfg(os.path.join(td, "no-such-store"))
            try:
                skilllocker.rescan(con, cfg)
                raise AssertionError("应该抛 RuntimeError")
            except RuntimeError as exc:
                assert "不可读" in str(exc) or "不存在" in str(exc), exc
        finally:
            con.close()
            skilllocker_base.DB_FILE = old


@case("copy_tree_atomic: 成功翻正 / 目标已存在报错 / 无残留临时目录（L5）")
def _():
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "src")
        write(os.path.join(src, "sub", "a.txt"), "data")
        store = os.path.join(td, "store")
        os.makedirs(store)
        dst = os.path.join(store, "skill-a")
        skilllocker.copy_tree_atomic(src, dst)
        assert os.path.isfile(os.path.join(dst, "sub", "a.txt"))
        assert not [n for n in os.listdir(store) if ".skpart-" in n]
        os.makedirs(os.path.join(store, "skill-b"), exist_ok=True)
        write(os.path.join(store, "skill-b", "old.txt"), "old")
        try:
            skilllocker.copy_tree_atomic(src, os.path.join(store, "skill-b"))
            raise AssertionError("目标已存在应失败")
        except OSError:
            pass
        assert not [n for n in os.listdir(store) if ".skpart-" in n], "临时目录必须清干净"
        assert os.path.isfile(os.path.join(store, "skill-b", "old.txt")), "原目录不能被动过"


@case("import_dir: 非法名拒绝（L1），合法导入入库")
def _():
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "myskill")
        write(os.path.join(src, "SKILL.md"), "---\nname: myskill\n---\n")
        store = os.path.join(td, "store")
        con, old = _temp_db(td)
        try:
            cfg = tmp_cfg(store)
            for bad in ("../evil", "a/b", "..", "."):
                try:
                    skilllocker.import_dir(con, cfg, src, bad)
                    raise AssertionError("%r 应被拒绝" % bad)
                except RuntimeError as exc:
                    assert "非法 skill 名" in str(exc), exc
            # --name 空串等价于不传：回退源目录名
            name = skilllocker.import_dir(con, cfg, src, "")
            assert name == "myskill"
            assert con.execute(
                "select count(*) from skills where name='myskill'").fetchone()[0] == 1
            assert os.path.isfile(os.path.join(store, "myskill", "SKILL.md"))
        finally:
            con.close()
            skilllocker_base.DB_FILE = old


@case("resolve_conflict: 非法名拒绝（M6），store 与本地目录原样")
def _():
    with tempfile.TemporaryDirectory() as td:
        store = os.path.join(td, "store")
        agent = os.path.join(td, "agent", "skills")
        os.makedirs(os.path.join(store, "s1"))
        write(os.path.join(store, "s1", "SKILL.md"), "central\n")
        os.makedirs(os.path.join(agent, "s1"))
        write(os.path.join(agent, "s1", "SKILL.md"), "local\n")
        con, old = _temp_db(td)
        try:
            insert_tool(con, "t1", agent)
            cfg = tmp_cfg(store)
            for bad in ("..", "a/b", "../evil"):
                for direction in ("central-to-local", "local-to-central"):
                    try:
                        skilllocker.resolve_conflict(con, cfg, "t1", bad, direction)
                        raise AssertionError("%r/%s 应被拒绝" % (bad, direction))
                    except RuntimeError as exc:
                        assert "非法 skill 名" in str(exc), exc
            # 非法名在入口校验就被拦：两个目录都必须原样（name=".." 曾会
            # delete_tree 到 skills_dir 的父目录 / store 根）
            assert os.path.isfile(os.path.join(store, "s1", "SKILL.md"))
            assert os.path.isfile(os.path.join(agent, "s1", "SKILL.md"))
        finally:
            con.close()
            skilllocker_base.DB_FILE = old


@case("sha_tree: 目录不可枚举时严格报错，展示/判定/删除不下结论（L4）")
def _():
    if sys.platform != "win32":
        raise AssertionError("仅 Windows（需要 icacls 构造不可读目录）")

    def deny(path):
        # 只拒「列目录」(RD) 保留 READ_CONTROL：sha_tree 拿不到清单，但
        # icacls 还能摘掉 deny ACE，临时目录才能清理干净（全拒 R 摘不回来）
        r = subprocess.run(["icacls", path, "/deny", "*S-1-1-0:(RD)"],
                           capture_output=True)
        assert r.returncode == 0, r.returncode

    def undeny(path):
        subprocess.run(["icacls", path, "/remove:d", "*S-1-1-0"],
                       capture_output=True)

    with tempfile.TemporaryDirectory() as td:
        inner = os.path.join(td, "locked")
        os.makedirs(inner)
        write(os.path.join(inner, "b.txt"), "two")
        write(os.path.join(td, "a.txt"), "one")
        deny(inner)
        try:
            # 哈希路径严格：不可枚举绝不算成「空树 / 缺块」哈希（旧实现会）
            try:
                skilllocker.sha_tree(td)
                raise AssertionError("不可枚举的目录应让 sha_tree 抛错")
            except RuntimeError as exc:
                assert "不可枚举" in str(exc), exc
            # 展示路径宽容：dir_stats / file_list 照常工作
            skilllocker.dir_stats(td)
            skilllocker.file_list(td)

            # 判定路径不下结论：target_state 返回 conflict-dir（--fix 跳过）
            store = os.path.join(td, "store")
            agent = os.path.join(td, "agent")
            os.makedirs(store)
            os.makedirs(os.path.join(agent, "s1"))
            write(os.path.join(agent, "s1", "SKILL.md"), "local\n")
            locked = os.path.join(agent, "s1", "locked")
            os.makedirs(locked)
            deny(locked)
            cfg = tmp_cfg(store)
            srow = {"name": "s1", "content_hash": "x" * 64}
            trow = {"mode": "copy", "skills_dir": agent.replace(os.sep, "/")}
            state, detail = skilllocker.target_state(
                os.path.join(agent, "s1"), cfg, srow, trow)
            assert state == "conflict-dir" and "不可读" in detail, (state, detail)
            assert skilllocker._hash_tree(os.path.join(agent, "s1")) is None

            # 删除守卫：不可枚举就不删（宁可残留也不误删）
            con, old = _temp_db(td)
            try:
                insert_tool(con, "t1", agent)
                con.execute("insert into skills(name,content_hash) values('s1',?)",
                            ("x" * 64,))
                con.commit()
                removed = skilllocker.unlink_skill(con, cfg, "s1")
                assert removed == [], removed
                assert os.path.isdir(os.path.join(agent, "s1"))
            finally:
                con.close()
                skilllocker_base.DB_FILE = old
        finally:
            undeny(inner)
            undeny(locked)


@case("migrate_folders: 加列竞态不抛，且不重放回填（L6）")
def _():
    with tempfile.TemporaryDirectory() as td:
        con, old = _temp_db(td)
        try:
            class RaceCon:
                """pragma 看不到 folder_id、alter 撞 duplicate：模拟双进程竞态。"""

                def __init__(self, real):
                    self._real = real
                    self._raced = False

                def execute(self, sql, *a):
                    if "pragma table_info(skill_meta)" in sql:
                        return [r for r in self._real.execute(sql, *a)
                                if r[1] != "folder_id"]
                    if ("alter table skill_meta add column folder_id" in sql
                            and not self._raced):
                        self._raced = True
                        raise sqlite3.OperationalError("duplicate column name: folder_id")
                    return self._real.execute(sql, *a)

                def commit(self):
                    return self._real.commit()

                def __getattr__(self, item):
                    return getattr(self._real, item)

            # 对方进程已把列加上并回填：老 cats 已是历史，我方绝不重放
            con.execute("alter table skill_meta add column folder_id text")
            con.execute("insert into skill_meta(scope,key,categories) "
                        "values('shared','k','[\"eng\"]')")
            con.commit()
            skilllocker.migrate_folders(RaceCon(con), {"store": td})  # 不抛即过
            row = con.execute(
                "select folder_id from skill_meta where key='k'").fetchone()
            assert not row["folder_id"], row["folder_id"]
            assert con.execute("select count(*) from folders").fetchone()[0] > 0
        finally:
            con.close()
            skilllocker_base.DB_FILE = old


@case("check_skill_name: Windows 保留名 / 尾点尾空格 / 冒号（L7）")
def _():
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "myskill")
        write(os.path.join(src, "SKILL.md"), "---\nname: myskill\n---\n")
        store = os.path.join(td, "store")
        os.makedirs(store)
        con, old = _temp_db(td)
        try:
            cfg = tmp_cfg(store)
            for bad in ("con", "NUL", "com1", "LPT9", "aux", "a.", "a ", "a:b"):
                try:
                    skilllocker.import_dir(con, cfg, src, bad)
                    raise AssertionError("%r 应被拒绝" % bad)
                except RuntimeError as exc:
                    assert "非法 skill 名" in str(exc), exc
            # 拒绝发生在 copytree 之前：store 始终干净
            assert os.listdir(store) == [], os.listdir(store)
            assert skilllocker.check_skill_name("normal-skill") == "normal-skill"
        finally:
            con.close()
            skilllocker_base.DB_FILE = old


@case("scan_unique: .skpart-* 崩溃残留不进独有列表（L5）")
def _():
    with tempfile.TemporaryDirectory() as td:
        store = os.path.join(td, "store")
        agent = os.path.join(td, "agent", "skills")
        os.makedirs(store)
        part = os.path.join(agent, ".skpart-4242-half")
        os.makedirs(part)
        write(os.path.join(part, "SKILL.md"), "---\nname: half\n---\n")
        cfg = tmp_cfg(store)
        con, old = _temp_db(td)
        try:
            insert_tool(con, "t1", agent)
            trow = con.execute("select * from tools where key='t1'").fetchone()
            out, errors = [], []
            skilllocker_store._scan_unique_root(
                trow, agent, "main", True, [], 1, {}, cfg, {}, {}, out, errors)
            assert out == [], out
        finally:
            con.close()
            skilllocker_base.DB_FILE = old


# ---------------------------------------------------------------- 冲突对照
@case("conflict_detail: 同大小不同内容也要报 diff（L3）")
def _():
    with tempfile.TemporaryDirectory() as td:
        store = os.path.join(td, "store")
        local = os.path.join(td, "local")
        os.makedirs(os.path.join(store, "s"))
        write(os.path.join(store, "s", "SKILL.md"), "C")
        write(os.path.join(store, "s", "a.txt"), "12345")
        write(os.path.join(store, "s", "b.txt"), "same")
        os.makedirs(os.path.join(local, "s"))
        write(os.path.join(local, "s", "SKILL.md"), "D")       # 同大小(1B)不同内容
        write(os.path.join(local, "s", "a.txt"), "54321")      # 同大小不同内容
        write(os.path.join(local, "s", "b.txt"), "same")       # 一致
        write(os.path.join(local, "s", "c.txt"), "local only") # 仅本地
        con, old = _temp_db(td)
        try:
            insert_tool(con, "t1", local)
            cfg = tmp_cfg(store)
            det = skilllocker.conflict_detail(con, cfg, "t1", "s")
            assert det["commonDiffs"] == 2, det["commonDiffs"]
            cflags = {f["name"]: f["flag"] for f in det["central"]["files"]}
            lflags = {f["name"]: f["flag"] for f in det["local"]["files"]}
            assert cflags == {"SKILL.md": "diff", "a.txt": "diff", "b.txt": "same"}, cflags
            assert lflags == {"SKILL.md": "diff", "a.txt": "diff", "b.txt": "same",
                              "c.txt": "local-only"}, lflags
        finally:
            con.close()
            skilllocker_base.DB_FILE = old


# ---------------------------------------------------------------- 删除清理
@case("remove_skill: 连带清理 suppressed 与 skill_meta（L2）")
def _():
    with tempfile.TemporaryDirectory() as td:
        store = os.path.join(td, "store")
        agent = os.path.join(td, "agent")
        os.makedirs(os.path.join(store, "r1"))
        write(os.path.join(store, "r1", "SKILL.md"), "r1\n")
        os.makedirs(agent)
        con, old = _temp_db(td)
        try:
            cfg = tmp_cfg(store)
            insert_tool(con, "t1", agent)
            skilllocker.rescan(con, cfg)
            con.execute("insert into suppressed(tool,skill,kind,note) values('t1','r1','conflict','x')")
            con.execute("insert into skill_meta(scope,key) values('shared','r1')")
            con.commit()
            skilllocker.remove_skill(con, cfg, "r1")
            assert con.execute("select count(*) from skills where name='r1'"
                               ).fetchone()[0] == 0
            assert con.execute("select count(*) from suppressed where skill='r1'"
                               ).fetchone()[0] == 0
            assert con.execute("select count(*) from skill_meta where key='r1'"
                               ).fetchone()[0] == 0
            assert not os.path.isdir(os.path.join(store, "r1"))
        finally:
            con.close()
            skilllocker_base.DB_FILE = old


@case("uninstall_unique: 容器子目录 / foreign-link 只摘链接 / 拒绝不存在")
def _():
    with tempfile.TemporaryDirectory() as td:
        store = os.path.join(td, "store")
        agent = os.path.join(td, "agent", "skills")
        elsewhere = os.path.join(td, "elsewhere", "body")
        os.makedirs(store)
        # 容器子目录：deletable=false 的 unique 行（真身在 agent 目录内）
        cont = os.path.join(agent, "cont")
        sub = os.path.join(cont, "sub1")
        os.makedirs(sub)
        write(os.path.join(sub, "SKILL.md"), "---\nname: sub1\n---\n")
        # foreign-link：junction 指向 agent 目录之外的真身
        os.makedirs(elsewhere)
        write(os.path.join(elsewhere, "SKILL.md"), "---\nname: body\n---\n")
        link = os.path.join(agent, "linkdir")
        skilllocker.make_junction(link, elsewhere)
        con, old = _temp_db(td)
        try:
            cfg = tmp_cfg(store)
            insert_tool(con, "t1", agent)
            con.execute("insert into skill_meta(scope,key) values('unique','t1/cont/sub1')")
            con.commit()

            # 容器子目录：永久删除子目录，容器与 meta 行一起清
            u = skilllocker.find_unique_row(con, cfg, "t1", "cont/sub1")
            assert u and u["deletable"] is False and u["kind"] == "unique", u
            act = skilllocker.uninstall_unique(con, cfg, "t1", "cont/sub1")
            assert act["files"] >= 1 and act["kind"] == "unique", act
            assert not os.path.isdir(sub) and os.path.isdir(cont)
            assert con.execute("select count(*) from skill_meta where key='t1/cont/sub1'"
                               ).fetchone()[0] == 0

            # foreign-link：只摘链接，真身目录原样保留
            u = skilllocker.find_unique_row(con, cfg, "t1", "linkdir")
            assert u and u["kind"] == "foreign-link" and u["deletable"] is False, u
            act = skilllocker.uninstall_unique(con, cfg, "t1", "linkdir")
            assert act["files"] == 0 and act["kind"] == "foreign-link", act
            assert not os.path.lexists(link)
            assert os.path.isfile(os.path.join(elsewhere, "SKILL.md"))

            # 拒绝：行已不存在
            try:
                skilllocker.uninstall_unique(con, cfg, "t1", "cont/sub1")
                raise AssertionError("已卸载的行应被拒绝")
            except RuntimeError:
                pass
        finally:
            con.close()
            skilllocker_base.DB_FILE = old


# ---------------------------------------------------------------- 独有转共有
@case("promote_unique: 开关关闭时拷入中央仓库 + meta 迁移 + 源副本保留")
def _():
    with tempfile.TemporaryDirectory() as td:
        store = os.path.join(td, "store")
        agent = os.path.join(td, "agent", "skills")
        os.makedirs(store)
        os.makedirs(os.path.join(agent, "uniq1", "assets"))
        write(os.path.join(agent, "uniq1", "SKILL.md"), "---\nname: uniq1\ndescription: d\n---\n")
        write(os.path.join(agent, "uniq1", "assets", "a.txt"), "data")
        con, old = _temp_db(td)
        try:
            cfg = tmp_cfg(store)
            skilllocker.migrate_folders(con, cfg)  # set_meta 需要 folder_id 列
            insert_tool(con, "t1", agent, enabled=0)  # 分发开关关闭
            skilllocker.rescan(con, cfg)
            skilllocker.set_meta(con, "unique", "t1/uniq1",
                              {"display": "独有一号", "note": "用户备注"})
            act = skilllocker.promote_unique(con, cfg, "t1", "uniq1")
            assert act["name"] == "uniq1" and act["relinked"] == 0, act
            # 中央仓库实体与注册表
            assert os.path.isfile(os.path.join(store, "uniq1", "assets", "a.txt"))
            row = con.execute("select * from skills where name='uniq1'").fetchone()
            assert row and row["origin"] == "scan", row
            # 源副本原样保留（开关关，不动 agent 目录）
            assert os.path.isfile(os.path.join(agent, "uniq1", "SKILL.md"))
            # 显示名 / 备注迁给共有行
            m = skilllocker.get_meta(con)
            assert m["uniq1"]["display"] == "独有一号", m
            assert m["uniq1"]["note"] == "用户备注", m
            # 收编后内容与中央一致：不再是独有行
            uniq = [u for u in skilllocker.unique_entries(con, cfg)
                    if u["tool"] == "t1" and u["name"] == "uniq1"]
            assert not uniq, uniq
        finally:
            con.close()
            skilllocker_base.DB_FILE = old


@case("promote_unique: 分发开着（junction）源副本接管成链接，状态 ok-link")
def _():
    if sys.platform != "win32":
        raise AssertionError("仅 Windows（需要 junction）")
    with tempfile.TemporaryDirectory() as td:
        store = os.path.join(td, "store")
        agent = os.path.join(td, "agent", "skills")
        os.makedirs(store)
        os.makedirs(os.path.join(agent, "uniq2"))
        write(os.path.join(agent, "uniq2", "SKILL.md"), "---\nname: uniq2\n---\n")
        con, old = _temp_db(td)
        try:
            cfg = tmp_cfg(store)
            insert_tool(con, "t1", agent)  # enabled=1 + junction
            skilllocker.rescan(con, cfg)
            act = skilllocker.promote_unique(con, cfg, "t1", "uniq2")
            assert act["relinked"] == 1, act
            local = os.path.join(agent, "uniq2")
            assert skilllocker.is_reparse(local), "源副本应已转成链接"
            assert os.path.isfile(os.path.join(local, "SKILL.md")), "链接必须可用"
            assert skilllocker.link_target(local) == skilllocker.norm_path(
                os.path.join(store, "uniq2")), skilllocker.link_target(local)
            srow = dict(con.execute("select * from skills where name='uniq2'").fetchone())
            trow = dict(con.execute("select * from tools where key='t1'").fetchone())
            state, _ = skilllocker.target_state(local.replace(os.sep, "/"), cfg, srow, trow)
            assert state == "ok-link", state
        finally:
            con.close()
            skilllocker_base.DB_FILE = old


@case("promote_unique: 同名覆盖 / foreign-link / 不存在 三类拒绝，不动盘")
def _():
    if sys.platform != "win32":
        raise AssertionError("仅 Windows（需要 junction）")
    with tempfile.TemporaryDirectory() as td:
        store = os.path.join(td, "store")
        agent = os.path.join(td, "agent", "skills")
        os.makedirs(os.path.join(store, "s1"))
        write(os.path.join(store, "s1", "SKILL.md"), "central\n")
        os.makedirs(os.path.join(agent, "s1"))
        write(os.path.join(agent, "s1", "SKILL.md"), "local\n")  # 同名不同内容
        elsewhere = os.path.join(td, "elsewhere")
        os.makedirs(elsewhere)
        write(os.path.join(elsewhere, "SKILL.md"), "---\nname: body\n---\n")
        skilllocker.make_junction(os.path.join(agent, "flink"), elsewhere)
        con, old = _temp_db(td)
        try:
            cfg = tmp_cfg(store)
            insert_tool(con, "t1", agent)
            skilllocker.rescan(con, cfg)
            for name, frag in (("s1", "同名"), ("flink", "链接")):
                try:
                    skilllocker.promote_unique(con, cfg, "t1", name)
                    raise AssertionError("%s 应被拒绝" % name)
                except RuntimeError as exc:
                    assert frag in str(exc), exc
            try:
                skilllocker.promote_unique(con, cfg, "t1", "nope")
                raise AssertionError("不存在的独有行应被拒绝")
            except RuntimeError as exc:
                assert "不存在" in str(exc), exc
            # 拒绝路径不得动盘：中央仓库仍是入库前的 1 个 skill
            assert con.execute("select count(*) from skills").fetchone()[0] == 1
            assert os.path.isfile(os.path.join(elsewhere, "SKILL.md"))
        finally:
            con.close()
            skilllocker_base.DB_FILE = old


@case("promote_unique: extra 外部目录来源收编后，源目录与 agent 主目录都不动")
def _():
    with tempfile.TemporaryDirectory() as td:
        store = os.path.join(td, "store")
        agent = os.path.join(td, "agent", "skills")
        extra = os.path.join(td, "extra")
        adapters = os.path.join(td, "adapters.json")
        os.makedirs(store)
        write(adapters, json.dumps([
            {"key": "t1", "label": "T1", "skills_dir": ".t1/skills",
             "detect_dir": ".t1", "extra_dirs": [extra.replace(os.sep, "/")]}]))
        os.makedirs(os.path.join(extra, "exskill"))
        write(os.path.join(extra, "exskill", "SKILL.md"), "---\nname: exskill\n---\n")
        con, old = _temp_db(td)
        old_ad = skilllocker_base.ADAPTERS_FILE
        skilllocker_base.ADAPTERS_FILE = adapters
        try:
            cfg = tmp_cfg(store)
            insert_tool(con, "t1", agent)  # 开关开着：extra 来源也不得对齐分发
            skilllocker.rescan(con, cfg)
            act = skilllocker.promote_unique(con, cfg, "t1", "exskill")
            assert act["name"] == "exskill" and act["relinked"] == 0, act
            assert os.path.isfile(os.path.join(store, "exskill", "SKILL.md"))
            assert os.path.isfile(os.path.join(extra, "exskill", "SKILL.md")), \
                "extra 真身必须原样保留"
            assert not os.path.lexists(os.path.join(agent, "exskill")), \
                "不得在 agent 主目录偷偷建链接"
        finally:
            con.close()
            skilllocker_base.ADAPTERS_FILE = old_ad
            skilllocker_base.DB_FILE = old


@case("promote_unique: 容器子目录行按真身目录名入库，不补链接")
def _():
    with tempfile.TemporaryDirectory() as td:
        store = os.path.join(td, "store")
        agent = os.path.join(td, "agent", "skills")
        os.makedirs(store)
        sub = os.path.join(agent, "cont", "inner1")
        os.makedirs(sub)
        write(os.path.join(sub, "SKILL.md"), "---\nname: inner1\n---\n")
        con, old = _temp_db(td)
        try:
            cfg = tmp_cfg(store)
            insert_tool(con, "t1", agent)  # 开关开着：容器行的真身不在直属位，禁止对齐
            skilllocker.rescan(con, cfg)
            act = skilllocker.promote_unique(con, cfg, "t1", "cont/inner1")
            assert act["name"] == "inner1", act
            assert os.path.isfile(os.path.join(store, "inner1", "SKILL.md"))
            assert os.path.isfile(os.path.join(sub, "SKILL.md")), "源副本保留"
            assert not os.path.lexists(os.path.join(agent, "inner1")), \
                "容器行不得在 skills 根补链接（agent 会加载到两份）"
        finally:
            con.close()
            skilllocker_base.DB_FILE = old


# ---------------------------------------------------------------- 杂项
@case("log_event: events 按水位轮转（L6）")
def _():
    with tempfile.TemporaryDirectory() as td:
        con, old = _temp_db(td)
        try:
            keep = skilllocker_base.EVENTS_KEEP
            skilllocker_base.EVENTS_KEEP = 10
            try:
                for i in range(30):
                    skilllocker.log_event(con, "t", str(i))
                con.commit()
                n = con.execute("select count(*) from events").fetchone()[0]
                assert n <= 22, n  # 超过 2 倍水位就裁，最多留 10+ 插入中的几条
                top = con.execute("select max(id) from events").fetchone()[0]
                assert top == 30
            finally:
                skilllocker_base.EVENTS_KEEP = keep
        finally:
            con.close()
            skilllocker_base.DB_FILE = old


@case("sha_tree: 忽略名单生效且内容变化敏感（M4）")
def _():
    with tempfile.TemporaryDirectory() as td:
        write(os.path.join(td, "a.txt"), "one")
        write(os.path.join(td, ".git", "x"), "ignored")
        h1 = skilllocker.sha_tree(td)
        h2 = skilllocker.sha_tree(td)
        assert h1 == h2
        write(os.path.join(td, "a.txt"), "two")
        assert skilllocker.sha_tree(td) != h1


@case("_hash_tree: 无缓存时直接现算，与 sha_tree 一致")
def _():
    with tempfile.TemporaryDirectory() as td:
        write(os.path.join(td, "a.txt"), "x")
        assert skilllocker._hash_tree(td) == skilllocker.sha_tree(td)
        cache = {}
        assert skilllocker._hash_tree(td, cache) == skilllocker.sha_tree(td)
        assert skilllocker.norm_path(td) in cache


@case("sync_tools_from_adapters: adapters.json 损坏时沿用库内工具表（L4）")
def _():
    with tempfile.TemporaryDirectory() as td:
        con, old = _temp_db(td)
        try:
            old_file = skilllocker_base.ADAPTERS_FILE
            broken = os.path.join(td, "adapters.json")
            write(broken, "{not json")
            skilllocker_base.ADAPTERS_FILE = broken
            try:
                insert_tool(con, "t1", os.path.join(td, "agent"))
                con.commit()
                rows = skilllocker.sync_tools_from_adapters(con, {"store": td})
                assert [(r[0], r[4]) for r in rows] == [("t1", 1)], rows
            finally:
                skilllocker_base.ADAPTERS_FILE = old_file
        finally:
            con.close()
            skilllocker_base.DB_FILE = old


@case("load_config: config.json 损坏退回默认并留痕（L4）")
def _():
    old_file = skilllocker_base.CONFIG_FILE
    with tempfile.TemporaryDirectory() as td:
        broken = os.path.join(td, "config.json")
        write(broken, "{oops")
        skilllocker_base.CONFIG_FILE = broken
        try:
            cfg = skilllocker.load_config()
            assert cfg["store"] == skilllocker.DEFAULT_STORE
            assert "config.json 解析失败" in cfg.get("_load_error", "")
        finally:
            skilllocker_base.CONFIG_FILE = old_file


@case("load_config: store 为空串/相对根退回默认仓库（L3）")
def _():
    old_file = skilllocker_base.CONFIG_FILE
    with tempfile.TemporaryDirectory() as td:
        cfg_file = os.path.join(td, "config.json")
        skilllocker_base.CONFIG_FILE = cfg_file
        try:
            for bad in ('{"store": ""}', '{"store": "."}', '{"store": ".."}'):
                write(cfg_file, bad)
                cfg = skilllocker.load_config()
                assert cfg["store"] == os.path.normpath(
                    skilllocker.DEFAULT_STORE).replace(os.sep, "/"), cfg
                assert "为空或相对根" in cfg.get("_load_error", ""), cfg
        finally:
            skilllocker_base.CONFIG_FILE = old_file


# ---------------------------------------------------------------- 导入预检
@case("import_preflight: 标准目录 / frontmatter / 规模与结构统计")
def _():
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "my-skill")
        write(os.path.join(src, "SKILL.md"),
              "---\nname: my-skill\ndescription: 预检冒烟。\n---\nbody")
        write(os.path.join(src, "scripts", "a.py"), "x=1")
        cfg = tmp_cfg(os.path.join(td, "store"))
        os.makedirs(cfg["store"])
        p = skilllocker.import_preflight(cfg, src)
        assert p["ok"], p
        assert p["conflict"] == "none" and p["resolved"] is None, p
        assert p["has_skill_md"] and p["fm"]["name"] == "my-skill", p
        assert p["files"] >= 2 and p["bytes"] > 0, p
        assert p["tree"][0] == "SKILL.md" and "scripts/ ×1" in p["tree"], p


@case("import_preflight: 同名 same-name / 同内容 same-content 带 hash8")
def _():
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "my-skill")
        write(os.path.join(src, "SKILL.md"), "---\nname: my-skill\n---\nbody")
        store = os.path.join(td, "store")
        cfg = tmp_cfg(store)
        con, old = _temp_db(td)
        try:
            skilllocker.import_dir(con, cfg, src)
            # 内容抄一份一致的 → same-content + hash8（显式传名：预检名取传入值
            # 或源目录名，mirror 目录名与仓库里的 my-skill 无关）
            mirror = os.path.join(td, "mirror")
            shutil.copytree(src, mirror)
            p = skilllocker.import_preflight(cfg, mirror, "my-skill")
            assert p["conflict"] == "same-content" and p.get("hash8"), p
            # 改一个字节 → same-name
            write(os.path.join(mirror, "SKILL.md"), "---\nname: my-skill\n---\nbody2")
            p = skilllocker.import_preflight(cfg, mirror, "my-skill")
            assert p["conflict"] == "same-name" and "hash8" not in p, p
        finally:
            con.close()
            skilllocker_base.DB_FILE = old


@case("import_preflight: 缺 SKILL.md 警示不阻断；skill.md 小写也算有")
def _():
    with tempfile.TemporaryDirectory() as td:
        cfg = tmp_cfg(os.path.join(td, "store"))
        os.makedirs(cfg["store"])
        nomd = os.path.join(td, "no-md")
        os.makedirs(nomd)
        write(os.path.join(nomd, "readme.txt"), "hi")
        p = skilllocker.import_preflight(cfg, nomd)
        assert p["ok"] and not p["has_skill_md"] and p["conflict"] == "none", p
        lower = os.path.join(td, "lower-md")
        os.makedirs(lower)
        write(os.path.join(lower, "skill.md"), "---\nname: lower\n---\n")
        p = skilllocker.import_preflight(cfg, lower)
        assert p["ok"] and p["has_skill_md"], p


@case("import_preflight: junction 源解析 resolved；死链拒绝")
def _():
    if sys.platform != "win32":
        raise AssertionError("仅 Windows（需要 junction）")
    with tempfile.TemporaryDirectory() as td:
        real = os.path.join(td, "real-skill")
        os.makedirs(real)
        write(os.path.join(real, "SKILL.md"), "---\nname: real\n---\n")
        cfg = tmp_cfg(os.path.join(td, "store"))
        os.makedirs(cfg["store"])
        link = os.path.join(td, "link-skill")
        skilllocker.make_junction(link, real)
        try:
            p = skilllocker.import_preflight(cfg, link)
            assert p["ok"] and p["resolved"], p
            assert p["resolved"].endswith("real-skill"), p
        finally:
            os.remove(link)
        # 死链没法直接建（mklink 拒绝不存在目标）：建好后删目标变成死链
        dead = os.path.join(td, "dead-link")
        vanish = os.path.join(td, "vanish-target")
        os.makedirs(vanish)
        skilllocker.make_junction(dead, vanish)
        shutil.rmtree(vanish)
        try:
            p = skilllocker.import_preflight(cfg, dead)
            assert not p["ok"] and "解析不到" in p.get("reason", ""), p
        finally:
            os.remove(dead)
        # is_reparse 对不存在的路径必须返回 False（GetFileAttributesW 的 -1
        # 不能撞上 _REPARSE 位——死链预检曾因此误判成「链接解析不到」）
        assert not skilllocker.is_reparse(os.path.join(td, "nope-at-all"))


@case("import_preflight: 非法名 name_error 不抛；不是目录 ok=False")
def _():
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "ok-skill")
        os.makedirs(src)
        write(os.path.join(src, "SKILL.md"), "---\nname: ok\n---\n")
        cfg = tmp_cfg(os.path.join(td, "store"))
        os.makedirs(cfg["store"])
        # 空串名等价于「取源目录名」（与 import_dir 的 name or basename 同语义），不在非法名单
        for bad in ("a/b", "..", "con", "x.", "a:b"):
            p = skilllocker.import_preflight(cfg, src, bad)
            assert p["ok"] and p["name_error"], (bad, p)
        p = skilllocker.import_preflight(cfg, os.path.join(td, "missing"))
        assert not p["ok"] and "不是目录" in p["reason"], p


# ---------------------------------------------------------------- 在线检索 / 下载
# 全部离线：monkeypatch 掉 _net_get / _net_json / _net_bytes / _list_repo_files，
# 只验证 URL 组装、size 校验、落盘、安全防线与重试语义本身（退避置 0 加速）。

@case("online: _parse_source 归一化与拒绝")
def _():
    ps = skilllocker_links._parse_source
    assert ps("anthropics/skills") == ("anthropics/skills", "")
    assert ps("anthropics/skills", "dev") == ("anthropics/skills", "dev")
    assert ps("anthropics/skills@dev") == ("anthropics/skills", "dev")
    assert ps("https://github.com/a-b/c_d.git") == ("a-b/c_d", "")
    assert ps("https://github.com/a/b/tree/feature/x") == ("a/b", "feature/x")
    assert ps("https://www.github.com/a/b") == ("a/b", "")
    for bad in ("", "justone", "a/b/c", "https://gitlab.com/a/b", "a b/c", "../x", "a/.."):
        try:
            ps(bad)
            raise AssertionError("应拒绝: %r" % bad)
        except RuntimeError:
            pass


@case("online: _safe_dest 拒绝越界 / 盘符 / Windows 保留名")
def _():
    sd = skilllocker_links._safe_dest
    with tempfile.TemporaryDirectory() as td:
        ok = sd(td, "scripts/run.py")
        assert os.path.normcase(ok).startswith(os.path.normcase(td))
        for bad in ("../evil.txt", "a/../../evil", "/etc/x", "C:/x/y",
                    "con", "aux.md", "com1.dat", ".", ""):
            try:
                sd(td, bad)
                raise AssertionError("应拒绝: %r" % bad)
            except RuntimeError:
                pass


@case("online: online_fetch 假网络下载落盘与建议名")
def _():
    lk = skilllocker_links
    md_body, py_body = b"---\nname: foo\n", b"print(1)"
    files = [("README.md", 9),
             ("skills/foo/SKILL.md", len(md_body)),
             ("skills/foo/scripts/run.py", len(py_body)),
             ("other/bar/SKILL.md", 5)]
    bodies = {"skills/foo/SKILL.md": md_body, "skills/foo/scripts/run.py": py_body}
    saved = (lk._list_repo_files, lk._net_bytes)

    def fake_list(source, branch=None):
        return "main", files, "jsdelivr", "a" * 40

    def fake_bytes(url, expected_size=None, deadline=None, cancel=None):
        for p, b in bodies.items():
            if p in url:
                assert len(b) == expected_size, (p, expected_size)
                return b
        raise AssertionError("不该下载: " + url)

    lk._list_repo_files, lk._net_bytes = fake_list, fake_bytes
    got = None
    try:
        got = lk.online_fetch("anthropics/skills", "foo")
        root_f = lk._dl_root().replace(os.sep, "/")
        assert got["name"] == "foo" and got["branch"] == "main"
        assert got["listing"] == "jsdelivr" and got["path"].startswith(root_f), got
        real = got["path"].replace("/", os.sep)
        assert open(os.path.join(real, "SKILL.md"), "rb").read() == md_body
        assert os.path.isfile(os.path.join(real, "scripts", "run.py"))
        assert got["files"] == 2 and got["bytes"] == len(md_body) + len(py_body), got
        assert got["timings"]["parallel"] == 2 and got["timings"]["total_ms"] >= 0, got
        assert not os.path.isdir(os.path.join(real, "..", "other"))
    finally:
        lk._list_repo_files, lk._net_bytes = saved
        if got:
            shutil.rmtree(got["path"].replace("/", os.sep), ignore_errors=True)


@case("online: blob sha 内容校验（正确接受 / 错误换源 / 全错拒绝）")
def _():
    lk = skilllocker_links
    body = b"hello"
    blob = lk._git_blob_sha(body)
    assert len(blob) == 40 and blob == lk.hashlib.sha1(b"blob 5\0hello").hexdigest()
    saved = lk._net_bytes
    try:
        lk._net_bytes = lambda url, expected_size=None, deadline=None, cancel=None: body
        assert lk._download_blob("a/b", "main", "x", 5, blob_sha=blob) == body

        def flip(url, expected_size=None, deadline=None, cancel=None):
            return b"HELLO" if "gcore" in url else body

        lk._net_bytes = flip
        assert lk._download_blob("a/b", "main", "x", 5, blob_sha=blob) == body
        lk._net_bytes = lambda url, expected_size=None, deadline=None, cancel=None: b"HELLO"
        try:
            lk._download_blob("a/b", "main", "x", 5, blob_sha=blob)
            raise AssertionError("坏内容应拒绝")
        except RuntimeError as exc:
            assert "内容校验失败" in str(exc), exc
    finally:
        lk._net_bytes = saved


@case("online: 进度回调 / 预取消 / 下载中取消")
def _():
    lk = skilllocker_links
    files = [("skills/foo/SKILL.md", 4), ("skills/foo/a.py", 4)]
    saved = (lk._list_repo_files, lk._net_bytes)
    lk._LIST_CACHE.clear()
    lk._list_repo_files = lambda s, b=None: ("main", files, "github", None)
    lk._net_bytes = lambda url, expected_size=None, deadline=None, cancel=None: b"xxxx"
    got = None
    try:
        events = []
        got = lk.online_fetch("a/b", "foo",
                              progress=lambda d, t, n, p: events.append((d, t, n)))
        assert events and events[0] == (0, 2, 0), events
        assert events[-1][0] == 2 and events[-1][1] == 2 and events[-1][2] == 8, events
        # 预取消：进入下载前就退出
        ev = threading.Event()
        ev.set()
        try:
            lk.online_fetch("a/b", "foo", cancel=ev)
            raise AssertionError("应取消")
        except lk._NetFatal as exc:
            assert "取消" in str(exc), exc
        # 下载中取消：两个文件都返回后，最终检查置位 → 清临时目录并报取消
        ev2 = threading.Event()
        state = {"n": 0}

        def cancel_bytes(url, expected_size=None, deadline=None, cancel=None):
            state["n"] += 1
            if state["n"] >= 2:
                ev2.set()
            return b"xxxx"

        lk._net_bytes = cancel_bytes
        try:
            lk.online_fetch("a/b", "foo", cancel=ev2)
            raise AssertionError("下载中取消应失败")
        except lk._NetFatal as exc:
            assert "取消" in str(exc), exc
        assert state["n"] >= 2, state
    finally:
        lk._list_repo_files, lk._net_bytes = saved
        lk._LIST_CACHE.clear()
        if got:
            shutil.rmtree(got["path"].replace("/", os.sep), ignore_errors=True)


@case("online: 找不到 skill 名报候选；越界清单路径拒绝")
def _():
    lk = skilllocker_links
    saved = (lk._list_repo_files, lk._net_bytes)
    lk._net_bytes = lambda url, expected_size=None, cancel=None: b"xxxx"  # noqa: E731
    try:
        lk._list_repo_files = lambda s, b=None: (  # noqa: E731
            "main", [("skills/x/SKILL.md", 4), ("skills/y/SKILL.md", 4)], "jsdelivr", None)
        try:
            lk.online_fetch("a/b", "nope")
            raise AssertionError("找不到时应报错")
        except RuntimeError as exc:
            assert "找不到" in str(exc) and "skills/x" in str(exc), exc
        lk._list_repo_files = lambda s, b=None: (  # noqa: E731
            "main", [("skills/x/SKILL.md", 4), ("skills/x/../../../evil.txt", 5)], "jsdelivr", None)
        try:
            lk.online_fetch("a/b", "x")
            raise AssertionError("越界路径应报错")
        except RuntimeError as exc:
            assert "越界" in str(exc), exc
    finally:
        lk._list_repo_files, lk._net_bytes = saved


@case("online: Windows 保留名文件拒绝")
def _():
    lk = skilllocker_links
    saved = (lk._list_repo_files, lk._net_bytes)
    lk._list_repo_files = lambda s, b=None: (  # noqa: E731
        "main", [("aux/SKILL.md", 4), ("aux/con.txt", 5)], "jsdelivr", None)
    lk._net_bytes = lambda url, expected_size=None, cancel=None: b"xxxx"  # noqa: E731
    try:
        try:
            lk.online_fetch("a/b", "aux")
            raise AssertionError("保留名应报错")
        except RuntimeError as exc:
            assert "Windows 无法创建" in str(exc), exc
    finally:
        lk._list_repo_files, lk._net_bytes = saved


@case("online: 仓库根就是 skill（根 SKILL.md 命中）")
def _():
    lk = skilllocker_links
    d = lk._find_skill_dir([("SKILL.md", 5), ("scripts/a.py", 1)],
                           "agent-browser", "vercel-labs/agent-browser")
    assert d == "", d
    try:
        lk._find_skill_dir([("skills/a/SKILL.md", 1)], "zzz", "u/r")
        raise AssertionError("应报错")
    except RuntimeError as exc:
        assert "skills/a" in str(exc), exc


@case("online: _net_bytes 传输完整性 / 重试 / 404 换源 / Range 分块")
def _():
    import urllib.error
    lk = skilllocker_links
    saved = (lk.NET_RETRIES, lk.NET_BACKOFF, lk.NET_BIGFILE, lk.NET_CHUNK, lk._net_get)
    lk.NET_RETRIES, lk.NET_BACKOFF = 3, 0
    try:
        # 半截响应（Content-Length 与实际不符）：重试到底后报「传输不完整」
        calls = []

        def short(url, headers=None, rng=None, deadline=None, cancel=None):
            calls.append(url)
            return 200, b"abc", {"Content-Length": "10"}

        lk._net_get = short
        try:
            lk._net_bytes("http://x/f", expected_size=3)
            raise AssertionError("传输不完整应报错")
        except RuntimeError as exc:
            assert "传输不完整" in str(exc) and len(calls) == 3, (exc, len(calls))
        # 网络错误重试后成功
        state = {"n": 0}

        def flaky(url, headers=None, rng=None, deadline=None, cancel=None):
            state["n"] += 1
            if state["n"] < 3:
                raise urllib.error.URLError("reset")
            return 200, b"hello", {}

        lk._net_get = flaky
        assert lk._net_bytes("http://x/f", expected_size=5) == b"hello"
        assert state["n"] == 3
        # 0 字节文件（expected=0）合法
        lk._net_get = lambda url, headers=None, rng=None, deadline=None, cancel=None: (200, b"", {})  # noqa: E731
        assert lk._net_bytes("http://x/e", expected_size=0) == b""
        # 404 → _NetFatal（确定性失败，换源不重试）
        def nf(url, headers=None, rng=None, deadline=None, cancel=None):
            raise urllib.error.HTTPError(url, 404, "nf", None, None)

        lk._net_get = nf
        try:
            lk._net_bytes("http://x/f")
            raise AssertionError("404 应 _NetFatal")
        except lk._NetFatal:
            pass
        # 大文件：首块直接 0..CHUNK-1 拿总大小（不再先探测 1 字节），逐块拼装
        data = b"0123456789" * 10  # 100 字节
        seen = []

        def ranges(url, headers=None, rng=None, deadline=None, cancel=None):
            assert rng and rng.startswith("bytes="), rng
            seen.append(rng)
            a, b = [int(x) for x in rng[6:].split("-")]
            hs = {"Content-Range": "bytes %d-%d/100" % (a, b)} if a == 0 else {}
            return 206, data[a:b + 1], hs

        lk.NET_BIGFILE, lk.NET_CHUNK = 50, 30
        lk._net_get = ranges
        assert lk._net_bytes("http://x/big", expected_size=999) == data  # 清单 size 999 是陈旧的
        assert len(seen) == 4, seen  # 0-29 / 30-59 / 60-89 / 90-99
        # 实测 jsDelivr 会返回自相矛盾的 206：Content-Range 陈旧（总长 50），
        # 响应体却是整文件（100，与 tree size 一致）→ 按整包处理，不能截断
        def stale(url, headers=None, rng=None, deadline=None, cancel=None):
            return 206, data, {"Content-Range": "bytes 0-49/50"}

        lk._net_get = stale
        assert lk._net_bytes("http://x/stale", expected_size=100) == data
        try:
            lk._net_bytes("http://x/stale", expected_size=99)
            raise AssertionError("头与 body 都不可信时应拒绝")
        except RuntimeError as exc:
            assert "首块不完整" in str(exc), exc
    finally:
        lk.NET_RETRIES, lk.NET_BACKOFF, lk.NET_BIGFILE, lk.NET_CHUNK, lk._net_get = saved


@case("online: _download_blob 双源切换与 LFS 拒绝")
def _():
    lk = skilllocker_links
    saved = lk._net_bytes
    try:
        def raw_ok(url, expected_size=None, deadline=None, cancel=None):
            if "jsdelivr" in url:
                raise lk._NetFatal("HTTP 404")
            assert "raw.githubusercontent" in url
            return b"ok"

        lk._net_bytes = raw_ok
        assert lk._download_blob("a/b", "main", "x/SKILL.md", 2) == b"ok"

        def both_404(url, expected_size=None, deadline=None, cancel=None):
            raise lk._NetFatal("HTTP 404")

        lk._net_bytes = both_404
        try:
            lk._download_blob("a/b", "main", "x/SKILL.md", 2)
            raise AssertionError("双源失败应报错")
        except RuntimeError as exc:
            assert "全部候选源" in str(exc), exc

        def lfs(url, expected_size=None, deadline=None, cancel=None):
            return b"version https://git-lfs.github.com/spec/v1\noid sha256:abc"

        lk._net_bytes = lfs
        try:
            lk._download_blob("a/b", "main", "x.bin", 60)
            raise AssertionError("LFS 指针应报错")
        except RuntimeError as exc:
            assert "LFS" in str(exc), exc
    finally:
        lk._net_bytes = saved


@case("online: 镜像顺序 / 301 守卫 / 源健康度")
def _():
    lk = skilllocker_links
    urls = lk._blob_urls("a/b", "main", "x/SKILL.md", "c" * 40)
    hosts = [u[1].split("/")[2] for u in urls]
    assert hosts[0] == "gcore.jsdelivr.net", hosts
    assert "testingcf.jsdelivr.net" in hosts and hosts[-1] == "raw.githubusercontent.com"
    # raw 重定向直接判失败，不跟随到不可达主机
    req = lk.urllib.request.Request("https://cdn.jsdelivr.net/x")
    try:
        lk._NoRawRedirect().redirect_request(
            req, None, 301, "m", {}, "https://raw.githubusercontent.com/x")
        raise AssertionError("应拦截 raw 重定向")
    except lk._NetFatal:
        pass
    # 源健康度：成功源优先；连续失败 2 次本轮跳过
    st = lk._FetchState()
    assert st.order(urls)[0][1] == urls[0][1]
    st.mark("gcore.jsdelivr.net", False)
    st.mark("gcore.jsdelivr.net", False)
    assert all("gcore.jsdelivr.net" not in u[1] for u in st.order(urls))
    st.mark("testingcf.jsdelivr.net", True)
    assert "testingcf.jsdelivr.net" in st.order(urls)[0][1]


@case("online: _net_retry 总预算到期立即停止")
def _():
    lk = skilllocker_links
    calls = {"n": 0}

    def once():
        calls["n"] += 1
        return b"ok"

    try:
        lk._net_retry(once, "x", deadline=lk.time.monotonic() - 1)
        raise AssertionError("预算耗尽应停止")
    except lk._NetFatal as exc:
        assert "预算" in str(exc), exc
    assert calls["n"] == 0, calls


@case("online: online_search 解析 / 重试 / 空词不请求")
def _():
    lk = skilllocker_links
    saved = (lk._net_get, lk.NET_RETRIES, lk.NET_BACKOFF)
    lk.NET_RETRIES, lk.NET_BACKOFF = 3, 0
    payload = {"skills": [
        {"id": "anthropics/skills/pdf", "source": "anthropics/skills", "skillId": "pdf",
         "name": "pdf", "installs": 220593},
        {"name": "non-github", "source": "", "installs": None},
    ]}
    seen = {"n": 0, "url": ""}

    def flaky(url, headers=None, rng=None):
        seen["n"] += 1
        seen["url"] = url
        if seen["n"] < 3:
            raise OSError("reset")
        return 200, json.dumps(payload).encode("utf-8"), {}

    lk._net_get = flaky
    try:
        rows = lk.online_search(" pdf ")
        assert seen["n"] == 3 and "q=pdf" in seen["url"], (seen, rows)
        r0 = rows[0]
        assert r0["name"] == "pdf" and r0["source"] == "anthropics/skills"
        assert r0["installs"] == 220593
        assert r0["page_url"] == "https://www.skills.sh/anthropics/skills/pdf"
        assert r0["source_url"] == "https://github.com/anthropics/skills"
        assert rows[1].get("source_url") is None  # 非 GitHub 源只给页面
        seen["n"] = 0
        assert lk.online_search("") == [] and seen["n"] == 0
    finally:
        lk._net_get, lk.NET_RETRIES, lk.NET_BACKOFF = saved


@case("online: 清单以 GitHub tree 为准，jsDelivr 只兜底；sha 解析")
def _():
    lk = skilllocker_links
    saved = (lk._github_tree, lk._jsdelivr_flat, lk._resolve_commit)
    lk._LIST_CACHE.clear()
    try:
        # tree 可用 → 直接用，不问 jsDelivr
        lk._github_tree = lambda s, b: [("a/SKILL.md", 1)]  # noqa: E731
        lk._jsdelivr_flat = lambda s, b: [("phantom.txt", 1)]  # noqa: E731
        lk._resolve_commit = lambda s, b: "c" * 40  # noqa: E731
        b, files, provider, sha = lk._list_repo_files("u/r")
        assert provider == "github" and files == [("a/SKILL.md", 1)] and sha == "c" * 40
        # tree 拿不到 → 退 jsDelivr；兜底路径不再花一次 API 解析 commit
        lk._LIST_CACHE.clear()
        lk._github_tree = lambda s, b: None  # noqa: E731
        b, files, provider, sha = lk._list_repo_files("u/r")
        assert provider == "jsdelivr" and files == [("phantom.txt", 1)] and sha is None
        # 都拿不到 → 报错
        lk._LIST_CACHE.clear()
        lk._jsdelivr_flat = lambda s, b: None  # noqa: E731
        try:
            lk._list_repo_files("u/r")
            raise AssertionError("应报错")
        except RuntimeError as exc:
            assert "拿不到" in str(exc), exc
    finally:
        lk._github_tree, lk._jsdelivr_flat, lk._resolve_commit = saved
        lk._LIST_CACHE.clear()


@case("online: _resolve_commit 解析 40 位 sha / 坏响应给 None")
def _():
    lk = skilllocker_links
    saved = lk._net_json
    try:
        lk._net_json = lambda url, headers=None: {"object": {"sha": "ca45fc496a3436251f191354091d557da31c15b5",  # noqa: E731
                                                            "type": "commit"}}
        assert lk._resolve_commit("u/r", "main") == "ca45fc496a3436251f191354091d557da31c15b5"
        lk._net_json = lambda url, headers=None: {"version": None}  # noqa: E731 - jsDelivr resolved 的坏形态
        assert lk._resolve_commit("u/r", "main") is None

        def boom(url, headers=None):
            raise RuntimeError("dns down")

        lk._net_json = boom
        assert lk._resolve_commit("u/r", "main") is None
    finally:
        lk._net_json = saved


# ---------------------------------------------------------------- rename 重试
# 在线获取后立即导入曾必现 WinError 5：刚 copytree 落盘的文件被杀软 / 索引
# 服务短暂握住句柄，目录改名被拒。_replace_dir 退避重试；这里用真实句柄复现。

@case("copy_tree_atomic: _replace_dir 瞬时句柄重试后成功（真实句柄）")
def _():
    if sys.platform != "win32":
        return  # 非 Windows 上打开的句柄不阻止目录改名，无此场景
    lk = skilllocker_links
    saved = lk.RENAME_BACKOFF
    lk.RENAME_BACKOFF = (0.05,) * 12
    td = tempfile.mkdtemp()
    try:
        tmp = os.path.join(td, ".skpart-t-dst")
        dst = os.path.join(td, "dst")
        os.makedirs(os.path.join(tmp, "sub"))
        write(os.path.join(tmp, "sub", "a.py"), "x = 1\n")
        fh = open(os.path.join(tmp, "sub", "a.py"), "rb")  # 模拟杀软握住句柄

        def release():
            time.sleep(0.3)
            fh.close()

        th = threading.Thread(target=release)
        th.start()
        try:
            lk._replace_dir(tmp, dst)
        finally:
            th.join()
        assert os.path.isdir(dst) and not os.path.lexists(tmp)
    finally:
        lk.RENAME_BACKOFF = saved
        shutil.rmtree(td, ignore_errors=True)


@case("copy_tree_atomic: 句柄一直被占 → 重试耗尽抛原错误；释放后可过")
def _():
    if sys.platform != "win32":
        return
    lk = skilllocker_links
    saved = lk.RENAME_BACKOFF
    lk.RENAME_BACKOFF = (0.01,) * 3
    td = tempfile.mkdtemp()
    try:
        tmp = os.path.join(td, ".skpart-t-dst2")
        dst = os.path.join(td, "dst2")
        os.makedirs(tmp)
        write(os.path.join(tmp, "a.py"), "x = 1\n")
        with open(os.path.join(tmp, "a.py"), "rb"):
            try:
                lk._replace_dir(tmp, dst)
                raise AssertionError("句柄被占应报错")
            except PermissionError:
                pass
        # 释放后同一调用应成功：重试不吞真实的成功路径
        lk._replace_dir(tmp, dst)
        assert os.path.isdir(dst)
    finally:
        lk.RENAME_BACKOFF = saved
        shutil.rmtree(td, ignore_errors=True)


@case("copy_tree_atomic: replace 最终失败清掉 .skpart 临时目录")
def _():
    lk = skilllocker_links
    saved = lk._replace_dir
    td = tempfile.mkdtemp()
    try:
        src = os.path.join(td, "src")
        os.makedirs(src)
        write(os.path.join(src, "SKILL.md"), "---\nname: t\n---\n")
        store = os.path.join(td, "store")
        os.makedirs(store)

        def boom(a, b):
            raise PermissionError(5, "拒绝访问", a, b)

        lk._replace_dir = boom
        try:
            lk.copy_tree_atomic(src, os.path.join(store, "t"))
            raise AssertionError("replace 失败应上抛")
        except PermissionError:
            pass
        leftovers = [n for n in os.listdir(store) if n.startswith(".skpart-")]
        assert not leftovers, leftovers
    finally:
        lk._replace_dir = saved
        shutil.rmtree(td, ignore_errors=True)


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
    print("=== UNIT %d/%d ===" % (len(CASES) - len(failed), len(CASES)))
    if failed:
        print("failed:", "; ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
