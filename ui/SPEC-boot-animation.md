# skilllocker · 启动加载动画（方案 A · 徽章汇入）— 实施规格

> 版本：v0.2.1　交互基准：[`design-boot-animation.html`](design-boot-animation.html)（方案 A · 双击打开可重播），动效参数以本文为准
> 交付目录：`D:\skilllocker\ui\`　设计稿尺寸：1440 × 900（桌面默认窗口 1200 × 760，最小 1180 × 720）
> 关联：`UI-SPEC.md` §2.4（缓动/时长 token）、§7（响应式与状态）、`kit/DESKTOP.md`（桌面壳）

---

## 0. 目标与不做的事

| 项 | 结论 |
|---|---|
| 目标 | 窗口首帧 → 首个看板渲染之间，显示全窗口 boot overlay；完成后 300ms 淡出，主 UI 六大块按 `rowIn` 同款缓动级联进场 |
| 层级定位 | **纯展示层**：不改数据流、不加表、`bridge.js` / `data.js` 零改动；overlay `pointer-events:none`，全程不阻塞标题栏拖动与窗口三键 |
| 不做：后端进度回报 | 重扫描（junction 探测等）发生在窗口出现之前；窗口期只有一次 `bootstrap()` 快查（SQLite，实测 <300ms），编排时钟足够。未来若把重活搬进窗口期，再议 `boot_progress()` 轮询 |
| 不做：刷新复用 | 顶栏「刷新」（`rescan`）沿用现有 `.progress` 细条 + 按钮转圈，不显示 boot overlay |
| 不做：托盘/常驻 | 与 `kit/DESKTOP.md` 边界一致，不新增常驻逻辑 |

---

## 1. 真实启动时序（对接依据）

```
python 启动                     launch 日志
  └ import pywebview + 建窗       ~4–5s（WebView2 初始化，窗口尚未出现/未绘制）
      └ index.html 首帧           ← overlay 在此已可见（纯 CSS 入场，无需 JS）
          └ bridge ready           pywebviewready（首帧后很快）
              └ SKB.bootstrap()    Api.bootstrap：db() + load_config + migrate_folders + seed_meta + list_boards
                  └ hydrate + start()  首次 renderAll → 可交互（记为 R）
```

- `app.js` 的 `boot()` 有三类落点：db 正常；**桌面壳里桥没接上**（`?desktop=1` 但 `SKB.ready`
  拿到 null）→ 自动整页重载一次（`bridgeRetry=1`），仍失败就 `MODE='error'` 走启动错误页，
  **绝不**降级 `bootLocal`；非桌面壳（file:// 直开原型）才降级 `bootLocal`。
  注入窗口分两轮：首轮 20s、重试轮 8s（`bridge.js`）。实测从打开到错误页 ≈28s
  （2026-11-08 三轮 28.00 / 28.43 / 28.44s；两轮都 20s 时是 ≈40s）。
  **overlay 在所有路径都必须结束**（见 §5 兜底）。
- 实测 R ≈ 首帧后 0.6–1.2s；`applyBootstrap`（`app.js:2858`）同时服务刷新，**不得**在它里面触发 boot 相关逻辑。

---

## 2. DOM（index.html 增量，约 25 行）

位置：`.window` 内、`.app-body` 之前；**静态写入**（覆盖首帧，禁 JS 也显示静态层）。`.window` 已是 `position:relative`（styles.css:169），overlay 直接 `absolute; inset:0`。

```html
<!-- 在 <div class="progress" id="progress"><i></i></div> 之后 -->
<div class="boot" id="boot" role="status" aria-label="正在加载技能库">
  <div class="boot-logo" aria-hidden="true">
    <svg viewBox="0 0 24 24" width="30" height="30">
      <rect x="4" y="4" width="6.5" height="6.5" rx="2" fill="#fff" fill-opacity=".92"></rect>
      <rect x="13.5" y="4" width="6.5" height="6.5" rx="2" fill="#fff" fill-opacity=".54"></rect>
      <rect x="8.75" y="13.5" width="6.5" height="6.5" rx="2" fill="#fff" fill-opacity=".76"></rect>
      <path d="M10.4 8.6 12 12.1" stroke="#fff" stroke-width="1.4" stroke-opacity=".8"></path>
    </svg>
  </div>
  <div class="boot-word">skilllocker</div>
  <div class="boot-eyebrow">SKILL LIBRARY · V0.2.1</div>
  <div class="boot-chips" aria-hidden="true"></div>
  <div class="boot-agents" aria-hidden="true"></div>
  <div class="boot-foot">
    <div class="boot-phrow"><span class="boot-ph">正在启动</span><span class="boot-cnt">0 / 51</span></div>
    <div class="boot-line">kernel ready</div>
    <div class="boot-track"><i></i></div>
  </div>
