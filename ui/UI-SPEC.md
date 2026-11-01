# skillkit · Skill Library — 桌面端 UI 实现规格

> ⚠️ **分类系统已升级（v0.2.0）**：「分类（多选 cats）」相关章节已被「分类文件夹 + 卡片网格视图」取代，
> 实施以 [`SPEC-category-folders.md`](SPEC-category-folders.md) 为准（交互基准：`design-category-folders.html`），
> 被取代处均带「⚠️ 已取代」标注；未提及部分本规格继续有效。
>
> 交付目录：`D:\skillkit\ui\`　设计稿尺寸：1440 × 900（桌面默认窗口 1200 × 760）　最小可用尺寸：1180 × 720
> 版本：v0.2.0（v0.1.0 基础上落地分类文件夹）　快照时间：2026-10-29 22:16　数据源：`D:\skillkit\store` + `D:\skillkit\kit\skillkit.db`

---

## 0. 交付物与打开方式

| 文件 | 作用 | 说明 |
|---|---|---|
| `index.html` | 窗口骨架 + SVG 图标 sprite | 单文件结构，无 CDN 依赖 |
| `styles.css` | 全部样式与 token | 浅色 / 深色 / 紧凑 / 降低动效四套状态 |
| `data.js` | 真实数据快照 | `window.SKILLKIT_DATA`，51 共有 + 2 独有（仅 `file://` 原型模式使用） |
| `app.js` | 全部交互逻辑 | 双模式：桌面端写 `skillkit.db`，原型写 localStorage |
| `bridge.js` | pywebview 桥封装 | 只在 `?desktop=1` 下等待 `window.pywebview.api`，否则立即回退 |
| [`SPEC-boot-animation.md`](SPEC-boot-animation.md) | 启动加载动画实施规格（v0.2.1） | 方案 A · 徽章汇入；交互基准 [`design-boot-animation.html`](design-boot-animation.html)，纯展示层，`bridge.js` / `data.js` 零改动 |
| `UI-SPEC.md` | 本文档 | 尺寸、状态、数据契约、后端对接 |

三种打开方式：

| 方式 | 命令 | 数据来源 |
|---|---|---|
| 双击启动（推荐） | `D:\skillkit\启动 skillkit.bat` | `skillkit.db`（`pythonw` 静默启动，无控制台） |
| 原型（浏览器） | 双击 `index.html` | `data.js` 快照 + localStorage |
| 桌面（真实库） | `python D:\skillkit\kit\desktop.py` | `skillkit.db`（SQLite，编辑即时落库） |

调试直达某个看板：`index.html?board=unique`／`?board=agents`；桌面壳内部固定带 `?desktop=1`，用于区分两种模式。桌面壳细节见 `kit/DESKTOP.md`。

---

## 1. 信息架构（做减法后的最终形态）

```
┌──────────────────────────── 标题栏 46 ────────────────────────────┐
│ Logo skillkit  SKILL LIBRARY │ 全局搜索 Ctrl+K │ LOCAL 主题 窗口键 │
├──────────┬───────────────────────────────────────┬───────────────┤
│ 侧栏 216 │ 工作区（flex）                        │ 检查器 344    │
│ 仓库卡片 │ 顶栏 66：视图分段/标题/排序/密度    │ Skill Detail  │
│ 看板 ×3  │ 看板切换卡片 ×3（80 高）              │ 显示名称      │
│ 库统计   │ 网格 / 列表（表头 36 + 行 60 + 脚 36）│ 归属分类(单选)│
│ 分类文件夹│                                      │ 备注          │
│ 设置入口 │                                       │ 分发覆盖 / 差异│
├──────────┴───────────────────────────────────────┴───────────────┤
│ 状态栏 30：共有/分类 · 仓库路径 · 已编辑 · 快照时间              │
└──────────────────────────────────────────────────────────────────┘
```

**三个看板：**

| 看板 | 判定口径 | 当前数据 | 行的 key |
|---|---|---|---|
| 共有技能 `shared` | 同名目录同时存在于全部已启用 agent，且为 junction 指向中央仓库 | 49 条（7/7 agent 全覆盖；原型快照 51） | `skill.name`（如 `pdf`） |
| 独有技能 `unique` | 只存在于某一个 agent：中央仓库无同名（`unique`）或同名但内容不同（`overridden`） | 2 条（`codex/.system`、`workbuddy/pdf`） | `tool + '/' + name`（如 `workbuddy/pdf`） |
| 智能体 `agents` | 本机已知 agent 的三态 + 分发开关（含 `external[]`：agent 自己读取的外部技能目录统计） | 10 个（6 已连接、4 已关闭、0 未安装） | `tool.key`（如 `claude_code`） |

智能体行不是 skill：最左与共有 / 独有看板同为 26×26 序号列（按当前列表顺序 1..N，状态筛选后重新从 1 编，纯展示），列表列义为「序号 / AGENT / 状态 / 目录 / 分发开关」，状态筛为 全部 / 已连接 / 已关闭 / 未安装；开关打开=`do_sync(only_tools=[key])` 补 junction，关闭=二次确认后 `unlink_tool()` 只撤链接，中央实体不动；未安装的 agent 开关禁用。右侧详情栏展示目录、已链接数、冲突数与「打开目录 / 重新探测」。

**外部来源徽标**：agent 若有第二条读取路径（由 `adapters.json` 的 `extra_dirs` 声明，本机当前只有 Trae CN → `~/.agents/skills`），且那里存在与中央仓库同名的实体，行标题右侧出现 `.ext-badge`「外部 N」（warning-soft 底、9.5px mono、16 高），hover 提示完整路径与「本工具不分发这些目录，开关不影响它们；移除单个 skill 可在独有看板里卸载」；详情栏多出「外部来源」行与一段黄色说明（N 个内容一致 / 不同）。它只做检测展示，不改变开关行为——关掉开关后这些实体仍会被该 agent 加载。

