# skilllocker · 导入 skill 功能 — 实施执行文档

> 视觉与交互的**唯一基准**是高仿真原型 [`ui/design-skill-import.html`](design-skill-import.html)（双击打开可操作；
> 弹窗内「原型直达」一行可一键到达每种预检状态，侧栏「原型说明」有摘要）。
> 本文档把原型翻译为对现有代码的精确改动：改哪个文件、哪个函数、签名是什么、验收怎么判。
> 现状基准：`kit/DESKTOP.md` §7.1 自检 92 项、`ui/UI-SPEC.md` v0.2.0（分类文件夹已落地）。
> 定位一句话：**导入 = 把外部 skill 收进中央仓库，从此获得 junction 分发能力**。

---

## 1. 目标与范围

| 项 | 决定 |
|---|---|
| 核心语义 | 外部目录 → `import_dir()` 拷入中央仓库（原子、不改源目录）→ `rescan()` 注册 → 可选归档 → 按各 agent 当前分发开关自动补链（`auto_distribute`） |
| 入口 | ① 共有看板顶栏「导入 Skill」主按钮 ② `Ctrl+I` ③ 拖文件夹进窗口任意位置（全窗投放遮罩 → 松手直接进预检）。侧栏常驻 CTA 已移除（2026-11-01 产品取舍：侧栏保持纯导航；原型 `design-skill-import.html` 里的侧栏按钮仅作评审基准保留，落地不实现） |
| 来源 ×2 | 「本地文件夹」（磁盘 / U 盘 / 其他 agent 的 skills 目录）与「在线检索 skills.sh」（检索→临时下载→标准导入，两段式） |
| 状态机 | `source → review(扫描中/报告/冲突/同内容) → running → done / error`，单弹窗内推进，详见 §2 |
| 收编澄清 | **独有 → 共有的「收编」已由 `promote_unique` 实现**（后端 + 桥 + 独有检查器 promote-box），与导入弹窗互不相干、互不迁移。本文不改动它 |
| 归档 | 导入时可选一个分类夹（默认未归档）；不引入多选、不影响分类文件夹规则 |
| 不做（本期） | 批量导入（一次一个目录）、zip / URL 直装、导入时改名源目录、同名覆盖式导入（同名一律先改名或先处理现有 skill）、独有收编弹窗化 |

---

## 2. 状态机与 UI 精确规格

### 2.1 状态机

```
source ──预检──▶ review ──导入──▶ running ──成功──▶ done
   ▲               │  ▲              │
   └──「重新选择」──┘  └─名称键入实时重检─┘
                      └──失败──▶ error ──重试→ running / 返回→ review
```

- `review` 的扫描动画期间（约 600ms，真实为 `import_preflight` 耗时）不可点导入；
- `running` 期间**一切关闭路径禁用**：X 隐藏、遮罩点击无效、Esc 无效、取消按钮 disabled——后端同步执行不可中断，UI 不许撒谎；
- `done` / `error` 无脚部按钮，动作全部在正文 CTA；
- Esc 链保持既有约定：弹窗（非 running）→ 清搜索 → 退出文件夹。

### 2.2 布局 token（全部从原型 `<style>` 拷贝，此处只列关键值）