</div>
```

同时给窗口根补初始态类：`<div class="window booting" id="app-window">`。

状态约定：

| 类 | 挂载点 | 含义 |
|---|---|---|
| `#boot.run` | overlay | 编排时钟已启动（logo/chips/foot 的入场动画都挂在 `.run` 下，保证重播/重启可复现） |
| `#boot.ok` | overlay | 就绪相位（logo 环、阶段名转 `--success`） |
| `#boot.done` | overlay | 淡出完成，300ms 后 JS 置 `hidden` 回收渲染层 |
| `.window.booting` | 窗口 | 主 UI 各块隐藏（级联初始态），HTML 静态带上 |
| `.window.ready` | 窗口 | 触发级联进场（移除 `booting` 的同时加上） |

z-index：overlay `60`，在 `.grips`(90) 与 `win-controls`(100) 之下；配合 `pointer-events:none`，启动中可拖窗、可点三键、可 Esc。

---

## 3. 样式（styles.css 增量，约 130 行）

全部复用现有 token：`--bg --surface --surface-2 --line --ink-2 --ink-3 --accent --success --ease --spring --display --mono`；深色由 `body.dark` 自动成立，唯一不变的是 logo 渐变（品牌色）。

### 3.1 keyframes

| 名 | 定义 | 用途 |
|---|---|---|
| `bootPop` | `from{opacity:0; transform:scale(.7) translateY(6px)}` | logo 弹入，480ms `--spring` |
| `bootRing` | `0%{scale(.86);opacity:.55} 70%,100%{scale(1.26);opacity:0}` | logo 外环脉冲，2.2s 循环；`.ok` 时边框转 `--success` |
| `bootShine` | 高光从 130% 扫到 -130% | logo 表面扫光，2.6s 循环 |
| `bootFadeUp` | `from{opacity:0; transform:translateY(8px)}` | word/eyebrow/chips/agents/foot 依次入场，360ms，延迟 80/120/160/200ms |
| `bootChipIn` | `from{opacity:0; transform:translateY(8px) scale(.7)}` | chip 弹入，280ms `--spring` |
| `bootChipOut` | `to{opacity:0; transform:scale(.8)}` | chip 移除，200ms |
| `bootSlide` | `translateX(-70px) → 270px` | 方案 A 未用（预留） |

### 3.2 结构要点