> key 的差异是刻意的：`workbuddy/pdf` 在独有看板改名，不会污染共有看板里的 `pdf`。

**每个 skill 的三项核心编辑能力（唯一必需功能集）：**

1. **更改显示名称** — 只改界面标题，磁盘目录名不变。
2. **备注** — 400 字，用于记用途 / 坑 / 来源。
3. **归属分类** — ⚠️ 已取代：单归属文件夹（一个 skill 最多属于一个文件夹，
   不归档 = 未归档），见 `SPEC-category-folders.md`；旧多选 cats 已废弃。

其余能力全部收敛为「只读信息」：分发覆盖、冲突差异查看、复制路径、导出 JSON。

---

## 2. 设计 token

### 2.1 尺寸 token

| Token | 值 | 用途 |
|---|---|---|
| `--sidebar-w` | 216px（≤1340 → 204，≤1200 → 192） | 左栏 |
| `--inspector-w` | 344px（≤1340 → 312，≤1200 → 292） | 右栏 |
| `--titlebar-h` | 46px | 标题栏 |
| `--topbar-h` | 66px | 工作区顶栏 / 检查器头 |
| `--statusbar-h` | 30px | 状态栏 |
| `--page-pad` | 20px | 工作区左右内边距 |
| `--row-h` | 60px（`body.compact` → 48px） | 列表行 |

### 2.2 色彩（浅色 / 深色）

| Token | 浅色 | 深色 | 用途 |
|---|---|---|---|
| `--bg` | `#f3f6fb` | `#080b12` | 窗口底色 |
| `--surface` | `#ffffff` | `#111722` | 卡片、输入框 |
| `--surface-2` | `#f7f9fd` | `#151d2a` | 表头、hover |
| `--surface-3` | `#edf1f8` | `#1d2635` | 徽标底、色块 |
| `--ink` | `#0b1020` | `#ffffff` | 主文字 |
| `--ink-2` | `ink 62%` | `#fff 64%` | 次文字 |
| `--ink-3` | `ink 40%` | `#fff 42%` | 辅助文字、标签 |
| `--ink-4` | `ink 24%` | `#fff 24%` | 占位符 |
| `--line` | `ink 7.5%` | `#fff 7.5%` | 分隔线 |
| `--line-strong` | `ink 14%` | `#fff 15%` | 输入框描边 |
| `--accent` | `#2e63f6` | `#7c9dff` | 主色（共有看板 / 焦点） |
| `--violet` | `#6f56e3` | `#a78bfa` | 独有看板 / 独占徽标 |
| `--rose` | `#d2406f` | `#ff7aa6` | 视频分类 |
| `--teal` | `#0a8f96` | `#46d4d8` | 记忆分类 |
| `--slate` | `#51607a` | `#9fb0c9` | 工程分类 |
| `--success` | `#0e9f6e` | `#3dd68c` | 正常分发 |
| `--warning` | `#c07a09` | `#f4b740` | 文档分类 / 仅本地 |
| `--danger` | `#d93a3f` | `#ff6b6b` | 冲突 / 大小不同 |

### 2.3 字体与字号

```css
--font:    "Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI", "Noto Sans SC", sans-serif;
--display: "Bahnschrift", "Segoe UI Variable Display", "Segoe UI", "Microsoft YaHei UI", sans-serif;
--mono:    "Cascadia Mono", "Consolas", "SFMono-Regular", monospace;
```

| 层级 | 字号 / 行高 | 字重 | 用在哪 |
|---|---|---|---|
| 页面标题 `h1` | 21 / 1.12，字距 -0.025em | 600 | 共有技能 / 独有技能 |
| 品牌字 | 15 | 600 | 标题栏 skillkit |
| 看板卡数字 | 26 / 1，字距 -0.03em | 600 | 49 / 2 |
| 看板卡标题 | 13.5 | 600 | 共有技能 |
| 行名称 | 13 / 1.3，字距 -0.01em | 600 | skill 名 |
| 行副标题 | 9.5 mono | 400 | 原始名 / agent |
| 备注正文 | 11.5 / 1.6 | 400 | 检查器 textarea |
| 行备注摘要 | 11 / 1.4 | 400 | 列表备注列 |
| 分类 chip | 10 / 20 高 | 550 | 列表 |
| 检查器标签 | 10.5，字距 0.10em，大写 | 600 | 显示名称 / 分类 / 备注 |
| 状态栏 | 10.5 | 400 | 底部 |

> 本轮整体上调：小字约 +2px、大字约 +1px（`app.js` 里 3 处内联字号同步），组件高度跟着加高以免裁切。

### 2.4 圆角 / 阴影 / 动效

| Token | 值 |
|---|---|
| 卡片圆角 | 14px（看板卡、列表卡、store 卡 12） |
| 输入框圆角 | 9px（新增分类 7） |
| 弹窗圆角 | 16px |
| chip 圆角 | 6–7px（筛选 chip 13 = 全圆） |
| 阴影 1 | `0 1px 2px ink 4%, 0 10px 26px ink 5%` |
| 阴影 2（hover） | `0 2px 6px ink 6%, 0 20px 44px ink 10%` |
| 阴影 3（toast） | `0 30px 90px rgba(4,8,18,.30)` |
| 缓动 `--ease` | `cubic-bezier(.2,0,0,1)` |
| 缓动 `--spring` | `cubic-bezier(.34,1.56,.64,1)` |
| 时长 | 120ms（微交互）/ 180ms（标准）/ 300ms（状态切换） |
| 行进入 | `rowIn 220ms`，`opacity 0→1` + `translateY(3px→0)`，每行 +12ms，最多 14 行错峰 |
| boot 入场 | logo `bootPop 480ms --spring`；外环 `bootRing 2.2s` 循环；扫光 `bootShine 2.6s`；word/chips/agents/foot `bootFadeUp 360ms` 依次 +80/120/160/200ms；chip `bootChipIn 280ms --spring` / `bootChipOut 200ms` |
| boot 时间轴 | 七相位 0→4940ms（详见 SPEC-boot-animation.md §4）；真实 R 到达则剩余提速 `clamp(剩余/1.2s, 1, 4)`，总时长 ≈ `max(R + 1.2s, 1.8s)`；`?noboot=1` 关闭 |
| 界面级联 | reveal 时 `[data-boot-rv]` 六大块 `220ms --ease`，每块 +12ms（--rv 0…6） |

