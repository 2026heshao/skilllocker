# Skillkit 代码审查与优化报告

| 项目 | 内容 |
|---|---|
| 审查日期 | 2026-10-31 |
| 审查基线 | git `main` 分支，提交 `488d657`（初始源码导入） |
| 审查范围 | 10 个代码/配置文件，约 9,700 行，覆盖率 100%，无跳过 |
| 审查方式 | open-code-review（委托模式）：三路并行深度审查 + 关键结论人工复核 |
| 问题统计 | **0 critical · 1 high · 8 medium · 14+ low** |
| 总体评价 | 中上水平的工具型项目，核心安全设计出色；短板在并发治理、测试基建与模块边界 |

**审查文件清单**

| 文件 | 行数 | 审查人 |
|---|---|---|
| `kit/skillkit.py`（核心库 + CLI） | 1428 | 专项代理 A |
| `kit/desktop.py`（桌面壳 + bridge + selftest） | 1431 | 专项代理 B |
| `ui/app.js`（前端主逻辑） | 2313 | 专项代理 C |
| `ui/data.js` / `ui/bridge.js` / `ui/index.html` / `ui/styles.css` / `ui/UI-SPEC.md` | 1575 / 84 / 303 / 1763 / 460 | 专项代理 C |
| `kit/adapters.json` / `kit/config.json` / `启动 skillkit.bat` | 349 / 5 / 28 | 人工 |

---

## 1. 专项确认：刷新功能是否覆盖"独有技能"

**结论：✅ 是。** 刷新链路完整覆盖独有技能，三个刷新入口（工具栏刷新按钮、设置弹窗"探测"、agent 详情"重新探测"）走同一条链路。

```
#refresh-btn (index.html:143)
→ requestRefresh() (app.js:1659，refreshBusy 防重入)
→ SKB.rescan() → Api.rescan (desktop.py:507)
→ skillkit.rescan()（中央仓库入库）+ list_boards() (desktop.py:513)
→ unique_entries() 现扫磁盘 (skillkit.py:1323)
→ payload 返回 "unique": [...] (skillkit.py:1370)
→ applyBootstrap → hydrate：UNIQUE = D.unique (app.js:29)
→ renderAll 重渲染独有看板 (app.js:742) 与侧栏独有计数 (app.js:496)
```

**设计核实要点**

- 独有技能每次刷新都从磁盘**现算**，不落库、不读跨调用缓存（`_hash_tree` 备忘仅单次快照内有效，skillkit.py:849）。
- 覆盖三类来源（skillkit.py:1147-1153）：agent 主 `skills_dir`（main）、adapters.json 声明的 `extra_dirs` 含 `根/**` 递归扫插件缓存（extra）、指向中央仓库以外的链接（foreign-link）。
- **分发开关关闭的 agent 也扫**（skillkit.py:1153-1155 显式设计）：agent 自己装的 skill 关了开关仍会被它加载，不能从看板上消失。语义正确。
- 共享外部目录按 adapters.json 声明顺序固定归属（skillkit.py:1156-1159），避免开关切换导致行键漂移、误清用户 meta。

**两处小瑕疵（非缺陷）**

1. `Api.rescan` 不调 `seed_meta`（只有 bootstrap 调），刷新后新增独有行的预置分类要等下次启动才写入；前端有 `seedCat` JS 兜底（app.js:226），实际影响小。
2. 刷新按钮 tooltip 只写"重新扫描中央仓库与 agent"（index.html:143），建议补上"与独有技能"。

---

## 2. 问题清单（按严重程度分组）

> 定级依据：critical = 数据损毁/远程可利用；high = 真实丢用户数据；medium = 状态不一致/本地安全边界/明显性能浪费；low = 边界缺陷与可维护性。

### 2.1 High

#### H1 · saveMeta 并发乱序可丢编辑

- **位置**：`ui/app.js:114-140`（配合 desktop.py:403、skillkit.py:738-756）
- **类别**：bug / 数据丢失
- **现象**：`flushMeta` 每次击键即发一次 `SKB.saveMeta`，无按 key 串行化。pywebview 每个 js_api 调用在**独立线程**执行（desktop.py:403 注释明示），各用独立 SQLite 连接。快速连续输入时，旧 payload 可能晚于新 payload 落库（最后 commit 者胜）——DB 留存中间值而 UI 显示最新值，重载后表现为"丢编辑"。
- **次生问题**：写入失败时 key 塞回 `dirtyKeys`，但没有任何重试时机，静默丢弃。
- **修复建议**：按 key 维护 Promise 链串行发送（保留现有"不去抖"语义——去抖会在隐藏窗口节流下丢编辑，注释已说明理由）；失败项在下次 flush 或定时器中重试。