| 元素 | 规格 |
|---|---|
| `.boot` | `position:absolute; inset:0; z-index:60; pointer-events:none; background:var(--bg); flex 纵向居中`；背景叠两团径向渐变（accent 7% / teal 6%），`::before` 实现 |
| `.boot.done` | `opacity:0; visibility:hidden; transition: opacity 300ms var(--ease), visibility 0s linear 300ms` |
| `.boot-logo` | 64 × 64，圆角 14，`linear-gradient(135deg,#3d7bff,#2e63f6 55%,#12c2cf)`（与 `.logo` 同款）；投影 `0 12px 32px accent 32%`；`::after` 外环 `-7px` 圆角 19 |
| `.boot-word` | 21px `--display` 600，字距 -0.02em；`.boot-eyebrow` 9.5px 600 字距 0.2em `--ink-3` |
| `.boot-chips` | 高 60（两行 26px chip + 6 gap），宽 `min(520px,84%)`，flex wrap 居中，`overflow:hidden`，底部 `mask-image` 线性渐隐 |
| `.boot-chip` | 高 26，圆角 7，`--surface` 底 + `--line` 边；头部 20 × 20 圆角 6 monogram 块，底色 `color-mix(in srgb, var(--c) 14%, transparent)`，文字 `var(--c)`（`--c` 按分类内联：`--accent/--violet/--rose/--teal/--warning/--slate`）。旧 WebView2 兜底：color-mix 不可用时退化为 `--surface-3` 底 + `--ink-2` 字（@supports 即可，非必需） |
| `.boot-agents` | 7 个 26 × 26 圆角 8 块，mono 9px；未点亮 `--surface-2` + `--line`；`.on`：`--success` 字 + `color-mix(success 45%)` 边 + 12% 底 + `translateY(-2px)` |
| `.boot-foot` | 宽 `min(340px,80%)`；`.boot-ph` 10.5px 600 字距 0.14em；`.boot-cnt` mono 10.5 `tabular-nums`；`.boot-line` mono 10.5 单行省略；`.boot-track` 高 3 圆角 2，底 `color-mix(ink 9%)`；`.boot-fill` 渐变 `linear-gradient(90deg, var(--accent), #12c2cf)`（与 `.progress i` 同款），width 过渡 280ms |
| 级联 | `.window [data-boot-rv]{transition: opacity/transform 220ms var(--ease); transition-delay: calc(var(--rv)*12ms)}`；`.window.booting [data-boot-rv]{opacity:0; translateY(3px); transition:none}` |

### 3.3 状态适配

| 条件 | 行为 |
|---|---|
| `body.dark` | token 自动；验证 chip/agent 软底在深色下可读（`color-mix` 基于亮色分量，深色下对比足够） |
| `body.reduce-motion` / `prefers-reduced-motion` | 全局已有 0.001ms 规则 → 动画静止；JS 侧同步走「即时模式」（§5），静态层只停留到 R |
| ≤1200 | chips/bfoot 百分比宽度自适应，无横向溢出；不随 compact 改尺寸（无列表行） |
| 1180 × 720 最小窗口 | 居中构图自适应，无裁切（验收项） |

---

## 4. 编排时间轴（1× 满速，总长 4.94s）

| t(ms) | 阶段 `.boot-ph` | 日志行 `.boot-line` | 进度 | 动作 |
|---|---|---|---|---|
| 0 | 正在启动 | `kernel ready · python {py}` | 6% | logo pop |
| 460 | 打开中央仓库 | `{store} · {storeSize}` | 14% | — |
| 860 | 扫描技能目录 | `scan store … {N} skills · {U} unique` | 30% | chips 开始（间隔 118ms）；计数器 easeOutCubic 0→N（860→2960） |
| 2060 | 解析 SKILL.md | `parse {name}/SKILL.md → {hash12}` | 60% | 每 150ms 换一条真实 skill 的 name + hash |
| 2960 | 探测 agent 分发 | `link {tool} → {dir} … ok` | 78% | 清空 chips；marks 逐个点亮（135ms/个）；agents 数点亮完即止 |
| 3960 | 载入分类与元数据 | `folders ×{F} · meta ×{M} · prefs ok` | 94% | — |
| 4420 | 就绪 | `{N} 个技能就绪 · {M} 个 agent 已连接` | 100% | `.ok`（外环/阶段名转 success） |
| 4940 | — | — | — | reveal（§6） |

数据来源（`hydrate` 已拿到真实 payload）：

| 显示 | 字段 |
|---|---|
| 计数器 N / 就绪行 | `data.meta.sharedCount` |
| unique 数 | `data.meta.uniqueCount` |
| agent marks + M | `data.tools` 中 `enabled` 的 `mark`（CC CX DS QD TR WB ZC，按 tools 顺序）与 `meta.agentCount` |
| folders F | `data.folders.length` |
| chips / parse 日志 | `data.skills` 的 `name` / `hash`（A→Z 排序取用；chips 是装饰，用 `data.js` 快照预跑，真实数据到手后仅改计数目标，**刻意接受短暂不一致**） |

