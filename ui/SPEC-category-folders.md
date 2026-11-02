# skilllocker · 分类文件夹功能 — 实施执行文档

> 视觉与交互的**唯一基准**是高仿真原型 `ui/design-category-folders.html`（浏览器直接打开可操作）。
> 本文档把原型翻译为对现有代码的精确改动：改哪个文件、哪个函数、签名是什么、验收怎么判。
> 现状基准：`APP_VERSION 0.1.0`（skilllocker.py）、`ui/UI-SPEC.md` 所描述的版本。
> 取代关系：UI-SPEC.md 中所有「分类（多选 cats）」相关章节自本功能上线起作废，以本文为准。

---

## 1. 目标与范围

| 项 | 决定 |
|---|---|
| 分类模型 | 多选标签 → **单归属文件夹**（一个 skill 最多属于一个文件夹；不归档 = 未归档） |
| 分类管理 | 预置 7 个固定分类废弃；文件夹由用户手动新建 / 重命名 / 换色 / 删除，可排序号 |
| 视图 | 共有技能看板新增**卡片网格视图**（文件夹卡片 + 未归档文件卡），保留列表视图，顶栏分段切换 |
| 归档操作 | 拖拽（行 / 文件卡 → 文件夹卡片 / 侧栏项）、行 ⋯ 菜单移动、检查器单选 chip、拖到面包屑「全部技能」移出 |
| 作用范围 | **仅共有（shared）看板**。独有 / 智能体看板逻辑不变，但独有列表的「分类」列随分类系统一并移除。<br>**v0.2.1 更新**：网格视图已扩展到独有看板——一个 agent 就是一个分类文件夹（agent 文件夹卡 + 夹内 5 列列表 + 面包屑），视图偏好按看板分存（`prefs.views`）；agent 夹不可新建 / 重命名 / 删除，独有 skill 不参与拖拽归档（不能跨 agent 移动）。本文其余内容不变 |
| 不做（本期） | 文件夹拖拽排序、独有技能归档、嵌套文件夹、多选恢复 |

---

## 2. 数据模型与迁移

### 2.1 新表 `folders`（加进 `skilllocker.db()` 的 executescript）

```sql
create table if not exists folders(
  id text primary key,          -- 预置夹沿用旧分类 id：design/frontend/doc/video/memory/eng/search
  name text not null,           -- 1..12 字符
  color text not null,          -- violet|accent|warning|rose|teal|slate|green（green 映射 --success）
  position integer not null default 0,
  created_at integer
);
```

### 2.2 `skill_meta` 加列（幂等，禁止重建表）

```sql
alter table skill_meta add column folder_id text;   -- 仅 shared 行使用；'' / NULL = 未归档
```

执行前用 `pragma table_info(skill_meta)` 判列是否存在。**`categories` 列不删不改**：
`set_meta` 整行 insert-or-replace 时把旧值原样写回（见 3.3），保证回滚旧代码后仍能读到旧分类。

### 2.3 迁移 `migrate_folders(con, cfg)`（新函数，`bootstrap()` 内、`seed_meta()` 之前调用）

1. 建列（2.2）。
2. `folders` 为空时播种 7 个预置夹：`(design,设计,violet,0) (frontend,前端,accent,1) (doc,文档,warning,2) (video,视频,rose,3) (memory,记忆,teal,4) (eng,工程,slate,5) (search,检索,green,6)`。
3. 回填：遍历 `skill_meta`，对 `scope='shared'` 且 `folder_id` 为空的行，取 `categories` JSON **首元素**，若命中预置夹 id 则写入 `folder_id`；其余（含用户自建分类 id）留空 = 未归档。幂等：已回填的行跳过。

### 2.4 客户端一次性迁移（db 模式，`app.js` 启动时）

后端看不到 localStorage 里的 `prefs.customCats`，由前端补一步：

- `prefs.foldersMigrated` 为真则跳过。
- 否则：逐个 `SKB.folderCreate(label, color)` 建夹（记 label→id 映射）；再遍历 `meta`，凡 `cats[0]` 的**分类 label** 与新夹名一致且当前无归属的 skill，调 `saveMeta` 写 `folder`；最后置 `prefs.foldersMigrated = true`、删除 `prefs.customCats`。
- file:// 原型模式（`MODE==='local'`）不走此流程：`FOLDERS` 直接由预置 7 夹 + `seedFolder()` 规则在内存推导（沿用原型实现）。