---

## 3. 精确布局

### 3.1 窗口

```
stage:  padding 24px，深色网格背景 + 双径向渐变遮罩
window: 设计稿 1440 × 900（桌面默认 1200 × 760，min 1180 × 720），圆角 16，
        1px 内高光 + 0 44px 120px rgba(0,0,0,.58)
```
当视口 ≤1500 宽或 ≤960 高：去掉舞台留白，窗口 100vw × 100vh、圆角 0（模拟最大化）。

桌面端是无边框窗口：**建窗时即以 1200 × 760 居中**（把 WinForms 去边框的尺寸差额加回请求尺寸），首帧不跳位；整条标题栏可拖（前端 pointer 事件），双击标题栏空白切换最大化；8 个贴边手柄缩放（`body.desktop` 下显示）。

### 3.2 标题栏（46px）

| 元素 | 尺寸 / 间距 |
|---|---|
| 左内边距 | 14px |
| Logo | 24 × 24，圆角 7（渐变 `#3d7bff → #2e63f6 → #12c2cf`） |
| 品牌 → 副标 | 间距 9 / 分隔线 1 × 16 |
| 全局搜索 | 300 × 30，圆角 8，左图标 14，右侧 `Ctrl K` 键帽 19 高 mono 9.5px |
| 右侧 | LOCAL 胶囊 20 高 + 主题按钮 30 × 30 + 三个窗口键 46 × 46 |

### 3.3 侧栏（216px，padding 12 / 10 / 10，内部 gap 12）

| 区块 | 规格 |
|---|---|
| 仓库卡片 | padding 11，圆角 12；路径 mono 10.5；`49 skills` 数字 13；健康条 4 高（绿→`#12b981`） |
| 看板导航 ×3 | 40 高，圆角 9，图标 15，右侧计数徽标 20 × 18（mono/display 10.5px）；激活态 = `accent-soft` 底 + 左侧 3px 实色条；智能体用 teal |
| 库统计 2 × 2 | 数字 13，标签 10.5；无边框，仅间距 |
| 分类文件夹导航 | ⚠️ 已取代：32 高，圆点 `catdot` 7 × 7，计数徽标 17 高；**计数为 0 的分类不渲染** |
| 底部 | 设置按钮 32 高 + 版本/快照 10.5px |

### 3.4 工作区

| 区块 | 规格 |
|---|---|
| 顶栏 | 66 高；标题 21 / 副标题 11.5；右侧刷新按钮 30 × 30（忙碌时图标旋转、忽略重复点击）+「名称 A→Z」按钮 26 高 + 密度按钮 30 × 30 |
| 内容区 | padding 14 / 20 / 16；看板区三列等宽，与列表纵向 flex，列表占满剩余高度 |
| 看板卡 | 80 高（紧凑 70，≤1200 → 76），三列等宽 gap 10，圆角 14，padding 0 14 |
|  └ 图标 | 40 × 40，圆角 11；激活时实色（共有=accent，独有=violet）+ 顶部 2px 渐变条 |
|  └ 数字 | 26px，激活时用对应主色 |
| 列表上方 | ⚠️ 已取代：共有看板顶部新增视图分段（网格 / 列表）；进入文件夹时在列表卡上方渲染面包屑（含重命名 / 删除与「移出分类」投放区） |
| 列表卡 | 圆角 14，overflow hidden；表头 36，行 60 |
| 表头 | 10px 大写、字距 0.12em、`surface-2` 底 |
| 行网格 | `34px minmax(196px,1.3fr) 168px minmax(150px,1fr) 132px 18px`，gap 10，padding 0 14 |
| 行内元素 | 首列序号 26 × 26 圆角 8（`--surface-3` 底、11.5px 等宽数字 `tabular-nums`，按当前列表顺序 1..N，纯展示；选中行数字转 accent）；名称 13；徽标 19 高；分类 chip 20 高；覆盖条 7 段 × 9 × 7，gap 2；尾部箭头 12 |
| 行状态 | hover `surface-2`；选中 `accent 6%` + 左侧 2px accent；冲突选中 `danger 7%` + 左侧 2px danger |
| 独有看板分组 | 仅根层级 + 列表视图：每 agent 一条 sticky 分割线（32 高，紧凑 26）：18 × 18 mark 块 + agent 名 + `N 个 skill · n 同名覆盖` + 通栏细线；组顺序按 unique 扫描顺序，组内保持当前排序，序号跨组连续 1..N；滚动时当前组名钉在列表顶部。agent 夹内与网格视图不走分组（平铺行 / 卡片流） |
| 独有看板网格 | 与共有看板同款 folder-card 视觉：每个 agent 一张 agent 文件夹卡（bot 图标、颜色按 agent key 稳定散列取调色板、`N 个 skill` + 前 3 个 skill 预览 chip），点击进入该 agent 的独有技能（5 列列表 + 面包屑）；搜索态输出文件卡流（副标题 = 来源 agent）。独有 skill 不能跨 agent 移动：卡片不可拖拽、无 ⋯ 菜单、面包屑根节点不是投放区 |
| footer | 36 高，左提示 + 右计数，11px |