| 项 | 值 |
|---|---|
| 弹窗 `.imp-modal` | 660 宽（窄于 `calc(100% - 20px)` 收缩），圆角 16，复用 `.overlay`（rgba(4,7,12,.56)+blur10） |
| 步骤条 `.imp-stp` | 24 高胶囊 ×3：`01 选择来源 / 02 预检命名 / 03 导入完成`；态 = todo 灰 / `cur` accent-soft+点发光 / `done` success-soft |
| 来源 tab `.src-tab` | 双列等宽，64 高；激活 = accent 边 + soft 底；「在线检索」带 `需联网` mini 徽标 |
| 投放区 `.dz` | 108 高，1.6px 虚线；`hot` 态（dragover）accent 边 + soft 底 |
| 报告卡 `.rep` | 每行 34 起，左列 66 宽灰标签；SKILL.md / 名称 / 描述 / 规模 / 结构（chips） |
| 名称输入 `.imp-input` | 40 高、mono、14px/600；错误态红边红光晕 + `.field-err` 行 |
| 面板 `.panel` | danger（同名冲突，含建议 chips）/ same（同内容，含「知道了，跳过」）；**始终渲染，用 `hidden` 切换** |
| 执行 `.run-step` | 42 高 ×3：复制到临时目录（`.skpart-…`）/ 原子改名落库（`os.replace`）/ 重新扫描并注册（`rescan()`）；态 = doing（转圈+accent 边）/ ok（绿勾） |
| 进度 `.imp-prog` | 4 高，width 过渡 0.55s |
| 完成 / 失败 `.hero` | 54 图标块 + mono 标题 + 副句；ok=success-soft，bad=danger-soft；外圈 7% 大光晕 |
| 拖拽遮罩 `.drop-ov` | `position:absolute; inset:0`（挂 `#sk-app` 内，z-index 96 < 弹窗 100）；中央 `.drop-card` 虚线卡 |
| 全部类清单 | `.imp-modal/.imp-steps/.imp-stp/.src-tabs/.src-tab/.st-top/.st-sub/.mini-badge/.dz/.dz-ic/.path-row/.path-input/.imp-label/.demo-strip/.demo-chip/.sr-list/.sr-row/.sr-main/.sr-inst/.rep/.rep-row/.rep-chips/.r-mono/.r-desc/.note-bar(.warn/.info)/.name-wrap/.imp-input/.field-err/.field-help/.panel(.danger/.same)/.panel-t/.sug-chips/.sug-chip/.dest-box/.fold-picker/.fold-chip/.flow-hint/.run-steps/.run-step/.run-ic/.imp-prog/.hero(.ok/.bad)/.hero-ic/.cta-row/.drop-ov/.drop-card/.res-pill/.src-row-line/.file-card.is-new/.ins-import-hint` |

### 2.3 关键文案（与原型逐字一致）

| 位置 | 文案 |
|---|---|
| 来源页标题 | 把 skill 导入中央仓库 |
| 投放区 | 把文件夹拖到这里 ／ 松手后自动预检 — 也可以直接拖进主窗口任意位置 |
| 本地 tab 副句 | 磁盘 / U 盘 / 其他 agent 的 skills 目录 |
| 在线 tab 副句 | skills.sh 社区索引 · `需联网` |
| 在线说明条 | 「获取」= 先下载到临时目录，再走标准预检导入。下载失败可重试，不会直接写仓库。 |
| 名称帮助 | 仅限单层目录名：字母 / 数字 / 点 / 连字符，不能含路径分隔符（与 import_dir 同一规则）。 |
| 冲突面板 | 中央仓库已有同名 skill：`<name>` ／ 继续导入会失败（import_dir 拒绝同名）。换一个名字，或先到共有看板处理现有的同名 skill。 |
| 同内容面板 | 仓库已有同名且内容完全一致 ／ 全部文件哈希一致（`<hash8>…`），无需重复导入。若想覆盖，请先删除仓库中的同名 skill。 |
| 缺 SKILL.md 警示 | 未找到 SKILL.md — 这个目录大概率不是标准 skill，agent 可能不识别。仍可导入，建议先确认来源。 |
| junction 提示条 | 源是链接（junction / symlink），已解析到真实目录：`<resolved>`。导入复制的是真实内容，链接本身不受影响。 |
| 执行帮助 | 执行期间不能关闭窗口或再次修改名称；失败会回滚临时目录，仓库保持原样。 |
| 流程尾注 | 复制 到 .skpart 临时目录 → 原子改名 落库 → 重新扫描 → 到「智能体」页分发 |
| 完成 CTA | 完成 ／ 去分发（`去分发` 关弹窗 → 切到智能体看板并高亮该 skill） |
| 失败页 | 写入中央仓库失败 ／ **仓库没有变化。** 临时目录 .skpart-* 已回滚；可以重试、换来源，或检查目标磁盘。 |

