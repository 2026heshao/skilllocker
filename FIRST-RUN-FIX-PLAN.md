# skillkit 首次启动「识别并链接 agent/skill」修复 · 执行文档

| 项 | 内容 |
|---|---|
| 文档版本 | v1.0（2026-10-31） |
| 依据 | 首轮代码评审（OCR delegate 模式）+ 临时目录全链路仿真实测 |
| 修复范围 | `kit/` 后端 4 文件、`ui/` 前端 2 文件、`.gitignore`、2 份文档 |
| 预计工时 | 代码 3~4h + 测试 2~3h，合计约 1 人日 |
| 前置条件 | 评审结论成立（见 §2 问题清单）；实施前备份 `kit/skillkit.db` |

---

## 1. 背景与问题总览

对 skillkit 做了「软件被别人安装后首次打开，能否正确识别并链接 agent 与 skill」的专项评审。
在隔离临时目录中模拟了全新机器（全新数据库 + 假 HOME + 两个已安装 agent + 中央仓库含 1 个 skill），
逐帧复现 `bootstrap → 手动刷新 → 开关切换` 完整链路，实测结论：

| 实测步骤 | 结果 |
|---|---|
| 首次打开（bootstrap） | 看板 **0 agent、0 skill**，尽管本机装了 Claude Code / Cursor、仓库里有 skill |
| 手动「重新探测」后 | 2 个 agent 被识别，但全部显示「已连接」而 **linked=0，junction 一个都没建** |
| 点击 agent 开关（ON） | **no-op**（`changed=0`，无 junction）；必须先关再开才建链（off→on 后 `changed=1`、junction 出现） |
| 仓库 store 指向不存在的盘符 | `bootstrap` 抛 `FileNotFoundError`，前端**静默退回内置演示快照**（开发者机器的数据），用户看到假数据 |

问题清单（编号后文引用）：

| 编号 | 严重度 | 位置 | 问题一句话 |
|---|---|---|---|
| BUG-1 | Critical | `kit/skillkit_base.py:34,66`、`kit/config.json` | 中央仓库路径硬编码 `D:/skillkit/store` 且随仓库分发；无 D: 盘机器首启必崩，且失败被前端吞掉换成演示数据 |
| BUG-2 | High | `kit/desktop.py:482-493` | `bootstrap()` 从不调用 `sync_tools_from_adapters`，首屏 agent/skill 全空，识别只能靠手动「重新探测」 |
| BUG-3 | High | `kit/skillkit_store.py:49-51` + `kit/desktop.py:673` | 首次入库 `enabled=detected` 使 agent 显示「已连接」但整条链路无 `do_sync` 建链；且 `set_agent` 对已 enabled 工具是 no-op，开关无法补链，陷入死区 |
| BUG-4 | Medium | `kit/desktop.py:28` | `import webview` 在顶层 try 之外，目标机器缺 pywebview 时进程静默死亡（无日志无弹窗） |
| BUG-5 | Medium | `kit/skillkit_store.py:29-34` | adapters.json 缺失/损坏时静默回退，全新机器空表 → 永远 0 agent，界面无感知 |
| BUG-6 | Medium | 仓库分发契约 | `kit/config.json` 被 git 跟踪（带出开发者绝对路径）；`.gitignore` 漏掉 `kit/config.json`、`kit/skillkit.log*`；打包清单未定义，zip 全量打包会把开发者本机 db/状态带出去 |
| BUG-7 | Low | `kit/skillkit_store.py:20,484` | 「已连接」语义 = 开关开而非实际有链接，首启误导（BUG-3 修复后自然消解，不单独立项） |

---

## 2. 修复目标与验收总纲（Definition of Done）

全部满足才算完成：

1. **干净机器**（无 `skillkit.db`、无 `config.json`、任意安装盘符）双击 `启动 skillkit.bat`：
   首屏即出现本机已装 agent（状态「已关闭」、开关为 off）与中央仓库 skill 列表。
2. 打开任一 agent 开关：其 skills 目录出现指向 `<安装目录>/store/<skill>` 的 junction，
   `python skillkit.py status` 中该格口为 `ok-link`。