### 3.5 检查器（344px）

| 区块 | 规格 |
|---|---|
| 头部 | 66 高，padding 0 16；eyebrow 10 + 原始名 mono 13.5；右侧保存态胶囊 21 高 |
| 主体 | padding 14 / 16 / 16，可滚动 |
| 显示名称 | 标签 10.5，右侧「还原」；输入框 40 高，圆角 9，14px/600；下方 10.5 说明 |
| 分类 | chip 26 高，圆角 6，可多选、选中带勾；新增行 30 高输入 + 30 高按钮 |
| 备注 | textarea min-height 92，padding 9/10，11.5px/1.6；右下 `0 / 400` |
| 分发覆盖 | 2 列网格，项 28 高；异常项（`workbuddy` 同名冲突）红底红边 |
| 差异块 | `danger-soft` 底 + 22% 红边，圆角 9，含 28 高「查看文件差异」按钮 |
| 元信息 | 行高 ≥32，左列 58 宽灰标签，右列 mono 10.5 可省略 |
| 路径盒 | padding 7 / 8，圆角 7，mono，右 22 × 22 复制按钮 |
| 删除区 | `danger-soft` 底 + 22% 红边，圆角 10；整宽按钮「彻底删除 skill / 删除本地副本 / 卸载」+ 10.5 说明。独有行三选一：deletable（main 实体）→「删除本地副本」（`delete_skill`）；外部行（extra 目录 / 容器子目录）→「卸载（删除外部目录）」、foreign-link →「摘除链接（卸载）」（均为 `uninstall_unique`，二次确认、危险按钮，卸载文案给出完整路径 / 文件数与「agent 可能重新安装」提示） |
| 空态 | 居中，图标 40，文案 11 |

### 3.6 弹窗 / 提示

| 组件 | 规格 |
|---|---|
| 遮罩 | 全屏，`rgba(4,7,12,.56)` + blur 10，padding 28 |
| 弹窗 | 560 宽（差异弹窗 820），圆角 16，max-height `100vh - 56`，内部可滚；删除确认的主按钮用 `btn-danger` |
| 头 / 体 / 脚 | 20/22 上内距；正文 12/1.62；底部右对齐按钮 32 高 |
| 差异表 | 表头 32，行 30；列 `1fr 84 84 96`；标记 18 高（仅中央=蓝 / 仅本地=橙 / 大小不同=红 / 一致=绿） |
| 开关 | 40 × 22，圆角 11，选中渐变蓝 |
| Toast | 330 × ≥50，右下（right 20 / bottom 42），圆角 11，blur 18，进入 spring 280ms |
| Tooltip | 最大 280 宽，`#0b0f17` 底，10.5px |

### 3.7 状态栏（30px）

`● 49 共有 · 2 独有 │ D:\skillkit\store │ 已编辑 0 项 │ 快照 22:16`（字号 10.5）

---

## 4. 交互规格

| 交互 | 行为 |
|---|---|
| 切换看板 | 点看板卡或侧栏导航；同步 `body[data-view]`、标题/副标题、表头第 5 列（分发覆盖 / 来源）、列表、检查器；选中项重置为该看板首行 |
| 选中行 | 单击任意行 → 检查器渲染该 skill；`↑ / ↓` 键可连续切换（输入框聚焦时禁用） |
| 拖动窗口（桌面端） | 按住标题栏任意空白（含 skillkit 品牌区）拖动；`pointerdown` 记起点，`pointermove` 按 `screenX/screenY` 绝对位移调 `win_set_bounds`，`pointerup` 补终值；搜索框 / 按钮 / LOCAL 胶囊 / 窗口三键不触发；最大化态先把窗口还原（`win_restore()`）再按还原后的外框继续拖。顶部约 5px（四角 13px）是缩放手柄，按在那里只会缩放，不会移动窗口 |
| 分发开关（智能体页） | 动作只由原生 `change` 触发，`input.checked` = 目标状态 → `requestAgent(key, checked)`；`click` 只用来拦下事件、不读 `checked`（点击冒到 `document` 时它已被 Chromium 翻转，`label` 还会把点击转发给 `input`，一次按下出现两个 `click`）。关 = 二次确认后撤链接；取消 / 失败时开关回到数据模型的值，界面不撒谎 |
| 双击标题栏（桌面端） | 双击标题栏空白 = 最大化 / 还原；同上排除交互控件 |
| 改名 | 输入即写 `meta[key].display`，**即时落盘不去抖**；行内名称、副标题（原目录名）、已编辑计数即时更新，**不整表重渲染**（避免失焦） |
| 备注 | 同上；列表备注列同步，空值显示「添加备注…」占位 |
| 归属分类 | ⚠️ 已取代：检查器「归属分类」单选 chip（未归档 + 各夹 + ＋新建分类），点 chip 即移动并落库；行内第三列输出文件夹 chip（可点击进夹）；列表/网格支持拖拽归档与 ⋯ 菜单移动，详见 `SPEC-category-folders.md` |
| 搜索 | `Ctrl+K` 聚焦；空格分词按 AND；名称优先（显示名 / 原名 / label，支持连续子串与同段跳字模糊）；只有当前筛选下名称无命中时，才回退到 备注 / 分类名 / 描述（连续子串）；实时过滤 + 计数 `命中 / 总数`；过滤后选中项失效则自动选中首条结果 |
| 筛选 | ⚠️ 已取代：侧栏为导航（共有 = 全部技能 / 各夹 / 未归档，带计数、可投放；独有 = 全部独有 / 各 agent 文件夹，带计数、可点击；智能体仍为状态筛） |
| 排序 | 「名称 A→Z」（`localeCompare zh-Hans-CN`）/「最近编辑」 |
| 视图 | 顶栏分段「网格 / 列表」（共有与独有看板的根层级出现，按看板各记一份偏好 `prefs.views = { shared, unique }`，旧单一 `prefs.view` 首次启动自动迁移，刷新保持）：共有网格 = 分类夹卡片 + 未归档文件卡 + 搜索结果卡流；独有网格 = agent 文件夹卡 + 搜索结果卡流；列表 = 6 列（共有根）/ 5 列（夹内与独有） |
| 刷新 | 顶栏刷新按钮：桌面端 `rescan()`（同步 adapters.json + 重扫中央仓库）后整套重渲染，toast 报告扫描数与已连接 agent 数；原型只重渲染内置快照。看板 / 筛选 / 选中保留，列表滚动位置恢复；忙碌时按钮转圈并忽略重复点击 |
| 密度 | 按钮切换 `body.compact`（行 60 → 48） |
| 主题 | 按钮切换 `body.dark`，图标 moon ↔ sun |
| 差异 | 仅共有 `pdf` 与独有 `workbuddy/pdf` 有冲突；检查器差异块 → 820 宽弹窗，列出「仅中央 / 仅本地 / 大小不同 / 一致」 |
| 复制路径 | 路径盒按钮 → `navigator.clipboard` + Toast「路径已复制」 |
| 设置 | `Ctrl+,` 或侧栏入口；主题、密度、动效、显示停用 agent、导出 JSON、清空我的编辑、复制仓库路径、重新探测 |
| 空态 | 无匹配时隐藏列表体，显示图标 + 「没有匹配的 skill」 + 「清除筛选」 |
| 启动错误页 | 桌面端 `bootstrap()` 返回 `ok:false`（或桥调用 reject）时进入 `MODE='error'`：全屏 `#boot-error` 覆盖层（z-index 300），含标题「后端启动失败」、错误详情（`err.message`）、指向 `kit\skillkit.log` 的提示与【重试】按钮（`location.reload()`）；**绝不退回内置演示快照冒充真实数据**。仅 `file://` 原型保留只读快照降级 + toast。错误态下 `refresh` 等入口禁用 |
| Esc | 依次关闭弹窗 → ⋯ 菜单 → 清空搜索 → 退出文件夹 |
| 保存态胶囊 | 隐藏 → 输入中显示「已保存」（`success-soft`），`dirty` 态为 accent |