### 2.4 校验规则（前端与后端同一口径）

```text
validName(n):  n 非空 且 n ∉ {'.','..'} 且 /^[A-Za-z0-9][A-Za-z0-9._-]*$/ 且不含 / \（路径分隔符）
冲突:          store 内已存在同名（大小写不敏感）→ conflict
同内容:        conflict 且 sha_tree(src) == skills.content_hash(name) → 建议跳过（不阻断改名路径）
可点「导入」:   预检完成 && validName && 非冲突 && 非同内容建议未确认
```

- 建议名 `suggestNames(base)`：`base-2 / base-20261101 / base-import`，过滤掉仍同名的；
- 名称键入时**只做原位更新**（err 类切换、`hidden` 切换、`dest-box` 改写、按钮 disabled），绝不整弹窗重渲染——会丢焦点 / 光标；
- 冲突面板内容随键入重建（`conflictPanelHtml(name, true)` 替换 `outerHTML`）。

### 2.5 「原型直达」示例条

仅 `MODE==='local'`（file:// 原型）渲染；桌面模式不渲染。五个 chip：标准 skill / 缺 SKILL.md / 同名冲突 / 同名·内容一致 / 写入失败演练。它是**评审工具，不是产品 UI**。

---

## 3. 后端改动

### 3.1 `kit/skilllocker_links.py` 新增 `import_preflight(cfg, src, name=None)`

紧挨 `import_dir()` 放（130 行附近），**只读**：不写库、不产生任何副作用。

```python
def import_preflight(cfg, src, name=None):
    """导入前预检：路径归一化 → junction 解析 → 名称/冲突判定 → SKILL.md 解析 → 规模统计。
    返回 dict（见 §6.1）；任何致命问题以 ok=False + reason 返回，不抛异常（前端直接渲染）。"""
```

处理顺序（每步都对应预检报告的一行）：

1. `src = normpath(...).replace(os.sep,'/')`；`os.path.isdir(src)` 为假 → `ok=False, reason='不是目录'`。
2. junction / symlink：`is_reparse(src)` 为真 → `link_target(src)` 解析真实路径；真实路径下必须 `isfile(SKILL.md)`（与 `import_dir` 同一防线），否则 `ok=False, reason='源是链接但解析不到 SKILL.md'`；成功则 `resolved=<真实路径>`。
3. `name = name or basename(src)`；校验规则与 `import_dir` 逐字一致（非法 → `name_error` 文案字段，前端复用同一句）。
4. 冲突判定：`lexists(join(cfg['store'], name))`：
   - 不存在 → `conflict='none'`；
   - 存在且 `sha_tree(src)` == `skills` 表该名 `content_hash` → `conflict='same-content'`（附 `hash8`）；
   - 存在且不同 → `conflict='same-name'`。
5. `SKILL.md`：`isfile(join(resolved或src,'SKILL.md'))`；为真则 `parse_frontmatter()`（已有，`skilllocker_base.py:294`）取 `name/description` 等字段，容错（解析失败 = 有文件无 frontmatter，不算错误）；为假 → `has_skill_md=False`（警示不阻断）。
6. 规模：`iter_files()` 计数 + `dir_stats()` 字节（已有）；`tree`：对顶层条目输出 `{目录名/} ×N` 聚合 chips（目录给条目数，文件给文件名，SKILL.md 恒列首），最多 6 项。

### 3.2 `kit/desktop.py` Api 新增三个方法

模式与 `promote_unique` 一致：`@_guard`（异常 → `{ok:false,error}` + 堆栈进 skilllocker.log）；碰文件系统/库的加 `@_serialized`（OPS_LOCK）。