3. **无 D: 盘的机器**：不出现演示数据冒充真实数据；要么正常工作（store 跟随安装目录），要么显示明确错误页（含错误详情与重试按钮）。
4. 缺 pywebview 的 Python 环境：启动时弹出明确提示框并写日志，而非静默退出。
5. **老机器升级零迁移**：已有 `config.json` / `skillkit.db` 的机器升级后行为不变（enabled、链接、meta 均不动）。

---

## 3. 修复项详单

> 约定：每项独立提交（commit message 前缀 `fix(first-run):`），便于逐项回滚。
> 代码草案均为 diff 语义示意，行号以当前工作区为准，实施时以内容定位。

### FIX-01 [P0] 中央仓库路径可移植化 + load_config 容错 + config.json 出库

**现状与根因**（BUG-1）

- `kit/skillkit_base.py:34`：`DEFAULT_STORE = "D:/skillkit/store"` 硬编码开发者盘符；
- `kit/config.json`（git 已跟踪）内容 `{"store": "D:/skillkit/store"}`，随仓库分发把绝对路径带给每个用户；
- `kit/skillkit_base.py:66`：`load_config()` 里 `os.makedirs(cfg["store"], exist_ok=True)` 不捕获异常，
  盘符不存在时直接抛 `FileNotFoundError` → bootstrap 失败 → 前端冒充演示数据（联动 FIX-04）。

**改动**

① `kit/skillkit_base.py:34` — 默认值改为跟随安装目录（开发机 `D:\skillkit` 下结果与旧值完全一致，零迁移）：

```python
# 现状
DEFAULT_STORE = "D:/skillkit/store"
# 改为：KIT = kit/ 目录，上一级即安装根目录
DEFAULT_STORE = os.path.normpath(
    os.path.join(os.path.dirname(KIT), "store")).replace(os.sep, "/")
```

② `kit/skillkit_base.py:66` — makedirs 容错，失败留痕不抛：

```python
# 现状
os.makedirs(cfg["store"], exist_ok=True)
# 改为
try:
    os.makedirs(cfg["store"], exist_ok=True)
except OSError as exc:
    cfg["_load_error"] = ("中央仓库目录不可创建: %s（%s）；"
                          "请检查磁盘或修改 config.json 的 store" % (cfg["store"], exc))
```

失败后的行为链：`load_config` 不抛 → bootstrap 继续 → FIX-02 的 rescan 走到
`store_skill_dirs` 抛出「中央仓库目录不存在」→ `_guard` 返回 ok:false → FIX-04 错误页如实展示。
**不允许任何一层把它换成演示数据。**

③ `kit/config.json` 出库（本地文件保留不动）：

```bash
git rm --cached kit/config.json
```

④ `.gitignore` 追加（与 FIX-07 合并提交亦可）：

```gitignore
kit/config.json
kit/skillkit.log
kit/skillkit.log.*
```

**验收**

- 无 D: 盘环境（或 config.json 指向不存在盘符）：`load_config()` 不抛异常，`cfg["_load_error"]` 有值；
- 无 config.json 全新机器：store 解析为 `<安装目录>/store`；
- 开发机升级后：store 仍解析为 `D:/skillkit/store`（安装目录推导 == 旧硬编码值），现有 db/store 无感。

**风险**

- `kit/test_unit.py:549` 断言 `cfg["store"] == skillkit.DEFAULT_STORE` —— 常量**名**保留即通过，无需改测试；
- 用户手工改过 store 到非默认路径的（config.json 存在）不受影响，config.json 优先级不变。

---

### FIX-02 [P1] bootstrap 首屏探测：sync_tools + 空库自动 rescan

**现状与根因**（BUG-2）

`desktop.py:482-493` `bootstrap()` 只做 `load_config → migrate_folders → seed_meta → list_boards`；
`sync_tools_from_adapters` 只在 `rescan()` 桥（desktop.py:652）与 CLI `detect` 里调用，
而 `list_boards` 明确只读（`skillkit_boards.py:4`）。全新机器 tools 表为空 → 首屏 0 agent。
同时 skills 表也为空（rescan 未跑过）→ 0 skill。实测见 §1 表格第 1 行。

**改动**（`kit/desktop.py` `Api.bootstrap`）