**归属预置规则（⚠️ 已取代原分类预置）：**

```
remotion|video → 视频     mnemosyne|memory → 记忆     aihot → 检索
pdf|docx|xlsx|pptx → 文档  ^web-|webapp → 前端
design|taste|stitch|minimalist|brutalist|brandkit|imagegen|image-to-code|redesign|ui-* → 设计
其余 → 工程
独有：不参与归档（folder 恒为未归档语义）
```

> 规则与旧 `seedCat()` 完全一致（函数更名 `seedFolder`，后端 `seed_category()` 同口径）。
> 升级迁移把旧 `categories` JSON 首元素回填为 `folder_id`（仅首次加列那一刻跑一次，
> 之后用户「移出到未归档」不会被反复撤销）；预置归属**不计入「已编辑」**——
> 已编辑只统计显示名 / 备注（前端 `editedCount()` 与后端 `_edited_count()` 同口径）。

### 4.1 启动错误页（后端 bootstrap 失败）

桌面壳已连上（`SKB.isDesktop()` 为真）而 `bootstrap()` 失败（`ok:false` / 桥 reject）时，
`boot()` 的 catch 进入 `MODE='error'` 并渲染 `renderBootError(err)`：

| 项 | 规格 |
|---|---|
| 容器 | `body > #boot-error`，`position:fixed; inset:0; z-index:300`（盖过 toasts 200），背景 `var(--bg)`，主题 token 自动跟随深色 |
| 卡片 | 520 宽居中：红色 `!` 图标 → 标题「后端启动失败」→ 副标题 → `<pre>` 错误详情（`err.message`，可滚动，`danger-soft` 底）→ 提示行「详细原因见安装目录 kit\skillkit.log」→【重试】主色按钮 |
| 重试 | `location.reload()` 重跑整条 boot 链路（后端恢复后自然成功） |
| 数据 | **不调用 `hydrate`、不渲染任何演示快照数据**；`window.__SK_MODE = 'error'`、`data-mode=error`；`applyBootstrap()` 顶部守卫直接 return（刷新入口禁用） |
| 降级边界 | 「桌面壳已连上但后端失败」与「桌面壳内 pywebview 注入超时」都走错误页；仅 `file://` 原型保留只读快照 + toast（原有行为） |
| 启动动画 | 进入错误页前先 `BootFX.fail()` 收尾编排时钟，overlay 不会卡在加载态 |

---

## 5. 数据契约

### 5.1 `data.js` → `window.SKILLKIT_DATA`

```js
{
  meta:   { generated, version, python, store, storeSize, db, scanAt,
            eventsTotal, skillCount, agentCount, disabledCount,
            sharedCount, uniqueCount },
  skills: [ { name, label, description, hash, origin, agents:[toolKey], realIn:[toolKey] } ],
  tools:  [ { key, label, mark, sub, dir, enabled, mode:"junction", detected } ],
  unique: [ { name, tool, toolLabel, mark, path, files, bytes,
              sameName, kind:"unique"|"overridden", storeFiles, storeBytes } ],
  events: [ { id, time:"10-29 22:16", kind, detail, object } ],
  conflict: {
    skill, tool,
    central: { path, count, bytes, files:[{name,size,flag}] },
    local:   { path, count, bytes, files:[...] },
    commonDiffs
  }
}
```

工具 mark 约定：`claude_code=CC`、`codex=CX`、`deepseek_harness=DS`、`qoder=QD`、`trae_cn=TR`、`workbuddy=WB`、`zcode=ZC`（启用）；`codebuddy=CB`、`github_copilot=GH`、`pi=PI`（停用）。