### 2.2 Medium

#### M1 · open_dir shared 分支缺少路径校验（本地注入面）

- **位置**：`kit/desktop.py:612-613`（已人工复核属实）
- **类别**：security
- **现象**：`scope` 既非 agent 也非 unique 时，直接 `path = os.path.join(cfg["store"], key)` 后 `os.startfile(path)`，不查库、不过滤分隔符。`os.path.join` 遇到绝对路径会整体替换，`key="C:/Windows"` 或 `../..` 即可打开本机任意目录，与函数"避免任意路径注入"的声明相悖。agent/unique 分支都现查了库，唯独 shared 分支没有。
- **现实风险**：bridge 仅本机 WebView 可达，利用门槛较高，故定 medium。
- **修复建议**：shared 分支先 `select ... from skills where name=?` 验证 key 存在（与 `delete_skill:461` 一致），并拒绝含路径分隔符的 key。

#### M2 · set_agent 先落库后同步，失败无回滚

- **位置**：`kit/desktop.py:532-545`
- **类别**：bug / 状态不一致
- **现象**：开启分发分支先 `update tools set enabled=1` 并 `con.commit()`，之后才跑 `do_sync`。junction 创建失败很常见（权限、路径被占用），失败后 enabled=1 已持久化、链接未建成，异常被 `_guard` 吞成 `ok:False`，重启后状态持续不一致。
- **修复建议**：enable 的 commit 移到 `do_sync` 成功之后；或失败时回写 `enabled=0` 再抛出。

#### M3 · 刷新链路的 meta 修剪数据丢失边界

- **位置**：`kit/desktop.py:507-518` + `skillkit.py:346-355, 776-788`
- **类别**：bug / 数据丢失
- **现象**：`prune_meta` 用现场扫描结果做白名单。若刷新那一刻某目录临时不可枚举（U 盘未挂载、网络目录断连、杀软锁目录）：
  1. `rescan` 把"消失"的共享 skill 直接从 `skills` 表删除；
  2. `prune_meta` 再把对应 meta 行（含独有技能的用户改名/备注/分类）**永久清除**。
  一次刷新静默不可逆。
- **修复建议**：根目录不可读时跳过本轮 rescan/prune 并明确报错；或对"消失"条目要求连续 N 轮扫描后再清理。`prune_meta` 的同型调用还有 bootstrap(:420)、delete_skill(:472)、set_agent(:560)，需一并覆盖。

#### M4 · sha_tree 零容错，单文件读取失败毁掉整次刷新

- **位置**：`kit/skillkit.py:240`（`rescan:336`、`target_state:379` 为调用方；已人工复核属实）
- **类别**：bug / 健壮性
- **现象**：`hashlib.sha256(open(p, "rb").read())` 无任何异常捕获。任一文件被杀软锁定、权限拒绝、或扫描间隙被删（TOCTOU）都会让整个 `rescan`/`list_boards` 抛 OSError，前端只见"刷新失败"。同文件中 `parse_frontmatter`(:250)、`dir_stats`(:819) 都有容错，口径不一致。
- **修复建议**：`sha_tree`/`iter_files` 内单文件粒度捕获 OSError 并跳过（与 `dir_stats` 口径对齐）。

#### M5 · 库层无互斥，文件系统操作可交错

- **位置**：`kit/skillkit.py`（配合 desktop.py:403 的线程模型）
- **类别**：concurrency
- **现象**：bridge 方法在独立线程调用，UI 的 `refreshBusy` 只护刷新按钮。并发执行 `rescan`（含 `delete from skills`）与 `delete_skill`/`set_agent` 时，SQLite WAL 保证库不坏，但**文件系统操作会交错**：如 `make_junction` 在对方建链瞬间因路径已存在抛 RuntimeError，sync 半途中断。
- **修复建议**：skillkit 层加模块级 `threading.Lock`（同进程内生效）；若要防 CLI 与桌面端并发，再加 Windows 命名互斥。

#### M6 · list_boards 同一格口全树哈希两遍