```python
@_guard
@_serialized
def bootstrap(self):
    con = skillkit.db()
    try:
        cfg = skillkit.load_config()
        if cfg.get("_load_error"):
            skillkit.log_event(con, "warn", cfg["_load_error"])
            con.commit()
        # 首屏探测：幂等、毫秒级（读 adapters.json + 每工具一次 isdir）
        skillkit.sync_tools_from_adapters(con, cfg)
        # 全新数据库：中央仓库内容从未登记过，自动扫一次（仅 skills 表为空时，
        # 避免每次启动都全量哈希 store）。store 不可读时 rescan 抛错 → 走 FIX-04 错误页
        if not con.execute("select 1 from skills limit 1").fetchone():
            skillkit.rescan(con, cfg)
        skillkit.migrate_folders(con, cfg)
        skillkit.seed_meta(con, cfg)
        return _boards_payload(con, cfg)
    finally:
        con.close()
```

**顺序约束（必须遵守）**

- `sync_tools_from_adapters` 必须在 `seed_meta` **之前**：`seed_meta → unique_entries → ordered_tools` 依赖 tools 表；
- `rescan` 必须在 `seed_meta` **之前**：seed 逐行读 skills 表播种。

**验收**

- 全新机器首屏：agents > 0（本机已装的）、skills == store 内 skill 数、folders 已播种；
- 第二次启动不再触发 rescan（skills 表非空），启动耗时无回退；
- store 指向不存在盘符：bootstrap 返回 ok:false、error 含「中央仓库目录不存在」，**不是**静默空看板。

**风险**

- rescan 全量哈希只在空库时发生一次（新机器 54MB 级 store 约 1~3s，可接受）；
- `启动首屏变慢` 仅限首次，`kit/selftest.py` 的 bootstrap 超时（180s 桥超时）覆盖无虞，回归跑一次即可。

---

### FIX-03 [P1] 首次入库 enabled=0，开关语义修正

**现状与根因**（BUG-3）

- `kit/skillkit_store.py:47-51`：首次入库 `(key,label,sd,dd,detected, detected)` —— 最后一个 `detected` 落在
  enabled 列，探测到的 agent 一律默认「已连接」；但整条首次链路（bootstrap/rescan）都不调 `do_sync` 建链；
- `kit/desktop.py:673`：`if bool(on) and not row["enabled"]` —— 已 enabled 的工具点 ON 走 no-op 分支
  （UI 开关渲染为 ON，点击只会发 OFF），用户**无法通过开关补建链接**，必须关→开。
  实测见 §1 表格第 3 行。

**改动**（`kit/skillkit_store.py:47-51`）

```python
# 现状
con.execute("insert into tools(key,label,skills_dir,detect_dir,detected,enabled,mode) "
            "values(?,?,?,?,?,?, 'junction')",
            (key, label, sd, dd, detected, detected))
# 改为：首次入库一律默认关闭分发，由用户显式打开开关（set_agent → do_sync 建 junction）
con.execute("insert into tools(key,label,skills_dir,detect_dir,detected,enabled,mode) "
            "values(?,?,?,?,?,?, 'junction')",
            (key, label, sd, dd, detected, 0))
```

同步更新该处注释（原文「首次入库：检测到的工具默认开启分发，没检测到的默认关」）。

**行为变化推演**

| 场景 | 修复前 | 修复后 |
|---|---|---|
| 新机器探测后 | 全部「已连接」、linked=0、开关点不动 | 「已关闭」、开关 off；打开 → `set_agent` on 分支成立 → `do_sync` 建 junction |
| 老机器升级（db 里已有行） | — | **不受影响**：update 分支不碰 enabled，已 enabled 的行保持原值与链接 |
| agent 后装（先探测时未装） | detected 0→1，enabled 保持 0 | 同左，语义一致 |

**验收**

- 全新库跑 `sync_tools_from_adapters`：detected=1 的行 enabled=0，看板状态「已关闭」；
- `set_agent(key, True)`：junction 出现在 `<HOME>/<skills_dir>/<skill>`，`target_state` = ok-link；
- 开发机现有 db：所有行 enabled 值不变。

**可选加固（不强制，时间允许再做）**

`set_agent` 对 already-enabled 的 ON 调用执行一次 `do_sync(only_tools=[key], apply_changes=True)`
补链，防御历史库里可能存在的「enabled=1 但链接缺失」脏行。改动点：`desktop.py:704-706` 的 else 分支。

**风险**

- 无 schema 变更、无数据回写，纯 insert 默认值变化，回滚 = revert commit；
- 前端无需改动：`AGENT_STATUS_LABEL.off = "已关闭"`（skillkit_store.py:20）与开关渲染已覆盖该态。