```python
@_guard
def choose_dir(self):
    """原生目录选择。create_file_dialog 在 js_api 线程阻塞等待用户，属预期行为。"""
    r = self._window.create_file_dialog(webview.FOLDER_DIALOG)
    return {"ok": True, "data": (r[0] if r else "")}

@_guard
def import_preflight(self, src, name=None):        # 只读，不 serializaed
    cfg = skilllocker.load_config()
    pre = skilllocker.import_preflight(cfg, src, name)
    return {"ok": True, "data": pre}

@_guard
@_serialized
def import_skill(self, src, name, folder=""):
    """执行导入：import_dir（原子拷贝 + rescan + log_event('import')）+ 可选归档。
    folder 传非空必须是存在的 folders.id；set_meta 单行 upsert 会新建该 skill 的 meta 行，
    下次 bootstrap 的 seed_meta 对已存在行是 no-op，两不冲突。"""
    con = skilllocker.db()
    try:
        cfg = skilllocker.load_config()
        got = skilllocker.import_dir(con, cfg, src, name)
        if folder:
            skilllocker.set_meta(con, "shared", got, {"folder": folder})
        payload = _boards_payload(con, cfg)
        central = os.path.join(cfg["store"], got)
        payload["action"] = {
            "name": got, "path": central.replace(os.sep, "/"),
            "files": sum(len(f) for _, _, f in os.walk(central)),
            "bytes": skilllocker.dir_stats(central),
            "folder": folder or "",
        }
        return payload
    finally:
        con.close()
```

`skilllocker.py` CLI **不动**（`import` 子命令已存在）。`import_dir` 内部已 `rescan()` + `log_event(con,'import',...)`，无需重复。

> 失败路径：`import_dir` 在拷贝中途失败由 `copy_tree_atomic` 兜底（`.skpart-*` 临时目录 → `os.replace` 原子翻正；崩溃残留由下次 rescan 顺手清理）——失败页「仓库没有变化」文案以此为准，不是安慰话。

---

## 4. 桥接层 `ui/bridge.js`

1. `CALL_TIMEOUT` 追加：`import_preflight: 60000`、`import_skill: 180000`、`choose_dir: 600000`（等用户选目录，不能短）。
2. `window.SKILLLOCKER_BRIDGE` 追加（放 `promoteUnique` 之后）：

```js
chooseDir: function () { return call('choose_dir'); },
importPreflight: function (src, name) { return call('import_preflight', src, name); },
importSkill: function (src, name, folder) { return call('import_skill', src, name, folder); },
```

---

## 5. 前端改动

### 5.1 `ui/index.html`

1. ~~侧栏 `store-card` 之后插主 CTA~~（已移除，2026-11-01：侧栏不再放导入按钮，入口只留顶栏 / `Ctrl+I` / 拖拽；`.import-cta` 样式一并删除）。
2. `topbar-actions` 末尾插 `<button class="btn btn-primary" id="btn-import-top">`（refresh 之后）。
3. 雪碧图追加 14 个 symbol（path 从原型 `#sk-sprite` 原样拷贝）：`i-download`、`i-upload`、`i-globe`、`i-file-text`、`i-hard-drive`、`i-link`、`i-ext-link`、`i-arrow-left`、`i-package`、`i-ok`、`i-bad`、`i-spin`、`i-db`、`i-inbox`（若分类夹版本已带则跳过）。
4. 版本号文案与 `?v=` 资源戳随构建更新。

### 5.2 `ui/styles.css`

**追加** §2.2 列出的全部类（从原型 `<style>` 整段拷贝，类名不变）。两处落地差异：

1. 原型 `.overlay/.toasts/.drop-ov` 用 `position:absolute`（内联预览需要）；真机**保留 app.js 现有 `.overlay` 的 fixed 体系不动**，只新增 `.drop-ov { position:fixed; inset:0 }`（桌面壳内 fixed == 视口）。
2. 删原型为预览加的 `.list-body/.ins-body` `max-height`、`.folder-scroll` overflow 覆盖——真机沿用 flex 链（同 SPEC-category-folders §5.2 的约定）。