---

## 5. 与真实就绪对齐（核心规则）

`R` = bootstrap 成功且 `start()` 首帧渲染完成的时刻。编排时钟为主，真实 R 只负责「提前收尾 / 延后收尾」：

| 情形 | 行为 |
|---|---|
| R < 4420ms（常态） | R 时刻起把剩余时间轴提速：`timescale = clamp(剩余ms / 1200, 1, 4)` → 剩余部分约 1.2s 走完，总时长 ≈ `max(R + 1.2s, 1.8s)`（实测 R≈0.8s → 全程约 2.0–2.4s，不比 v0.2.0 慢多少） |
| R ≥ 4420ms（慢机/大库） | 时间轴走完后停在「载入分类与元数据」94% 等待；R 到达后 600ms 内完成 就绪 → reveal |
| R > 8s（桥卡死） | 分两种情况。**桌面壳里桥还没落地**（`boot()` 已把 `SKB.ready` 交给 `BootFX.awaitBridge`）：不 reveal，停在「载入分类与元数据」94%，相位改「等待桌面端」、逐秒报「正在连接桌面端… 已等待 N 秒」；等待态自带 26s 绝对兜底（首轮注入窗口 20s + 余量），兜底到点或桥判死后交回原逻辑。**其余情况**（桥已接上、只是 bootstrap 慢）：仍按原硬上限强制走就绪 + reveal，overlay 永不卡死。实测（不注入 pywebview）：t≈10s 与 t≈17s 采样均为 `#boot` 未 hidden、`#app-window` 仍带 `booting`、rows=0；去掉等待态则同一时刻是 `hidden=True booting=False` + 「51 个技能就绪」的空壳 |
| 失败/降级路径 | `bootstrap` 失败（桌面壳里）与 pywebview 注入超时：先 `BootFX.fail()` 收尾 overlay，再由 `renderBootError` 接管整页 —— 错误页是 `position:fixed` 覆盖层，不是 `bootLocal`。非桌面壳的原型才走 `bootLocal`。三条路径 overlay 都必然消失 |
| `?noboot=1` | `BootFX.disable()`：不启动编排，HTML 静态移除 overlay（JS 置 hidden），行为与 v0.2.0 完全一致 |
| reduce-motion | 同 disable，但允许静态层显示到 R 后 200ms 再移除（避免完全无过渡） |
| file:// 原型 | 走 1× 满速完整编排（可展示用）；`?noboot=1` 可关 |

`BootFX` 接口（`app.js` 内新模块，约 150 行，一次性，`done` 标志防重入——刷新走 `applyBootstrap` 不会重触发）：

```js
BootFX.begin()          // boot() 入口处调用；读 URL/reduced-motion 决定 disable 或起时钟
BootFX.data(boardsData) // hydrate() 内调用；填 N/M/F 目标与 agent marks（计数器平滑改目标）
BootFX.ready()          // start() 返回后（boot() 的 db 分支与 bootLocal 内各一处）
BootFX.fail()           // 两个 catch 分支；等价 ready()
BootFX.disable()        // noboot / reduce-motion
```

改动点（4 处调用 + 1 处模块）：`app.js:2883 boot()`、`app.js:2894-2898`（db 成功分支）、`app.js:2899-2903`（catch）、`app.js:2872 bootLocal()`、`app.js:24 hydrate()`。

---

## 6. reveal（主界面进场）

1. `start()` 完成且编排走到 reveal 时刻：`#boot` 加 `.done`；`.window` 移除 `booting`、加 `ready`。
2. 六大块 + 看板行按 `data-boot-rv` 锚点级联（220ms `--ease`，每块 +12ms，总铺开 ≤120ms）：

| 锚点 | --rv |
|---|---|
| `.titlebar` | 0 |
| `.sidebar` | 1 |
| `.topbar` | 2 |
| `.boards` | 3 |
| `.content`（看板下内容根） | 4 |
| `.inspector` | 5 |
| `.statusbar` | 6 |