---

### FIX-04 [P1] bootstrap 失败不再冒充演示数据（UI 错误页）

**现状与根因**（BUG-1 的前端半边）

- `ui/bridge.js:77`：`res.ok === false` → reject；
- `ui/app.js:3392-3400` boot() 的 catch：**一律** `bootLocal(window.SKILLKIT_DATA)` ——
  把开发者机器的真实快照（`ui/data.js`，含 `D:\skillkit\store`、54.4 MB 等信息）端给用户，仅一条 toast。
  演示降级本意是「pywebview 注入超时的只读原型」，被扩大成了「后端任何失败都冒充成功」。

**改动**（`ui/app.js:3392-3400`）

```js
}).catch(function (err) {
  BootFX.fail();
  if (SKB.isDesktop()) {          // 桌面壳已连上但后端报错：明确失败，绝不给演示数据
    MODE = 'error';
    renderBootError(err);
    return;
  }
  // pywebview 注入超时（api=null）：保留现有只读原型降级 + toast
  MODE = 'local';
  toast('bad', '后端连接失败', String(err && err.message || err) + '，已退回内置快照');
  bootLocal(window.SKILLKIT_DATA);
});
```

新增 `renderBootError(err)`（app.js）+ 对应样式（styles.css）：全屏面板，含
标题「后端启动失败」、错误详情（`err.message`）、按钮【重试】（`location.reload()`）、
提示行「详细原因见 kit\skillkit.log」。`window.__SK_APP` 状态同步（MODE='error' 时
refresh 等入口禁用或提示）。

**验收**

- store 指向不存在盘符：显示错误页（含 FileNotFoundError 文案），**无演示数据**；
- 点【重试】可重载；非桌面原型模式（file:// 打开）降级行为不变。

**风险**

- 无；注意 `BootFX`（启动动画编排）需在 fail 分支先行收尾，避免动画卡在加载态（现有 `BootFX.fail()` 已调用，保持顺序）。

---

### FIX-05 [P2] pywebview 缺依赖兜底

**现状与根因**（BUG-4）

`kit/desktop.py:28` `import webview` 在模块顶层、`main()` 的 try 之外。pythonw 无控制台，
ImportError 发生在 `_setup_logging()` 之前 → 无日志、无弹窗、bat `start` 后立即退出，用户一无所知。

**改动**

```python
# kit/desktop.py:28
try:
    import webview
except ImportError:
    webview = None
```

`main()` 中 `_setup_logging()` 之后、`os.path.isfile(INDEX)` 检查之前：

```python
if webview is None:
    _report("缺少依赖：pywebview 未安装。请在当前 Python 环境执行 pip install pywebview 后重试")
    return 2
```

`_report` 已具备「日志 + MessageBoxW」双通道，无需新增机制。

**验收**：用未装 pywebview 的解释器启动 → 弹出提示框 + `kit/skillkit.log` 有记录，进程退出码 2。

**可选**：`启动 skillkit.bat` 在 `:run` 前加 `"%PYW%" -c "import webview" 2>nul || goto nopywebview` 预检（提示更早，非必须）。

---

### FIX-06 [P2] adapters.json 降级显性化

**现状与根因**（BUG-5）

`kit/skillkit_store.py:29-34`：解析失败 → 沿用库内工具表。全新机器库是空表 → 返回 `[]`，
只有一条 warn 事件，界面永远 0 agent 且无任何异常感。

**改动**（`kit/skillkit_store.py` except 分支）——**回退结果为空时升格为错误**：

```python
except (OSError, ValueError) as exc:
    rows = [(r["key"], r["label"], r["skills_dir"], r["detect_dir"], 1)
            for r in con.execute(
                "select key,label,skills_dir,detect_dir from tools where detected=1")]
    if not rows:
        # 全新机器 + adapters.json 不可读 = 无从探测，静默返回空表会让用户
        # 困在「永远 0 agent」里；宁可让本轮失败并如实报错
        raise RuntimeError("adapters.json 缺失或损坏，且工具表为空，无法探测任何 agent: %s" % exc)
    log_event(con, "warn", "adapters.json 解析失败，沿用库内工具表: %s" % exc)
    con.commit()
    return rows
```

**验收**