各 agent skills 目录：`.claude/skills`、`.codex/skills`、`.dsh/skills`、`.qoder/skills`、`.trae-cn/skills`、`.workbuddy/skills`、`.zcode/skills`、`.codebuddy/skills`、`.copilot/skills`、`.pi/agent/skills`。

### 5.2 localStorage（原型期持久化）

```js
// skillkit.library.meta.v1
{
  "aihot":      { display:"AI HOT · 检索", note:"…", cats:["search","doc"], seed:"search", updated:1729... },
  "workbuddy/pdf": { display:"", note:"", cats:["doc"], seed:"doc", updated:0 },
  "pdf":        { display:"", note:"", cats:["doc"], seed:"doc", updated:0 }
}

// skillkit.library.prefs.v1
{
  theme:"light"|"dark", density:"comfortable"|"compact", motion:true,
  showDisabled:true, sort:"name"|"edited", board:"shared"|"unique",
  view:"grid"|"list"
}
// v0.2.0：customCats / seeded 已废弃，改由 foldersMigrated 标记 + prefs.folders（含自建夹）承载；
// skill_meta v1 里每条 meta 增加 folder 字段（''=未归档），cats 字段仅作回滚兼容保留不再读写。
```

**为什么原型期不写数据库**：UI 只依赖只读快照，避免误改 51 个 junction 的分发状态；确认视觉后一次性接后端（见 §6）。

---

## 6. 落地到 skillkit 后端

### 6.1 数据库增量（建议）

```sql
create table if not exists skill_meta(
  scope      text not null,          -- 'shared' | 'unique'
  key        text not null,          -- 共有=name；独有=tool+'/'+name
  tool       text,                   -- 独有时记录来源 agent
  display_name text,
  note       text,
  categories text,                   -- JSON 数组，如 ["design","doc"]
  updated_at integer,
  primary key(scope, key)
);
```

> 不直接往 `skills` 表加列：独有 skill 不在 `skills` 表里（`codex/.system` 中央无同名、`workbuddy/pdf` 是本地真实目录），用 `scope + key` 才能同时承载两类。

### 6.2 新增后端函数（建议命名）

| 函数 | 作用 | UI 触发点 |
|---|---|---|
| `list_boards(con, cfg)` | 返回 `{shared:[...], unique:[...]}`，含每个 skill 的 agent 覆盖、junction/真实目录判定 | 首屏 / 切看板 |
| `get_meta(con)` | 返回全部 `skill_meta` 行（供改名 / 备注 / 分类回填） | 首屏 |
| `set_meta(con, scope, key, payload)` | ⚠️ 已取代：一次性提交 `{display, note, folder}`，`folder` 仅 shared 生效（''=未归档，缺省=保留原归属）；`categories` 列读旧值原样写回（回滚兼容） | 三个编辑框、归属 chip、拖拽 / 菜单移动 |
| `conflict_detail(con, cfg, tool, name)` | 返回中央/本地文件树 + `commonDiffs` | 「查看文件差异」 |
| `clear_meta(con)` | 清空所有人工编辑 | 设置 → 清空我的编辑 |
| `export_meta(con)` | 导出 JSON（含 board 快照） | 设置 → 导出 JSON |

复用现有函数：

| 现有函数 | UI 对应 |
|---|---|
| `rescan(con, cfg)` | 设置 →「重新探测本机 agent」（原型中为模拟进度）/ 首屏刷新 |
| `matrix(con, cfg)` | 分发覆盖格（7 个启用 agent） |
| `target_state(link, cfg, skill_row, tool_row)` | 每格状态：ok-link / ok-copy / missing / foreign-link / conflict-dir / broken-link / off |
| `resolve_conflict(..., 'local-to-central'\|'central-to-local'\|'ignore')` | 后续「处理冲突」按钮（本期未做） |
| `do_sync(con, cfg)` | 后续「一键修复」按钮（本期未做） |
| `toggle()` | 设置 → 显示停用 agent（仅 UI 过滤，不写 tools.enabled） |

### 6.3 pywebview 桥（建议）

```python
class Api:
    def boards(self):        return list_boards(con, cfg)
    def meta(self):          return get_meta(con)
    def save_meta(self, scope, key, field, value): ...
    def diff(self, tool, name): ...
    def copy_path(self, path): ...          # 走 pyperclip 或 win32clipboard
```

前端把 `app.js` 里 4 个数据入口（`SKILLKIT_DATA`、`localStorage` 读、`localStorage` 写、`openDiff`）替换为 `window.pywebview.api.*` 即可，其余渲染逻辑零改动。

### 6.4 实际实现（v0.1.0，已完成）

上面 6.1–6.3 是设计建议，落地时做了如下取舍（细节见 `kit/DESKTOP.md`）：

