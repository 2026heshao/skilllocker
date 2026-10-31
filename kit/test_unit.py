#!/usr/bin/env python3
"""skillkit 纯函数 / 库层单元测试（无第三方依赖，直接 python test_unit.py 运行）。

覆盖 CODE_REVIEW.md 第三批点名的表驱动对象：
  parse_frontmatter / seed_category / target_state 状态机 / prune_meta 降级 /
  meta 读写截断 / 原子拷贝 / 内容哈希冲突对照 / 删除清理 / import 名校验 /
  events 轮转 / hash 缓存。每个用例自建临时目录与临时 DB，互不污染。
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import skillkit  # noqa: E402
import skillkit_base  # noqa: E402

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
        fm = skillkit.parse_frontmatter(p)
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
        fm = skillkit.parse_frontmatter(p)
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
        assert skillkit.parse_frontmatter(p1) == {}
        assert skillkit.parse_frontmatter(p2) == {}
        assert skillkit.parse_frontmatter(p3).get("name") == "bom"


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
        got = skillkit.seed_category(name)
        assert got == want, "%s -> %s (want %s)" % (name, got, want)


@case("fmt_bytes / norm_path 边界")
def _():
    assert skillkit.fmt_bytes(0) == "0 B"
    assert skillkit.fmt_bytes(512) == "512 B"
    assert skillkit.fmt_bytes(2048) == "2.0 KB"
    assert skillkit.fmt_bytes(20 * 1024) == "20 KB"
    assert skillkit.fmt_bytes(2 * 1024 * 1024) == "2.0 MB"
    assert skillkit.norm_path("C:\\Users\\x\\") == "c:/Users/x"  # 只小写盘符
    assert skillkit.norm_path("\\\\?\\C:\\x") == "c:/x"
    assert skillkit.norm_path("") == ""


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
        h1 = skillkit.sha_tree(os.path.join(store, "c1"))
        os.makedirs(os.path.join(store, "c2"))  # 无 SKILL.md → 断链素材
        other = os.path.join(td, "other")
        os.makedirs(other)
        write(os.path.join(other, "SKILL.md"), "other\n")
        os.makedirs(agent)
        # ok-link / broken-link / foreign-link
        skillkit.make_junction(os.path.join(agent, "c1"), os.path.join(store, "c1"))
        skillkit.make_junction(os.path.join(agent, "c2"), os.path.join(store, "c2"))
        skillkit.make_junction(os.path.join(agent, "c3"), other)
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
            state, _ = skillkit.target_state(
                os.path.join(agent, n), cfg, srow(n, "x" * 64), trow)
            assert state == expect, "%s -> %s (want %s)" % (n, state, expect)
        assert skillkit.target_state(
            os.path.join(agent, "c5"), cfg, srow("c5", h1), trow)[0] == "takeover"
        assert skillkit.target_state(
            os.path.join(agent, "c5"), cfg, srow("c5", "x" * 64), trow)[0] == "conflict-dir"
        cpy = {"mode": "copy", "skills_dir": agent.replace(os.sep, "/")}
        assert skillkit.target_state(
            os.path.join(agent, "c6"), cfg, srow("c6", h1), cpy)[0] == "ok-copy"
        assert skillkit.target_state(
            os.path.join(agent, "c7"), cfg, srow("c7", h1), cpy)[0] == "stale-copy"
        off = {"mode": "off", "skills_dir": agent.replace(os.sep, "/")}
        assert skillkit.target_state(
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
        h1 = skillkit._hash_tree(os.path.join(agent, "s1"), cache)
        write(os.path.join(agent, "s1", "SKILL.md"), "v2\n")
        assert skillkit._hash_tree(os.path.join(agent, "s1"), cache) == h1  # 缓存命中
        assert skillkit._hash_tree(os.path.join(agent, "s1"), {}) != h1    # 新快照现算


# ---------------------------------------------------------------- prune / meta
def _temp_db(td):
    # DB_FILE 等常量归属于 skillkit_base；打补丁必须打到所属模块，
    # 打到 skillkit 门面上不会影响 db() 的实际读取
    old = skillkit_base.DB_FILE
    skillkit_base.DB_FILE = os.path.join(td, "test.db")
    con = skillkit.db()
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
            assert skillkit.prune_meta(con, boards) == 2
            left = {(r["scope"], r["key"]) for r in
                    con.execute("select scope,key from skill_meta")}
            assert left == {("shared", "a"), ("unique", "t/u")}, left
            # 降级快照：本轮绝不删
            con.execute("insert into skill_meta(scope,key) values('shared','gone2')")
            con.commit()
            degraded = {"meta": {"scanDegraded": ["C:\\x: 失败"]}, "skills": [], "unique": []}
            assert skillkit.prune_meta(con, degraded) == 0
            assert con.execute(
                "select count(*) from skill_meta where key='gone2'").fetchone()[0] == 1
        finally:
            con.close()
            skillkit_base.DB_FILE = old


@case("set_meta / get_meta: 截断与回读")
def _():
    with tempfile.TemporaryDirectory() as td:
        con, old = _temp_db(td)
        try:
            skillkit.migrate_folders(con, {"store": td})  # 建 folder_id 列 + 预置夹
            fid = skillkit.folder_create(con, "测试夹", "teal")["id"]
            con.commit()
            row = skillkit.set_meta(con, "shared", "k", {
                "display": "x" * 60, "note": "n" * 500, "folder": fid})
            assert len(row["display"]) == 48 and len(row["note"]) == 400
            assert row["folder"] == fid
            got = skillkit.get_meta(con)["k"]
            assert got["display"] == "x" * 48 and got["folder"] == fid
            # 不带 folder 字段 = 保留原归属；categories 列按口径原样写回
            skillkit.set_meta(con, "shared", "k", {"display": "y"})
            assert skillkit.get_meta(con)["k"]["folder"] == fid
            skillkit.set_meta(con, "unique", "t/k", {"display": "u", "folder": fid})
            uk = skillkit.get_meta(con)["t/k"]
            assert uk["tool"] == "t" and uk["folder"] == ""
        finally:
            con.close()
            skillkit_base.DB_FILE = old


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
            assert skillkit.rescan(con, cfg) == 1
            assert skillkit.migrate_folders(con, cfg) >= 0
            seeded = skillkit.seed_meta(con, cfg)
            assert seeded == 3, seeded
            meta = skillkit.get_meta(con)
            # shared 行：folder_id = 预置夹、categories 写 '[]'；unique 行 folder 恒空
            assert meta["shared1"]["folder"] == "eng"
            assert meta["shared1"]["cats"] == []
            assert meta["t1/uniq1"]["folder"] == ""
            assert meta["t1/uniq1"]["cats"] == ["eng"]
            assert meta["t1/shared1"]["folder"] == ""
            assert meta["t1/shared1"]["cats"] == ["doc"]
            uniq = skillkit.unique_entries(con, cfg)
            kinds = {(u["name"]): u["kind"] for u in uniq}
            assert kinds == {"uniq1": "unique", "shared1": "overridden"}, kinds
        finally:
            con.close()
            skillkit_base.DB_FILE = old


# ---------------------------------------------------------------- rescan / 拷贝
@case("rescan: 中央仓库不可读直接报错，不清库（M3）")
def _():
    with tempfile.TemporaryDirectory() as td:
        con, old = _temp_db(td)
        try:
            cfg = tmp_cfg(os.path.join(td, "no-such-store"))
            try:
                skillkit.rescan(con, cfg)
                raise AssertionError("应该抛 RuntimeError")
            except RuntimeError as exc:
                assert "不可读" in str(exc) or "不存在" in str(exc), exc
        finally:
            con.close()
            skillkit_base.DB_FILE = old


@case("copy_tree_atomic: 成功翻正 / 目标已存在报错 / 无残留临时目录（L5）")
def _():
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "src")
        write(os.path.join(src, "sub", "a.txt"), "data")
        store = os.path.join(td, "store")
        os.makedirs(store)
        dst = os.path.join(store, "skill-a")
        skillkit.copy_tree_atomic(src, dst)
        assert os.path.isfile(os.path.join(dst, "sub", "a.txt"))
        assert not [n for n in os.listdir(store) if ".skpart-" in n]
        os.makedirs(os.path.join(store, "skill-b"), exist_ok=True)
        write(os.path.join(store, "skill-b", "old.txt"), "old")
        try:
            skillkit.copy_tree_atomic(src, os.path.join(store, "skill-b"))
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
                    skillkit.import_dir(con, cfg, src, bad)
                    raise AssertionError("%r 应被拒绝" % bad)
                except RuntimeError as exc:
                    assert "非法 skill 名" in str(exc), exc
            # --name 空串等价于不传：回退源目录名
            name = skillkit.import_dir(con, cfg, src, "")
            assert name == "myskill"
            assert con.execute(
                "select count(*) from skills where name='myskill'").fetchone()[0] == 1
            assert os.path.isfile(os.path.join(store, "myskill", "SKILL.md"))
        finally:
            con.close()
            skillkit_base.DB_FILE = old


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
            det = skillkit.conflict_detail(con, cfg, "t1", "s")
            assert det["commonDiffs"] == 2, det["commonDiffs"]
            cflags = {f["name"]: f["flag"] for f in det["central"]["files"]}
            lflags = {f["name"]: f["flag"] for f in det["local"]["files"]}
            assert cflags == {"SKILL.md": "diff", "a.txt": "diff", "b.txt": "same"}, cflags
            assert lflags == {"SKILL.md": "diff", "a.txt": "diff", "b.txt": "same",
                              "c.txt": "local-only"}, lflags
        finally:
            con.close()
            skillkit_base.DB_FILE = old


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
            skillkit.rescan(con, cfg)
            con.execute("insert into suppressed(tool,skill,kind,note) values('t1','r1','conflict','x')")
            con.execute("insert into skill_meta(scope,key) values('shared','r1')")
            con.commit()
            skillkit.remove_skill(con, cfg, "r1")
            assert con.execute("select count(*) from skills where name='r1'"
                               ).fetchone()[0] == 0
            assert con.execute("select count(*) from suppressed where skill='r1'"
                               ).fetchone()[0] == 0
            assert con.execute("select count(*) from skill_meta where key='r1'"
                               ).fetchone()[0] == 0
            assert not os.path.isdir(os.path.join(store, "r1"))
        finally:
            con.close()
            skillkit_base.DB_FILE = old


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
        skillkit.make_junction(link, elsewhere)
        con, old = _temp_db(td)
        try:
            cfg = tmp_cfg(store)
            insert_tool(con, "t1", agent)
            con.execute("insert into skill_meta(scope,key) values('unique','t1/cont/sub1')")
            con.commit()

            # 容器子目录：永久删除子目录，容器与 meta 行一起清
            u = skillkit.find_unique_row(con, cfg, "t1", "cont/sub1")
            assert u and u["deletable"] is False and u["kind"] == "unique", u
            act = skillkit.uninstall_unique(con, cfg, "t1", "cont/sub1")
            assert act["files"] >= 1 and act["kind"] == "unique", act
            assert not os.path.isdir(sub) and os.path.isdir(cont)
            assert con.execute("select count(*) from skill_meta where key='t1/cont/sub1'"
                               ).fetchone()[0] == 0

            # foreign-link：只摘链接，真身目录原样保留
            u = skillkit.find_unique_row(con, cfg, "t1", "linkdir")
            assert u and u["kind"] == "foreign-link" and u["deletable"] is False, u
            act = skillkit.uninstall_unique(con, cfg, "t1", "linkdir")
            assert act["files"] == 0 and act["kind"] == "foreign-link", act
            assert not os.path.lexists(link)
            assert os.path.isfile(os.path.join(elsewhere, "SKILL.md"))

            # 拒绝：行已不存在
            try:
                skillkit.uninstall_unique(con, cfg, "t1", "cont/sub1")
                raise AssertionError("已卸载的行应被拒绝")
            except RuntimeError:
                pass
        finally:
            con.close()
            skillkit_base.DB_FILE = old


# ---------------------------------------------------------------- 杂项
@case("log_event: events 按水位轮转（L6）")
def _():
    with tempfile.TemporaryDirectory() as td:
        con, old = _temp_db(td)
        try:
            keep = skillkit_base.EVENTS_KEEP
            skillkit_base.EVENTS_KEEP = 10
            try:
                for i in range(30):
                    skillkit.log_event(con, "t", str(i))
                con.commit()
                n = con.execute("select count(*) from events").fetchone()[0]
                assert n <= 22, n  # 超过 2 倍水位就裁，最多留 10+ 插入中的几条
                top = con.execute("select max(id) from events").fetchone()[0]
                assert top == 30
            finally:
                skillkit_base.EVENTS_KEEP = keep
        finally:
            con.close()
            skillkit_base.DB_FILE = old


@case("sha_tree: 忽略名单生效且内容变化敏感（M4）")
def _():
    with tempfile.TemporaryDirectory() as td:
        write(os.path.join(td, "a.txt"), "one")
        write(os.path.join(td, ".git", "x"), "ignored")
        h1 = skillkit.sha_tree(td)
        h2 = skillkit.sha_tree(td)
        assert h1 == h2
        write(os.path.join(td, "a.txt"), "two")
        assert skillkit.sha_tree(td) != h1


@case("_hash_tree: 无缓存时直接现算，与 sha_tree 一致")
def _():
    with tempfile.TemporaryDirectory() as td:
        write(os.path.join(td, "a.txt"), "x")
        assert skillkit._hash_tree(td) == skillkit.sha_tree(td)
        cache = {}
        assert skillkit._hash_tree(td, cache) == skillkit.sha_tree(td)
        assert skillkit.norm_path(td) in cache


@case("sync_tools_from_adapters: adapters.json 损坏时沿用库内工具表（L4）")
def _():
    with tempfile.TemporaryDirectory() as td:
        con, old = _temp_db(td)
        try:
            old_file = skillkit_base.ADAPTERS_FILE
            broken = os.path.join(td, "adapters.json")
            write(broken, "{not json")
            skillkit_base.ADAPTERS_FILE = broken
            try:
                insert_tool(con, "t1", os.path.join(td, "agent"))
                con.commit()
                rows = skillkit.sync_tools_from_adapters(con, {"store": td})
                assert [(r[0], r[4]) for r in rows] == [("t1", 1)], rows
            finally:
                skillkit_base.ADAPTERS_FILE = old_file
        finally:
            con.close()
            skillkit_base.DB_FILE = old


@case("load_config: config.json 损坏退回默认并留痕（L4）")
def _():
    old_file = skillkit_base.CONFIG_FILE
    with tempfile.TemporaryDirectory() as td:
        broken = os.path.join(td, "config.json")
        write(broken, "{oops")
        skillkit_base.CONFIG_FILE = broken
        try:
            cfg = skillkit.load_config()
            assert cfg["store"] == skillkit.DEFAULT_STORE
            assert "config.json 解析失败" in cfg.get("_load_error", "")
        finally:
            skillkit_base.CONFIG_FILE = old_file


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