- 空表 + 损坏 adapters.json：`sync_tools_from_adapters` 抛 RuntimeError，bootstrap/rescan 走错误页；
- 非空表 + 损坏 adapters.json：行为不变（沿用库内工具表，warn 留痕）——
  现有用例 `kit/test_unit.py:519`（先 `insert_tool` 再调）**保持通过**；
- CLI `python skillkit.py detect`：异常直接抛出属可接受（信息明确）；如需友好化，在 `skillkit.py` detect 分支包一层 try 打印错误后 `SystemExit(1)`。

---

### FIX-07 [P2] 分发契约固化

**现状与根因**（BUG-6）

`kit/config.json` 被 git 跟踪（FIX-01③ 已出库）；`.gitignore` 已忽略
`kit/skillkit.db`、`kit/desktop_profile/`、`store/`，但漏 `kit/config.json`、`kit/skillkit.log*`；
无成文打包清单，zip 全量分发会把开发者本机 db（含 enabled 状态与用户 meta）、日志、profile 带出去。

**改动**

① `.gitignore` 追加（见 FIX-01④，合并提交）。

② 成文打包清单（写入 `kit/DESKTOP.md` 新章节「分发与安装」）：

| 处理 | 内容 |
|---|---|
| 必含 | `kit/*.py`、`kit/adapters.json`、`kit/assets/`、`ui/`（index.html/app.js/bridge.js/data.js/styles.css）、`启动 skillkit.bat` |
| 必排除 | `kit/config.json`、`kit/skillkit.db*`、`kit/skillkit.log*`、`kit/desktop_profile/`、`kit/ui-drag.log`、`kit/launch.log`、`store/`、`__pycache__/` |
| 环境要求 | Windows 10+；Python 3.12（pythonw 可用）且已装 pywebview |

③ 文档同步：

- `kit/DESKTOP.md`：bootstrap 行为变更（首屏探测 + 空库自动 rescan）、enabled 语义变更
  （首次入库默认关闭）、store 默认路径变更（跟随安装目录）、依赖要求；
- `ui/UI-SPEC.md`：新增启动错误页规格（FIX-04）。

---

## 4. 实施顺序与依赖

```
FIX-01 (P0 路径可移植)  ──┐
FIX-03 (P1 enabled=0)  ──┼─→  FIX-02 (P1 bootstrap 探测)  →  FIX-04 (P1 UI 错误页)
                          │        ↑ 顺序不能反：先 FIX-03 再 FIX-02，
                          │        否则首屏探测会把所有 agent 自动置为「已连接」而 0 链接，
                          │        复刻 BUG-3
FIX-05 (P2 pywebview)  ───┴→  FIX-06 (P2 adapters 降级)  →  FIX-07 (P2 契约与文档)
```

| 阶段 | 内容 | 预计 | 产出 |
|---|---|---|---|
| 0 | 备份 `kit/skillkit.db`、确认工作区干净、建分支 | 10min | 分支 `fix/first-run-detection` |
| 1 | FIX-01 + .gitignore | 40min | commit 1 |
| 2 | FIX-03 | 20min | commit 2 |
| 3 | FIX-02 | 40min | commit 3 |
| 4 | FIX-04（app.js + styles.css） | 60min | commit 4 |
| 5 | FIX-05、FIX-06 | 40min | commit 5、6 |
| 6 | FIX-07（清单 + 两份文档同步） | 30min | commit 7 |
| 7 | 测试（§5）与修复回补 | 2~3h | 测试通过记录 |

---

## 5. 测试计划

### 5.1 自动化：新增 `kit/test_first_run.py`

以本轮评审的仿真脚本为基础，沿用 `test_unit.py` 的 `@case` 风格（tempfile + 模块属性打补丁，
`skillkit_base.DB_FILE/CONFIG_FILE/ADAPTERS_FILE/DEFAULT_STORE/HOME` +
`skillkit_store.HOME`、`skillkit_boards.HOME` 一并 patch —— 注意 store/boards 的 HOME 是
from-import 值绑定，只 patch base 不生效）。四个用例对应四个核心验收：