---

## 3. 后端改动

### 3.1 `kit/skilllocker.py`

| 函数 | 改动 |
|---|---|
| `db()` | executescript 追加 `folders` 建表（2.1） |
| `migrate_folders(con, cfg)` | **新增**，见 2.3 |
| `folder_rows(con)` | **新增**：`select * from folders order by position, created_at` → `[{id,name,color,position}]` |
| `folder_create(con, name, color)` | **新增**：校验 `1<=len(name.strip())<=12`、`color in ("violet","accent","warning","rose","teal","slate","green")`；`position = max(position)+1`；返回行 dict。名称不去重（id 唯一即可） |
| `folder_rename(con, folder_id, name, color)` | **新增**：两参均可选，至少传一个；folder_id 不存在抛 `RuntimeError` |
| `folder_delete(con, folder_id)` | **新增**：`update skill_meta set folder_id=NULL where folder_id=?` → 记 moved 数 → `delete from folders where id=?`；返回 `{"moved": n}` |
| `get_meta(con)` | 输出每行增 `"folder": r["folder_id"] or ""`；`cats` 字段保留输出（回滚兼容）但前端不再消费 |
| `set_meta(con, scope, key, payload)` | payload 增 `folder`（str）：shared 时必须是存在的 folder id 或 `''`；写 `folder_id` 列。**categories 列读旧值原样写回**（不再接收 cats）。unique 行 folder 恒为 NULL，传了也忽略 |
| `seed_meta(con, cfg, force)` | shared 新行：`folder_id = seed_category(name)`，`categories` 写 `'[]'`；`seed_category()` 规则函数**原样保留**（它就是回填与"清空编辑"的口径）；unique 新行 folder 恒 NULL |
| `reseed_meta(con, cfg)` | 语义变更（设置→「清空我的编辑」）：**不再清表**。`update skill_meta set display_name='', note=''`；shared 行 `folder_id = seed_category(key)` 回预置夹；`folders` 表不动（用户自建夹保留）。返回影响行数 |
| `prune_meta(con, boards)` | 不变（整行删除，天然带走 folder_id） |
| `list_boards(con, cfg)` | 返回 dict 增 `"folders": folder_rows(con)`（放在 `"tools"` 旁） |
| `APP_VERSION` | `0.1.0` → `0.2.0` |

### 3.2 `kit/desktop.py`

```python
# Api 新增三个方法（模式与 set_agent/delete_skill 一致：变更后回整套 boards+meta）
@_guard
def folder_create(self, name, color):
    con = skilllocker.db()
    try:
        cfg = skilllocker.load_config()
        row = skilllocker.folder_create(con, name, color)
        skilllocker.log_event(con, "folder-create", "%s (%s)" % (name, color))
        boards = skilllocker.list_boards(con, cfg); skilllocker.prune_meta(con, boards)
        return {"ok": True, "data": boards, "meta": skilllocker.get_meta(con), "folder": row}
    finally: con.close()

def folder_rename(self, folder_id, name, color)   # 同上，log_event("folder-rename", folder_id)
def folder_delete(self, folder_id)                # 同上，action={"moved": n}
```

`bootstrap()`：`skilllocker.seed_meta(...)` 之前插一行 `skilllocker.migrate_folders(con, cfg)`。
`rescan()` 不动（新 skill 的 meta 由下次 bootstrap 的 seed_meta 补，与现状一致）。

### 3.3 自检（`--selftest`）必须同步的四处

| 位置 | 现状 | 改为 |
|---|---|---|
| `_edited_count`（~L893） | `display/note` 或 cats 偏离 seed 计为编辑 | 只算 `display || note` |
| ~L922-951「分类数」 | 读 `prefs.customCats` 求 `7+N`，断言 `stat-cats` | 整段删除，换成「分类夹数」：断言 `stat-folders` = 后端 `folders` 数 |
| ~L1100-1110 改名恢复 | 8 列 `insert or replace` 整行还原（会覆盖 folder_id） | 改 `update skill_meta set display_name=?, updated_at=? where scope='shared' and key=?` |
| 断言集 | 无文件夹用例 | 新增 §7.1 的 8 项 |

---

## 4. 桥接层 `ui/bridge.js`

`window.SKILLLOCKER_BRIDGE` 追加三个包装（紧挨 `reseedMeta` 放）：

```js
folderCreate: function (name, color) { return call('folder_create', name, color); },
folderRename: function (id, name, color) { return call('folder_rename', id, name, color); },
folderDelete: function (id) { return call('folder_delete', id); },
```