| 项 | 实际做法 | 与建议的差异 |
|---|---|---|
| 表结构 | `skill_meta(scope, key, tool, display_name, note, categories, seed, updated_at)` | 多一列 `seed`，记录预置分类，用于「用户是否改过分类」判定 |
| 播种 | 后端 `seed_meta()` 首次启动写入 49 + 2 条预置分类（`events` 里留 `meta-seed` 标记） | 前端不再负责播种，避免两边各播一次 |
| 写库 | `set_meta(scope, key, payload)` 一次性提交显示名/备注/分类，**即时写库不去抖** | 隐藏窗口下 `setTimeout` 会被节流，去抖会丢编辑 |
| 差异 | `conflict_detail(con, cfg, tool, name)` 现算中央 vs 本地文件表 | 与 `data.js` 快照同构，19 行一致 |
| 桥 | `bridge.js` 统一封装 `bootstrap / saveMeta / clearMeta / reseedMeta / diff / rescan / exportJson / copyPath / openDir / setAgent / winMin / winMax / winBounds / winSetBounds / winClose` | 每个方法返回 `{ok:true,data}` 或 `{ok:false,error}`，前端统一 toast |
| 窗口 | `pywebview` 无边框窗口，`?desktop=1` 触发桌面模式 | 不用 `pywebview-drag-region` / `easy_drag`：整条标题栏由前端 `pointerdown/pointermove` 按 `screenX/screenY` 绝对位移调 `win_set_bounds`，`pointerup` 补终值（异步基准晚到也不丢位移）；双击标题栏空白 = 最大化 / 还原；三键调 `window.minimize/maximize/restore/destroy`；四边/四角 8 个 grip 同样前端算目标矩形、后端 `SetWindowPos` 落窗（系统原生 hit-test 在 WebView2 下不生效） |
| 尺寸 | 默认 1200 × 760，**建窗时即按工作区居中**；本机 125% 缩放 → 1500 × 950 物理 / (210, 35) 物理 = (168, 28) CSS，首帧不跳位 | `build_window()` 直接把居中 `x/y` 传给 `create_window`；frameless 会被 WinForms 裁掉标题栏/边框，用 `_nc_css()` 把 `SM_CXSIZEFRAME/SM_CYSIZEFRAME/SM_CXPADDEDBORDER/SM_CYCAPTION` 差额加回请求尺寸（125% 下 +15 / +38 CSS）；`on_start` 只兜底 `IsIconic → SW_RESTORE`，不再有 `place_window()` |
| 智能体页 | 新增第三个看板，`list_boards()` 输出 `agents[]`（三态 + 实际链接数）；开关调 `set_agent` | 关闭只删 junction/一致拷贝（`unlink_tool`），不删中央实体；冲突副本保留并回报数量 |
| 缓存 | URL 带 `?v=<五个资源 mtime>`，`index.html` 把同一版本号带到 css/js | 否则 WebView2 会继续执行缓存里的旧 `app.js` |

### 6.5 实际实现（v0.2.0 分类文件夹，已完成）

实施细节见 [`SPEC-category-folders.md`](SPEC-category-folders.md) 与 `kit/DESKTOP.md` §7.1，要点：

| 项 | 实际做法 |
|---|---|
| 表结构 | 新增 `folders(id, name, color, position, created_at)` 表；`skill_meta` 加 `folder_id` 列（pragma 判存在再 alter，不重建表），`categories` 列保留不写（回滚兼容） |
| 迁移 | `migrate_folders(con, cfg)` 在 `bootstrap()` 里 `seed_meta()` 之前跑：folders 为空播种 7 预置夹；旧 `categories` 首元素命中夹 id 才回填，且**只在加列那一刻跑一次**——若每次启动都回填，用户「移出到未归档」会被旧 cats 在下次启动静默撤销 |
| 归属 | 单归属：`set_meta(payload.folder)`，''=未归档、缺省=保留原归属、未知夹 id 抛错；unique 行 folder 恒 NULL |
| 桥 | `bridge.js` 增 `folderCreate / folderRename / folderDelete` 三个包装 |
| reseed | 「清空我的编辑」新语义：只清显示名 / 备注、shared 行归属拉回预置夹，**不再清表**——自建文件夹保留 |
| 已编辑 | 只有显示名 / 备注算编辑（归属不算），前后端同一口径 |
| 视图 | 共有看板顶栏新增「网格 / 列表」分段（默认网格）；夹内恒为 5 列列表 + 面包屑；智能体看板不出现任何文件夹元素。v0.2.1 起网格视图扩展到独有看板：agent 即分类文件夹（agent 文件夹卡 + 夹内列表 + 面包屑），偏好按看板分存 |
| 快照 | `list_boards()` 输出增 `"folders": [...]`；`data.js` 补同构字段，本地模式另有 `prefs.folders`（自建夹持久化）+ 一次性 `migrateLocalFolders()` |

验收结果：桌面端 `desktop.py --selftest` **79/79 通过**（新增分类夹数 / 未归档数、网格卡片数、进夹行数、面包屑返回、建夹 / 删夹回落、归属落库、迁移幂等等 11 项文件夹断言）；原型（file://）Playwright 回归 **32 项 + 迁移 7 项 + 三路拖拽与检查器 chip 若干全过，0 条 JS 错误**；单元测试 **20/20**。`ui/data.js` 已补 `folders` 字段。

验收结果：桌面端 `desktop.py --selftest` **52/52 通过**（数据、差异、写库、计数、搜索、设置、智能体三态与开关、整条标题栏拖动、最大化态拖动先还原、8 个缩放手柄、外框 API 读写与最小尺寸兜底、启动尺寸与布局）；自检的期望行数一律从真实 `list_boards()` 取（当前 49 + 2），不写死数字，项数随库内数据浮动。`file://` 原型用 Playwright（系统 Edge）回归通过；开关的建/删链接另用临时库验证（见 `kit/DESKTOP.md` §7.1）；截图见 `D:\skillkit\.screenshots\desktop-*.png`。

---

## 7. 响应式与状态

| 条件 | 变化 |
|---|---|
| 宽度 ≤1340 | 侧栏 204，检查器 312，搜索框 240，列表列宽收紧，看板卡隐藏副标题与 tab 小字 |
| 宽度 ≤1200 | 侧栏 192，检查器 292，隐藏品牌副标，看板卡 76 高（数字 22），行内只显示前 2 个分类 chip（搜索框保留） |
| `body.compact` | 行 60 → 48，名称 13 → 12.5，看板卡 80 → 70，关闭行入场动画 |
| `body.dark` | §2.2 深色 token；筛选激活 chip 改为 `#fff 13%` 半透底，避免纯白刺眼 |
| `body.reduce-motion` | 所有动画 0.001ms、迭代 1 次 |
| `prefers-reduced-motion` | 同上（系统级） |

