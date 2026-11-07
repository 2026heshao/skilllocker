# skilllocker 桌面端 · 使用与实现说明

> 版本 v0.1.0　最后更新：2026-10-31
> 代码位置：`D:\skilllocker\kit\desktop.py`（外壳） + `D:\skilllocker\kit\skilllocker.py`（数据层）
> 界面位置：`D:\skilllocker\ui\`（`index.html` / `styles.css` / `app.js` / `bridge.js` / `data.js`）

---

## 1. 它是什么 / 不是什么

**是**：一个无边框的本地窗口（pywebview + Edge WebView2），直接把 `skilllocker.db` 里的真实数据渲染成「共有 / 独有 / 智能体」三个看板；改名、备注、分类即时写进 SQLite。

**不是**：不是一个独立发布的桌面应用——**没有**安装包、托盘、开机自启、单实例锁；**没有** HTTP/REST 服务；**没有**任何 mock 数据（界面上的每一行都来自扫描真实仓库与磁盘）。

关掉窗口 = 结束进程。

### 1.1 智能体看板（本版新增）

左侧第三个看板列出本机已知 agent，状态全部由 `skilllocker.list_boards()` 现算，不信任开关本身：

| 状态 | 判定 | 界面 |
|---|---|---|
| 已连接(N) | `detected=1` 且 `enabled=1` | N = 磁盘上实际指向中央仓库的 junction/拷贝数；开关打开 |
| 已关闭(0) | `detected=1` 但 `enabled=0` | 开关关闭，目录里没有本工具建立的链接 |
| 未安装 | `detect_dir` 不存在 | 开关禁用，提示安装后「重新探测」 |

开关语义：

- **首次入库默认关闭**：`sync_tools_from_adapters` 探测到的新 agent 一律 `enabled=0`（状态「已关闭」）——探测到 ≠ 授权分发。用户打开开关时才经 `set_agent → do_sync` 建 junction。首次启动即自动探测（`bootstrap()` 内置，无需手动「重新探测」），老库已有行的 enabled 不受影响。
- **开**：`do_sync(only_tools=[key], apply_changes=True)`，按中央仓库补齐链接，不覆盖冲突目录。
- **持续状态**：开关开着时，之后新入库、扫描到新增或内容变更的 skill 会自动补链（桌面导入 / 刷新扫描 / 启动时发现仓库新目录 / CLI `import` / 独有转共有），不用再重新开关一次。分发仍走 `do_sync`（junction 优先，copy 模式刷新副本）；单个 agent 失败不影响其他 agent，错误进 `events` 并在导入 / 刷新提示里如实计数。
- **关**：二次确认后 `unlink_tool()` —— 只删除本工具建立的 junction、以及内容与中央一致的拷贝；**中央仓库的 skill 实体一个不动**。冲突副本会保留，并在 toast 里报告保留数量。
- **事件入口只认 `change`**：开关的 `<input>` 是透明覆盖层（`position:absolute; inset:0; z-index:2`），点 `label` 或装饰 `i` 时 Chromium 还会把点击转发给 `input`，一次按下会派发 **两个 `click`**；且 `click` 冒到 `document` 时 `checked` **已经是翻转后的值**。因此 `click` 里读 `!box.checked` 再取反正好反了——点「开」着的开关反而又发一次 open（界面表现为「关不掉链接」）。现在 `click` 只负责拦下（不触发行选中），真正动作在 `document` 的 `change` 里按 `box.checked`（= 目标状态）走 `requestAgent(key, checked)`，一次按下只产生一个 change。
- 取消/失败回滚：`requestAgent(key, on, onAbort)` 的 `onAbort` 会把开关恢复到数据模型的值（`confirmModal` 支持 `opts.onCancel`，取消、右上角关闭、点遮罩都会触发）。

**外部来源（只检测、不分发）**：有些 agent 除 `skills_dir` 还会读自己的额外目录。典型是 Trae CN：`~/.trae-cn/skills` 之外还读 `~/.agents/skills`（ClawHub / GitHub 安装的**真身**，安装记录在 `~/.agents/.skill-lock.json`）。这类目录由 `adapters.json` 的 `extra_dirs` 声明（本机现在 Trae CN 和 ZCode 都声明了它——ZCode 会话同样从这个目录加载技能），`list_boards()` 只统计其中**与中央仓库同名**的实体，输出 `agents[].external[]`（`rel` / `path` / `count` / `same` / `diff` / `names`）：界面在行标题上标「外部 N」，详情栏列出来源目录并说明「本工具不分发、不删除这里，开关关闭后它们仍会被加载」。**不同名**的实体归独有看板，见 §1.2。

> 实机例子：Trae CN 关闭后 `~/.trae-cn/skills` 为 0 项，但 `~/.agents/skills` 仍有 15 个与仓库同名的实体（15 个内容一致、0 个不同），所以**新开对话照样能调用**——这是如实展示，不是开关失效。本工具刻意不删这些实体（它们是 ClawHub 装的资产）。

### 1.2 独有看板（检测口径，2026-10-31 修订）

「独有」= **agent 能加载、但中央仓库没有（或同名但内容不同）的 skill**，由 `skilllocker.unique_entries()` 现扫磁盘得出。修订单口径只扫「已启用 agent 的主 skills 目录」，现在覆盖四类来源，行的 `src` 字段标注：

| src | 来源 | 例子 | 可删副本 |
|---|---|---|---|
| `main` | agent 主 skills 目录下的实体目录 | `~/.codex/skills/...` | 是 |
| `extra` | `adapters.json` 的 `extra_dirs` 外部目录（支持 `~` 相对、HOME 相对与**盘符绝对路径**三种写法） | `~/.agents/skills` 里的 wbexpert / clawhub 系列；`~/.trae-cn` 的 12 个 Trae 内置技能；`~/.dsh/.../dsh-memory` 灵枢插件的 7 个 lingshu-*；`D:/Deepseek Harness/resources/runtime/office-skills` 的 7 个 office 文档技能 | 否（agent 自己管理） |
| `extra`（`/**` 扫描） | `extra_dirs` 写 `根/**` 时递归扫插件缓存 / 应用内置目录树 | `~/.zcode/cli/plugins/cache/**`（23 个官方插件技能）+ `D:/Zcode/resources/glm/packages/**`（包源，缓存没有的如 dynamic-workflows 从这里出）；`~/.qoder/plugins/cache/**`、`~/.qoder/tmp/kbstage/skills/_自定义`（5 个自建）、`D:/Qoder/.qoder-versions/**`（扩展技能 canvas/qmind/sites-* 等）；`D:/workbuddy/.../workbuddy-builtin/**`（50 个内置与内置插件技能，含 workbuddy-docx 套件的 8 个子技能） | 否 |
| `foreign-link`（`kind`） | 指向中央仓库**以外**的 junction | `~/.agents/skills/tabbit` → Tabbit 应用目录 | 否（真身在别处） |
| 容器展开 | 顶层无 `SKILL.md` 但子目录是 skill 的容器，钻一层 | `~/.codex/skills/.system/` → 5 个 codex 系统 skill，名记 `.system/<子目录>` | 否 |

`/**` 扫描的规则：只认「父目录叫 `skills` 且自己含 SKILL.md」的目录，技能名取 SKILL.md 的 `name` 字段（fallback 目录名）。同名条目两步去重：**先做版本折叠** —— 把路径里的版本号段（`0.2.2`、`0.5.1` 这类）去掉得到变体键，同键只留 mtime 最新的（缓存/版本目录里新旧并存而 agent 只加载最新装的）；**剩下的真变体都保留**，名字加「（首次分歧的路径段）」区分（Qoder 的 computer-use 有 Windows/macOS/Linux 三个 OS 变体，就出三行）。同一 agent 的多个来源再按 `extra_dirs` 声明顺序做同名去重（先声明的赢：zcode 的插件缓存赢过安装目录包源）。链接不跟进，防止 node_modules 式依赖链接把扫描带飞。

**刻意不收的外部技能源**（避免看板被目录/暂存噪音淹没，需要时往对应 agent 的 `extra_dirs` 里加一条即可）：

- `~/.claude/plugins/marketplaces/**`：只是插件市场目录的 git 克隆（本机没装任何 Claude 插件），不是可用技能。
- `~/.workbuddy/buddy-skill-store`、`buddy-snapshots`、`connectors-marketplace`：WorkBuddy 应用商店的源与快照（2300+ 条），是否激活由 WorkBuddy 内部管理。**应用内置**的 50 个（`workbuddy-builtin`）已收。
- `~/.codex/.tmp/**`：插件市场的临时解包目录。
- `~/.trae-cn/builtin/baseline/code/<模型变体>/skills`：与已收录的 `global` 同一套内置技能按模型分 variant 重复存放。
- `~/.trae-cn/design_libraries`：设计素材库（带 SKILL.md 但无证据表明被技能运行时加载）。

规则：

- **口径与中央仓库一致**：目录必须含 `SKILL.md` / `skill.md` 才算 skill；随机数据目录、断链、非 skill 容器不再误报（修订前 `codex/.system` 被当成 1 条独有，里面真正的 5 个 skill 反而看不见）。
- **分发开关关闭的 agent 也扫**：开关只管本工具建立的链接，agent 自装的 skill 关了开关仍会被加载，不能从看板上消失。
- **同名且内容与中央一致的实体一律不算独有**（无论来源）；同名不同内容记 `overridden`（同名覆盖，详情栏可看差异——`trae_cn/skill-creator` 就是这么来的：Trae 内置版与中央版同名不同内容）。
- **共享目录去重归属**：多个 agent 声明同一个 `extra_dirs`（如 `~/.agents/skills`）时按路径去重，行归 `adapters.json` 里**先声明**它的 agent（固定归属，本机 = ZCode；不跟 `ordered_tools` 的 enabled 排序走，否则切换 agent 开关会让行键漂移、把行上已写的 meta 清掉），其余读取者放 `sharedWith`；行上带「外部」徽标，agent 徽标 hover 提示共读者，详情栏有「共读 agent」「来源目录」行。
- **删除防线**：`deletable=false` 的行详情栏不渲染删除按钮；即便绕过 UI 直调 `delete_skill`，后端也按 `find_unique_row()` 现查行并拒绝（提示「外部来源，由 agent 自己管理」）；`open_dir` 同样走现查路径，不再假设主目录直拼。
- **卸载外部来源（v0.2 显式开通）**：`deletable=false` 的行详情栏渲染「卸载」按钮（foreign-link 为「摘除链接」），走独立的 `uninstall_unique(key)` 接口——`delete_skill` 的原防线不动。规则：foreign-link **只摘链接**（真身在别处，绝不顺着链接删实体）；extra 目录 / 容器子目录**永久删除该目录**（`delete_tree` 自带 reparse 防护），确认弹窗给出完整路径、文件数与「agent 可能重新安装」警示；路径一律 `find_unique_row()` 现查，拒绝中央仓库内的任何路径；卸载同时清该行 unique meta 并记 `uninstall-unique` 事件。
- `seed_meta()` 改为幂等补种：磁盘上新增的独有条目下次启动自动补预置分类，已有的 meta 不动。


---

### 1.3 首启自动接管 / 自动建库 + 人工出口（v0.3.2）

安装版的中央仓库默认在 **`<安装目录>\\store`**（`DEFAULT_STORE = <程序根>/store`，onedir 下程序根 = `_MEIPASS` 上一级）；`skilllocker.db` / `config.json` / `desktop_profile` / 日志仍在 `%LOCALAPPDATA%\\SkillLocker`。安装包不预置任何技能，所以仓库首启是空的 —— 但**不再需要用户点任何东西**：

0. **自动接管优先**：`bootstrap` 在空库首启先调 `auto_adopt_store()`。若各 agent 目录里已有 junction 汇聚到同一份真仓库（≥2 条链接 + ≥2 个带 `SKILL.md` 的子目录），直接改指并重扫，写 `events(kind='first-run-adopt')`；首屏「共有」就是这份仓库的内容。已有仓库的机器（升级安装 / 用过别的管理器）走这条，不会造第二份仓库。
1. 没有候选仓库时调 `seed_shared_store()`：仓库仍为空、首启未决策（`has_first_run_action`：建库 / 自动接管 / 手动接管任一留痕）、`seed_auto` 未关时，扫描各 agent 主目录，优先把 `shared(name) >= seed_min_agents`（默认 2）的技能按真身拷进仓库。**没有任何名字达到阈值时退化为「全部候选」**：单 agent（`single_agent`）或各 agent 技能完全不重叠（`fallback_all`）都全收，避免新用户首屏又是「共有 0」；同名但内容不同的组仍整组不选边。
2. 同名不同内容**整组不选边**；`extra_dirs` 实体、容器展开行不收；main 下可解析真身的 `foreign-link` **按真身收**（真身含 `SKILL.md` 且不在本仓库内）。
3. 拷贝用 `copy_tree_atomic`，源目录一律保留；分发开关全 off，**不改任何 agent 目录**。收完 `rescan` + `seed_meta`，独有行按内容哈希自动消失。
4. 预算：`seed_max_skills`（300）/ `seed_budget_s`（20s）/ `seed_max_bytes`（400 MB）；触顶停止但**仍打标记**，剩余数写进 `meta.seedNote`。全部候选都**硬失败**（目录读不了 / 拷不进去）时不写一次性标记，`seedNote` 给出真实原因，下次启动自动重试。
5. 幂等：`first-run-adopt` / `first-run-seed` / `adopt-store` 任一留痕都算首启已决策（`has_first_run_action`），之后手动清空仓库不会灌回。`seed_auto=false` 同时关掉自动接管与自动建库；`config.json` 显式写过 `store` 时也不自动改指。
6. 轻量快照：仅**全新数据库的第一次启动**只登记名字与来源（不统计文件数）；同名副本仍按内容哈希合并，不把同一个技能在各 agent 的副本算成多条。之后即使仓库仍为空也走完整扫描。`meta.uniqueLight` 只在该次为 `true`。

`list_boards()` 的 `data.firstRun`：

- 空仓库 → 整块面板：`candidate`（已有仓库汇聚点）、`adoptable`（含实体行 + 可解析真身的链接行）、`adoptableAgents`、`seedNote`、`seedBlocked`（`candidate` / `disabled` / `None`）、`autoAdoptError`（自动接管失败原因，成功时为 `None`）。
- 自动接管 / 建库后仍有可收独有行 → `strip: true`，前端渲染成单行提示条（「补充收编」），不占内容区。
- 正常有库且无可收行 → `None`。

人工出口与守卫：

- `adopt_store(con, cfg, path)`：只写 config.json + 重扫登记，不搬文件；当前仓库非空、目标落在 agent 目录、目标无 `SKILL.md` 等一律拒绝。自动接管复用同一守卫，失败时面板显示 `autoAdoptError`。
- `adopt_local_skills(con, cfg)`：显式批量收编，与单行 `promote_unique` 共用 `_adopt_groups`；同名不同内容收第一份、其余跳过并说明。只对开关开着的 agent、且只对主目录直属名字做 `do_sync` 对齐。
- `resolve_adopt_target()`：实体行取真身目录名；`foreign-link` 解析 junction 真身（断链 / 缺 `SKILL.md` / 真身在本仓库内 → 拒绝）。详情栏按钮据行形态显示「转为共有技能」或「按真身收进仓库」。

CLI：`python kit/skilllocker.py seed [--dry]`，`--dry` 只打印将收的名字与来源。

回归：`python kit/test_first_run.py`（33 项：自动接管 / 自动建库 / 单 agent 退化 / **多 agent 不重叠全收** / 同名不选边 / 候选阻断 / 幂等 / 预算 / seed_auto / extra·容器·foreign-link / 仓库落点 / 逻辑聚合 / **轻量同名合并** / 收编判定）+ `python kit/test_firstrun_ui.py`（49 项：三块引导面板 + 自动接管提示条 + 自动建库提示条 + 单 agent + **不重叠全收** + **按技能去重计数** + 逻辑技能组视图）。

---

## 2. 依赖与环境

| 项 | 实际值 | 说明 |
|---|---|---|
| Python | `D:\develop\python\3.12\python.exe`（3.12.6） | 只用标准库 + `pywebview` |
| pywebview | 6.2.1（`pip install pywebview`） | 唯一的第三方依赖，用来开窗口；缺失时启动弹窗提示（写日志、退出码 2），不再静默退出 |
| WebView2 Runtime | 156.0.4314.50（已在本机） | 由 Edge 提供；缺失则需要装 Evergreen Runtime |
| 屏幕 | 1920 × 1080，工作区 1920 × 1020，缩放 125%（120 DPI） | 影响窗口尺寸计算，见 §6 |
| 数据库 | `kit\skilllocker.db`（SQLite，WAL） | 与 CLI 共用同一个库 |

---

## 3. 启动方式

**双击启动（推荐）**：`D:\skilllocker\启动 skilllocker.bat`

- 用 `pythonw.exe` 启动，**没有控制台黑框**；窗口出现的第一帧就已经是 1200 × 760、位于工作区正中——不再先出现在别处再跳过去（建窗时即传入居中的 `x/y`，见 §6）。
- 启动失败不会静默：错误写进 `kit\launch.log`，并弹出系统提示框。
- 传参数：在命令行里跑 `"启动 skilllocker.bat" --debug` 即可透传给 `desktop.py`。
- **任务栏行为**：按钮图标取自 `kit\assets\skilllocker.ico`（不是 Python 的图标）；窗口在前台时点一下 = 最小化，再点 = 恢复；窗口被别的窗口挡住时第一下是切到前台（Windows 习惯），第二下才最小化。实现见 §6 的「窗口图标 / 最小化位」。
- **任务栏名称**：用 `D:\skilllocker\skilllocker.lnk`（或开始菜单里它的副本）启动时显示 `skilllocker`（Windows 按快捷方式文件名生成 AppUserModelID）；直接双击 `启动 skilllocker.bat` 或跑 `pythonw desktop.py` 时显示 `Python`（AUMID 就是 `pythonw.exe`，名称取自它自己的 FileDescription）。显式设 AUMID 反而会把快捷方式那条「skilllocker」弄丢，所以**不要**加显式 AUMID。

**命令行启动**：

```powershell
cd D:\skilllocker\kit
D:\develop\python\3.12\python.exe desktop.py            # 正常启动（带控制台，便于看日志）
D:\develop\python\3.12\python.exe desktop.py --debug    # 打开 WebView2 调试面板
D:\develop\python\3.12\python.exe desktop.py --selftest # 离屏自检，跑完自动退出（见 §7）
```

也可以直接双击 `ui\index.html` 用浏览器打开——那是**原型模式**（`data.js` 快照 + localStorage），不碰数据库，适合只改样式时预览。

两种模式靠 URL 上的 `?desktop=1` 区分（桌面壳会自动加上），前端据此决定数据来源。

`启动 skilllocker.bat` 里写死了解释器路径 `D:\develop\python\3.12\pythonw.exe`（不依赖 PATH）；换机器/换 Python 目录时改这一行即可。找不到解释器或缺少 `ui\index.html`、`kit\desktop.py` 时会直接弹框说明缺什么。

---

## 4. 文件分工

| 文件 | 职责 | 关键点 |
|---|---|---|
| `kit\desktop.py` | 窗口外壳 + `js_api` | 只做「开窗（建窗即定位）/ 转发调用」，业务全在 `skilllocker.py` |
| `kit\skilllocker.py` | 扫描、分发、元数据、看板快照 | 本版新增 `skill_meta` 表与 `list_boards / get_meta / set_meta / ...` |
| `ui\bridge.js` | 前端唯一与 Python 通信的层 | `?desktop=1` 才等 `pywebviewready`，否则立即返回 `null` |
| `ui\app.js` | 渲染与交互 | `MODE='db'` 时写库，`'local'` 时写 localStorage |
| `ui\styles.css` | 样式 | `body.desktop` 下窗口铺满、去圆角与投影 |
| `ui\data.js` | 原型快照 | 桌面模式不使用，但保留以便对比与回退 |

---

## 5. API 契约

所有方法都返回 `{ok:true, ...}` 或 `{ok:false, error}`；前端 `bridge.js` 把 `ok:false` 统一转成 `Promise.reject`，由 `app.js` 弹 toast。

| 方法 | 入参 | 返回 | 前端触发点 |
|---|---|---|---|
| `bootstrap()` | — | `{ok, data: 看板快照, meta: 全部元数据}` | 启动首屏：先 `sync_tools_from_adapters` 首屏探测 agent，skills 表为空（全新库）时自动 `rescan` 登记中央仓库；仍为空则先 `auto_adopt_store()` 接管已有仓库、再 `seed_shared_store()` 建库，最后 `migrate_folders + seed_meta` 播种、`prune_meta` 清理；失败返回 `ok:false` → 前端启动错误页（绝不退回演示快照）。`data.agents[].external[]` 是 agent 自己读取的外部技能目录统计（方案 A 只检测展示）；`data.firstRun` 在空库或自动接管/建库后仍有可收行时非空（首启引导的数据源，见 §1.3） |
| `save_meta(scope, key, payload)` | `scope`: `shared`/`unique`；`payload`: `{tool, display, note, cats}` | `{ok, row}` | 改名 / 备注 / 分类修改（即时调用，无去抖） |
| `clear_meta(scope, key)` | 同上 | `{ok}` | 详情栏「还原」 |
| `delete_skill(scope, key)` | `scope`: `shared`/`unique` | `{ok, data, meta, action}` | 详情栏「彻底删除 skill / 删除本地副本」；`shared` 删中央实体 + 所有链接，`unique` 只删该 agent 的副本 |
| `reseed_meta()` | — | `{ok, data, meta}` | 设置 →「清空我的编辑」 |
| `diff(scope, key)` | — | `{ok, conflict: {skill, central, local, diff}}` | 「查看文件差异」 |
| `rescan()` | — | `{ok, data, meta, scanned}` | 设置 →「重新探测本机 agent」 |
| `export_json(name, payload)` | 文件名 + 前端组装内容 | `{ok, path}`（取消则 `path=''`） | 设置 →「导出 JSON」 |
| `copy_path(text)` | 文本（≤2000 字） | `{ok, text}` | 详情栏「复制路径」 |
| `open_dir(scope, key)` | `scope`: `shared`/`unique`/`agent`，只接受 scope+key | `{ok, path}` | 详情栏「打开目录」（路径由后端现算，防任意路径） |
| `set_agent(key, on)` | agent key + 开关目标值 | `{ok, data, meta, action}` | 智能体看板的开关；开=补链接，关=撤链接 |
| `promote_unique(key)` | `tool/name` | `{ok, data, meta, action}` | 独有行详情栏「转为共有」（单个实体拷进中央仓库 + 迁移改名备注） |
| `adopt_store(path)` | 候选仓库绝对路径 | `{ok, data, meta, action:{store, previous, skills}}` | 首启引导「接管该仓库」；守卫见 §1.3，只写 config.json + 重扫 |
| `adopt_local_skills()` | — | `{ok, data, meta, action:{adopted, skipped, relinked, linked, files, bytes, metaMoved}}` | 首启引导「一键收编」（只收 `src=main` 实体行） |
| `win_min / win_max / win_close` | — | `{ok}` | 标题栏三键 |
| `win_bounds()` | — | `{ok, bounds:{x,y,w,h}, maximized}` | 标题栏拖动 / 四边四角 grip 按下时取基准外框（CSS 像素） |
| `win_set_bounds(x, y, w, h)` | CSS 像素矩形 | `{ok, bounds}` | 标题栏拖动与 grip 拖动中按位移算出的目标矩形，后端 `SetWindowPos` 落窗 |
| `win_restore()` | — | `{ok, bounds, maximized:false}` | 最大化态拖标题栏越过阈值（>6px）后调用：先还原，再按还原后的外框继续拖；单击标题栏不还原（缩小只认双击与真实拖动） |
| `log_ui(kind, detail)` | 事件名 + JSON 字符串 | `{ok}` | 前端拖动诊断落盘 `kit/ui-drag.log`（加锁追加，超 48KB 只留最近 200 行） |

`set_agent` 的 `action` 字段：开 → `{on:true, changed, conflicts, skipped}`；关 → `{on:false, removed, kept}`。`kept` 是撤链接后仍留在 agent 目录里的冲突副本，界面会如实提示。

约束：`display_name ≤ 48`、`note ≤ 400`、`categories ≤ 12` 项，超长由 `set_meta` 截断。

---

## 6. 窗口与 DPI

- 默认外框 **1200 × 760**（CSS 像素），最小 1180 × 720（`min_size` 同样以 CSS 像素传入）。
- 建窗即定位：`build_window()` 先按工作区算居中左上角（`_centered_rect()`：`(work_w - w) / 2`、`(work_h - h) / 2`），把 `x/y` 直接传给 `create_window`，**首帧就在目标位置**，不再移动窗口。
- 窗口图标 / 最小化位（`on_start()` 负责，见「启动时序」）：`on_start` 先 `window.events.shown.wait(10)` 等窗口真正出现，再轮询 `_hwnd_by_title(visible_only=False)` 拿句柄，然后 `_refresh_window_chrome()` 一次做两件事——按系统 DPI 从多尺寸 ico 里取 16/20/24… 中最合适的一帧 `WM_SETICON`（`ICON_SMALL` / `ICON_BIG`），以及补回 `WS_MINIMIZEBOX`。最后仍兜底 `pythonw` 的最小化态（`IsIconic` → `SW_RESTORE`）。
- 启动时序（这是「图标不显示 / 点了不最小化」的老根因）：`webview.start(func)` **先起 func 线程、后执行 `guilib.create_window()`**，所以 `on_start` 里立刻查句柄只会拿到 `None`。必须等 `shown` 事件，否则图标与最小化位一次都不会生效。
- 最小化位会被 WinForms 抹掉：`FormBorderStyle=None` 的窗口本身没有 `WS_MINIMIZEBOX`（任务栏按钮点了只会切前台），手工 `SetWindowLong(GWL_STYLE)` 补上后，WinForms 在最小化/最大化/还原时还会重算样式把这一位去掉。因此 `on_start` 往 `window.events.minimized / maximized / restored` 挂了 `_keep_window_chrome()`（分 0 / 0.15 / 0.5 秒重试三次），状态一变就补回。
- `WM_SETICON` 的坑：**不要**给 `ICON_SMALL2`（slot 2）设小图。Win11 上实测这一发会把 `ICON_BIG` 一起改成那张小图（`WM_GETICON(1)` 回读到小图句柄），Alt-Tab 的大图标立刻变糊；而 slot 2 不设本来就会回退到 `ICON_SMALL`。现在只设 slot 0 / 1。
- 本机实测（125%，为 DPI 感知进程）：窗口 `WM_GETICON` 三档分别是「20px 帧 / 40px 帧 / 同小图」；任务栏按钮图标与 `skilllocker.ico` 的帧逐像素一致（截图核对过），窗口样式 `0x16030000` 含 `WS_MINIMIZEBOX`；点任务栏按钮两次 = 最小化 → 恢复，且 `WS_MINIMIZEBOX` 一直在。
- 尺寸补偿：WinForms 会在 `FormBorderStyle=None` 时裁掉标题栏与边框，`create_window` 的宽高是「裁之前」的 Size。`_nc_css()` 用 `SM_CXSIZEFRAME / SM_CYCAPTION / SM_CXPADDEDBORDER` 算出差额（本机 125%：左右 18、上下 47 物理像素），向上取整后加回请求尺寸，首帧外框才正好等于目标值。
- 本机实测：工作区 1920 × 1020（物理），缩放 1.25，外框 **1500 × 950 物理 = 1200 × 760 CSS**，左上角 (210, 35) 物理 = (168, 28) CSS，中心偏移 0 / 0；启动轨迹只有一条记录，没有二次跳位。
- 拖动：**整条标题栏**可拖（`.brand`、标题栏空白都可），由前端 `pointerdown` 自己实现，不依赖 pywebview 的 `drag-region`；输入框、按钮、窗口三键、`LOCAL` 胶囊不参与拖动。双击标题栏空白 = 最大化 / 还原（Windows 习惯），双击这些交互控件不会触发。
  > 窗口最上方约 5px（四角 13px）是缩放手柄（`g-n` 等），按在那里只会缩放不会移动；`beginResize()` 现在会写一条 `resize.down {edge, x, y}` 到 `kit/ui-drag.log`，避免这种「按在热区上」被误判成「事件没收到」。标题栏 `touch-action:none`，触控板按住时不会被当成滚动手势而 `pointercancel`。
  > 拖动链路全程写 `kit/ui-drag.log`（`boot` / `move.down` / `move.begin` / `move.first` / `move.end` / `move.fail` / `move.skip` / `move.nobounds` / `move.restore` / `resize.down`），真实手拖出问题先看这个文件：没有 `boot` = 跑的还是旧进程/旧代码；有 `boot` 没有 `move.down` = 按下点没落到 `.titlebar`；`move.skip why=control` = 命中交互控件；`move.fail` = 后端拒绝，看错误文本。
- 拖动的健壮性：`beginMove()` 先记下按下点，`pointermove` **始终**记录最新光标位置；基准外框（`win_bounds()` 是跨线程异步）即使晚于松手才回来，也会在回来时补发一次终值，不会丢掉整段位移。拖动中 `body.moving` 禁止选中文字。
- 缩放：无边框窗口自己实现 8 个贴边手柄（`#app-window > .grips > [data-resize]`，仅 `body.desktop` 显示，5px 边 / 13px 角）。拖动逻辑在前端：`pointerdown` 调 `win_bounds()` 取基准矩形，`pointermove` 用 `screenX/screenY` 的**绝对位移**算目标矩形（`≥1180×720`），节流后调 `win_set_bounds()`，由后端 `SetWindowPos(SWP_NOZORDER|SWP_NOACTIVATE)` 落窗；`pointerup` 补发终值。
  > 曾试过把按下事件交给系统原生缩放（`ReleaseCapture()` + `SendMessage(WM_NCLBUTTONDOWN, HT*)`），但 WebView2 持有鼠标捕获，`ReleaseCapture` 只对调用线程生效（pywebview 的 API 跑在别的线程），实测拖完外框 delta 恒为 0，故改为自算矩形。
  > 另有一处坑：**不能**给全局 `ctypes.windll.user32.SetWindowPos` 设 `argtypes`——pywebview 的 `BrowserForm.move()` 会传 `None` 当宽高，全局声明会让它抛 `ArgumentError`，标题栏拖动直接失效。`desktop.py` 因此改用独立的 `ctypes.WinDLL("user32")` 实例 `_spos` 承载签名。
- 缩放坐标系：前端 `screenX/innerWidth` 是 CSS 像素，后端 `GetWindowRect` 是物理像素，`window_bounds()` / `set_window_bounds()` 统一按系统缩放（1.25）折算，读写两侧才闭合。最大化态（`IsZoomed`）下：**拖标题栏越过 6px 阈值才 `win_restore()` 还原、再按还原后的外框继续拖**（Windows 习惯；单击 / 轻微抖动不还原，缩小只认双击标题栏），`win_set_bounds()` 在最大化态直接返回 `False` 不落窗；grip 缩放遇到最大化则直接放弃（要先还原再改尺寸）。程序化的最大化 / 还原（`win_max` / `win_restore`）会用 `DwmSetWindowAttribute(DWMWA_TRANSITIONS_FORCEDISABLED)` 临时关掉 DWM 缩放动画——WebView2 在动画期间内容不跟帧，瞬移到目标尺寸才不会闪；走的是 Win32 `ShowWindow` 而不是 pywebview 同名方法（后者带 `@_shown_call`，从未 Shown 的隐藏窗口调用是静默 no-op，自检里最大化会永远不生效）。
- 进程在建窗前声明 DPI 感知（`SetProcessDpiAwareness(1)`），否则 Windows 会把坐标再虚拟化一次，导致窗口跑到屏幕外。
- 缓存：`desktop.py` 以 `ui` 五个资源文件的 mtime 合成 `?v=` 挂在 URL 上；`index.html` 再把同一个版本号带到 `styles.css` / `bridge.js` / `data.js` / `app.js`，避免 WebView2 继续使用旧的缓存脚本。


---

## 7. 自检与验收

### 7.1 桌面端自检（`--selftest`，离屏窗口，当前 111 项）

**期望值一律现算，不写死库内数据**：共有/独有行数、独有行键集合、序号范围、已编辑条数、已连接 agent 数、store 路径、**分类夹数与未归档数（`boards.folders` / `skill_meta.folder_id`）** 全部取自 `skilllocker.list_boards()` / `get_meta()` / `load_config()`。库内增删 skill、冲突收口之后重跑自检仍然成立。

覆盖：模式=db、0 条 JS 错误、共有行数与侧栏计数、独有逻辑组行数 / `g#` 组键集合与后端 `unique_groups` 一致（含 extra / 容器展开 / 外部链接来源，见 §1.2）、独有序号连续 1..N、已编辑条数与 meta 一致（只有显示名 / 备注算编辑，归属夹不算）、**分类夹数、未归档数**、已连接 agent 数、路径来自 `config.json`、选中 `pdf`、列表上方筛选行已移除、**序号列（首行=1、共有序号连续 1..N、第 10 行两位数仍 26×26 不溢出）**、详情栏删除按钮存在、**分类文件夹 11 项**（网格视图卡片数=后端 folders 数、`#grid-new-folder` 虚卡存在、点 `data-folder-card` 进夹后标题=夹名且行数=后端夹内计数、面包屑返回根后 `.folder-grid` 重新出现、临时建夹+归入 pdf 后卡片数 +1、`folder_delete` 后卡片数复原且夹内行 `folder_id` 置空 / moved 计数正确、切列表视图 `.row` 数=sh_total、`set_meta(folder)` 落库与 `''` 复原、全新库上迁移回填一次且二次迁移零改动——自检用的临时库已在 `skilllocker.db` 升级时完成真实迁移）、**差异入口按冲突数据分支**（`boards.conflict` 来自独有板的同名覆盖行；共有板差异分支只在 `find_conflict_tool()` 现查到实体冲突时才测，否则核对「共有板无差异按钮」；独有板按 `scope=unique` 核对表头 `CONFLICT · tool / skill` 与弹窗出现）、改名写库（**目标选一条当前没有人工改名/备注的共有 skill，结束后只还原显示名与时间戳——整行 insert-or-replace 会把 folder_id 抹掉**）、已编辑计数 ±1、搜索（只校验「命中 pdf + 列表收窄」，不写死命中条数）与清筛选、非法删除被拒、从 UI 详情栏点击「彻底删除」并确认（临时 skill 的磁盘目录与列表行都消失、自动选中下一条）、设置弹窗文案、深色主题、智能体看板（10 行 / 侧栏 10 / tab 已连接数现算 / 4 个状态筛 / 每行开关 / 未安装开关禁用 / **序号列（首行=1、连续 1..N、≥10 行时第 10 行两位数仍 26×26 不溢出，不足 10 行跳过该项）**；没有未安装行时跳过「未安装调 API 被拒」）、**开关方向回归（点「开」着的开关必须先弹「关闭 … 分发？」确认框、一次点击只弹一个框、取消后开关回到开）**、**外部来源（agent 自己安装的目录如 `~/.agents/skills`：行标题出现「外部 N」标记且 N 与后端 `external[0].count` 一致、详情栏出现「外部来源」行、外部行详情有「卸载」按钮且无「删除副本」按钮；本机没有外部同名实体时整组跳过）**、**原生窗口（5 项：`WS_MINIMIZEBOX` 在、窗口小图标=ico 帧、窗口大图标=ico 帧、`ICON_SMALL2` 回退到小图标、最小化位被抹掉后能自动补回）**、8 个缩放手柄、窗口外框 API 可读（本机回读 1200 × 760 @ 168,28）、**标题栏拖动窗口（合成 pointerdown/move/up，外框应位移 dx=60,dy=40）**、启动尺寸 1180~1200 × 720~760、底部设置按钮与版本号完整可见、详情栏右边界 ≤ 窗口宽、1180 宽无横向溢出、外框写入 `1240×780` 回读一致、写入 `900×500` 被兜底成 `1180×720`、**收编入口（实体行有「转为共有」、foreign-link 行有「按真身收进仓库」；同名覆盖不出现）**、**导入 skill 全链路 12 项（SPEC-skill-import.md §7.1 之 6-10：顶栏入口开弹窗且步骤条=选择来源、tempfile 源走真桥预检后 imp-start 可用、步骤条=预检命名且报告卡存在、键入现存名后按钮 disabled+冲突面板、改回合法名恢复可用、执行态 X 不渲染且 Esc 无效、真库落库到完成页、`#store-count`=后端现算值、搜索态下 `.file-card.is-new` 高亮、完成页位置指向新 skill、skills+skill_meta.folder_id 落库、完成态 Esc 可关；结束清临时 skill）**、**在线检索 / 获取 5 项（SPEC-skill-import.md §10.1：全程假网络——patch 门面 `skilllocker.online_search` 与 `_list_repo_files`/`_net_bytes`，桥与 UI 走真实链路；桥直调 `online_fetch` 落系统临时目录且 `listing=github`、结果走标准 `import_preflight` 可过、`open_url` 白名单外域名拒绝（不真开浏览器）、在线 tab 检索假结果渲染成 `.sr-row`、点「获取」→ 预检报告回填 `#imp-name`；结束恢复补丁 + 清临时目录，不写中央仓库）**；**窗口态测试放最后**（最大化拖动越过阈值先还原再位移、**最大化单击不还原**）——真实最大化过一次后，隐藏窗口的 WebView2 布局会卡在最大化尺寸不再跟随 OS 外框（`innerWidth` 恒 1536），所以这两项排在所有 innerWidth / 布局断言之后，末轮 boot 重载页面拿回干净布局；程序化最大化 / 还原走 `ShowWindow`（pywebview 同名方法带 `@_shown_call`，隐藏窗调用是静默 no-op——不加这一层，自检里最大化永远不生效，最大化拖动会靠普通拖动假通过）并临时禁用 DWM 缩放动画（WebView2 动画期间内容不跟帧，闪烁的根源）。

结果：**111/111 PASS，`ERRORS: []`**（2026-11-01：92 项 + 转共有 2 项 + 导入 12 项 + 在线 5 项；本机当前库 52 共有 / 10 agents 行（7 已连接）/ 7 个分类文件夹，全部现算不写死，具体计数随库变化）。原生窗口那 5 项同时也在一台真实可见窗口（快捷方式启动的实例）上单独核对过：`WM_GETICON` 三档 = 20px 帧 / 40px 帧 / 同小图，样式 `0x16030000`，点任务栏按钮 = 最小化 ↔ 恢复，`WS_MINIMIZEBOX` 全程未丢。

> 旧版自检写死了 `独有 2 行`、`workbuddy/pdf` 的 19 行差异、`分类 7`、`agent 7`、`搜索 pdf 行数=1`，`workbuddy/pdf` 冲突收口成 junction 后这些项必 FAIL；现在已按上面的「现算」方式改写。

删除能力另用**临时数据库 + 临时目录**做过一次性集成验证（脚本属临时探针，已随 `.scratch` 清理）：`remove_skill` 删中央实体 + 2 处 agent 链接 + `skill_meta` 行；`remove_unique` 只删 agent 副本、中央同名实体不动；`..` / `../store` / `a/b` 等非法名全部被拒。

开关本身（建/删 junction）另用**临时数据库 + 临时目录**做了集成验证：开 → 中央仓库有多少个实体就补多少个 junction；关 → 全部撤掉、目录清空、中央实体一个未动；对未安装 agent 调 `set_agent` 返回 `ok:false`。

### 7.2 原型模式回归（Playwright + 系统 Edge，`file://`，13 项）

结果：**13/13 PASS**（模式=local、快照 51/2 行、编辑器写 localStorage、刷新保留、快照差异 19 行、清理后回到 0 条、0 条 JS 错误）。注意原型读的是 `ui\data.js` 冻结快照（51 + 2），桌面端读的是真实仓库（当前 51 + 152），两者行数不同属正常。

**分类文件夹功能的原型回归（同一方式，45 项全过）**：网格默认渲染 / 卡片数 / 新建虚卡 / 检查器单选 chip、进夹标题与 5 列窄行、面包屑返回、列表 ⇄ 网格切换与行内文件夹 chip、chip 跳转进夹、⋯ 菜单（移动 toast、菜单关闭）、侧栏未归档项与标题、Esc 退出文件夹、新建弹窗→创建卡片数 +1、⋯ 重命名生效、删除确认文案含 N、删除后复原、刷新后视图偏好保持（prefs）、独有看板无视图分段 / 无文件夹导航、智能体侧栏=状态、0 条 JS 错误；**旧 localStorage 一次性迁移**（cats 多选 + customCats → 7 预置夹 + 自建夹、cats[0] 命中才回填、显示名保留、migrated 标记置位）；**三路拖拽**（行→面包屑移出、文件卡→文件夹卡、行→侧栏夹）与检查器 chip 移动后行即时离开原夹、未归档徽标 +1。

字号调整后另做了一轮布局复查（同样 Playwright + 系统 Edge）：`body` 13px、标题栏 46、顶栏 66、状态栏 30、行 60、检查器名输入 14px / 40 高；1200 与 1180 宽均无横向溢出（`scrollWidth == innerWidth`），导航 / 看板 tab / agent chip 无纵向裁切，设置弹窗 560 宽，0 条 JS 错误。

### 7.3 截图

`D:\skilllocker\.screenshots\desktop-shared.png`（共有看板）、`desktop-unique.png`（独有看板）、`desktop-agents.png`（智能体看板）、`desktop-diff.png`（差异弹窗）；同名 `-preview.png` 是 1200 宽缩略图。截图按物理像素 `GetWindowRect` + `ImageGrab` 抓取，与本机 1500 × 950 窗口一致。

放回可重复执行的方式：`--selftest` 自带断言；截图脚本属一次性工具，已从 `kit` 目录移除，需要时可重写（要点：`SetProcessDPIAware` → 按物理像素 `GetWindowRect` → 抓屏）。

### 7.4 启动脚本实测

`启动 skilllocker.bat` 双击等价流程实测：窗口**第一帧**即为 **1500 × 950 物理像素（1200 × 760 CSS）、(168, 28) CSS（物理 (210, 35)）、中心偏移 0 / 0**，启动期间外框轨迹只有一条记录（没有「先出现在别处再跳到中间」的闪烁），随后 30 秒逐秒轮询窗口始终存活、位置尺寸稳定；画面内容为真实数据库的共有看板（侧栏 49 / 2 / 7、详情栏完整、底部状态栏可见）。

### 7.5 拖拽缩放手测（真实鼠标 + 合成指针事件）

窗口 1200 × 760、左上 (168, 28) 起，外框读数（CSS 像素）：

| 操作 | 结果 |
|---|---|
| 拖 `.brand` 中心 +80 / +50 | d=(80, 50)，尺寸保持 1200 × 760 |
| 拖标题栏空白 +70 / +45 | d=(70, 45) |
| 拖 `.local-pill` / 搜索框 | d=(0, 0)（交互控件不参与拖动） |
| 双击标题栏空白 | `IsZoomed=True`，外框 1536 × 864（frameless 最大化覆盖整屏，属既有行为） |
| 最大化后单击标题栏 | 不还原、外框不动（缩小只认双击与拖动越过阈值；无缩放动画闪动） |
| 右边缘向右 120 | 1320 × 760，x 不变（合成事件与真实鼠标各测一次，均 `delta=(120,0)`） |
| 左边缘向右 100 | x 168→268，宽 1320→1220 |
| 右下角 +80 / +60 | 1300 × 820（真实鼠标 +90 / +70 → 1290 × 830） |
| 右边缘向左 600（越界） | 夹到最小 **1180 × 820** |
| 最大化后拖标题栏 | 位移越过 6px 阈值后先还原成 1200 × 760，再按还原后的外框继续位移（`win_restore()` → `moveFrom()`） |
| 拖完松开 | `body.moving` / `body.resizing` 均已移除，选中行仍为 `pdf`，`__SK_ERRORS` 为空 |

标题栏拖动是在「每 10ms 轮询外框」的压力下验证的：即使 `win_bounds()` 的基准矩形晚于鼠标松手才返回，补发终值的逻辑仍能让外框精确位移，不会出现「拖了但没动」。

---

## 8. 边界与已知问题

1. **未做**：托盘、开机自启、单实例、安装包、自动更新；`--selftest` 会在隐藏窗口跑（Chromium 会节流 `setTimeout`，所以桌面端写库刻意不走去抖）。
2. **独有看板的外部行只展示不管理**：`extra` / `foreign-link` / 容器展开来源的行一律 `deletable=false`——UI 不渲染删除按钮，直调 `delete_skill` 也被后端拒绝。这些真身由 agent（ClawHub / codex / Tabbit 等）自己装、自己升级，本工具删了会被装回来或直接弄坏 agent。要把某个外部 skill 收进中央仓库，用 CLI `import <目录>`。
3. **`workbuddy/pdf` 冲突已收口成 junction**：`store\pdf` 现在是 WorkBuddy 1.0.1 的那份内容（含 `_skillhub_meta.json`），`C:\Users\LENOVO\.workbuddy\skills\pdf` 是指回中央的 junction，所以「独有」里不再有它。改动前的中央目录整份备份在 `D:\skilllocker\.scratch\pdf-backup-20261030-222737\`（`central` / `local` 两份，尚未清理）。`suppressed` 表已空，`skilllocker.py verify` 不应再报这条冲突。
4. **设计稿 1440 × 900 只用于原型**：桌面窗口默认 1200 × 760，`≤1340` 断点会收起看板副标题与 tab 小字，列表与详情栏仍完整；最小 1180 × 720 已实测无横向溢出。
5. **`zoomable=False`、`easy_drag=False`**：缩放内容与 pywebview 自带的拖动区都被刻意关掉。拖动改由前端实现——整条标题栏（含 `.brand`、空白处）`pointerdown` 记起点，`pointermove` 按 `screenX/screenY` 绝对位移调 `win_set_bounds`，`pointerup` 补发终值；搜索框 / 按钮 / LOCAL 胶囊 / 窗口三键不触发。双击标题栏空白切换最大化。改变窗口大小走 8 个 grip。6. **数据库是单文件**：界面开着时另跑 `skilllocker.py scan/remove` 也能工作（WAL），但改完建议在界面里点「重新探测」刷新。
7. **`pythonw` 下窗口可能以最小化状态出现**：Windows 建窗时给的是 (-32000, -32000)，`on_start()` 先补图标/最小化位，再兜底——检测 `IsIconic` 后 `ShowWindow(SW_RESTORE)`，位置与尺寸在建窗时已正确，不再二次摆位。若手改这段代码，注意别把还原步骤删掉，否则窗口会"看不见但其实在跑"。
8. **同前缀的 WebView2 工具窗**：枚举窗口时会看到一个 `Chrome_WidgetWin_1`（标题带 `-联想浏览器`）。`_hwnd_by_title()` 只认 `WindowsForms10.*` 主窗口，改选择逻辑时别只看标题。
9. **缩放是自算矩形，不是系统原生缩放**：8 个 grip 只在 `body.desktop` 下存在；拖动依赖 `setPointerCapture` 在窗口外继续收 `pointermove`，光标离开窗口太远或系统抢走捕获时会提前结束。最大化态（`IsZoomed`）下：grip 缩放直接放弃（需先还原）；标题栏拖动越过 6px 阈值才 `win_restore()` 还原再位移，单击不还原。坐标折算按**系统级 DPI**（`GetDpiForSystem`）写死，跨不同缩放比例的副屏拖动未验证。

---

## 9. 排障

| 现象 | 原因 | 处理 |
|---|---|---|
| 界面显示「后端启动失败」错误页（含错误详情与重试按钮） | 后端 `bootstrap()` 抛错：常见为 store 指向不存在的盘符 / 目录、adapters.json 损坏且工具表为空；或桌面壳内 pywebview 注入超时（WebView2 冷启动过慢，桥未接上；注入窗口 20s，超时先自动重载一次，仍失败才出错误页） | 看错误详情与 `kit\skilllocker.log`；修好 `config.json` 的 `store` 或 `kit\adapters.json` 后点【重试】。这是明确失败，界面**不会**拿内置演示快照冒充真实数据 |
| 界面显示「LOCAL」胶囊、编辑没写进数据库（仅 `file://` 直开才会这样） | `bridge.js` 没等到 `window.pywebview`；桌面壳内注入超时已改为启动错误页，不再进只读原型 | 确认是用 `desktop.py` 打开（URL 必须带 `?desktop=1`）；`--debug` 看控制台错误 |
| 窗口跑到屏幕外 / 右侧被切 | 进程未声明 DPI 感知，或尺寸没按工作区收口 | 确认 `desktop.py` 的 `_make_dpi_aware()`、`_centered_rect()`、`_nc_css()` 未被改动；建窗即居中，`on_start()` 只兜底最小化还原 |
| 改了 `app.js` / `styles.css`，界面还是旧样子 | WebView2 缓存了 `file://` 资源 | 确认 `desktop.py` 的 `_asset_version()` 仍在 URL 上带 `&v=`，且 `index.html` 用版本号加载资源；必要时重启进程 |
| 白屏 | 找不到 `ui\index.html` | 启动时会校验并报「找不到界面文件」；确认 `ui` 与 `kit` 同级 |
| 改名后「已编辑」不刷新 | 旧版本用了 300ms 去抖 | 本版已改为即时 `flushMeta()` + 同步 `renderChrome()` |
| 点「开」着的分发开关，却提示「已连接 … 建立/刷新 0 处链接」（关不掉） | 旧实现用 `click` + `!box.checked`：`click` 冒到 `document` 时 `checked` 已翻转（一次按下还有两个 `click`，`label` 会把点击转发给 `input`） | 本版开关只认 `change`，按 `box.checked` 作为目标状态走 `requestAgent()`；`click` 只拦下、不动作。改回 `click` 前请先跑 `--selftest` 的「开关点开→先问关闭」三项 |
| 标题栏拖不动 | 见 §6：按在顶部 5px 缩放热区（`resize.down` 日志）／界面跑的是旧进程（`ui-drag.log` 无 `boot`）／`SetWindowPos` 签名污染了全局 `windll` | 先看 `kit/ui-drag.log` 最后几行，再按对应分支处理 |
| 任务栏图标还是 Python 图标 / Alt-Tab 图标糊 | ① `on_start()` 早于 `guilib.create_window()`，直接查句柄拿到 `None`，`_apply_window_icon()` 从未执行；② 给 `ICON_SMALL2` 设小图会把 `ICON_BIG` 一起改掉 | 确认 `on_start()` 里先 `window.events.shown.wait(10)` 再轮询句柄；`WM_GETICON(0/1)` 应分别是 20px 帧与 40px 帧（125% 缩放）。别把 `WM_SETICON(2, small)` 加回来（见 §6） |
| 点任务栏按钮只切前台、不最小化 | 无边框 WinForms 窗口没有 `WS_MINIMIZEBOX` | 看 `GetWindowLong(GWL_STYLE)` 是否有 `0x00020000`；`_ensure_minimize_box()` 会补位，`minimized/maximized/restored` 事件上挂了 `_keep_window_chrome()` 兜底重补 |
| 弹「启动失败：TypeError: create_window() got an unexpected keyword argument 'icon'」 | 旧版 `main()` 把 `icon=ICON` 传给 `webview.start()`，有的 pywebview 版本会把该参数透传给 `create_window()` | 本版已不再传 `icon`（Windows 图标由 `on_start()` → `_apply_window_icon()` 走 `WM_SETICON`）；不要把 `icon` 加回 `webview.start()` |
| 打开/重连 agent 时黑窗口连闪（关掉→重连最明显） | `make_junction()` 每次都用 `cmd /c mklink` 建链接；`pythonw` 无控制台，每个控制台子进程都会新分配一个黑窗口，49 个 skill 就是 49 次闪窗 | `_quiet_kwargs()` 已给子进程加 `CREATE_NO_WINDOW` + `SW_HIDE`；实测子进程 `GetConsoleWindow()` 由 `1051536` 变 `0`。另：`cmd` 输出是 ANSI，**不能**用 `text=True`（会 `UnicodeDecodeError`），统一走 `_decode()` |
| 最大化态单击标题栏，窗口闪动一下缩小成原尺寸 | 旧实现 `winBounds()` 一回来发现 `maximized` 就立刻 `winRestore()`——根本没等用户真的拖动，单击也触发还原；还原又带着 DWM 缩放动画，WebView2 动画期间内容不跟帧，看起来就是「闪动 + 震荡」 | 前端把还原挂起（`moveState.zmax`），位移越过 6px 阈值才 `win_restore()`；单击只收尾不动作，缩小只认双击标题栏（`win_max` 切换）。后端 `win_max` / `win_restore` 程序化切换窗口态时 `DwmSetWindowAttribute(DWMWA_TRANSITIONS_FORCEDISABLED)` 临时关掉缩放动画，瞬移到目标尺寸；并改走 `ShowWindow`（pywebview 同名方法在隐藏窗口上是静默 no-op）。回归项：`--selftest` 的「最大化单击不还原」 |
| 自检卡住 | 异常抛出后窗口未销毁 | `selftest()` 已用 `try/finally` 保证打印并 `destroy()` |
| 导入（在线获取后尤其高发）偶发失败页「PermissionError: [WinError 5] 拒绝访问」指向 `.skpart-* -> store\<名>` | `os.replace` 改名时，刚 copytree 落盘的文件被杀软 / 索引服务短暂握着句柄（Windows 目录树内有任何打开句柄 rename 即被拒）；网络新下载内容首次落盘要全量云查杀，几秒内导入几乎必撞。本地导入的旧文件早有扫描缓存，所以少见 | `_replace_dir()` 已按 `RENAME_BACKOFF`（0.15s→3s 共 5 次 ≈6s）退避重试，瞬时句柄自动通过；重试耗尽才进失败页（临时目录已回滚，可点「重试」）。单测用真实句柄复现（test_unit「rename 重试」3 项） |
| 关闭某个 agent 后（skilllocker 显示「已关闭」、目录也空了），**新开会话仍能调用它的 skill** | 该 agent 还有第二条读取路径：Trae CN 除 `~/.trae-cn/skills` 外还读 `~/.agents/skills`（ClawHub / GitHub 安装的真身，见 `~/.agents/.skill-lock.json`）。本工具只管 `skills_dir`，删链接不影响那里 | 看智能体行是否标了「外部 N」、详情栏是否列出 `.agents/skills` 与「N 个内容一致」——有就是如实检测，不是开关失效。要真正失效可以在独有看板对那条 skill 点「卸载」（v0.2 起支持，`uninstall_unique` 永久删除该目录或只摘 foreign-link 链接；agent 更新时可能重新安装） |

---

## 10. 分发与安装（打包清单）

把本目录 zip 发给别人 / 拷到别的机器时，按此清单打包。**store 默认跟随安装目录**：
`config.json` 缺失时中央仓库解析为 `<安装目录>\store`（`kit\skilllocker_base.py` 的
`DEFAULT_STORE` 推导，不写死盘符）；想放到别处就带上自己的 `config.json`
（`{"store": "<路径>"}`），它不入库、随安装分发。

| 处理 | 内容 |
|---|---|
| 必含 | `kit\*.py`、`kit\adapters.json`、`kit\assets\`、`ui\`（index.html / app.js / bridge.js / data.js / styles.css）、`启动 skilllocker.bat` |
| 必排除 | `kit\config.json`、`kit\skilllocker.db*`、`kit\skilllocker.log*`、`kit\desktop_profile\`、`kit\ui-drag.log`、`kit\launch.log`、`store\`、`__pycache__\`（以上均已在 `.gitignore`） |
| 环境要求 | Windows 10+；Python 3.12（`pythonw.exe` 可用）且已装 pywebview（`pip install pywebview`）+ Edge WebView2 Runtime |

首次启动行为（干净机器：无 `skilllocker.db`、无 `config.json`）：`bootstrap()` 自动探测
本机已装 agent（状态「已关闭」，开关 off），空仓库时自动把共有技能收进 `<安装目录>\store`
（见 §1.3）；打开任一 agent 开关即在其 skills 目录建指向 `<安装目录>\store\<skill>` 的 junction。失败路径都有明确
出口：缺 pywebview → 系统弹窗 + 退出码 2；store 不可用 / adapters.json 损坏 → 界面
启动错误页（错误详情 + 重试），不会拿演示数据冒充真实数据。

**Setup.exe 形态（v0.3.2 起）**：中央仓库在 `<安装目录>\store`（用户看得见、带得走）；
`skilllocker.db` / `config.json` / `desktop_profile` / 日志仍在 `%LOCALAPPDATA%\SkillLocker`
（程序目录可能被同步/压缩工具碰）。安装程序只创建程序文件、空 `store` 与快捷方式，
**不预置任何技能**；全新安装首启按 §1.3 自动把各 agent 的共有技能收进仓库。
若安装目录不可写（机器级安装到 `Program Files`），空库首启会回退到
`%LOCALAPPDATA%\SkillLocker\store`，并在 `meta.loadError` 与日志里写明原因。

**卸载保护**：`packaging/setup.nsi` 卸载时默认**保留**技能库 —— 先把 `store` 搬到
`%LOCALAPPDATA%\SkillLocker\store.kept[-N]` 并写 `store-kept.txt` 说明路径，再删程序文件；
移动失败会中止卸载，绝不 `RMDir /r $INSTDIR` 把库删光。发布前必须做「装 → 卸 → 查 store 仍在」硬验。