---

## 5. 前端改动

### 5.1 `ui/index.html`

1. `topbar-actions` **最前**插入视图分段（原型同款）：
   ```html
   <div class="segmented" id="view-seg" role="group" aria-label="视图切换">
     <button data-view-mode="grid" title="卡片网格视图"><svg class="icon"><use href="#i-grid"/></svg>网格</button>
     <button data-view-mode="list" title="列表视图"><svg class="icon"><use href="#i-rows"/></svg>列表</button>
   </div>
   ```
2. 雪碧图追加 4 个 symbol（path 从原型 `#sk-sprite` 原样拷贝）：`i-ellipsis`、`i-folder-plus`、`i-folder-open`、`i-inbox`。
3. 侧栏 `side-stat`：`stat-cats`→`stat-folders`（文案「分类」→「分类文件夹」）、`stat-unique`→`stat-unfiled`（「独有」→「未归档」）。`stat-agents`、`stat-edited` 不动。
4. `side-cat-label` 文案改由 `renderChrome()` 管理（「分类文件夹」），nav 容器 id `side-cats` 改名 `side-folders`。
5. 其余结构（titlebar / boards 三卡 / list-card 骨架 / 检查器骨架 / statusbar）不动。

### 5.2 `ui/styles.css`

**追加**（从原型 `<style>` 整段拷贝，类名不变）：`.boards-wrap`、`.folder-scroll`、`.folder-grid`、`.grid-sect`、`.folder-card` 与全部 `.fc-*`、`.file-card`、`.crumb-bar` 与全部 `.crumb-*`、`.ctx-menu` 与全部 `.ctx-*`、`.palette`、`.fold-picker`、`.fold-chip`、`.mini-add`、`.nav-label-row`、`.row-more`、`@keyframes` 区新增的 `sk-spin/sk-fadeIn/sk-modalIn/sk-toastIn/sk-toastOut`、响应式区两条 5 列网格变体（`.row.in-folder, .list-head.in-folder`）。

**三个必须改的差异**（原型为自适应预览做了让步，落地按真机布局）：

1. 删掉原型的 `.folder-scroll`/`.list-body`/`.ins-body` 三条 `max-height`——真机沿用现有 flex 链（`.content` 已是 `overflow:hidden` + flex column），列表体保持 `.list-body { flex:1; min-height:0; overflow:auto }`。
2. `.list-head, .row` 的 6 列网格保留为默认；`.row.in-folder, .list-head.in-folder` 5 列变体同时服务于**进入文件夹的共有行**与**独有看板所有行**。
3. `.fold-chip` 直接替代 `.cat-chip`：删除 `.cat-picker/.cat-chip/.cat-add-row/.cat-add` 四组规则；`.cat`（行内 chip）保留，改用于文件夹 chip。

### 5.3 `ui/app.js`（核心）

**状态与数据层**

| 对象 | 改动 |
|---|---|
| `hydrate()` | 增 `FOLDERS = D.folders || deriveFolders()`；`deriveFolders()` 为本地模式推导（7 预置 + `seedFolder` 归属），db 模式永远信后端 |
| `prefs` | 增 `view: 'grid'|'list'`（默认 `'grid'`）；`customCats`、`seeded` 废弃 |
| `state` | 增 `folder: null`（null=根，`'none'`=未归档，其余=folder id）；删 `cat` |
| `meta[key]` | cats 弃用；归属即 `meta[key].folder`（由 get_meta 下发） |
| `flushMeta()` | payload 从 `{tool, display, note, cats}` 改为 `{tool, display, note, folder}`（folder 取 `metaOf(key).folder || ''`） |
| `editedCount()` | 只算 `display || note`（与 `_edited_count` 对齐） |
| `seedIfNeeded()` | 仅本地模式：改为播种 `folder` 归属（复用原 `seedCat` 规则，函数更名 `seedFolder`）+ `migrateLocalFolders()`（§2.4 逻辑的本地版：直接在内存建夹） |
| `filterRows()/passesFilters()` | 删 `state.cat` 分支；`fallbackHay()` 的分类文本改为 `folderOf(key).name` |
| `sortRows()` | 排序键不变（display / updated）；网格文件卡与列表行共用 |

**渲染层**（原型对应函数可直接移植，把 `window.SKILLLOCKER_DATA` 换成 `SKILLS/FOLDERS` 全局）