- **位置**：`kit/skillkit.py:1300-1302`（state_map 循环）、`932-933`（agent_entries）、`1353`（tree_stats）
- **类别**：performance
- **现象**：同一次快照中每个实体格口被全树哈希两次（`target_state`→`sha_tree` 各调一次），`tree_stats(cfg["store"])` 还要完整 walk 第三遍。为快照去重设计的 `_hash_tree` 缓存(:848)未接入 `target_state`。
- **修复建议**：给 `target_state` 加可选 `hash_cache` 参数并在 `list_boards` 贯通；`tree_stats` 结果在快照内复用。

#### M7 · Esc 关闭确认弹窗不触发 onCancel，界面撒谎

- **位置**：`ui/app.js:2198-2202`（对照 1438-1449）
- **类别**：bug / 规范违反
- **现象**：`onCancel` 只挂在取消按钮与遮罩 mousedown 上，按 Escape 直接 `closeModal()`。"关闭分发"确认框按 Esc 后，复选框停留在已翻转状态而后端未变，违反 UI-SPEC §4"取消时开关回到数据模型的值，界面不撒谎"。
- **修复建议**：confirmModal 记录 pending onCancel，Escape 分支调用与取消按钮相同的回调。

#### M8 · 方向键导航摧毁备注框编辑状态

- **位置**：`ui/app.js:2209-2211`
- **类别**：bug
- **现象**：↑/↓ 导航只排除 `INPUT`，未排除 `TEXTAREA`。在备注框内按方向键会切换选中行并 `renderInspector()` 重建整个检查器，丢失焦点/光标/IME 组合状态。
- **修复建议**：判断条件同时排除 `TEXTAREA`（或用 `isContentEditable`）。

### 2.3 Low（精选有实际价值的条目）

| # | 位置 | 类别 | 问题 |
|---|---|---|---|
| L1 | skillkit.py:472-488 | security | CLI `import --name ../../x` 可令 `copytree` 写到 store 之外（desktop API 未暴露，风险有限）；`name="a/b"` 写嵌套目录且永不被注册。按 `remove_unique:572` 同标准校验 `basename(name) == name` |
| L2 | skillkit.py:539-559 | bug | CLI `remove` 不清理 `suppressed`/`skill_meta`；重建同名 skill 静默继承旧的"冲突已忽略"状态。删除时补 `delete from suppressed where skill=?` |
| L3 | skillkit.py:1241-1250 | bug | `conflict_detail` 仅按文件大小比对中央/本地副本，同大小不同内容漏报，可能误导 `local-to-central` 回收决策。改用内容哈希比对 |
| L4 | skillkit.py:49-51, 292 | robustness | `load_config`/adapters 解析无异常处理，文件损坏时刷新链路第一步即崩；同文件在 `agent_entries:917`、`extra_dirs_map:968` 却有 try 兜底，口径不一致 |
| L5 | skillkit.py:463, 488 | bug | `copytree` 非原子，中途失败留半成品目录被判 conflict-dir。先拷同级临时名再 `os.replace`（同盘原子） |
| L6 | skillkit.py:70-71, 356, 464 | performance | `events` 表无轮转只增不减，且每次快照 `count(*)`。按条数/时间裁剪 |
| L7 | desktop.py:1376-1384 | bug | `--selftest` 经 pythonw 启动时 `finally` 中 `print` 抛 AttributeError（`sys.stdout is None`），且发生在 `window.destroy()` 之前——隐藏窗口永不销毁、进程挂死。先 destroy 再输出，或输出前判 `sys.stdout` |
| L8 | desktop.py:384-385 | bug | 剪贴板 `GlobalLock` 返回 NULL 时 `ctypes.memmove(None,...)` 原生层崩溃；`SetClipboardData` 失败路径泄漏 GlobalAlloc 句柄。判空 raise + 失败时 `GlobalFree` |
| L9 | desktop.py:407 | bug | `win_max` 用 Python 侧缓存 `_maxed` 判断，Win+Up 等系统快捷键改变真实状态后按钮"失灵"一次。与 `win_bounds` 一致改用 `IsZoomed` 现查 |
| L10 | desktop.py:154-185 | bug | `_hwnd_by_title` 按标题前缀枚举全局窗口，双开实例时 chrome 修补/`win_bounds` 会命中另一实例窗口。`on_start` 缓存 hwnd 或校验窗口 PID |
| L11 | desktop.py:104-108 | bug | `fit_size` 在工作区小于 MIN（小屏/200% 缩放）仍钳到 1180x720，窗口大于屏幕且自检断言必败。MIN 钳到工作区 |
| L12 | ui/bridge.js:49-60 | bug | 桥调用无超时（`ready` 有 6s，单调用没有）；`rescan` 若永不 settle，`refreshBusy` 永久 true，刷新按钮永久禁用。`call()` 加可配置超时 |
| L13 | ui/app.js:648 | bug | 空结果分支提前 return 不调 `updateFoot`，脚部"51 条"与空态文案矛盾 |
| L14 | ui/app.js:505 | bug | `tab-shared-em` 恒为 `N / N agents`，未完成的占位；同类的 `store-size` 硬编码 fallback `'54.4 MB'`（:480）、`start()` 默认选中 `pdf`（:2253）均为测试/截图痕迹 |
| L15 | 启动 skillkit.bat | 移植性 | 硬编码 `D:\develop\python\3.12\pythonw.exe` 无 PATH 回退，换机器即失效 |
| L16 | desktop.py:24, 692-697; skillkit.py:282-287 | maintainability | `import subprocess` 未使用；`log_ui` 的 detail 未转义换行破坏单行日志约定；`read_text` 死代码 |
| L17 | ui/styles.css:1726 | UI | ≤1200 宽时 `.cat:nth-child(n+3){display:none}` 把"+N"更多指示一并藏掉。改用 `.cat:not(.more):nth-child(...)` |
| L18 | ui/app.js:1352-1380 | bug | "清空我的编辑"确认回调未先 `closeModal` 就发请求，快速双击连发两次（其他回调都先 closeModal，不一致） |
| L19 | ui/app.js:2286-2301 | bug | desktop 下 pywebview 注入超时时静默回退原型快照，与 bootstrap 失败路径（有 toast）行为不一致，用户可能误在操作真库。该路径同样给降级提示 |