### 5.3 `ui/app.js`（核心）

**状态与入口**

| 项 | 做法 |
|---|---|
| `imp` 状态对象 | `{step:'source', tab:'local', demo:null, pre:null, name:'', folder:'', q:'', pathText:'', timers:[], runState:{}, runPct:0}`，模块级 |
| 入口 | `btn-import-top` → `impOpen('source')`（侧栏按钮已移除） |
| `Ctrl+I` | 挂进现有 keydown 链（与 `Ctrl+K` 并列） |
| 弹窗容器 | 复用现有 `openModal(html)` / `modal-root`（overlay 点击关闭行为一致）；`running` 态置 `modalOnCancel = null` 并由 imp 自己拦截 Esc |
| Esc | 现有 Esc 链最前面插一级：`imp.step && imp.step!=='running'` → `impClose()` |

**原型函数 → app.js 落点**（逻辑整段移植，数据源换掉）

| 原型 | 落地 | MODE 分叉 |
|---|---|---|
| `impSourceHtml()` | `renderImportModal()` 分支 | 「浏览」db 模式调 `SKB.chooseDir()`，回填路径后自动预检；local 模式 toast 说明 |
| `impLoadDemo()` | `impGoPreflight(src)` | db：`SKB.importPreflight(src, name)` → 渲染报告；local：`setTimeout` 620ms 出内置 DEMOS 模拟 |
| `impValidate()` | 同名原型 | `storeHas` 用 `SKILLS` 现值（db 模式信 `D.skills`） |
| `impStartRun()` | `impRun()` | db：先渲染 running，再 `SKB.importSkill(src, name, folder)` → `applyBootstrap(res)`（返回即含新 boards+meta，rescan 已在后端跑完）→ `imp.step='done'`；local：三段 setTimeout 动画 + 内存插入模拟行 |
| done 渲染 | `impDoneHtml()` | 元信息取 `res.action`；「去分发」→ `setBoard('agents')` 并高亮该行 |
| 失败渲染 | `impErrorHtml()` | `catch (err)` → `err.message` 进 `<pre>`；重试 = 重新 `importSkill` |
| 拖拽 | window 级 dragenter/leave 计数 + `.drop-ov`；drop → `impGoPreflight(路径, 名字)` | **路径解析三级降级**（见下） |
| 「原型直达」 | demo chips | 仅 `MODE==='local'` 渲染（§2.5） |

**拖拽路径解析（桌面端关键技术点，按序降级）**

1. `e.dataTransfer.value` —— pywebview 的 `pywebview-drag-drop` 注入在部分平台提供全路径数组；
2. `item.getAsFileSystemHandle()` —— WebView2/Chromium 可用但**只给名字不给路径**；
3. 都拿不到 → 只拿 `files[i].name`：预检弹窗打开、名称预填，路径栏置空并提示「拖拽只能拿到名字，请补全路径或用浏览选择」，不假装有路径。

**联动收尾**

- 导入成功后 `applyBootstrap(res)` 已重渲染全量；`state.selected = res.action.name`，未归档卡带 `.is-new` 高亮（`SKILLS[i].isNew` 标记，重扫后消失）；
- 在线检索：db 模式 `SKB.onlineSearch(q)` 直连 skills.sh、`SKB.onlineFetch(source, name)` 下载到 `%TEMP%/skilllocker-dl` 后拿 `data.path / data.name` 走 `impGoPreflight`（两段式，见 §10.1）；检索失败 / 下载失败 toast 可重试，仓库不写。原型模式仍用内置 `IMP_ONLINE` 演示行。

### 5.4 本地模式（file://）

与现有约定一致：localStorage 承载模拟（`sk.proto.skillImport.v1`：assign/theme/view/sort）。导入为模拟插入，toast 明示「原型模式：数据来自内置快照，未写真实仓库」；浏览 / 真实拖拽路径不可得时按 §5.3 降级提示。