| 原型函数 | 落地为 | 说明 |
|---|---|---|
| `renderSide()` | `renderFilters()` 重写 | 输出「全部技能 / 各文件夹（带计数、可投放）/ 未归档」；文件夹项带 `data-drop-folder` |
| `gridHtml()/folderCard()/fileCard()` | `renderGrid()` 系 | 仅 `state.board==='shared' && !state.folder && prefs.view==='grid'` 时走；搜索态输出结果文件卡流 |
| `crumbHtml()` | `renderCrumb()` | 进入文件夹 / 未归档时渲染于列表卡上方，含重命名 / 删除按钮与移出投放区 |
| `rowHtml(…, inFolder)` | `skillRowHtml()` 改造 | 第三列按场景输出：根列表=文件夹 chip（`data-jump-folder`），文件夹行与独有行=不输出（加 `in-folder` 类） |
| `listCardHtml()` | `renderList()` 内分支 | 表头列文案随分支切换；`updateFoot` 沿用 |
| `renderInspector()` | 分类块重写 | 「归属分类（单选）」：未归档 + 各夹 chip（`data-assign`）+ `＋新建分类`（`data-new-folder-for`）；删多选 picker 与 cat-add 行；独有 skill 不渲染该块 |
| `renderChrome()` | 文案与计数 | 标题/副标题按 根-网格 / 根-列表 / 文件夹 / 未归档 四态；`tab-shared-em`=「N 个分类文件夹」；`sb-health`=「N 共有 · M 分类」 |

**交互层**

| 功能 | 处理 |
|---|---|
| 视图切换 | `#view-seg` 点击 → `prefs.view` 持久化 → `renderChrome()+renderList(false)`；仅在 shared 看板显示（其他看板 `hidden`） |
| 进入 / 返回 | `data-folder-nav / data-folder-card / data-jump-folder / data-crumb-root` → `openFolder(id)`；进文件夹保留 `state.q` 但优先按夹内过滤——与原型一致：搜索时强制回根 |
| 文件夹 CRUD | 弹窗复用 `openModal` 骨架；db 模式调 `SKB.folderCreate/Rename/Delete` 后 `applyBootstrap(res)`；本地模式直接改 `FOLDERS`。删除走 `confirmModal`，文案「其中 N 个 skill 将回到未归档」 |
| ⋯ 菜单 | `openCtx/skillMenu/folderMenu` 移植；挂 `document` 冒泡处理（原型已踩平该坑：菜单在 body 上，不能用 root 委托）；Esc / 外点关闭 |
| 拖拽 | 5 个监听（dragstart/dragend/dragover/dragleave/drop）整段移植；drop 目标 = `[data-drop-folder]`（文件夹卡片 + 侧栏项）与 `[data-drop-unfile]`（面包屑根）；db 模式落库 `saveMeta`，本地改内存 |
| 键盘 | Esc 链：弹窗 → ⋯ 菜单 → 清搜索 → 退出文件夹；↑↓ 行导航跳过 `.folder-card/.file-card` 之外的场景不变（file-card 也带 `data-key` 可纳入） |
| `resetKey()` | 「还原」只清 display/note（`saveMeta` 写空值 + folder 不动），不再删整行 |
| `applyBootstrap()` | 保持 `state.folder`：若该夹已被删则回根；选中行丢失时回落到当前可见首行 |
| `setBoard()` | 离开 shared 时 `state.folder=null`；回来时回根 |

**删除清单**（一次性删净，防死代码）：`CATS/catById/catOf/allCats/catChip/CUSTOM_PALETTE/prefs.customCats/state.cat/toggleCat/addCategory`、`data-cat/data-toggle-cat/data-reset` 旧分支、`cat-add` 输入与回车处理、检查器多选块、独有行的分类 chip、UI 里全部「未分类」文案（改为「未归档」）。

---

## 6. 数据契约（快照增量）

```jsonc
// list_boards(...) / SKB.bootstrap()/rescan()/folder_*() 的 data 增：
"folders": [ { "id": "design", "name": "设计", "color": "violet", "position": 0 } ],

// get_meta(...) 每行增：
{ "folder": "design" }        // '' = 未归档；unique 行恒为 ''
```

`ui/data.js` 本地快照可后补 `folders` 字段；未补时本地模式走 `deriveFolders()` 推导，两条路径产物一致。

---

## 7. 验收