### 2.4 专项核查通过项（无问题）

- **XSS**：所有动态插值（显示名/目录名/备注/label/路径/hash/自定义分类，含属性值、title、textarea 内容）均经 `esc()`（app.js:151-155），`mark()` 白名单净化，未发现遗漏。
- **接口一致性**：bridge.js 17 个方法名与 desktop.py Api 类一一对应；`save_meta` payload、`get_meta` 返回、unique/agents 行字段与前端消费逐项核对一致。
- **删除安全**：reparse point 防线（`is_reparse`/`remove_link`/`delete_tree`）、`remove_unique` 路径校验、`_unlink_one` 只删指向中央副本的链接——未发现可误删实体或注入的路径。
- **事务口径**：WAL + 每调用一连接；写操作失败未 commit 自动回滚；只读快照与写操作分离清晰。

---

## 3. 工程质量评估

### 3.1 突出的优点

1. **链接安全防线是全库最大亮点**：`is_reparse`/`norm_path`/`remove_link`/`delete_tree` 构成"绝不顺着链接删实体"的纪律贯穿；junction/symlink/copy 三形态 8 态状态机（`target_state`）划分清晰；`iter_files` 跳过 reparse 子树防死循环。
2. **注释质量远超平均**：几乎每个非平凡函数都有"为什么"级注释（Win32/DPI 的坑与实测数据、归属固定防 meta 漂移、事件时序导致的旧 bug、pywebview 拖动失效的根因），这类隐含知识文档化非常难得。
3. **前端转义与事件架构干净**：单一 `esc()` 贯穿全部 HTML 拼接路径；全部交互走 document 级委托、`bind()` 只执行一次；开关用原生 `change` 并在注释里写清 checked 翻转双触发的坑。
4. **bridge 纪律性好**：`_guard` 统一把异常转成 `{ok, error}`，前端不拿裸 rejection；每调用独立 sqlite 连接规避跨线程共享；`open_dir`/`delete_skill`/`export_json`/`log_ui` 均有针对性输入收口。
5. **自检体系完整**：`--selftest` 52 项断言覆盖 bridge、原生窗口 chrome、拖拽缩放、写库与删除安全兜底；期望值尽量现算不写死；删除测试有 try/finally 清理。
6. **双模式隔离清晰**：`MODE = 'db' | 'local'` 贯穿读写，原型端 localStorage 兜底完整；桌面端写库不去抖有明确理由。
7. **与 UI-SPEC 契合度高**：尺寸 token、行网格、动画参数、Esc 层级、外部来源徽标、独有分组 sticky 等均与规范一致。

### 3.2 主要不足