已实测：1200 × 760（桌面默认）与 1180 × 720 均无横向溢出，详情栏完整可见；1440 × 900 原型舞台同样成立。

---

## 8. 验收清单

### 8.1 视觉 / 布局

- [x] 1440 × 900 设计稿圆角、投影、内高光正确（桌面默认 1200 × 760 同样完整）
- [x] 标题栏 46、侧栏 216、检查器 344、状态栏 30、顶栏 66 实测一致
- [x] 三个看板卡 80 高（≤1200 → 76，紧凑 70），激活态主色差异（蓝 / 紫 / teal）
- [x] 行高 60（紧凑 48），行内 6 列对齐，无子元素溢出（最高 26px 行标记）
- [x] 无横向溢出（1200、1180 与 1440 均 0px）
- [x] 智能体页 10 行、状态筛 4 项、每行开关齐全，未安装开关禁用
- [x] 8 个贴边缩放手柄仅在 `body.desktop` 显示，不遮挡标题栏拖动
- [x] 整条标题栏可拖 + 双击空白最大化；搜索框 / LOCAL 胶囊 / 窗口三键不触发
- [x] 空状态、清除按钮、保存胶囊均按 `hidden` 正确隐藏
- [x] 深色模式文字对比可读，筛选激活态不刺眼

### 8.2 交互（36 项自动测试，全部通过）

> 原型快照 `data.js` 仍是 51 共有 + 2 独有；桌面真实库当前 49 + 2，差异只在行数，布局与交互一致。

| # | 用例 | 结果 |
|---|---|---|
| 1–6 | 51 行（原型快照）、已编辑=0、行高 60、无超高子元素、无横向溢出、空态隐藏 | ✅ |
| 7–14 | 改名即时反映 / 计数=1 / 落盘；备注回显 / 字数 / 落盘；分类追加 / chip +1 | ✅ |
| 15–16 | 刷新后改名与计数仍在 | ✅ |
| 17–21 | 独有看板 2 行、标题切换、key 正确、来源列、侧栏高亮同步 | ✅ |
| 22–23 | 独有 pdf 改名生效、共有 pdf 未被污染 | ✅ |
| 24–25 | 搜索 pdf 命中 1 行、清除按钮出现 | ✅ |
| 26 | 无结果显示空状态 | ✅ |
| 27–30 | 深色切换、紧凑行高 48、设置弹窗打开、Esc 关闭 | ✅ |
| 31–32 | 差异弹窗打开并列出 19 行文件 | ✅ |
| 33–36 | 1180 无溢出、侧栏 192、检查器 292、搜索保留 | ✅ |
| — | 控制台异常 / 错误日志 | 0 条 |

智能体看板与窗口另由桌面端自检覆盖（`kit/DESKTOP.md` §7.1，当前 92 项）：三态计数、状态筛、开关、未安装禁用、开关方向回归（点开着的开关只弹一个关闭确认框、取消后回到开）、**外部来源徽标与详情行（数量与后端 `external[0].count` 一致）**、序号列（含两位数不溢出）、整条标题栏拖动、最大化态拖动越过阈值先还原、最大化单击不还原（缩小只认双击）、8 个 grip、外框 API 读写与最小尺寸兜底、启动尺寸与详情栏边界；开关的建/删链接用临时库做集成验证，未触碰真实 agent 目录。

---

## 9. 已知取舍与后续建议

1. **未做的能力（刻意）**：概览 KPI、7×51 矩阵、事件流、在线检索、独立冲突页、工具管理页——本版按「三个看板 + 三项编辑」做减法全部移除，相关能力留在 `skillkit.py`，后续可按需加回。
2. **停用 agent**：检查器「分发覆盖」仍显示 3 个停用 agent（CodeBuddy / GitHub Copilot / Pi），置灰不可点；设置里可整体隐藏。
3. **冲突处理入口**：目前只做「查看差异」；`resolve_conflict()` 已有三种处置方向，若要接按钮，建议只暴露 `central-to-local` 与 `ignore`，`local-to-central` 会覆盖中央仓库，需二次确认。
4. **快照时间**：`data.js` 的 `scanAt` 是生成时刻（22:16），接后端后改为「上次 rescan 时间」，状态栏需要随扫描更新。
5. **中文排序**：使用 `localeCompare(..., 'zh-Hans-CN')`，`AI HOT` 与 `aihot` 这类同名不同大小写的条目会相邻但不会合并；如需合并，应在数据层做大小写归一。
6. **桌面壳未做的部分**：不做托盘、开机自启、单实例锁、安装包（PyInstaller）；关闭窗口即退出进程。边界说明见 `kit/DESKTOP.md`。
7. **`workbuddy/pdf` 冲突已按「两份都留」处理**：`suppressed` 表写入 `ignore` 决策，`skillkit.py verify` 会显示为 `suppressed`（不再报冲突，也不会自动合并）。
8. **窗口默认 1200 × 760 并每次居中**：设计稿 1440 × 900 只作为原型画布；桌面壳 `build_window()` 把居中坐标直接传给 `create_window`，首帧即 1500 × 950 物理 / (210, 35)（本机 125% 缩放），不再有二次摆位；frameless 去边框的差额由 `_nc_css()` 补偿（内容区正好 1200 × 760）。四边/四角缩放由 8 个 grip 在前端算目标矩形、后端 `SetWindowPos` 落窗（不走系统原生缩放循环），最小 1180 × 720；最大化态下 grip 与标题栏拖动均不生效。
9. **智能体的「未安装」态**：`tools` 表只保留同步过的 adapter；从本机卸载 agent 后再「重新探测」会把它标成 `detected=0`，看板显示「未安装」且开关禁用。没同步过的 adapter 不会凭空出现在列表里。