---

## 6. 数据契约

### 6.1 `import_preflight` → `data`

```jsonc
{
  "ok": true,                      // false 时带 reason（人类可读，直接上 UI）
  "src": "C:/Users/LENOVO/.claude/skills/pdf",
  "resolved": "D:/ClaudeCode/.claude/skills/pdf",   // 非链接时为 null
  "name": "pdf",                   // 建议名（basename 或传入值）
  "name_error": "",                // 非法名时的文案（与 import_dir 同句）
  "has_skill_md": true,
  "fm": { "name": "pdf", "description": "PDF 读取 / 拆分 / 合并 / 表单全流程。" },  // 无则 null
  "files": 9, "bytes": 131072,
  "tree": ["SKILL.md", "scripts/ ×5", "references/ ×2"],
  "conflict": "none" | "same-name" | "same-content",
  "hash8": "1a2b3c4d"              // conflict 时附
}
```

### 6.2 `import_skill` 返回

`{ok, data: boards, meta, action:{name, path, files, bytes, folder}}`——boards/meta 与 `bootstrap` 同构（导入含 rescan，计数已刷新），前端只走 `applyBootstrap(res)` 一条路。

### 6.3 错误

桥层统一 `{ok:false, error:"<ExcType>: <msg>"}`（`_guard` 兜底），前端原样进失败页 `<pre>`；`RuntimeError("中央仓库已有同名 skill…")` 在预检阶段就应被拦住，出现在导入阶段的即是竞态（他人同秒导入），文案不变、走失败页。

---

## 7. 验收

### 7.1 `--selftest` 新增断言（现算，不写死；追加到 92 项之后）

| # | 名称 | 断言 |
|---|---|---|
| 1 | 预检标准目录 | 临时目录 + `SKILL.md`（含 frontmatter）→ `import_preflight` 返回 `ok/has_skill_md/conflict='none'/files>=1`，`fm.name` 正确 |
| 2 | 预检同名 | 目标目录改名成仓库已有名 → `conflict='same-name'`；内容抄一份一致的 → `conflict='same-content'` 且带 `hash8` |
| 3 | 预检缺 SKILL.md | 无 SKILL.md 目录 → `has_skill_md=False` 且 `ok=True`（警示不阻断） |
| 4 | 预检 junction | 对一个指向临时 skill 的 junction 预检 → `resolved` = 真实路径；死链 → `ok=False` |
| 5 | 预检非法名 | `name='a/b'` → `name_error` 非空、不抛异常 |
| 6 | 导入落库 | `import_skill(src, 'sk-import-it', folder='eng')` → skills 表有行、`skill_meta.folder_id='eng'`、`action.files` 与磁盘一致 |
| 7 | 导入同名拒绝 | 重复导入同名 → `ok=False`，错误文案含「同名」 |
| 8 | UI 弹窗状态机 | 点 `btn-import-top` → `.imp-modal` 存在、步骤条 = source；demo 路径预检后步骤条 = review、`imp-start` 可用；键入同名后 disabled |
| 9 | UI 执行不可关 | running 态按 Esc / 点遮罩，弹窗仍在 |
| 10 | UI 完成联动 | 假源目录走完 `import_skill` 后 `#store-count` = 后端现算值、`.file-card.is-new` 存在 |

> 全程用 `tempfile` 造源目录，不碰真实 agent 目录；结束时清理临时 skill（沿用现有「临时 skill 删除」自检模式）。

### 7.2 原型 Playwright 回归（file://，把冒烟脚本固化进回归清单）

已有 11 项全过（2026-11-01 实测，脚本要点：auto-open → demo 直达 → 冲突键入焦点保持 → 同内容 → 缺 SKILL.md → 全流程 done 计数 51→52 + `.is-new` → 失败演练 → 在线 fetch→review → 深色 → DataTransfer 合成拖拽全窗遮罩与预检回填）。固化时注意：`.panel-t` 是 flex 容器，`innerText` 会在图标与 `<span class="pv-name">` 之间插换行，断言别用跨元素连续子串。