3. 列表行的既有 `rowIn` 保持原样——它们在 overlay 覆盖下完成，不感知、不重排。
4. 300ms 后 JS 把 `#boot` 置 `hidden`（回收渲染层）；`display:none` 即可，无需移除 DOM。

---

## 7. 状态矩阵与回归

| 场景 | 处理 |
|---|---|
| 桌面 `--selftest` | `desktop.py:859` 的 URL 拼接增加 noboot：`"?desktop=1" + ("&noboot=1" if noboot else "") + "&v=" + _asset_version()`，`build_window(..., noboot=args.selftest)`——既有 52 项断言零影响。另加一轮「带 boot 启动」断言（≥3 项）：`#boot` 首帧存在 → `start()` 后 `#boot.done` / `.window.ready` → `#boot` 最终 `hidden` |
| 原型 Playwright 回归（32 项） | 用例 URL 统一加 `?noboot=1`；另加 6 项 boot 专项：chips 上限 14、计数器终值 = sharedCount、agent `.on` 数 = agentCount、reveal 类名时序、深色切换、1180 无溢出 |
| 资源缓存 | overlay 在 `index.html` 本体，无缓存问题；css/js 新 mtime 自动进 `_asset_version()` |

---

## 8. 改动清单

| 文件 | 改动 | 量 |
|---|---|---|
| `ui/index.html` | overlay 静态 markup + `.window` 加 `booting` 类 + 主块加 `data-boot-rv` | +30 行 |
| `ui/styles.css` | §3 全部样式与 keyframes（文件尾新分区 `/* ===== boot ===== */`） | +130 行 |
| `ui/app.js` | `BootFX` 模块 + 4 个调用点 | +150 行 |
| `kit/desktop.py` | selftest URL 加 `noboot=1`（859 / 904 两行） | 2 行 |
| `ui/bridge.js` / `ui/data.js` | 零改动 | — |
| `ui/UI-SPEC.md` | §0 交付物表加一行本规格；§2.4 动效表加 boot 相关键名 | +3 行 |

---

## 9. 验收清单

- [ ] 首帧即见 overlay（含禁 JS 场景：静态层可见，浏览器行为不崩）
- [ ] db 冷启动：首帧 → reveal 总时长 ∈ [1.8s, 2.6s]（bootstrap 正常时）
- [ ] 计数器终值、agent 点亮数、就绪行数字与真实库一致（49+2 库 = 49 / 6 或 7，随 `enabled` 浮动）
- [ ] bootstrap 人为卡死 8s：强制 reveal，期间标题栏可拖、三键可点
- [ ] 失败/降级路径（pywebview 超时、bootstrap 异常、data.js 未加载）overlay 必然消失
- [ ] 深色 / 紧凑 / reduce-motion / 1200×760 / 1180×720 无溢出无裁切，0 条 console 错误
- [ ] `?noboot=1` 下与 v0.2.0 行为逐项一致：原型回归 32 项 + selftest 52 项全绿；新增 boot 断言通过
- [ ] 刷新（rescan）不出现 overlay；连续启动重复观感一致

---

## 10. 已知取舍

1. **编排时钟为主、真实数据只填数字**：chips 流用 `data.js` 快照名预跑（装饰性），计数与就绪行用真实 payload——两者可能短暂不一致，刻意接受；若未来把重扫描搬进窗口期，再加 `boot_progress()` 轮询接口。
2. **首帧之前的 ~4–5s 白屏等待覆盖不到**：那是 WebView 建窗期，发生在页面绘制前。若要消除，需桌面壳提前建窗或加原生 splash，另行立项，不在本规格范围。
3. **加速规则让后段动画最快 4×**：chip 弹入在高倍速下会有跳跃感，属于刻意取舍——保证「数据早到就早收尾」优先于动画完整性；不接受可改为固定 2.4s 紧凑时间轴（备用方案，改动只在 PHASES 表）。
4. **reduce-motion 下的静态层**只有 logo + 文案 + 0% 进度条，不做骨架屏——启动期主 UI 本就未渲染，无内容可占位。