### 7.1 `--selftest` 新增断言（期望值一律后端现算，不写死）

| # | 名称 | JS 表达式 → 期望 |
|---|---|---|
| 1 | 网格视图渲染 | `document.querySelectorAll('.folder-card:not(.ghost)').length` = 后端 folders 数 |
| 2 | 新建虚卡存在 | `!!document.querySelector('#grid-new-folder')` = true |
| 3 | 切列表视图 | 点 `#view-seg [data-view-mode="list"]` 后 `.row` 数 = sh_total |
| 4 | 进入文件夹 | 点首个 `data-folder-card` 后 `#page-title` = 该夹名，`.row` 数 = 后端该夹计数 |
| 5 | 面包屑返回 | 点 `[data-crumb-root]` 后 `.folder-grid` 存在 |
| 6 | 归属落库 | `set_meta('shared', name, {folder: id})` 后查 `skill_meta.folder_id` = id；再传 `''` 复原 |
| 7 | 删夹回落 | `folder_delete(id)` 后原夹内行 `folder_id IS NULL`，卡片数 -1 |
| 8 | 迁移幂等 | 预置一条 `categories='["video"]'`、`folder_id=NULL` 的行 → 跑 `migrate_folders` → `folder_id='video'`；再跑一次不变 |

### 7.2 手工验收清单

- [ ] 首启升级：旧 7 分类变为 7 个同名文件夹，skill 归属与升级前分类一致；已删夹的 skill 未丢显示名 / 备注
- [ ] 网格 ⇄ 列表切换刷新后保持（prefs）；暗色下卡片 / 菜单 / 弹窗可读
- [ ] 拖拽三路：文件卡→文件夹卡、行→侧栏夹、行→面包屑移出；拖拽中高亮、松手 toast、计数即时刷新
- [ ] ⋯ 菜单：移动（含「当前」置灰）、移出、重命名、换色、删除（确认弹窗文案含 N）
- [ ] 检查器：单选 chip 即时移动 + 落库 + 行 chip 同步；「＋新建分类」创建后自动归入
- [ ] 搜索：网格下出结果卡流、列表下过滤行；Esc 逐级退出（弹窗→菜单→搜索→文件夹）
- [ ] 设置→「清空我的编辑」：显示名 / 备注清空、归属回预置夹、**自建文件夹保留**
- [ ] 独有 / 智能体看板不出现文件夹元素；重启后一切状态保持

---

## 8. 实施顺序（每步可独立验证）

1. **后端数据层**：`db()` 建表 + `migrate_folders` + get/set_meta 增量 → `python -c` 直调验证迁移与幂等。
2. **后端 CRUD**：`folder_rows/create/rename/delete` + `list_boards.folders` + `reseed_meta` 新语义 + `APP_VERSION`。
3. **Api + bridge**：`desktop.py` 三方法、`bootstrap()` 挂迁移、`bridge.js` 三包装。
4. **静态层**：`index.html`（分段控件 / 雪碧图 / stat id）+ `styles.css`（追加块 + 差异三点 + 删旧 cat 样式）。
5. **app.js 渲染**：状态层 → `renderChrome/renderFilters/renderList/renderInspector`（此时旧交互先断开也能看）。
6. **app.js 交互**：CRUD 弹窗、⋯ 菜单、拖拽、视图切换、Esc 链、`applyBootstrap` 兜底。
7. **自检**：§3.3 四处修改 + §7.1 八项断言，跑 `python desktop.py --selftest` 全绿。
8. **文档收口**：`UI-SPEC.md` 头部加取代注记（指向本文）、§3.3/§4/§6.2 按新模型改写；`DESKTOP.md` 自检项数更新；`data.js` 快照补 `folders`。

## 9. 风险与回滚

| 风险 | 缓解 |
|---|---|
| 迁移误伤用户编辑 | 只读 `categories[0]` 做回填，不改写该列；`prune_meta` 的 scanDegraded 跳过逻辑继续生效 |
| 旧代码回滚 | `folders` 表与 `folder_id` 列对旧代码不可见；`categories` 列从未被改写 → 回滚后分类原样恢复 |
| 清空编辑误删夹 | `reseed_meta` 新语义不触碰 `folders` 表 |
| 保存竞态 | 归属与改名 / 备注同走 `set_meta`（单行 upsert），沿用现有「不去抖直写」策略 |
| WebView2 缓存 | 资源 `?v=mtime` 机制自动生效，无需手动清缓存 |