### 7.3 手工清单

- [ ] 真实导入一个磁盘目录 → 共有看板出现、junction 正常建到 7 个已连接 agent（`verify` 通过）
- [ ] 从某 agent 的 skills 目录导入其独有 skill（junction 源）→ 提示条显示解析路径，拷贝内容而非链接
- [ ] 同名 / 同内容 / 缺 SKILL.md / 非法名（`a/b`、`.`、空）四种预检态文案与按钮态逐字核对
- [ ] 导入时选分类夹 → 落库 `folder_id`；导入后拖拽归档亦可用
- [ ] 「去分发」切智能体看板；完成 / 失败两态深色下可读
- [ ] 拖拽：真实资源管理器拖目录进窗口 → 遮罩 → 松手进预检（路径拿得到则预填，拿不到按 §5.3 降级提示）
- [ ] 1180 × 720 无横向溢出、弹窗可完整滚动；`Ctrl+I` / Esc 链不与 `Ctrl+K` 冲突

---

## 8. 实施顺序（每步可独立验证）

1. **后端**：`import_preflight()` + 单测（§7.1 之 1–5）→ `python -c` 直调验证。
2. **Api + 桥**：`choose_dir / import_preflight / import_skill` 三方法 + `bridge.js` 三包装与超时表。
3. **静态层**：`index.html`（CTA ×2 + 14 symbol）+ `styles.css`（§2.2 全部类 + 两处落地差异）。
4. **app.js 状态机**：`imp` 对象 + `renderImportModal()` 三步渲染（此时只有 source 可开）。
5. **app.js 交互**：预检 / 校验 / 运行 / 完成 / 失败 + `applyBootstrap` 联动 + Esc 链 + `Ctrl+I`。
6. **拖拽**：遮罩 + 三级路径降级；真机验证 `pywebview-drag-drop`（§10 风险项）。
7. **自检与回归**：§7.1 十项并入 `--selftest`（92 → 约 102），§7.2 脚本固化，全绿后收口。
8. **文档收口**：`UI-SPEC.md` §0 表登记本档；`DESKTOP.md` §7.1 项数更新；`data.js` 无需改动（导入不进快照）。

## 9. 风险与回滚

| 风险 | 缓解 |
|---|---|
| 拖拽拿不到全路径（平台差异） | §5.3 三级降级是**产品行为**而非兜底：名称预填 + 明确提示补路径；永不假装有路径 |
| 导入与 rescan 交错绊倒文件系统 | `import_skill` 挂 `@_serialized`（OPS_LOCK），与 delete/sync 同一把锁 |
| 大目录预检慢 | `iter_files` 只统计不哈希；同内容判定才 `sha_tree`（仅同名时一次）；超时 60s 桥层兜底 |
| `set_meta` 与 seed_meta 竞争 | `set_meta` 是单行 upsert，seed 对已存在行 no-op（import 后的下次 bootstrap 不会再播种该行） |
| 同名竞态（预检后他人导入） | `import_dir` 自身仍拒绝 → 失败页如实展示，文案与预检一致 |
| 杀软 / 索引器握住刚落盘文件的句柄（在线获取后几秒内导入几乎必现 WinError 5） | `copy_tree_atomic` 的 `os.replace` 走 `_replace_dir` 退避重试（0.15s→3s 五次 ≈6s），瞬时句柄自动通过；重试耗尽才清 `.skpart` 进失败页（§3.1 失败页文案不变） |
| 回滚 | 全部为**纯新增**：新函数 / 新方法 / 新 CSS 类 / 新弹窗，不改动三看板任何既有逻辑；删掉入口按钮即整体下线 |
| WebView2 缓存 | `?v=mtime` 机制自动生效 |

## 10. 已知取舍与后续