1. **并发治理缺失**（H1/M5/L12）：库层无互斥、桥调用无超时、写库无串行化——当前唯一能真实丢用户数据的一类问题。
2. **无类型注解、无独立单元测试**：`parse_frontmatter`、`_scan_unique_sweep` 版本折叠、`seed_category` 都是纯函数，极适合表驱动测试，但现在只能靠 desktop selftest 与 scratch 脚本兜底。
3. **单文件过大、职责混杂**：skillkit.py（CLI+库+扫描+分发+meta 四块混于 1428 行）、desktop.py（约 540 行自检驱动代码嵌在生产外壳里）、app.js（2313 行）。
4. **错误处理口径不一致**：细粒度容错（`_hash_tree`、`dir_stats`、adapters 两处 try）与零容错（`sha_tree` 链、`load_config`）并存，调用方无法预期"什么会抛"。
5. **设施只建了一半**：`_hash_tree` 缓存未接入主路径（M6）；`reseed_meta` 与 `seed_meta(force=True)` 双重 delete+commit 冗余；日志三套并存（launch.log / ui-drag.log / stdout）格式不一且 print 通道在 pythonw 下即 L7。
6. **样板重复**：`list_boards → prune_meta → return {ok, data, meta}` 同段在 5 个 bridge 方法重复；前端 chip/cats HTML 在 `skillRowHtml` 与 `patchRow` 间重复；跨端规则复刻（`_edited_count` 复刻 app.js `editedCount()`，分类数 7+custom 硬编码在自检里）。
7. **全局可变状态偏多**：app.js 十几个模块级变量（state/prefs/meta/dirtyKeys/MODE/UNIQUE）直接跨函数改写，无单一更新入口；规模尚可控，再加功能会加速腐化。

---

## 4. 优化路线图（按优先级）

### 第一批 · 堵住数据丢失口（合计约 1 天，全部边界清晰、低风险）

| 序 | 修复项 | 对应问题 | 涉及文件 |
|---|---|---|---|
| 1 | saveMeta 按 key 串行化 + 失败重试 | H1 | ui/app.js |
| 2 | 桥调用加可配置超时 | L12 | ui/bridge.js |
| 3 | skillkit 层模块级互斥锁 | M5 | kit/skillkit.py |
| 4 | prune_meta 对不可枚举根跳过本轮 | M3 | kit/desktop.py, kit/skillkit.py |
| 5 | sha_tree 单文件容错 | M4 | kit/skillkit.py |

### 第二批 · 安全与行为一致性（约半天）

| 序 | 修复项 | 对应问题 |
|---|---|---|
| 6 | open_dir shared 分支查库校验 + 拒绝分隔符 | M1 |
| 7 | set_agent 失败回滚 enabled | M2 |
| 8 | Esc 触发 onCancel；方向键排除 TEXTAREA | M7, M8 |
| 9 | selftest 先 destroy 再输出；GlobalLock 判空 | L7, L8 |
| 10 | bat 增加 PATH 回退 | L15 |

### 第三批 · 结构化（1-2 天，建议单独分支）

1. desktop.py 的 selftest 拆出独立模块（约 540 行），生产外壳回到 ~900 行。
2. skillkit.py 按现成边界拆 3-4 个模块：扫描（store/unique/agent）、链接（junction/sync）、boards 快照、meta/CLI。
3. 给纯函数补表驱动单元测试：`parse_frontmatter`、`target_state` 状态机、`seed_category`、前端 `esc()`/分组渲染。
4. 接通 `_hash_tree` 缓存到 `target_state`，贯通 hash_cache（M6），`tree_stats` 快照内复用。

### 第四批 · 持续改善

1. 统一错误处理口径：明确"库层什么会抛、什么吞"，config/adapters 解析统一 try + 记事件（L4）。
2. 统一日志到 `logging`（替代三套旁路日志），`events` 表按条数轮转（L6）。
3. 消除五处 boards payload 样板：抽 `_boards_payload()` 单一来源。
4. 前端为全局状态收敛单一更新入口；chip/cats HTML 抽公共函数。
5. 修复 L13/L14/L18/L19 一类行为占位与不一致；清理死代码（L16）。

---

## 5. 附录

- 审查方法：OCR 委托模式（文件过滤 + 规则解析人工完成），三路并行深度审查（每个文件全文逐行读完），两个 medium 结论（M1、M4）经人工二次复核确认。
- 定级过滤：低严重度中仅保留有实际价值的条目，疑似误报已静默丢弃。
- 本文档基于审查时代码状态（`488d657`）；修复后建议以本清单为验收底稿逐项勾销。