```python
# 骨架（补丁基建复用 test_unit 的 _temp_db 模式）
@case("first_run: 全新库 bootstrap 即识别 agent 与 skill，且 enabled=0")
def _():
    # 假 HOME 装 2 个 agent（.claude/skills、.cursor），假 store 放 1 个 skill，
    # 无 config.json；依次执行 bootstrap 五步（load_config → sync_tools →
    # 空库 rescan → migrate_folders → seed_meta）后 list_boards：
    # assert len(agents) == 2, all(a["status"] == "off")、skills == 1

@case("first_run: 开关 ON 建 junction 且 ok-link")
def _():
    # 接上例：desktop.Api().set_agent("claude_code", True) 后
    # assert os.path.isdir(<HOME>/.claude/skills/demo-skill)
    # 且 target_state(...) == ("ok-link", ...)

@case("first_run: store 指向不存在盘符时 load_config 不抛、留痕")
def _():
    # config.json 写 {"store": "<absent>:/skillkit/store"}：
    # assert load_config() 不抛 且 "_load_error" in cfg

@case("first_run: 空表 + adapters.json 损坏 → 显式报错")
def _():
    # assertRaises RuntimeError: sync_tools_from_adapters(空表, 坏 adapters)
```

### 5.2 受影响的现有测试

| 位置 | 影响 | 处理 |
|---|---|---|
| `kit/test_unit.py:549`（load_config 断言 DEFAULT_STORE） | 常量名保留、值变为可移植路径 | 断言仍通过，无需改；建议追加断言「无 config.json 时 store 以安装目录为基」 |
| `kit/test_unit.py:519`（adapters 损坏回退 L4） | 回退时表非空，行为不变 | 保持通过；新增空表抛错用例（见 5.1 第 4 例） |
| `kit/selftest.py`（离屏自检跑真实 db） | bootstrap 行为变化 | 回归跑 `python desktop.py --selftest`，确认 UI 与写库断言全绿 |

### 5.3 手动验收清单（按 DoD 四场景）

**场景 A · 干净新机器**（或删除 kit/skillkit.db、kit/config.json 模拟）：
双击 bat → 首屏出现已装 agent（状态「已关闭」）与 store 内 skill → 打开开关 →
agent skills 目录出现 junction → `python kit/skillkit.py status` 该格口 ok-link。

**场景 B · 无 D: 盘**：config.json 指向不存在盘符（或临时把安装目录移到 C: 且删除 config.json）→
启动显示错误页（含错误详情 + 重试），全程**不出现演示快照数据**。

**场景 C · 缺 pywebview**：用未装 pywebview 的解释器启动 → 弹窗提示 + skillkit.log 有记录。

**场景 D · 老机器升级**：保留现有 config.json/db 启动 → enabled/链接/meta 全部原样，
store 仍解析到原路径。

---

## 6. 回滚方案

- 全部修复无 DB schema 变更、无数据迁移（FIX-03 只改 insert 默认值，不回写已有行）；
- 按 commit 逐个 revert 即可，建议 revert 顺序与实施顺序相反；
- FIX-01③ 的 `git rm --cached` 只动索引，本地 config.json 不动，revert 后恢复跟踪；
- 唯一需人工确认的回滚点：FIX-02 使空库首次启动产生一次 rescan 事件（events 表留痕），回滚后无副作用。

---

## 7. 附录：本轮评审实测记录（复现凭据）

仿真脚本要点：tempfile 隔离，patch `skillkit_base.{DB_FILE,CONFIG_FILE,ADAPTERS_FILE,DEFAULT_STORE,HOME}`
与 `skillkit_store.HOME`、`skillkit_boards.HOME`；假 HOME 内建 `.claude/skills`、`.cursor`；
假 store 内建 1 个含 SKILL.md 的 skill；adapters 只含 cursor/claude_code 两条。

```
STEP1 bootstrap  -> agents on board: 0, skills: 0, tools rows: 0
STEP2 after rescan -> agents: 2, skills: 1
   tool claude_code  detected=1 enabled=1 status=connected linked=0
   tool cursor       detected=1 enabled=1 status=connected linked=0
   junction created automatically in agent dir? False
STEP3 set_agent(ON)  while enabled=1 -> changed=0, junction=False
      set_agent(OFF) -> removed=0
      set_agent(ON)  after OFF  -> changed=1, junction=True
Scenario B（store 指向不存在的 Q: 盘）:
   bootstrap -> ok=False, error=FileNotFoundError: [WinError 3] 系统找不到指定的路径。: 'Q:/'
```

该脚本将按 §5.1 改造为 `kit/test_first_run.py` 入库，作为本修复的永久回归防线。