1. **在线检索后端已真实化（2026-11-01，`skilllocker_links.py` 在线检索与下载段）**：`online_search`（skills.sh，重试加固）+ `online_fetch(source, name, branch=None, progress=None, cancel=None)`（GitHub 下载到 `%TEMP%/skilllocker-dl`，返回 path/commit/files/bytes/timings，随后走标准 `import_preflight` → `import_skill`）。可靠性配方沿用 github-sandbox-download 技能实践：**清单以 GitHub git tree 为准**（jsDelivr flat 的分支快照有幻影文件 / 陈旧 size，只在 api.github.com 不可用时兜底，且兜底时不再解析 commit）；**下载 URL 钉定 commit sha**（不可变快照，避开 CDN 对分支名的缓存漂移）；**GitHub tree 的 blob sha 在下载后校验**（`sha1("blob <len>\\0" + body)`），不匹配视为该源失败并换源，防止 CDN 陈旧版本 / 镜像篡改；**jsDelivr 镜像按实测健康度排序：gcore / testingcf 优先**，cdn / fastly / quantil 次之；它们对未缓存文件可能 301 到 raw.githubusercontent，重定向守卫会把落点为 raw 的跳转直接判失败并立刻换源（不再等超时），raw 仅作最后兜底；单次 fetch 内记住成功源、连续失败 2 次的源本轮跳过。**文件级并发 4 workers**（单 skill 总量 >24MB 时降为 2，控制内存峰值）；单次请求 15s 超时、3 次指数退避重试、单次 fetch 180s 总预算（WebView 侧 240s 兜底）；>300KB 走 HTTP Range 分块（首块直接取 1MB 并从 Content-Range 拿总大小，逐块重试），传输完整性用响应自身 Content-Length / Content-Range 校验（清单 size 只作规模参考——快照可能差几个提交，硬对会永远失败）；GitHub 清单缓存 60s；可选 `SKILLLOCKER_GITHUB_TOKEN` / `GITHUB_TOKEN` 提升 API 限额；路径越界 / Windows 保留名 / Git LFS 指针 / 文件数与总量超限一律拒绝；半途失败清临时目录，仓库不写。**进度与取消**：下载期间经 `window.__SK_FETCH_PROGRESS` 回传 `done/total/bytes`，按钮显示「解析中 / 下载 n/N」，忙碌行出现「取消」；`online_fetch_cancel` 置位后每个 HTTP 请求前检查，最多等当前请求（≤15s）退出并清临时目录。已知边界：CDN 缓存窗口内文件内容可能滞后分支 HEAD 数小时（sha 钉定后仅当新提交尚未被 jsDelivr 处理时回退 raw）；个别 jsDelivr 节点会对大文件返回 Content-Range 陈旧但整包 body 与 tree size 一致的 206——此时按整包接受并再过 blob sha 校验，否则拒绝换源，避免把旧版本截断落库；后端日志含 timings；`open_url` 白名单只放行 skills.sh 与 GitHub。自检覆盖见 DESKTOP.md §7.1 在线 5 项（假网络）。
   可选 `config.json.network`（只读、下次启动生效）：`mirrors`（镜像主机名数组）、`proxy`、`retries`、`backoff`、`backoff_max`、`timeout`、`deadline`、`parallel`、`list_ttl`、`chunk_kb`、`bigfile_kb`；不配则用上表默认值。
2. **批量导入**：多目录拖拽是高频需求，但会引入逐项预检 + 批量命名 + 部分失败的队列 UI，留待下期；本期一次一个目录。
3. **同名覆盖式导入**刻意不做：`import_dir` 的「先改名或删掉再导入」是仓库安全边界，UI 不提供绕过。
4. **独有收编**不走本弹窗：`promote_unique` 语义不同（迁移显示名/备注、源副本对齐分发），保持现有独有检查器入口。
5. `import_preflight` 的 `tree` 仅聚合顶层，不做全量文件树预览（与差异弹窗的职责区分）。
