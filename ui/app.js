/* ============================================================
   skilllocker · Skill Library — logic
   桌面端：bridge.js → pywebview → skilllocker.py，数据与编辑直接走 SQLite。
   原型：file:// 打开时退回 data.js 快照 + localStorage。
   ============================================================ */
(function () {
  'use strict';

  var SKB = window.SKILLLOCKER_BRIDGE || null;
  var D = null;
  var MODE = 'local';
  var META = {};
  var SKILLS = [];
  var TOOLS = [];
  var ENABLED = [];
  var UNIQUE = [];
  // 独有看板的逻辑技能组（同名同内容多 agent 副本 = 1 组 N 实例）；
  // 后端 list_boards 给 unique_groups，原型快照没有时按名字本地兜底分组。
  var UNIQUE_GROUPS = [];
  var AGENTS = [];
  var FOLDERS = [];
  var CONFLICT = null;
  // 首启引导块（后端 list_boards 在仓库为空时给，见 kit/skilllocker_boards.py）
  var FIRST_RUN = null;
  var firstRunDismissed = false;
  var firstRunSig = '';
  var firstRunLogged = false;
  var frBusy = false;   // 接管 / 收编进行中：按钮禁用并显示「处理中」，避免重复点击

  var STORE = 'D:\\skilllocker\\store';
  var SCAN = '22:16';

  function hydrate(data) {
    // 这一轮快照没带独有技能数据时，界面沿用上一轮的行 —— 不是「没有独有」，
    // 是「还没扫」。两个来源：两段式刷新的第一段（uniqueDeferred + uniquePending，
    // 第二段 rescan_unique 回来后带完整快照覆盖），以及开关切换的回包
    // （只带 uniqueDeferred：切换不增删技能，也没有第二段会来补扫，
    //  所以「独有扫描中…」的标记必须是 false，否则它在界面上永远不走）。
    var pending = !!(data && (data.uniqueDeferred ||
                              (data.meta && data.meta.uniquePending)));
    var prevU = pending ? UNIQUE : null;
    var prevG = pending ? UNIQUE_GROUPS : null;
    var prevC = pending ? CONFLICT : null;
    var prevF = pending ? FIRST_RUN : null;
    D = data || {};
    META = D.meta || {};
    SKILLS = D.skills || [];
    TOOLS = D.tools || [];
    ENABLED = TOOLS.filter(function (t) { return t.enabled; });
    UNIQUE = pending ? prevU : (D.unique || []);
    UNIQUE_GROUPS = pending ? prevG : (D.unique_groups || localUniqueGroups(UNIQUE));
    AGENTS = D.agents || [];
    CONFLICT = pending ? prevC : ((D.conflict && D.conflict.skill) ? D.conflict : null);
    // 首启引导：后端只在「仓库里一个技能都没有」时给这个块，接管 / 收编成功后
    // 下一次快照自然为 null，面板随之消失；内容变化时清掉「以后再说」的忽略态
    var frNext = pending ? prevF : (D.firstRun || null);
    var frSig = frNext ? JSON.stringify([frNext.candidate && frNext.candidate.store,
                                        (frNext.adoptable || []).length]) : '';
    if (frSig !== firstRunSig) { firstRunSig = frSig; firstRunDismissed = false; firstRunLogged = false; }
    FIRST_RUN = frNext;
    STORE = META.store || STORE;
    SCAN = META.scanAt || SCAN;
    // 桌面端永远信后端快照；本地模式的文件夹定义以 prefs（含自建夹）为准
    FOLDERS = D.folders || [];
    if (MODE !== 'db') FOLDERS = localFolders();
    BootFX.data(data); // 就绪相位的目标数与 agent 点亮名单（刷新路径 BootFX 已收尾，自动跳过）
  }

  // agent key -> 显示名；共享目录提示 / 详情栏共读行都用它
  function toolLabelOf(key) {
    for (var i = 0; i < TOOLS.length; i++) if (TOOLS[i].key === key) return TOOLS[i].label || key;
    return key;
  }

  /* ---------------------------------------------------------- storage */
  var META_KEY = 'skilllocker.library.meta.v1';
  var PREF_KEY = 'skilllocker.library.prefs.v1';

  function lsGet(key, fallback) {
    try {
      var raw = localStorage.getItem(key);
      return raw ? JSON.parse(raw) : fallback;
    } catch (e) {
      return fallback;
    }
  }

  function lsSet(key, value) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
      return true;
    } catch (e) {
      return false;
    }
  }

  function lsDel(key) {
    try { localStorage.removeItem(key); } catch (e) {}
  }

  /* ---------------------------------------------------------- 分类文件夹 */
  // 单归属模型：一个 skill 最多属于一个文件夹（meta[key].folder），
  // '' = 未归档。旧「多选 cats」系统已废弃（prefs.customCats / meta.cats 不再读写）。
  var PRESET_FOLDERS = [
    { id: 'design', name: '设计', color: 'violet' },
    { id: 'frontend', name: '前端', color: 'accent' },
    { id: 'doc', name: '文档', color: 'warning' },
    { id: 'video', name: '视频', color: 'rose' },
    { id: 'memory', name: '记忆', color: 'teal' },
    { id: 'eng', name: '工程', color: 'slate' },
    { id: 'search', name: '检索', color: 'green' }
  ];

  var PALETTE = ['violet', 'accent', 'warning', 'rose', 'teal', 'slate', 'green'];

  var COLORS = {
    violet: ['var(--violet)', 'var(--violet-soft)'],
    accent: ['var(--accent)', 'var(--accent-soft)'],
    warning: ['var(--warning)', 'var(--warning-soft)'],
    rose: ['var(--rose)', 'var(--rose-soft)'],
    teal: ['var(--teal)', 'var(--teal-soft)'],
    slate: ['var(--slate)', 'var(--slate-soft)'],
    green: ['var(--success)', 'var(--success-soft)']
  };

  var prefs = lsGet(PREF_KEY, null) || {
    theme: 'light',
    density: 'comfortable',
    motion: true,
    showDisabled: true,
    sort: 'name',
    board: 'shared',
    views: { shared: 'grid', unique: 'grid' }
  };
  // 旧版单一 prefs.view 只服务于共有看板；网格视图上线到独有看板后按看板各存
  // 一份。旧值迁给共有，独有首次默认网格；非法值一律拉回网格。
  if (prefs.view != null) {
    prefs.views = { shared: prefs.view === 'list' ? 'list' : 'grid', unique: 'grid' };
    delete prefs.view;
  }
  if (!prefs.views || typeof prefs.views !== 'object') {
    prefs.views = { shared: 'grid', unique: 'grid' };
  }
  if (prefs.views.shared !== 'list') prefs.views.shared = 'grid';
  if (prefs.views.unique !== 'list') prefs.views.unique = 'grid';

  function viewOf(board) {
    return prefs.views[board] === 'list' ? 'list' : 'grid';
  }

  var meta = {};
  var dirtyKeys = {};

  function savePrefs() { lsSet(PREF_KEY, prefs); }

  // 本地模式的文件夹定义：prefs.folders（用户自建夹在这里持久化）优先，
  // 其次快照自带，最后退回 7 个预置夹
  function localFolders() {
    if (Array.isArray(prefs.folders) && prefs.folders.length) return prefs.folders;
    if (D && D.folders && D.folders.length) return D.folders;
    return PRESET_FOLDERS.map(function (f, i) {
      return { id: f.id, name: f.name, color: f.color, position: i };
    });
  }

  // 桌面端：编辑落到 skilllocker.db 的 skill_meta 表；原型（file://）：写 localStorage。
  // 写库不做去抖：单机 SQLite 写入很轻，且避免隐藏窗口被节流时丢编辑。
  function scopeOf(key) { return key.indexOf('/') > -1 ? 'unique' : 'shared'; }

  function setMetaMap(map) { meta = map || {}; }

  // pywebview 每个 js_api 调用各跑一个线程，「后发先至」不成立：快速连续
  // 输入时旧 payload 可能晚于新 payload 落库（最后 commit 者胜），重载后就是
  // 「丢编辑」。所以同一个 key 一次只放一个请求在飞，在飞期间的新编辑保持
  // 脏标记，等在飞的落库后再补发最新值 —— 落库顺序与编辑顺序一致。
  var metaInflight = {};   // key -> 在飞的 saveMeta promise
  var metaRetries = {};    // key -> 已自动重试次数
  var metaRetryTimer = null;
  var META_RETRY_MAX = 3;
  var META_RETRY_MS = 3000;

  function saveMeta(key) {
    if (MODE === 'db') {
      if (key) dirtyKeys[key] = 1;
      flushMeta();
      return true;
    }
    return lsSet(META_KEY, meta);
  }

  function metaPayload(key) {
    var m = meta[key] || {};
    return {
      tool: scopeOf(key) === 'unique' ? key.split('/')[0] : '',
      display: m.display || '',
      note: m.note || '',
      folder: scopeOf(key) === 'shared' ? (m.folder || '') : '',
      starred: m.starred ? 1 : 0
    };
  }

  function sendMetaSave(key) {
    var p = SKB.saveMeta(scopeOf(key), key, metaPayload(key));
    metaInflight[key] = p;
    p.then(function () {
      if (metaInflight[key] === p) delete metaInflight[key];
      delete metaRetries[key];
      // 在飞期间的编辑：顺着这次落库补发，顺序才有保证
      if (dirtyKeys[key]) {
        delete dirtyKeys[key];
        sendMetaSave(key);
      }
    }, function (err) {
      if (metaInflight[key] === p) delete metaInflight[key];
      var n = (metaRetries[key] || 0) + 1;
      metaRetries[key] = n;
      if (n > META_RETRY_MAX) {
        delete metaRetries[key];
        toast('bad', '保存失败', String(err && err.message || err));
        return;
      }
      // 失败的 key 回写脏标记，稍后随下一轮 flush 重试（成功后清除）。
      // 注意：flushMeta 发送前已 delete，这里不回写就永远等不到重试。
      // 放弃重试的分支（上面）不回写 —— 已弹「保存失败」，别再无限留脏。
      dirtyKeys[key] = 1;
      if (!metaRetryTimer) {
        metaRetryTimer = setTimeout(function () {
          metaRetryTimer = null;
          flushMeta();
        }, META_RETRY_MS);
      }
    });
  }

  function flushMeta() {
    if (MODE !== 'db' || !SKB) return;
    Object.keys(dirtyKeys).forEach(function (key) {
      if (metaInflight[key]) return; // 在飞：保持脏标记，落库后自动补发
      delete dirtyKeys[key];
      sendMetaSave(key);
    });
  }

  var state = {
    board: /^(shared|unique|agents)$/.test(prefs.board || '') ? prefs.board : 'shared',
    folder: null, // null=根；'none'=未归档；其余=文件夹 id
    agentStatus: 'all',
    q: '',
    selected: null
  };

  function insScrollTop() {
    var ins = document.getElementById('ins-body');
    if (ins) ins.scrollTop = 0;
  }

  // state 的写入全部收敛到 setState：按变更的键决定渲染范围，调用方不再
  // 自行拼 renderChrome / renderContent / renderInspector 组合——漏刷一处
  // 就是「界面撒谎」类 bug。返回是否发生了变更（= 发生了渲染）。
  // 不走这里的三类例外：renderList 的选中自愈（渲染期修正，走 setState 会
  // 递归渲染）、applyBootstrap / start 的水合初始化、showResult /
  // requestDelete 在水合前写下的「选中意向」（applyBootstrap 会再校验）。
  function setState(patch, opts) {
    opts = opts || {};
    var keys = Object.keys(patch).filter(function (k) { return state[k] !== patch[k]; });
    if (!keys.length) return false;
    var board = keys.indexOf('board') > -1;
    var folder = keys.indexOf('folder') > -1;
    var filterReset = keys.indexOf('q') > -1 || keys.indexOf('agentStatus') > -1;
    var sel = keys.indexOf('selected') > -1;
    keys.forEach(function (k) { state[k] = patch[k]; });

    if (board) {
      // 换看板 = 换数据集：归属导航 / 状态筛选 / 搜索一并复位（归属导航只在
      // 共有看板有意义），搜索框与选中同步回落到新看板第一行
      prefs.board = state.board;
      savePrefs();
      if (!folder) state.folder = null;
      if (keys.indexOf('agentStatus') < 0) state.agentStatus = 'all';
      if (keys.indexOf('q') < 0) {
        state.q = '';
        var search = document.getElementById('gsearch');
        if (search) search.value = '';
      }
      if (!sel) {
        var rows = currentRows();
        state.selected = rows.length ? rows[0].key : null;
      }
      renderChrome();
      renderContent(true);
      renderInspector();
      var body = document.getElementById('list-body');
      if (body) body.scrollTop = 0;
      return true;
    }
    if (folder) closeCtx(); // 进出文件夹时收起 ⋯ 菜单（原 openFolder 语义）
    if (folder || filterReset) {
      renderChrome();
      renderContent(false);
      // 进出文件夹会改变「归属 chip」的语境，检查器总是刷新；仅搜索 / 状态
      // 筛选变化时原本就不刷——选中若失效，renderList 的自愈会兜住
      if (folder || sel) renderInspector();
      if (sel && opts.insTop) insScrollTop();
      return true;
    }
    // 纯选中变化：内容区高亮 + 检查器
    renderContent(false);
    renderInspector();
    if (opts.insTop) insScrollTop();
    return true;
  }

  /* ---------------------------------------------------------- helpers */
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function icon(id, cls) {
    return '<svg class="icon ' + (cls || '') + '" aria-hidden="true"><use href="#i-' + id + '"></use></svg>';
  }

  function mark(name) {
    var s = String(name || '').replace(/[^a-z0-9]/gi, '');
    return (s.slice(0, 3) || '?').toUpperCase();
  }

  function catVars(color) {
    var pair = COLORS[color] || COLORS.slate;
    return '--c:' + pair[0] + ';--cs:' + pair[1];
  }

  // agent 文件夹的颜色：agent 夹不可自选色，按 key 稳定散列到调色板，
  // 同一个 agent 在网格卡、面包屑、侧栏导航里永远同色
  function agentColor(key) {
    var s = String(key || '');
    var h = 0;
    for (var i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    return PALETTE[h % PALETTE.length];
  }

  function folderById(id) {
    if (!id) return null;
    for (var i = 0; i < FOLDERS.length; i++) if (FOLDERS[i].id === id) return FOLDERS[i];
    return null;
  }

  // 行内文件夹胶囊（可点击进入文件夹）；未归档显示虚线「未归档」
  function folderChipHtml(fid) {
    var f = folderById(fid);
    if (!f) return '<span class="cat none">未归档</span>';
    return '<button class="cat" data-jump-folder="' + esc(f.id) + '" style="' + catVars(f.color) +
      '" title="打开「' + esc(f.name) + '」"><i></i>' + esc(f.name) + '</button>';
  }

  function metaOf(key) {
    if (!meta[key]) meta[key] = { display: '', note: '', folder: '', updated: 0 };
    return meta[key];
  }

  function displayOf(key, fallback) {
    var m = meta[key];
    return (m && m.display) ? m.display : fallback;
  }

  // 只有「显示名 / 备注被改过」才算用户编辑；归属夹不在内（与后端
  // desktop._edited_count 同口径，预置归属不算编辑）。
  function editedCount() {
    return Object.keys(meta).filter(function (k) {
      var m = meta[k];
      return !!(m && (m.display || m.note));
    }).length;
  }

  /* ---------------------------------------------------------- 收藏（v0.3.1） */
  // 星标只存 meta（桌面=skill_meta.starred，原型=localStorage 同一张 map），
  // 不碰文件、不分发；key 随 meta，改名 / 移动分类 / 重链都不丢。
  function isStarredKey(key) { return !!(meta[key] && meta[key].starred); }

  function starredAtOf(key) { return (meta[key] && meta[key].starredAt) || 0; }

  function starCount() {
    var n = 0;
    Object.keys(meta).forEach(function (k) { if (meta[k] && meta[k].starred) n++; });
    return n;
  }

  // 收藏视图 = 共有看板里的一个虚拟夹（folder='starred'）：看板机制、面包屑、
  // Esc 退出全部复用，独有收藏以 agent 分组追加在共有之后。
  function isStarredView() { return state.board === 'shared' && state.folder === 'starred'; }

  function starredRows() {
    var s = sortRows(filterRows(sharedRows().filter(function (r) { return isStarredKey(r.key); })));
    var u = sortRows(filterRows(uniqueRows().filter(function (r) { return isStarredKey(r.key); })));
    return s.concat(u);
  }

  function fmtStarredAt(ms) {
    if (!ms) return '—';
    var d = new Date(ms);
    var p = function (n) { return (n < 10 ? '0' : '') + n; };
    return p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
  }

  function fmtBytes(n) {
    if (!n) return '0 B';
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(n < 10240 ? 1 : 0) + ' KB';
    return (n / 1048576).toFixed(1) + ' MB';
  }

  function seedFolder(name) {
    var n = String(name).toLowerCase();
    if (/remotion|video/.test(n)) return 'video';
    if (/mnemosyne|memory/.test(n)) return 'memory';
    if (/aihot|ai hot/.test(n)) return 'search';
    if (/pdf|docx|xlsx|pptx/.test(n)) return 'doc';
    if (/^web-|webapp/.test(n)) return 'frontend';
    if (/design|taste|stitch|minimalist|brutalist|brandkit|imagegen|image-to-code|redesign|ui-new|ui-design|soft-skill/.test(n)) return 'design';
    return 'eng';
  }

  // 本地模式一次性迁移（§2.4 的本地版）：旧多选分类 → 文件夹。
  // customCats 直接在内存建夹并写进 prefs.folders；旧 cats[0] 命中夹 id 才
  // 回填，其余 = 未归档。foldersMigrated 置位后不再进这个分支。
  function migrateLocalFolders() {
    if (prefs.foldersMigrated) {
      if (!Array.isArray(prefs.folders) || !prefs.folders.length) {
        prefs.folders = PRESET_FOLDERS.map(function (f, i) {
          return { id: f.id, name: f.name, color: f.color, position: i };
        });
        savePrefs();
      }
      FOLDERS = prefs.folders;
      return;
    }
    var folders = PRESET_FOLDERS.map(function (f, i) {
      return { id: f.id, name: f.name, color: f.color, position: i };
    });
    (prefs.customCats || []).forEach(function (c, i) {
      folders.push({ id: c.id, name: c.label || '未命名', color: PALETTE[i % PALETTE.length],
                     position: folders.length });
    });
    prefs.folders = folders;
    FOLDERS = folders;
    Object.keys(meta).forEach(function (key) {
      if (scopeOf(key) !== 'shared') return;
      var m = meta[key];
      if (!m) return;
      if (m.folder == null) {
        var first = (m.cats || [])[0];
        m.folder = (first && folderById(first)) ? first : '';
      }
    });
    delete prefs.customCats;
    prefs.foldersMigrated = true;
    savePrefs();
  }

  function seedIfNeeded() {
    if (MODE === 'db') return; // 桌面端由 skilllocker.migrate_folders + seed_meta 在首启迁移/播种
    migrateLocalFolders();
    SKILLS.forEach(function (s) {
      var m = meta[s.name];
      if (!m) {
        meta[s.name] = { display: '', note: '', folder: seedFolder(s.name), updated: 0 };
      } else if (m.folder == null) {
        // '' 是用户显式移出的未归档，不能重新播种
        m.folder = seedFolder(s.name);
      }
    });
    saveMeta();
  }

  /* ---------------------------------------------------------- rows */
  function sharedRows() {
    return SKILLS.map(function (s) {
      var m = meta[s.name] || {};
      return {
        key: s.name,
        board: 'shared',
        name: s.name,
        label: s.label,
        display: m.display || s.name,
        folder: m.folder || '',
        note: m.note || '',
        updated: m.updated || 0,
        starred: m.starred || 0,
        starredAt: m.starredAt || 0,
        skill: s,
        conflict: (s.realIn || []).length > 0
      };
    });
  }

  function uniqueRows() {
    return UNIQUE.map(function (u) {
      var key = u.tool + '/' + u.name;
      var m = meta[key] || {};
      return {
        key: key,
        board: 'unique',
        name: u.name,
        label: u.toolLabel,
        display: m.display || u.name,
        note: m.note || '',
        updated: m.updated || 0,
        starred: m.starred || 0,
        starredAt: m.starredAt || 0,
        entry: u,
        conflict: u.sameName
      };
    });
  }

  // 原型快照（ui/data.js）没有 unique_groups：按 (名字, kind, sameName) 兜底
  // 聚合，保证本地模式也能看到组视图；桌面端永远以后端分组为准。
  function localUniqueGroups(rows) {
    var groups = {}, order = [];
    (rows || []).forEach(function (u) {
      var ident = [u.kind || '', u.sameName ? 1 : 0, u.hash || ''].join('|');
      var key = u.name + '#' + ident;
      var g = groups[key];
      if (!g) {
        g = groups[key] = {
          key: key, name: u.name, hash: u.hash || '',
          count: 0, tools: [], toolLabels: [], marks: [],
          instances: [], files: 0, bytes: 0, kinds: [], srcs: [],
          sameName: !!u.sameName, hasForeign: false, deletable: true
        };
        order.push(key);
      }
      g.count++;
      g.instances.push(u.tool + '/' + u.name);
      g.files += u.files || 0;
      g.bytes += u.bytes || 0;
      if (g.tools.indexOf(u.tool) < 0) {
        g.tools.push(u.tool);
        g.toolLabels.push(u.toolLabel || u.tool);
        g.marks.push(u.mark || '');
      }
      if (u.kind && g.kinds.indexOf(u.kind) < 0) g.kinds.push(u.kind);
      if (u.src && g.srcs.indexOf(u.src) < 0) g.srcs.push(u.src);
      if (u.kind === 'foreign-link') g.hasForeign = true;
      if (u.deletable === false) g.deletable = false;
    });
    return order.map(function (k) { return groups[k]; });
  }

  function uniqueLogicalCount() {
    return UNIQUE_GROUPS.length;
  }

  function groupOfKey(key) {
    if (!key || key.indexOf('g#') !== 0) return null;
    var gid = key.slice(2);
    for (var i = 0; i < UNIQUE_GROUPS.length; i++) {
      if (UNIQUE_GROUPS[i].key === gid) return UNIQUE_GROUPS[i];
    }
    return null;
  }

  function groupEntry(g) {
    // 组内第一个实例的后端行，供卡片 / 兜底字段使用
    for (var i = 0; i < UNIQUE.length; i++) {
      var u = UNIQUE[i];
      if (g.instances.indexOf(u.tool + '/' + u.name) > -1) return u;
    }
    return null;
  }

  // 逻辑技能组 → 行。key 用 `g#` 前缀与实例 key（tool/name）区分：
  // 组行只做展示与入口，改名 / 备注 / 删除等高危操作仍按实例定位。
  function uniqueGroupRows() {
    return UNIQUE_GROUPS.map(function (g) {
      var first = groupEntry(g) || {};
      return {
        key: 'g#' + g.key,
        board: 'unique',
        group: true,
        groupData: g,
        name: g.name,
        display: g.name,
        label: (g.toolLabels || []).join('、'),
        note: '',
        updated: 0,
        starred: 0,
        starredAt: 0,
        entry: first,
        conflict: !!g.sameName
      };
    });
  }

  function currentRows() {
    if (isStarredView()) return starredRows();
    if (state.board === 'agents') return agentRows();
    return state.board === 'unique' ? uniqueRows() : sharedRows();
  }

  /* ---------------------------------------------------------- agents */
  var AGENT_STATUS = {
    connected: { label: '已连接', rank: 0, dot: 'var(--success)' },
    off: { label: '已关闭', rank: 1, dot: 'var(--ink-3)' },
    missing: { label: '未安装', rank: 2, dot: 'var(--warning)' }
  };
  var AGENT_STATUS_ORDER = ['connected', 'off', 'missing'];

  /* 在途开关：key -> 目标状态（true=正在连，false=正在撤）。
     连着拨多个开关时，每笔 set_agent 都会回一份「它自己那一刻」的整套看板；
     后端 OPS_LOCK 让后面的请求还在排队，它们的 enabled 此刻仍是旧值 —— 前端
     要是照单全收，用户刚拨开的开关就会被界面自己弹回「已关闭」，看起来像
     「只连上一个，剩下的还得重新打开」（用户反馈）。
     所以在途的行一律按用户的目标渲染，并标「连接中 / 关闭中」+ 禁用挡重复工；
     该笔回包落地（成功或失败）才由数据说话。序号防线见 applyBootstrap。 */
  var AGENT_PENDING = {};

  function setAgentPending(key, on) {
    AGENT_PENDING[key] = !!on;
    renderAgentBoard();
  }

  function clearAgentPending(key) {
    if (!Object.prototype.hasOwnProperty.call(AGENT_PENDING, key)) return;
    delete AGENT_PENDING[key];
  }

  // 开关只改智能体看板与计数：重绘这一整块（保住列表滚动位置），不碰别的看板
  function renderAgentBoard() {
    var listBody = document.getElementById('list-body');
    var scrollTop = listBody ? listBody.scrollTop : 0;
    renderAll(false);
    if (listBody) listBody.scrollTop = scrollTop;
  }

  // 桌面端由 list_boards 输出 agents；原型快照没有时退回 tools 推导（只读）。
  function agentRows() {
    var source = AGENTS.length ? AGENTS : TOOLS.map(function (t) {
      return {
        key: t.key, label: t.label, mark: t.mark, sub: t.sub, dir: t.dir,
        detectDir: '', detected: !!t.detected, enabled: !!t.enabled,
        mode: t.mode || 'junction',
        status: t.detected ? (t.enabled ? 'connected' : 'off') : 'missing',
        linked: t.enabled ? SKILLS.length : 0, conflicts: 0,
        skillsTotal: SKILLS.length
      };
    });
    return source.map(function (a) {
      var st = AGENT_STATUS[a.status] ? a.status : 'missing';
      var row = {
        key: a.key,
        board: 'agents',
        label: a.label || a.key,
        mark: a.mark || mark(a.label || a.key),
        sub: a.sub || '',
        dir: a.dir || '',
        detectDir: a.detectDir || '',
        detected: !!a.detected,
        enabled: !!a.enabled,
        mode: a.mode || 'junction',
        status: st,
        statusText: AGENT_STATUS[st].label,
        rank: AGENT_STATUS[st].rank,
        dot: AGENT_STATUS[st].dot,
        linked: a.linked || 0,
        conflicts: a.conflicts || 0,
        skillsTotal: a.skillsTotal == null ? SKILLS.length : a.skillsTotal,
        // agent 自己安装/读取的外部目录（如 ~/.agents/skills），本工具只展示不管理
        external: a.external || [],
        pending: false
      };
      // 在途的那一行按用户目标渲染：状态取目标值（这样筛选 / 排序 / 计数都不
      // 会在请求飞行中把行抖走），文案改「连接中 / 关闭中」，链接数还是磁盘
      // 上的真数 —— 不猜。未安装的开关本来点不动，不参与在途。
      var p = AGENT_PENDING[row.key];
      if (p !== undefined && row.detected) {
        row.pending = true;
        row.enabled = p;
        row.status = p ? 'connected' : 'off';
        row.rank = AGENT_STATUS[row.status].rank;
        row.dot = AGENT_STATUS[row.status].dot;
        row.statusText = p ? '连接中…' : '关闭中…';
      }
      return row;
    });
  }

  function agentCounts(rows) {
    var c = { all: rows.length, connected: 0, off: 0, missing: 0,
              connecting: 0, disconnecting: 0 };
    rows.forEach(function (r) {
      c[r.status] = (c[r.status] || 0) + 1;
      // 在途的行按目标状态计入（这样状态筛选的计数与点进去的结果一致），
      // 另外单独给「连接中 / 关闭中」留两个数：汇总文案要说得出它们在途
      if (r.pending) {
        if (r.status === 'connected') c.connecting++;
        else if (r.status === 'off') c.disconnecting++;
      }
    });
    return c;
  }

  function groupSubText(g) {
    var who = g.toolLabels || [];
    var base = g.count > 1
      ? g.count + ' 个 agent 各有一份 · 内容一致'
      : (who[0] || g.name) + ' · ' + (g.files || 0) + ' files';
    if (g.sameName) base += ' · 同名不同内容';
    else if (g.hasForeign) base += ' · 含外部链接';
    else if ((g.srcs || []).some(function (s) { return s && s !== 'main'; })) base += ' · 含外部来源';
    return base;
  }

  function rowSecondary(r) {
    if (r.group) return groupSubText(r.groupData);
    if (r.board === 'unique') {
      return r.entry.toolLabel + ' · ' + r.entry.files + ' files';
    }
    var m = meta[r.key] || {};
    if (m.display && m.display !== r.name) return r.name;
    if (r.label && r.label !== r.name) return r.label;
    return '';
  }

  // 身份契约：agent 注册用的名字永远是目录名（分发就是按 <skills_dir>/<目录名>
  // 建链接），而 SKILL.md 里 frontmatter 的 name 是规范声明的注册名。两者一旦
  // 分叉，链接全绿也用不上 —— 用户照着第二行的名字在 agent 里输，那个名字根本
  // 不存在（soft-skill 声明 high-end-visual-design 就是这一类）。判据与 Python
  // 侧 skilllocker_base.contract_issue 同一口径：label 为空视为未声明，合规。
  function contractIssue(r) {
    if (!r || r.board !== 'shared') return '';
    var declared = String(r.label || '');
    return declared && declared !== r.name ? declared : '';
  }

  function contractFlagHtml(r) {
    var declared = contractIssue(r);
    if (!declared) return '';
    return '<span class="mini-flag conflict" title="包内声明名 ' + esc(declared) +
      ' ≠ 目录名 ' + esc(r.name) +
      '；agent 只按目录名注册，第二行那个名字在 agent 里不存在。详情里可一键对齐">' +
      icon('alert') + '名字不符</span>';
  }

  /* ---------------------------------------------------------- search */
  // 名称优先：先只匹配名称（显示名 / 原名 / label），名称有命中就只显示名称结果；
  // 名称全部没命中时，才回退到备注 / 分类 / 描述等字段。
  // 名称支持连续子串和按顺序跳字（只在同一段内跳字，相邻字符缺口最多 4 个），
  // 回退字段只做连续子串。
  var FUZZY_GAP = 4;
  var WORD_SPLIT = /[^0-9a-z\u4e00-\u9fff]+/;

  function normText(s) {
    return String(s == null ? '' : s).toLowerCase();
  }

  function containsText(text, q) {
    return normText(text).indexOf(q) > -1;
  }

  function fuzzyPart(part, q) {
    var from = 0;
    var prev = -1;
    for (var i = 0; i < q.length; i++) {
      var at = part.indexOf(q.charAt(i), from);
      if (at < 0) return false;
      if (prev >= 0 && at - prev - 1 > FUZZY_GAP) return false;
      prev = at;
      from = at + 1;
    }
    return true;
  }

  function fuzzyText(text, q) {
    var s = normText(text);
    if (!s) return false;
    if (s.indexOf(q) > -1) return true;
    // 跳字只在同一段内生效，避免把 open-code-review 里的 d/e/v 拼成 dev。
    var parts = s.split(WORD_SPLIT);
    for (var i = 0; i < parts.length; i++) {
      if (parts[i] && fuzzyPart(parts[i], q)) return true;
    }
    return false;
  }

  function nameHay(r) {
    if (r.board === 'agents') return [r.label, r.key].join(' ');
    // 共有 skill 的 label 与目录名一致，一并视作名称；独有 skill 的 label 是 agent 名，放到回退字段。
    return [r.display, r.name, r.board === 'shared' ? r.label : ''].join(' ');
  }

  function fallbackHay(r) {
    if (r.board === 'agents') return [r.dir, r.sub, r.statusText, r.mode].join(' ');
    // 分类名 = 归属文件夹名；未归档不参与备注/分类回退匹配
    var f = r.board === 'shared' ? folderById(r.folder) : null;
    var catText = f ? f.name : '';
    if (r.board === 'unique') {
      return [r.label, r.entry ? r.entry.toolLabel : '', r.entry ? r.entry.mark : '', r.note, catText].join(' ');
    }
    return [r.label, r.note, catText, r.skill ? r.skill.description : ''].join(' ');
  }

  function passesFilters(r) {
    if (r.board === 'agents') {
      return state.agentStatus === 'all' || r.status === state.agentStatus;
    }
    // 共有 / 独有技能没有侧栏筛选了：归属在侧栏导航（文件夹）里完成
    return true;
  }

  function filterRows(all) {
    var rows = all.filter(passesFilters);
    if (!state.q) return rows;
    var tokens = normText(state.q).split(/\s+/).filter(function (t) { return t; });
    if (!tokens.length) return rows;

    var nameRows = rows.filter(function (r) {
      var hay = nameHay(r);
      return tokens.every(function (t) { return fuzzyText(hay, t); });
    });
    if (nameRows.length) return nameRows;

    return rows.filter(function (r) {
      var hay = fallbackHay(r);
      return tokens.every(function (t) { return containsText(hay, t); });
    });
  }

  function sortRows(rows) {
    var out = rows.slice();
    if (out.length && out[0].board === 'agents') {
      out.sort(function (a, b) {
        return a.rank - b.rank ||
          String(a.label).localeCompare(String(b.label), 'zh-Hans-CN');
      });
      return out;
    }
    if (prefs.sort === 'edited') {
      out.sort(function (a, b) {
        if (b.updated !== a.updated) return b.updated - a.updated;
        return a.display.localeCompare(b.display, 'zh-Hans-CN');
      });
    } else if (prefs.sort === 'starred') {
      // 收藏优先：星标置顶（收藏时间倒序），其余按名称
      out.sort(function (a, b) {
        var sa = a.starred ? 1 : 0;
        var sb = b.starred ? 1 : 0;
        if (sa !== sb) return sb - sa;
        if (sa) return (b.starredAt || 0) - (a.starredAt || 0);
        return String(a.display).localeCompare(String(b.display), 'zh-Hans-CN');
      });
    } else {
      out.sort(function (a, b) {
        return String(a.display).localeCompare(String(b.display), 'zh-Hans-CN');
      });
    }
    return out;
  }

  /* ---------------------------------------------------------- render: chrome */
  function unfiledCount() {
    return sharedRows().filter(function (r) { return !r.folder; }).length;
  }

  function renderChrome() {
    document.body.dataset.view = state.board;

    document.getElementById('store-path').textContent = STORE;
    var storeInText = META.storeIn === 'localappdata'
      ? '用户数据目录（安装目录不可写，已回退）'
      : META.storeIn === 'custom' ? '自定义目录' : '安装目录';
    document.getElementById('store-path').title =
      '技能库：' + STORE + '（' + storeInText + '）' +
      (META.db ? '\n数据：' + META.db + '（库文件与配置，升级/卸载不影响）' : '');
    document.getElementById('store-count').textContent = SKILLS.length;
    document.getElementById('store-size').textContent = META.storeSize || '—';
    // 隔离模式角标（安装包验收 / 冒烟）：这个实例的仓库是临时占位仓库，界面上的
    // 共有 / 独有数字都不属于用户真库，而且后端已经拒绝一切改动 agent 目录的操作
    // ——两件事都要写在脸上，别让人对着假数据做判断。
    var isoPill = document.getElementById('iso-pill');
    if (isoPill) {
      isoPill.hidden = !META.isolated;
      isoPill.title = META.isolated
        ? '隔离模式（config.json 的 isolated=true）：只读扫描，'
          + '分发 / 撤链 / 删除 / 收编 / 接管都会被拒绝，不会改动任何 agent 目录'
        : '';
    }
    var healthDen = SKILLS.length + UNIQUE.length;
    document.getElementById('store-health').style.width =
      (healthDen ? (100 - (UNIQUE.length / healthDen) * 100).toFixed(1) : '0') + '%';

    var sharedCount = SKILLS.length;
    var uniqueCount = UNIQUE.length;
    var uniqueLogical = uniqueLogicalCount();
    var agentStats = agentCounts(agentRows());
    var unfiled = unfiledCount();

    document.getElementById('nav-shared').textContent = sharedCount;
    // 计数口径：逻辑技能数（去重后）；实例数在 tab 副标题 / 页面标题里给出
    document.getElementById('nav-unique').textContent = uniqueLogical;
    document.getElementById('nav-unique').title =
      uniqueLogical + ' 个逻辑技能 · ' + uniqueCount + ' 个实例（含各 agent 副本）';
    document.getElementById('nav-agents').textContent = agentStats.all;
    document.getElementById('nav-starred-badge').textContent = starCount();
    document.getElementById('sb-starred').textContent = starCount();
    document.getElementById('stat-agents').textContent = agentStats.connected;
    document.getElementById('stat-edited').textContent = editedCount();
    document.getElementById('stat-folders').textContent = FOLDERS.length;
    document.getElementById('stat-unfiled').textContent = unfiled;
    document.getElementById('sb-edited').textContent = editedCount();
    var sbHealth = document.getElementById('sb-health');
    sbHealth.textContent = sharedCount + ' 共有 · ' + FOLDERS.length + ' 分类' +
      (META.seedNote ? ' · 首启完成' : '');
    sbHealth.title = META.seedNote || '';
    document.getElementById('sb-path').textContent = STORE;
    document.getElementById('sb-path').title = document.getElementById('store-path').title;
    document.getElementById('ver-scan').textContent = SCAN;
    document.getElementById('sb-scan').textContent = SCAN;

    document.getElementById('tab-shared-num').textContent = sharedCount;
    document.getElementById('tab-unique-num').textContent = uniqueLogical;
    document.getElementById('tab-agents-num').textContent = agentStats.all;
    document.getElementById('tab-shared-em').textContent = FOLDERS.length + ' 个分类文件夹';
    document.getElementById('tab-agents-em').textContent = agentStats.connected + ' 已连接' +
      (agentStats.connecting ? ' · ' + agentStats.connecting + ' 连接中' : '');
    var overridden = UNIQUE.filter(function (u) { return u.sameName; }).length;
    // 与共有 tab 的「N 个分类文件夹」同款口径：独有 tab 报 agent 文件夹数 + 实例数
    document.getElementById('tab-unique-em').textContent =
      uniqueAgentGroups().length + ' 个 agent 文件夹 · ' + uniqueCount + ' 个实例';

    var colName = document.getElementById('col-name');
    var colMid = document.getElementById('col-mid');
    var colRight = document.getElementById('col-right');
    var colCover = document.getElementById('col-cover');
    var emptyTitle = document.getElementById('empty-title');
    var emptySub = document.getElementById('empty-sub');
    var footHint = document.getElementById('list-foot-hint');
    var inFolder = state.board === 'shared' && !!state.folder;

    if (state.board === 'shared') {
      colName.textContent = 'SKILL';
      colRight.textContent = '备注';
      colMid.textContent = '分类';
      if (state.folder === 'starred') {
        // 收藏视图：第 5 列换义为「收藏于」，列头隐藏分类（5 列窄行）
        colCover.textContent = '收藏于';
        colMid.hidden = true;
        var sN = sharedRows().filter(function (r) { return isStarredKey(r.key); }).length;
        var uN = starCount() - sN;
        document.getElementById('page-title').textContent = '收藏';
        document.getElementById('page-sub').textContent =
          starCount() + ' 个常用 skill（' + sN + ' 共有 + ' + uN + ' 独有）· 星标存在元数据里，改名 / 移动分类都不会丢';
        emptyTitle.textContent = state.q ? '没有匹配的收藏' : '还没有收藏的 skill';
        emptySub.textContent = state.q
          ? '换个关键词，或清除搜索'
          : '在行尾、卡片角或详情栏点 ☆ 收藏常用技能；共有与独有都汇集在这里直达';
        footHint.textContent = '点 ☆ 取消收藏即从列表移除 · 排序切「收藏优先」按收藏时间倒序';
      } else if (state.folder === 'none') {
        colCover.textContent = '分发覆盖';
        colMid.hidden = inFolder;
        document.getElementById('page-title').textContent = '未归档';
        document.getElementById('page-sub').textContent =
          unfiled + ' 个 skill 还没放入任何分类 · 拖到侧栏文件夹即可归档';
        emptyTitle.textContent = '全部 skill 都已归档';
        emptySub.textContent = '所有 skill 都已放入分类文件夹';
        footHint.textContent = '点 ⋯ 菜单或直接拖拽侧栏文件夹，把 skill 归档进分类';
      } else if (inFolder) {
        colCover.textContent = '分发覆盖';
        colMid.hidden = true;
        var f = folderById(state.folder);
        document.getElementById('page-title').textContent = f ? f.name : '分类文件夹';
        document.getElementById('page-sub').textContent =
          (f ? f.name + ' · ' : '') + skillsInFolder(state.folder).length +
          ' 个 skill · 由你手动维护的分类文件夹';
        emptyTitle.textContent = '这个分类还是空的';
        emptySub.textContent = '回到「全部技能」把 skill 拖进来，或用行菜单移动';
        footHint.textContent = '行菜单 ⋯ 可移动到其他分类；拖到面包屑「全部技能」移出分类';
      } else if (viewOf('shared') === 'grid') {
        colCover.textContent = '分发覆盖';
        colMid.hidden = false;
        document.getElementById('page-title').textContent = '共有技能';
        document.getElementById('page-sub').textContent =
          FOLDERS.length + ' 个分类文件夹 · ' + sharedCount + ' 个 skill · 拖拽归档，像资源管理器一样管理';
      } else {
        colCover.textContent = '分发覆盖';
        colMid.hidden = false;
        document.getElementById('page-title').textContent = '共有技能';
        document.getElementById('page-sub').textContent =
          sharedCount + ' 个 skill · ' + FOLDERS.length + ' 个分类文件夹 · 点行选中，⋯ 菜单移动分类';
        emptyTitle.textContent = '没有匹配的 skill';
        emptySub.textContent = '换个关键词，或把筛选切回“全部”';
        footHint.textContent = '点行选中 · ⋯ 菜单移动分类 · 分类列的 chip 可直接进入文件夹';
      }
    } else if (state.board === 'unique') {
      colName.textContent = 'SKILL';
      colMid.hidden = true;
      colRight.textContent = '备注';
      colCover.textContent = '来源';
      var uGroups = uniqueAgentGroups();
      if (state.folder) {
        var ug = uGroups.filter(function (g) { return g.tool === state.folder; })[0];
        document.getElementById('page-title').textContent = ug ? ug.label : 'Agent 文件夹';
        document.getElementById('page-sub').textContent =
          (ug ? ug.label + ' · ' : '') + (ug ? ug.rows.length : 0) +
          ' 个 skill · 只存在于该 agent · 不会被中央仓库覆盖';
        emptyTitle.textContent = '这个 agent 暂无独有 skill';
        emptySub.textContent = '该 agent 目录里没有与中央仓库不同名的内容';
        footHint.textContent = '这些 skill 不会被中央仓库覆盖，改名与备注只影响展示';
      } else if (viewOf('unique') === 'grid') {
        var mergedView = uniqueLogical < uniqueCount;
        document.getElementById('page-title').textContent = '独有技能';
        document.getElementById('page-sub').textContent = mergedView
          ? uniqueLogical + ' 个逻辑技能（' + uniqueCount + ' 个实例）· ' +
            uGroups.length + ' 个 agent 文件夹' +
            (overridden ? ' · ' + overridden + ' 个同名不同内容' : '') +
            ' · 点击查看实例' +
            (META.uniqueLight ? ' · 首启轻量视图，建库后补全明细' : '')
          : uGroups.length + ' 个 agent 文件夹 · ' + uniqueCount + ' 个独有技能' +
            (overridden ? ' · ' + overridden + ' 个同名不同内容' : '') +
            ' · 先按 agent 浏览' +
            (META.uniqueLight ? ' · 首启轻量视图，建库后补全明细' : '');
      } else {
        document.getElementById('page-title').textContent = '独有技能';
        document.getElementById('page-sub').textContent =
          uniqueLogical + ' 个逻辑技能 · ' + uniqueCount + ' 个实例 · ' +
          '同名同内容的多 agent 副本已合并' +
          (overridden ? '，' + overridden + ' 个同名不同内容未合并' : '') +
          (META.uniqueLight ? ' · 首启轻量视图，建库后补全明细' : '');
        emptyTitle.textContent = '没有匹配的 skill';
        emptySub.textContent = '换个关键词，或把筛选切回“全部”';
        footHint.textContent = '一行一个逻辑技能；点行看实例 · 改名与备注按实例保存';
      }
    } else {
      colName.textContent = 'AGENT';
      colMid.hidden = false;
      colMid.textContent = '状态';
      colRight.textContent = '目录';
      colCover.textContent = '分发开关';
      emptyTitle.textContent = '没有匹配的 agent';
      emptySub.textContent = '换个关键词，或把状态筛选切回“全部”';
      footHint.textContent = '开关只建/删 junction 链接，中央仓库的 skill 实体不受影响';
      document.getElementById('page-title').textContent = '智能体';
      document.getElementById('page-sub').textContent =
        '本机检测到 ' + (agentStats.connected + agentStats.off) + ' 个 agent · ' +
        agentStats.connected + ' 已连接' +
        (agentStats.connecting ? '（' + agentStats.connecting + ' 连接中）' : '') + '、' +
        agentStats.off + ' 已关闭' +
        (agentStats.disconnecting ? '（' + agentStats.disconnecting + ' 关闭中）' : '') +
        ' · 另有 ' + agentStats.missing + ' 个未安装';
    }

    // 侧栏导航区标题：文案与新建按钮随看板切换（独有=Agent 文件夹，不可新建）
    var foldersHead = document.getElementById('side-folders-head');
    var foldersLabel = document.getElementById('side-cat-label');
    var foldersAdd = document.getElementById('side-add-folder');
    if (state.board === 'agents') {
      foldersHead.hidden = false;
      foldersLabel.textContent = '状态';
      foldersAdd.hidden = true;
    } else if (state.board === 'unique') {
      foldersHead.hidden = false;
      foldersLabel.textContent = 'Agent 文件夹';
      foldersAdd.hidden = true;
    } else {
      foldersHead.hidden = false;
      foldersLabel.textContent = '分类文件夹';
      foldersAdd.hidden = false;
    }

    // 视图分段：共有 / 独有看板的根层级出现（夹内只有列表一种形态）
    var seg = document.getElementById('view-seg');
    seg.hidden = (state.board !== 'shared' && state.board !== 'unique') || !!state.folder;
    Array.prototype.forEach.call(seg.querySelectorAll('button'), function (b) {
      b.classList.toggle('active', b.getAttribute('data-view-mode') === viewOf(state.board));
    });

    document.getElementById('sort-btn').hidden = state.board === 'agents';

    Array.prototype.forEach.call(document.querySelectorAll('[data-board]'), function (el) {
      var on = el.getAttribute('data-board') === state.board;
      el.classList.toggle('active', on);
      if (el.classList.contains('board-tab')) el.setAttribute('aria-selected', on ? 'true' : 'false');
      if (el.classList.contains('nav-item')) {
        if (on) el.setAttribute('aria-current', 'page');
        else el.removeAttribute('aria-current');
      }
    });
    // 收藏导航不在 data-board 体系里：金色激活态单独同步
    var navStarred = document.getElementById('nav-starred');
    if (navStarred) {
      navStarred.classList.toggle('active', isStarredView());
      if (isStarredView()) navStarred.setAttribute('aria-current', 'page');
      else navStarred.removeAttribute('aria-current');
    }

    document.getElementById('sort-label').textContent =
      prefs.sort === 'edited' ? '最近编辑' : prefs.sort === 'starred' ? '收藏优先' : '名称 A→Z';
    document.body.classList.toggle('compact', prefs.density === 'compact');
    document.body.classList.toggle('dark', prefs.theme === 'dark');
    document.body.classList.toggle('reduce-motion', !prefs.motion);
    document.getElementById('theme-toggle').firstElementChild.setAttribute(
      'href', prefs.theme === 'dark' ? '#i-sun' : '#i-moon'
    );
  }

  /* ---------------------------------------------------------- render: sidebar filters */
  function updateFoot(shown, total) {
    var unit = state.board === 'agents' ? ' 个 agent' : ' 条';
    document.getElementById('list-foot-count').textContent =
      shown === total ? total + unit : '筛出 ' + shown + ' / ' + total + unit;
  }

  // 侧栏导航：共有 = 全部技能 / 各文件夹（可投放）/ 未归档；
  // 独有 = 全部独有 / 各 agent 文件夹；智能体 = 状态筛选。计数永远按全库算。
  function renderFilters() {
    var nav = document.getElementById('side-folders');

    if (state.board === 'agents') {
      var ac = agentCounts(currentRows());
      var sideHtml = '';
      ['all'].concat(AGENT_STATUS_ORDER).forEach(function (st) {
        var def = st === 'all'
          ? { label: '全部', dot: 'var(--ink-3)' }
          : AGENT_STATUS[st];
        var n = ac[st] || 0;
        var cls = state.agentStatus === st ? 'active' : '';
        sideHtml += '<button class="nav-item cat-nav ' + cls + '" data-agent-status="' + st + '">' +
          '<span class="catdot" style="--c:' + def.dot + '"></span>' + def.label +
          '<b class="nav-badge">' + n + '</b></button>';
      });
      nav.innerHTML = sideHtml;
      nav.setAttribute('aria-label', '按状态筛选');
      return;
    }
    // 独有看板：agent 即分类文件夹——全部独有 / 各 agent（带计数，可点击进入；
    // 独有 skill 不能跨 agent 移动，所以不是投放区）
    if (state.board === 'unique') {
      nav.setAttribute('aria-label', 'Agent 文件夹导航');
      var uHtml = '<button class="nav-item cat-nav' + (!state.folder ? ' active' : '') +
        '" data-folder-nav="all" title="' + uniqueLogicalCount() + ' 个逻辑技能 · ' +
        UNIQUE.length + ' 个实例">' +
        icon('grid') + '全部独有<b class="nav-badge">' + uniqueLogicalCount() + '</b></button>';
      uniqueAgentGroups().forEach(function (g) {
        uHtml += '<button class="nav-item cat-nav' + (state.folder === g.tool ? ' active' : '') +
          '" data-folder-nav="' + esc(g.tool) +
          '" title="打开「' + esc(g.label) + '」的独有技能">' +
          '<span class="catdot" style="--c:' + COLORS[g.color][0] + '"></span>' + esc(g.label) +
          '<b class="nav-badge">' + g.rows.length + '</b></button>';
      });
      nav.innerHTML = uHtml;
      return;
    }

    nav.setAttribute('aria-label', '分类文件夹导航');
    var html = '<button class="nav-item cat-nav' + (!state.folder ? ' active' : '') + '" data-folder-nav="all">' +
      icon('grid') + '全部技能<b class="nav-badge">' + SKILLS.length + '</b></button>';
    FOLDERS.forEach(function (f) {
      html += '<button class="nav-item cat-nav' + (state.folder === f.id ? ' active' : '') +
        '" data-folder-nav="' + esc(f.id) + '" data-drop-folder="' + esc(f.id) +
        '" title="打开「' + esc(f.name) + '」，也可拖拽 skill 进来">' +
        '<span class="catdot" style="--c:' + COLORS[f.color][0] + '"></span>' + esc(f.name) +
        '<b class="nav-badge">' + skillsInFolder(f.id).length + '</b></button>';
    });
    html += '<button class="nav-item cat-nav' + (state.folder === 'none' ? ' active' : '') +
      '" data-folder-nav="none" title="查看未归档 skill">' +
      '<span class="catdot dashed"></span>未归档<b class="nav-badge">' +
      skillsInFolder('none').length + '</b></button>';
    nav.innerHTML = html;
  }

  // 归属某文件夹（'none' = 未归档）的共有技能，按当前排序返回
  function skillsInFolder(fid) {
    var out = sharedRows().filter(function (r) {
      return fid === 'none' ? !r.folder : r.folder === fid;
    });
    return sortRows(out);
  }

  // 归属某 agent 的独有技能（agent = 独有看板的「分类文件夹」），按当前排序返回
  function skillsInAgentFolder(tool) {
    return sortRows(uniqueRows().filter(function (r) { return r.entry.tool === tool; }));
  }

  // 独有看板的 agent 分组：组顺序按 UNIQUE 的扫描顺序；agent 不在探测结果里时
  // 退回条目自带的 toolLabel / mark（与 uniqueGroupedHtml 的分割线同一口径）
  function uniqueAgentGroups() {
    var order = [];
    var groups = {};
    uniqueRows().forEach(function (r) {
      var t = r.entry.tool;
      if (!groups[t]) { groups[t] = []; order.push(t); }
      groups[t].push(r);
    });
    return order.map(function (tool) {
      var a = agentByKey(tool);
      var first = groups[tool][0].entry;
      return {
        tool: tool,
        label: a ? (a.label || tool) : (first.toolLabel || tool),
        mark: a ? a.mark : (first.mark || mark(tool)),
        color: agentColor(tool),
        rows: sortRows(groups[tool])
      };
    });
  }

  /* ---------------------------------------------------------- 首启引导 */
  // 安装版的中央仓库（%LOCALAPPDATA%\SkillLocker\store）天生是空的，安装程序
  // 不预置任何技能 —— 不处理就是「共有 0 / 全是独有」，看着像 bug。这里按后端
  // firstRun 快照给出口：① 接管从已有 junction 反推出来的仓库；② 一键收编各
  // agent 主目录里的实体技能；③ 手动导入。范围与守卫都在后端把关。
  function firstRunModel() {
    if (MODE !== 'db' || !FIRST_RUN || firstRunDismissed) return null;
    var items = FIRST_RUN.adoptable || [];
    var strip = !!(FIRST_RUN.strip && SKILLS.length);
    // 空仓库：整块面板；自动建库后仍有可收行：单行提示条（不长期占版面）
    if (SKILLS.length && !(strip && items.length)) return null;
    // 面板文案按「技能」计数而不是「副本」：同一个 skill 在 7 个 agent 里
    // 各有实体时是 1 个技能、7 份副本，别让用户以为是 7 个技能。
    var names = {};
    items.forEach(function (a) { names[a.name] = 1; });
    var skillCount = Object.keys(names).length;
    return {
      strip: strip,
      cand: FIRST_RUN.candidate || null,
      items: items,
      skillCount: skillCount,
      agents: FIRST_RUN.adoptableAgents || [],
      uniqueCount: UNIQUE.length,
      uniqueLogical: uniqueLogicalCount(),
      uniqueLight: !!META.uniqueLight,
      agentCount: AGENTS.filter(function (a) { return a.detected; }).length,
      seedNote: FIRST_RUN.seedNote || META.seedNote || '',
      blocked: FIRST_RUN.seedBlocked || null,
      autoAdoptError: FIRST_RUN.autoAdoptError || META.autoAdoptError || ''
    };
  }

  function frOptHtml(o) {
    return '<div class="fr-opt">' +
      '<span class="fr-opt-icon">' + icon(o.icon) + '</span>' +
      '<div class="fr-opt-main"><b>' + esc(o.title) + '</b><span>' + o.text + '</span></div>' +
      '<button class="btn' + (o.primary ? ' btn-primary' : '') + '" id="' + o.id + '">' +
      esc(o.label) + '</button></div>';
  }

  function firstRunHtml(m) {
    var opts = [];
    if (m.cand) {
      var labels = m.cand.tools.map(function (t) { return esc(t.label); });
      var who = labels.slice(0, 3).join('、') +
        (labels.length > 3 ? ' 等 ' + labels.length + ' 个 agent' : '');
      opts.push(frOptHtml({
        icon: 'folder-open', id: 'fr-adopt-store', label: '接管该仓库', primary: true,
        title: '接管本机已有的中央仓库',
        text: '<code>' + esc(m.cand.path) + '</code> 里有 ' + m.cand.skillCount +
          ' 个技能，正被 ' + who + ' 的 ' + m.cand.linkCount +
          ' 条链接指向。接管只写 config.json 并重新登记，不搬文件、不建链接，随时可退回默认仓库。'
      }));
    }
    if (m.items.length) {
      opts.push(frOptHtml({
        icon: 'download', id: 'fr-adopt-local', label: '收编',
        primary: !m.cand,
        title: '一键收编本机 ' + m.skillCount + ' 个技能',
        text: '把 ' + esc(m.agents.join('、')) + ' 主 skills 目录里的实体技能各拷一份进中央仓库，' +
          '拷完它们就是共有技能。源目录保留；外部目录、指向别处的链接、同名但内容不同的副本都不动。'
      }));
    }
    opts.push(frOptHtml({
      icon: 'layers', id: 'fr-import', label: '导入目录',
      title: '自己挑一个目录导入',
      text: '导入弹窗支持本地目录与 GitHub 来源，一次一个，入库前会给预检报告。'
    }));
    var seedText = '';
    if (m.autoAdoptError) {
      seedText = '<div class="fr-seed-note">' + icon('info') + '<span>自动接管已有仓库失败：' +
        esc(m.autoAdoptError) + '。仍可用下面的「接管该仓库」手动完成。</span></div>';
    } else if (m.seedNote) {
      seedText = '<div class="fr-seed-note">' + icon('info') + '<span>' +
        esc(m.seedNote) + '</span></div>';
    } else if (m.blocked === 'disabled') {
      seedText = '<div class="fr-seed-note">' + icon('info') +
        '<span>自动建库已在 config.json 关闭（seed_auto=false），以下为手动出口。</span></div>';
    }
    // 有独有技能但都不在自动收编范围（外部目录 / 容器展开）：说明原因，
    // 不让「没有可收编的技能」看起来像检测失败。
    if (!m.items.length && m.uniqueLogical) {
      seedText += '<div class="fr-seed-note">' + icon('info') +
        '<span>检测到的独有技能来自外部目录或容器（如 <code>.system</code>、' +
        '<code>~/.agents/skills</code>），自动建库不会改动它们；可在独有看板逐行' +
        '「转为共有」，或用「导入目录」加入仓库。</span></div>';
    }
    return '<div class="fr-head">' +
      '<div class="fr-title"><b>中央仓库还是空的，所以「共有技能」是 0</b>' +
      '<span>共有 = 中央仓库 <code>' + esc(STORE) + '</code> 里的技能；' +
      '独有 = 各 agent 目录里能加载、但仓库没有（或内容不同）的技能。' +
      '本机已检测到 ' + m.agentCount + ' 个 agent、' + m.uniqueLogical +
      ' 个逻辑独有技能（' + m.uniqueCount + ' 个实例）' +
      (m.uniqueLight ? '，建库后按内容合并同名副本' : '') + '。</span></div>' +
      '<button class="fr-dismiss" id="fr-dismiss" title="本次启动不再显示">' +
      icon('x') + '</button></div>' +
      seedText +
      '<div class="fr-opts">' + opts.join('') + '</div>';
  }

  // 自动建库后的单行提示条：共有看板已经可用，只是还有独有技能没进库。
  function firstRunStripHtml(m) {
    var names = m.skillCount;
    return '<div class="fr-strip-main">' + icon('check') +
      '<div><b>中央仓库已就绪</b><span>' +
      (m.seedNote ? esc(m.seedNote) + ' · ' : '') +
      '还有 ' + names + ' 个独有技能可一键收编。</span></div></div>' +
      '<div class="fr-strip-actions">' +
      '<button class="btn btn-sm btn-primary" id="fr-adopt-local">补充收编</button>' +
      '<button class="fr-dismiss" id="fr-dismiss" title="本次启动不再显示">' +
      icon('x') + '</button></div>';
  }

  function renderFirstRun() {
    var root = document.getElementById('first-run');
    if (!root) return;
    var m = firstRunModel();
    if (!m) {
      root.hidden = true;
      root.innerHTML = '';
      root.classList.remove('is-strip');
      return;
    }
    root.hidden = false;
    root.classList.toggle('is-strip', !!m.strip);
    root.innerHTML = m.strip ? firstRunStripHtml(m) : firstRunHtml(m);
    // 安装版首启排障留痕：日志里没有 firstrun 行 = 面板压根没渲染
    if (!firstRunLogged) {
      firstRunLogged = true;
      uiLog('firstrun', { store: STORE, unique: m.uniqueCount,
                          candidate: m.cand ? m.cand.store : null,
                          candidateSkills: m.cand ? m.cand.skillCount : 0,
                          adoptable: m.items.length });
    }
    var bind = function (id, fn) {
      var el = document.getElementById(id);
      if (el) el.addEventListener('click', fn);
    };
    bind('fr-adopt-store', requestAdoptStore);
    bind('fr-adopt-local', requestAdoptLocal);
    bind('fr-import', function () { impOpen('source'); });
    bind('fr-dismiss', function () { firstRunDismissed = true; renderFirstRun(); });
    if (frBusy) {
      // 后端串行执行（OPS_LOCK），重复点击只会排队；界面直接禁用并说明在做什么
      [['fr-adopt-store', '接管该仓库'], ['fr-adopt-local', '收编']].forEach(function (pair) {
        var el = document.getElementById(pair[0]);
        if (!el) return;
        el.disabled = true;
        el.dataset.idleLabel = pair[1];
        el.textContent = '处理中…';
      });
      var dismiss = document.getElementById('fr-dismiss');
      if (dismiss) dismiss.disabled = true;
    } else {
      ['fr-adopt-store', 'fr-adopt-local'].forEach(function (id) {
        var el = document.getElementById(id);
        if (el && el.dataset.idleLabel) { el.disabled = false; el.textContent = el.dataset.idleLabel; }
      });
      var d2 = document.getElementById('fr-dismiss');
      if (d2) d2.disabled = false;
    }
  }

  function requestAdoptStore() {
    var m = firstRunModel();
    if (!m || !m.cand || frBusy) return;
    var c = m.cand;
    confirmModal('把中央仓库改成 ' + c.path + '？',
      '接管后「共有技能」就是这个目录里的 ' + c.skillCount + ' 个技能，' + c.linkCount +
      ' 条已有链接直接认作本工具的分发结果，独有看板相应减少。' +
      '只更新 config.json 并重新扫描登记，不搬移、不删除任何技能文件。',
      '接管该仓库', function () {
        closeModal();
        frBusy = true;
        renderFirstRun();
        runProgress(900, function () {
          SKB.adoptStore(c.store).then(function (res) {
            frBusy = false;
            applyBootstrap(res);
            var act = res.action || {};
            if (SKILLS.length) setState({ board: 'shared', folder: null, selected: SKILLS[0].name });
            toast('ok', '已接管中央仓库',
              (act.skills || 0) + ' 个技能进入共有看板 · 原仓库 ' + (act.previous || ''));
          }).catch(function (err) {
            frBusy = false;
            toast('bad', '接管失败', String(err && err.message || err));
          });
        });
      });
  }

  function requestAdoptLocal() {
    var m = firstRunModel();
    if (!m || !m.items.length || frBusy) return;
    var uniq = {};
    m.items.forEach(function (a) { uniq[a.name] = 1; });
    var names = Object.keys(uniq);
    confirmModal('把 ' + names.length + ' 个技能收进中央仓库？',
      '来源是 ' + m.agents.join('、') + ' 的主 skills 目录（磁盘上 ' + m.items.length +
      ' 份副本，同名同内容只收一份）。每个技能拷贝一份进仓库并登记为共有；' +
      '分发开关开着的 agent 会把源目录对齐成指向仓库的链接，其余原样保留。',
      '开始收编', function () {
        closeModal();
        frBusy = true;
        renderFirstRun();
        runProgress(1200, function () {
          SKB.adoptLocalSkills().then(function (res) {
            frBusy = false;
            applyBootstrap(res);
            var act = res.action || {};
            var got = (act.adopted || []).length;
            var align = (act.relinked || 0) + (act.linked || 0);
            var skip = (act.skipped || []).length;
            if (SKILLS.length) setState({ board: 'shared', folder: null, selected: SKILLS[0].name });
            toast(got ? 'ok' : 'warn', got ? '已收编 ' + got + ' 个技能' : '没有可收编的技能',
              (skip ? skip + ' 个已跳过（同名不同内容等），到独有看板逐条处置'
                    : '源目录已保留') + (align ? ' · 对齐 ' + align + ' 处分发链接' : ''));
          }).catch(function (err) {
            frBusy = false;
            toast('bad', '收编失败', String(err && err.message || err));
          });
        });
      });
  }

  /* ---------------------------------------------------------- render: content / list */
  // 网格视图只在「共有 / 独有看板 + 根层级 + 该看板视图=网格」出现；
  // 文件夹 / agent 文件夹 / 未归档永远是列表 + 面包屑。
  function renderContent(animate) {
    var gridRoot = document.getElementById('grid-root');
    var listCard = document.getElementById('list-card');
    var crumbBar = document.getElementById('crumb-bar');
    renderFirstRun();
    // 首启空仓库：共有看板的内容区就是引导面板本身。再挂一面「0 个 skill 的
    // 分类夹墙」或「没有匹配的 skill」空态只会误导，两个容器一起收起。
    if (state.board === 'shared' && !state.folder && !SKILLS.length &&
        !document.getElementById('first-run').hidden) {
      listCard.hidden = true;
      gridRoot.hidden = true;
      gridRoot.innerHTML = '';
      crumbBar.hidden = true;
      renderFilters();
      return;
    }
    var gridView = (state.board === 'shared' || state.board === 'unique') &&
      !state.folder && viewOf(state.board) === 'grid';

    if (gridView) {
      listCard.hidden = true;
      crumbBar.hidden = true;
      gridRoot.hidden = false;
      gridRoot.innerHTML = state.board === 'unique' ? uniqueGridHtml() : gridHtml();
      renderFilters();
      return;
    }

    gridRoot.hidden = true;
    gridRoot.innerHTML = '';
    listCard.hidden = false;
    var inFolder = (state.board === 'shared' || state.board === 'unique') && !!state.folder;
    crumbBar.hidden = !inFolder;
    if (inFolder) crumbBar.innerHTML = crumbHtml();
    renderList(animate);
  }

  function renderList(animate) {
    var isUnique = state.board === 'unique';
    var starredView = isStarredView();
    var inFolder = (state.board === 'shared' || isUnique) && !!state.folder;
    // 独有根层级按「逻辑技能」展示：同名同内容的多 agent 副本只占一行，
    // 不再把 7 份 brandkit 列成 7 条；agent 夹内仍是实例行。
    var uniqueRoot = isUnique && !inFolder && !starredView;
    var all = uniqueRoot ? uniqueGroupRows() : currentRows();
    var rows;
    var narrow = false; // 5 列行（进入文件夹的共有行 / 独有看板所有行 / 收藏视图）
    if (starredView) {
      // currentRows 已按收藏过滤 + 当前排序（共有在前、独有按 agent 在后）
      rows = all;
      narrow = true;
    } else if (inFolder) {
      rows = isUnique ? skillsInAgentFolder(state.folder) : skillsInFolder(state.folder);
      narrow = true;
    } else {
      rows = sortRows(filterRows(all));
      if (isUnique) narrow = true;
    }

    // 过滤后选中项失效时，自动选中第一条结果；无结果则清空选中。
    // 这是渲染期自愈，不走 setState——setState 的渲染会再次进到这里，走它就递归了。
    var selChanged = false;
    if (!rows.length) {
      if (state.selected) { state.selected = null; selChanged = true; }
    } else if (!rows.some(function (r) { return r.key === state.selected; })) {
      state.selected = rows[0].key;
      selChanged = true;
    }
    if (selChanged) renderInspector();

    renderFilters();

    var head = document.getElementById('list-head');
    head.classList.toggle('in-folder', narrow);
    document.getElementById('col-mid').hidden = narrow;

    var body = document.getElementById('list-body');
    var empty = document.getElementById('list-empty');

    if (!rows.length) {
      body.innerHTML = '';
      body.style.display = 'none';
      empty.hidden = false;
      // 「清除筛选」只对搜索空态有意义；收藏空 / 文件夹空这类引导空态藏起来
      var resetBtn = document.getElementById('reset-filter-btn');
      if (resetBtn) resetBtn.hidden = !state.q;
      updateFoot(0, all.length); // 空态也要刷新脚部计数，别留下「51 条」的旧文案
      return;
    }

    body.style.display = '';
    empty.hidden = true;

    if (state.board === 'agents') {
      body.innerHTML = renderAgentRows(rows, animate);
    } else if (starredView) {
      body.innerHTML = starredGroupedHtml(rows, animate);
    } else if (uniqueRoot) {
      // 独有根列表：一行一个逻辑技能（同名同内容已合并）；agent 夹内才是实例行
      body.innerHTML = rows.map(function (r, i) {
        return skillRowHtml(r, i, animate, true);
      }).join('');
    } else {
      body.innerHTML = rows.map(function (r, i) {
        return skillRowHtml(r, i, animate, narrow);
      }).join('');
    }
    // 脚部计数：文件夹内没有筛选概念，直接显示条数；其余显示「筛出 x / y」
    updateFoot(rows.length, inFolder ? rows.length : all.length);
  }

  // 星标按钮：未收藏 hover 显现（行 / 卡父级 hover），已收藏常显金星。
  // 点击走文档级 [data-star] 委托（先于行选中 return），不会误触发行选中。
  function starBtnHtml(key) {
    var on = isStarredKey(key);
    var tip = on ? '取消收藏' : '收藏';
    return '<button class="row-star' + (on ? ' on' : '') + '" data-star="' + esc(key) +
      '" title="' + tip + '" aria-label="' + tip + '">' + icon('star') + '</button>';
  }

  // 收藏视图列表：共有收藏平铺在前，独有收藏按 agent 分组在后（复用独有
  // 看板的分割线）；序号跨组连续。rows 已由 starredRows 排好。
  function starredGroupedHtml(rows, animate) {
    var html = '';
    var num = 0;
    var uRows = [];
    rows.forEach(function (r) {
      if (r.board === 'unique') { uRows.push(r); return; }
      html += skillRowHtml(r, num++, animate, true);
    });
    if (uRows.length) html += uniqueGroupedHtml(uRows, animate, num);
    return html;
  }

  // 逻辑技能组行：一行一个技能，副标题说明「N 个 agent 各有一份」，点进去看实例。
  // 不提供星标 / ⋯ 菜单（那些都按实例定位），避免把组 key 写进 meta。
  function groupRowHtml(r, i, animate, inFolder) {
    var g = r.groupData;
    var isSel = state.selected === r.key;
    var flag;
    var hasExtSrc = (g.srcs || []).some(function (s) { return s && s !== 'main'; });
    if (g.sameName) flag = '<span class="mini-flag conflict">同名不同内容</span>';
    else if (g.hasForeign) flag = '<span class="mini-flag conflict">' + icon('alert') + '外部链接</span>';
    else flag = '<span class="mini-flag unique">内容一致</span>';
    if (hasExtSrc) {
      flag += '<span class="mini-flag ext" title="来自 extra_dirs 外部目录或容器展开">外部</span>';
    }
    if (g.count > 1) flag += '<span class="mini-flag count">' + g.count + ' 个实例</span>';
    var marks = (g.marks || []).slice(0, 5).map(function (m, idx) {
      return '<span class="agent-chip" title="' + esc(g.toolLabels[idx] || '') + '">' +
        esc(m || '?') + '</span>';
    }).join('');
    if ((g.marks || []).length > 5) marks += '<span class="agent-chip">+' + (g.marks.length - 5) + '</span>';
    var note = '<span class="row-note is-empty">' +
      (g.count > 1 ? '点行查看 ' + g.count + ' 个实例' : '点行查看实例') + '</span>';
    var cover = '<div class="row-agent">' + marks +
      '<span class="count-text" style="margin-left:auto">' + (g.files || 0) + ' 文件</span></div>';
    return '<div class="row group-row' + (inFolder ? ' in-folder' : '') +
      (isSel ? ' is-selected' : '') + (r.conflict ? ' is-conflict' : '') +
      (animate ? '' : ' no-anim') +
      '" data-key="' + esc(r.key) + '" data-row-board="unique" data-group-row="1"' +
      ' role="button" tabindex="0"' +
      (animate ? ' style="animation-delay:' + Math.min(i, 14) * 12 + 'ms"' : '') + '>' +
      '<span class="row-mark row-num">' + (i + 1) + '</span>' +
      '<div class="row-main"><div class="row-title"><span class="row-name">' +
      esc(r.display) + '</span>' + flag + '</div>' +
      '<span class="row-label">' + esc(rowSecondary(r)) + '</span></div>' +
      note + cover +
      '<span class="row-go">' + icon('chevron-right') + '</span>' +
      '</div>';
  }

  // 单条 skill 行（共有 / 独有看板共用）；inFolder = 5 列窄行
  function skillRowHtml(r, i, animate, inFolder) {
    if (r.group) return groupRowHtml(r, i, animate, inFolder);
    var secondary = rowSecondary(r);
    var isSel = state.selected === r.key;
    var conflictFlag;
    if (r.board === 'shared') {
      conflictFlag = (r.conflict
        ? '<span class="mini-flag conflict">' + icon('alert') + '冲突</span>' : '') +
        contractFlagHtml(r);
    } else if (r.entry.sameName) {
      conflictFlag = '<span class="mini-flag conflict">同名覆盖</span>';
    } else if (r.entry.kind === 'foreign-link') {
      conflictFlag = '<span class="mini-flag conflict">' + icon('alert') + '外部链接</span>';
    } else {
      conflictFlag = '<span class="mini-flag unique">独占</span>';
    }
    if (r.board === 'unique' && r.entry.src && r.entry.src !== 'main') {
      conflictFlag += '<span class="mini-flag ext" title="来自外部目录 ' + esc(r.entry.src) +
        '，本工具不分发；需要移除可在详情里卸载">外部</span>';
    }

    var note = r.note
      ? '<span class="row-note">' + esc(r.note) + '</span>'
      : '<span class="row-note is-empty">添加备注…</span>';

    var cover;
    if (isStarredView()) {
      // 收藏视图：第 5 列显示收藏时间（取代分发覆盖 / 来源）
      cover = '<div class="row-cover"><span class="row-starred-at">' +
        fmtStarredAt(r.starredAt) + '</span></div>';
    } else if (r.board === 'shared') {
      var linked = r.skill.agents || [];
      var real = r.skill.realIn || [];
      var segs = ENABLED.map(function (t) {
        var bad = real.indexOf(t.key) > -1;
        var ok = linked.indexOf(t.key) > -1;
        var cls = bad ? 'bad' : ok ? '' : 'none';
        var tip = bad ? '同名冲突' : ok ? '已分发' : '未分发';
        return '<span class="cover-seg ' + cls + '" title="' + esc(t.label + ' · ' + tip) + '"></span>';
      }).join('');
      var okCount = linked.length;
      var coverCls = 'cover-text' + (r.conflict ? ' bad' : okCount < ENABLED.length ? ' is-partial' : '');
      cover = '<div class="row-cover"><div class="cover-bar">' + segs + '</div>' +
        '<span class="' + coverCls + '">' + okCount + '/' + ENABLED.length + '</span></div>';
    } else {
      var sharedNames = (r.entry.sharedWith || []).map(toolLabelOf);
      var sharedTip = sharedNames.length
        ? ' title="' + esc(sharedNames.join('、') + ' 也读取该目录') + '"' : '';
      cover = '<div class="row-agent"><span class="agent-chip"' + sharedTip + '>' + esc(r.entry.mark) + ' ' + esc(r.entry.toolLabel) + '</span>' +
        '<span class="count-text" style="margin-left:auto">' + r.entry.files + ' 文件</span></div>';
    }

    var m = meta[r.key];
    var edited = !!(m && (m.display || m.note));
    var midCol = inFolder ? '' : '<div class="row-folder">' + folderChipHtml(r.folder) + '</div>';

    return '<div class="row' + (inFolder ? ' in-folder' : '') + (isSel ? ' is-selected' : '') +
      (r.conflict ? ' is-conflict' : '') + (animate ? '' : ' no-anim') +
      '" data-key="' + esc(r.key) + '" data-row-board="' + r.board + '" role="button" tabindex="0"' +
      (r.board === 'shared' ? ' draggable="true"' : '') +
      (animate ? ' style="animation-delay:' + Math.min(i, 14) * 12 + 'ms"' : '') + '>' +
      // 序号列：按当前列表顺序 1..N（筛选 / 搜索 / 改排序后重新从 1 编），纯展示
      '<span class="row-mark row-num">' + (i + 1) + '</span>' +
      '<div class="row-main"><div class="row-title"><span class="row-name">' + esc(r.display) +
      (edited ? '<span class="edited-dot" title="已编辑"></span>' : '') +
      '</span>' + conflictFlag + '</div>' +
      (secondary ? '<span class="row-label">' + esc(secondary) + '</span>' : '') + '</div>' +
      midCol +
      note +
      cover +
      '<span class="row-go">' +
      starBtnHtml(r.key) +
      (r.board === 'shared'
        ? '<button class="row-more" data-more-skill="' + esc(r.key) + '" title="移动 / 更多操作">' + icon('ellipsis') + '</button>'
        : '') +
      icon('chevron-right') +
      '</span>' +
      '</div>';
  }

  /* ---------------------------------------------------------- render: grid / crumb */
  function folderCardHtml(f) {
    var list = skillsInFolder(f.id);
    var chips = list.slice(0, 3).map(function (r) {
      return '<span class="fc-chip" title="' + esc(r.display) + '">' + esc(r.display) + '</span>';
    }).join('');
    if (list.length > 3) chips += '<span class="fc-chip more">+' + (list.length - 3) + '</span>';
    if (!list.length) chips = '<span class="fc-chip none">空文件夹</span>';
    return '<div class="folder-card" role="button" tabindex="0" data-folder-card="' + esc(f.id) +
      '" data-drop-folder="' + esc(f.id) + '" style="' + catVars(f.color) + '">' +
      '<div class="fc-top"><span class="fc-icon">' + icon('folder') + '</span>' +
      '<button class="fc-more" data-more-folder="' + esc(f.id) + '" title="重命名 / 删除">' + icon('ellipsis') + '</button></div>' +
      '<div class="fc-name">' + esc(f.name) + '</div>' +
      '<div class="fc-meta">' + list.length + ' 个 skill</div>' +
      '<div class="fc-chips">' + chips + '</div></div>';
  }

  function fileCardHtml(r) {
    if (r.group) {
      var g = r.groupData;
      var gm = (g.marks || []).slice(0, 4).join(' ') || mark(g.name);
      return '<div class="file-card group-card' + (state.selected === r.key ? ' is-selected' : '') +
        '" role="button" tabindex="0" data-key="' + esc(r.key) + '" data-group-row="1">' +
        '<span class="fc-mark">' + esc(gm) + '</span>' +
        '<div class="fc-main"><b>' + esc(r.display) + '</b><span>' +
        esc(groupSubText(g)) + '</span></div>' +
        '</div>';
    }
    var isSel = state.selected === r.key;
    // 共有卡副标题 = 归属夹；独有卡副标题 = 来源 agent（同名覆盖一并标出）
    var sub;
    var mk;
    if (r.board === 'unique') {
      sub = r.entry.toolLabel + (r.entry.sameName ? ' · 同名覆盖' : '');
      mk = r.entry.mark || mark(r.name);
    } else {
      var f = folderById(r.folder);
      sub = f ? '已归档 · ' + f.name : (r.skill && r.skill.isNew ? '刚导入' : '未归档');
      mk = mark(r.key);
    }
    // 独有 skill 不能跨 agent 移动：不设 draggable，也不出 ⋯ 菜单；星标两板都给
    return '<div class="file-card' + (isSel ? ' is-selected' : '') +
      (isStarredKey(r.key) ? ' is-starred' : '') +
      (r.board === 'shared' && r.skill && r.skill.isNew ? ' is-new' : '') +
      '" role="button" tabindex="0" data-key="' +
      esc(r.key) + '"' + (r.board === 'shared' ? ' draggable="true"' : '') + '>' +
      '<span class="fc-mark">' + esc(mk) + '</span>' +
      '<div class="fc-main"><b>' + esc(r.display) + '</b><span>' + esc(sub) + '</span></div>' +
      starBtnHtml(r.key) +
      (r.board === 'shared'
        ? '<button class="row-more" data-more-skill="' + esc(r.key) + '" title="移动 / 更多操作">' + icon('ellipsis') + '</button>'
        : '') +
      '</div>';
  }

  function gridHtml() {
    var html = '';
    if (state.q) {
      // 搜索态：结果以文件卡流展示（filterRows 已含名称优先 + 回退匹配）
      var hits = sortRows(filterRows(sharedRows()));
      html += '<div class="grid-sect"><b>搜索结果</b><span>“' + esc(state.q) + '”</span><span class="count-text">' +
        hits.length + ' 个匹配</span></div>';
      html += hits.length
        ? '<div class="folder-grid">' + hits.map(fileCardHtml).join('') + '</div>'
        : '<div class="empty" style="min-height:180px"><div class="empty-icon">' + icon('search') +
          '</div><b>没有匹配的 skill</b><span>换个关键词试试</span></div>';
      return html;
    }
    html += '<div class="grid-sect"><b>分类文件夹</b><span>点击进入 · 拖拽 skill 卡片到文件夹即可归档</span>' +
      '<span class="count-text">' + FOLDERS.length + ' 个文件夹</span></div>';
    html += '<div class="folder-grid">' + FOLDERS.map(folderCardHtml).join('') +
      '<button class="folder-card ghost" id="grid-new-folder">' +
      '<span class="fc-icon">' + icon('folder-plus') + '</span>' +
      '<span><span class="fc-name">新建分类</span><span class="fc-meta" style="display:block">手动建分类，把 skill 归档进去</span></span>' +
      '</button></div>';
    var unfiled = skillsInFolder('none');
    html += '<div class="grid-sect"><b>未归档</b><span>还没放入任何分类的 skill</span>' +
      '<span class="count-text">' + unfiled.length + ' 个</span></div>';
    html += unfiled.length
      ? '<div class="folder-grid">' + unfiled.map(fileCardHtml).join('') + '</div>'
      : '<div class="empty" style="min-height:140px"><div class="empty-icon">' + icon('check') +
        '</div><b>全部已归档</b><span>每个 skill 都在某个分类文件夹里</span></div>';
    return html;
  }

  // 独有看板的 agent 文件夹卡：与共有分类夹同一套 folder-card 视觉（icon 换成
  // bot、颜色按 agentColor 散列）；agent 夹不是用户建的，没有重命名 / 删除的 ⋯
  function agentFolderCardHtml(g) {
    var chips = g.rows.slice(0, 3).map(function (r) {
      return '<span class="fc-chip" title="' + esc(r.display) + '">' + esc(r.display) + '</span>';
    }).join('');
    if (g.rows.length > 3) chips += '<span class="fc-chip more">+' + (g.rows.length - 3) + '</span>';
    if (!g.rows.length) chips = '<span class="fc-chip none">空文件夹</span>';
    return '<div class="folder-card" role="button" tabindex="0" data-folder-card="' + esc(g.tool) +
      '" style="' + catVars(g.color) + '">' +
      '<div class="fc-top"><span class="fc-icon">' + icon('bot') + '</span></div>' +
      '<div class="fc-name">' + esc(g.label) + '</div>' +
      '<div class="fc-meta">' + g.rows.length + ' 个 skill</div>' +
      '<div class="fc-chips">' + chips + '</div></div>';
  }

  // 独有看板网格：根层级 = agent 文件夹卡墙（一个 agent 一个分类夹）；
  // 搜索态 = 结果文件卡流。没有「未归档」段——独有 skill 恒归属某 agent。
  function uniqueGridHtml() {
    if (state.q) {
      var hits = sortRows(filterRows(uniqueGroupRows()));
      return '<div class="grid-sect"><b>搜索结果</b><span>“' + esc(state.q) + '”</span><span class="count-text">' +
        hits.length + ' 个逻辑技能</span></div>' +
        (hits.length
          ? '<div class="folder-grid">' + hits.map(fileCardHtml).join('') + '</div>'
          : '<div class="empty" style="min-height:180px"><div class="empty-icon">' + icon('search') +
            '</div><b>没有匹配的 skill</b><span>换个关键词试试</span></div>');
    }
    var groups = uniqueAgentGroups();
    var logical = uniqueGroupRows();
    if (!groups.length) {
      return '<div class="empty" style="min-height:180px"><div class="empty-icon">' + icon('check') +
        '</div><b>没有独有 skill</b><span>每个 agent 都与中央仓库完全一致</span></div>';
    }
    // 按数据自适应：只有真的发生了合并（逻辑数 < 实例数）时才把「逻辑技能」
    // 放第一段；全是单实例时先给 agent 文件夹，避免 100+ 张平铺卡把入口埋掉。
    var merged = logical.length < UNIQUE.length;
    var agentSect = '<div class="grid-sect"' + (merged ? ' style="margin-top:18px"' : '') + '>' +
      '<b>Agent 文件夹</b>' +
      '<span>按 agent 浏览它的独有 skill</span>' +
      '<span class="count-text">' + groups.length + ' 个 agent</span></div>' +
      '<div class="folder-grid">' + groups.map(agentFolderCardHtml).join('') + '</div>';
    var logicalSect = '<div class="grid-sect"' + (merged ? '' : ' style="margin-top:18px"') + '>' +
      '<b>' + (merged ? '逻辑技能' : '全部独有技能') + '</b>' +
      '<span>' + (merged
        ? '同名同内容的多 agent 副本合并为一条 · 点卡查看实例'
        : '没有发生合并，按技能平铺 · 点卡查看实例') + '</span>' +
      '<span class="count-text">' + logical.length + ' 个 / ' + UNIQUE.length + ' 实例</span></div>' +
      '<div class="folder-grid">' + logical.map(fileCardHtml).join('') + '</div>';
    return merged ? logicalSect + agentSect : agentSect + logicalSect;
  }

  // 面包屑内容（#crumb-bar 是容器）：共有根节点同时是「移出分类」的投放区；
  // 独有根节点只是返回入口（独有 skill 不能跨 agent 移动，没有移出语义）
  function crumbHtml() {
    var isUnique = state.board === 'unique';
    var parts = '<button class="crumb-root" data-crumb-root' + (isUnique ? '' : ' data-drop-unfile="1"') +
      ' title="' + (isUnique ? '返回全部独有技能' : '返回全部，也可把 skill 拖到这里移出分类') + '">' +
      icon('folder-open') + (isUnique ? '全部独有' : '全部技能') + '</button>' +
      '<span class="crumb-sep">' + icon('chevron-right') + '</span>';
    if (isUnique) {
      var ug = uniqueAgentGroups().filter(function (g) { return g.tool === state.folder; })[0];
      parts += '<span class="crumb-here" style="' + catVars(ug ? ug.color : 'slate') + '">' +
        icon('bot') + esc(ug ? ug.label : state.folder) + '</span>';
      parts += '<span class="crumb-meta">' + (ug ? ug.rows.length : 0) +
        ' 个 skill · 只存在于该 agent 的目录里</span>';
      return parts;
    }
    if (state.folder === 'starred') {
      // 收藏视图面包屑：金星胶囊 + 计数 + 清空收藏（两段确认）
      var sN = sharedRows().filter(function (r) { return isStarredKey(r.key); }).length;
      parts += '<span class="crumb-here star">' + icon('star') + '收藏</span>';
      parts += '<span class="crumb-meta">' + sN + ' 共有 + ' + (starCount() - sN) +
        ' 独有 · ' + (prefs.sort === 'starred' ? '按收藏时间倒序' : '跟随当前排序') + '</span>';
      parts += '<div class="crumb-actions">' +
        '<button class="btn btn-sm clear-star" data-clear-starred="1" title="取消全部收藏">' +
        icon('trash') + '清空收藏</button></div>';
      return parts;
    }
    if (state.folder === 'none') {
      parts += '<span class="crumb-here" style="--c:var(--ink-2);--cs:var(--surface-2)">' + icon('inbox') + '未归档</span>';
      parts += '<span class="crumb-meta">' + skillsInFolder('none').length + ' 个 skill 待归档</span>';
    } else {
      var f = folderById(state.folder);
      if (!f) return '';
      parts += '<span class="crumb-here" style="' + catVars(f.color) + '">' + icon('folder') + esc(f.name) + '</span>';
      parts += '<span class="crumb-meta">' + skillsInFolder(f.id).length + ' 个 skill · 拖拽行到「全部技能」可移出</span>';
      parts += '<div class="crumb-actions">' +
        '<button class="btn btn-sm" data-rename-folder="' + esc(f.id) + '">' + icon('pencil') + '重命名</button>' +
        '<button class="btn btn-sm btn-ghost" data-del-folder="' + esc(f.id) + '" style="color:var(--danger)">' +
        icon('trash') + '删除分类</button></div>';
    }
    return parts;
  }

  // 独有技能看板（根层级 + 列表视图）：同一 agent 的 skill 归为一组，组与组之间
  // 插一条 agent 分割线；网格视图与 agent 夹内不走这里（卡片流 / 平铺行）。
  // 组顺序按 UNIQUE 里 agent 的出现顺序（后端扫描顺序）；rows 已按当前排序稳定排好，
  // 分组是稳定划分，不打乱组内次序。分割线不是 .row，行选中与键盘导航不会命中它。
  // startNum：收藏视图里独有组接在共有之后，序号跨组连续（与独有看板同口径）
  function uniqueGroupedHtml(rows, animate, startNum) {
    var order = [];
    var groups = {};
    UNIQUE.forEach(function (u) {
      if (groups[u.tool]) return;
      groups[u.tool] = [];
      order.push(u.tool);
    });
    rows.forEach(function (r) {
      var t = r.entry.tool;
      if (!groups[t]) { groups[t] = []; order.push(t); }
      groups[t].push(r);
    });

    var html = '';
    var num = startNum || 0;
    order.forEach(function (tool) {
      var g = groups[tool];
      if (!g.length) return;
      var head = g[0].entry;
      var overridden = g.filter(function (r) { return r.entry.sameName; }).length;
      html += '<div class="agent-divider">' +
        '<span class="ad-mark">' + esc(head.mark || mark(head.toolLabel)) + '</span>' +
        '<span class="ad-name">' + esc(head.toolLabel) + '</span>' +
        '<span class="ad-count">' + g.length + ' 个 skill' +
        (overridden ? ' · ' + overridden + ' 同名覆盖' : '') + '</span>' +
        '<span class="ad-line"></span>' +
        '</div>';
      g.forEach(function (r) { html += skillRowHtml(r, num++, animate, true); });
    });
    return html;
  }

  function agentSwitchHtml(a) {
    if (a.status === 'missing') {
      return '<label class="switch is-disabled" title="未安装，无法开启">' +
        '<input type="checkbox" disabled aria-label="分发开关"><i></i></label>';
    }
    // 在途：保持用户拨到的那一头，禁用挡住重复提交，视觉给「进行中」的呼吸态。
    // data-agent-pending 是界面自己的状态标记（回归用例按它断言），不参与数据。
    if (a.pending) {
      return '<label class="switch is-pending" data-agent-switch="' + esc(a.key) + '"' +
        ' data-agent-pending="1" title="' +
        (a.enabled ? '正在按中央仓库建立链接…' : '正在撤下本工具建立的链接…') + '">' +
        '<input type="checkbox"' + (a.enabled ? ' checked' : '') + ' disabled aria-label="分发开关"><i></i></label>';
    }
    return '<label class="switch" data-agent-switch="' + esc(a.key) + '" title="' +
      (a.enabled ? '关闭后移除本工具建立的链接' : '开启后按中央仓库建立链接') + '">' +
      '<input type="checkbox"' + (a.enabled ? ' checked' : '') + ' aria-label="分发开关"><i></i></label>';
  }

  function renderAgentRows(rows, animate) {
    return rows.map(function (r, i) {
      var isSel = state.selected === r.key;
      var dirText = r.sub ? '~/' + r.sub.replace(/^~\//, '') :
        (r.status === 'missing' ? '未检测到目录' : r.dir);
      // 外部来源：agent 自己读的目录（如 ~/.agents/skills、Trae 内置技能目录）里
      // 与仓库同名的实体。本工具不管理这些，开关关掉也不影响它们被加载，
      // 所以必须显式标出来。徽标数字 = 所有外部目录的实体总数，tooltip 分目录说明。
      var extHtml = '';
      if (r.external && r.external.length) {
        var extTotal = r.external.reduce(function (s, e) { return s + e.count; }, 0);
        var extTip = r.external.map(function (e) {
          return e.path + '：' + e.count + ' 个';
        }).join('\n') + '\n本工具不分发这些目录，开关不影响它们；移除单个 skill 可在独有看板里卸载';
        extHtml = '<span class="ext-badge" title="' + esc(extTip) +
          '">外部 ' + extTotal + '</span>';
      }
      return '<div class="row agent-row' + (isSel ? ' is-selected' : '') +
        (r.status === 'missing' ? ' is-missing' : '') + (animate ? '' : ' no-anim') +
        '" data-key="' + esc(r.key) + '" data-row-board="agents" role="button" tabindex="0"' +
        (animate ? ' style="animation-delay:' + Math.min(i, 14) * 12 + 'ms"' : '') + '>' +
        // 序号列：与共有 / 独有看板同款，按当前列表顺序 1..N（状态筛选后重新从 1 编），纯展示
        '<span class="row-mark row-num">' + (i + 1) + '</span>' +
        '<div class="row-main"><div class="row-title"><span class="row-name">' +
        esc(r.label) + '</span>' + extHtml + '</div>' +
        '<span class="row-label">' + esc(r.key) + '</span></div>' +
        '<div><span class="agent-state ' + r.status + (r.pending ? ' is-pending' : '') +
        '"><i></i>' + r.statusText +
        (r.status === 'connected' && !r.pending ? ' ' + r.linked : '') + '</span></div>' +
        '<div class="row-links" title="' + esc(r.dir) + '">' + esc(dirText) + '</div>' +
        '<div class="row-switch">' + agentSwitchHtml(r) + '</div>' +
        '<span class="row-go">' + icon('chevron-right') + '</span>' +
        '</div>';
    }).join('');
  }

  /* ---------------------------------------------------------- render: inspector */
  function selectedRow() {
    var g = groupOfKey(state.selected);
    if (g) return uniqueGroupRows().filter(function (r) { return r.key === state.selected; })[0] || null;
    var rows = currentRows();
    for (var i = 0; i < rows.length; i++) if (rows[i].key === state.selected) return rows[i];
    return null;
  }

  function renderInspector() {
    var r = selectedRow();
    var body = document.getElementById('ins-body');
    var head = document.getElementById('ins-raw-name');
    var saveState = document.getElementById('save-state');
    var insStar = document.getElementById('ins-star');

    if (!r) {
      head.textContent = '—';
      saveState.hidden = true;
      if (insStar) insStar.hidden = true;
      body.innerHTML = '<div class="ins-empty">' +
        '<div class="empty-icon">' + icon(state.board === 'agents' ? 'bot' : 'pencil') + '</div>' +
        '<b style="font-size:12.5px;color:var(--ink)">' +
        (state.board === 'agents' ? '选一个 agent' : '选一个 skill') + '</b>' +
        '<span style="font-size:11px">' +
        (state.board === 'agents'
          ? '左侧点任意一行，这里可以查看目录并切换分发开关'
          : '左侧点任意一行，这里可以改显示名、归属分类与备注') +
        '</span></div>';
      return;
    }

    saveState.hidden = true;
    if (r.group) {
      if (insStar) insStar.hidden = true; // 组不是单行实体，收藏按实例走
      head.textContent = r.name;
      renderGroupInspector(r);
      return;
    }
    if (r.board === 'agents') {
      if (insStar) insStar.hidden = true; // agent 不是 skill，不参与收藏
      renderAgentInspector(r);
      return;
    }
    if (insStar) {
      insStar.hidden = false;
      insStar.classList.toggle('on', isStarredKey(r.key));
      insStar.title = isStarredKey(r.key) ? '取消收藏' : '收藏';
      insStar.setAttribute('aria-label', insStar.title);
    }
    head.textContent = r.name;

    // 归属分类（单选）：未归档 + 各文件夹 + 新建；独有技能不参与归档
    var foldHtml = '';
    if (r.board === 'shared') {
      var cur = (meta[r.key] && meta[r.key].folder) || '';
      var chips = '<button class="fold-chip dashed' + (!cur ? ' on' : '') + '" data-assign="" ' +
        'style="--c:var(--ink-2);--cs:var(--surface-2)">' + icon('inbox') + '未归档</button>';
      chips += FOLDERS.map(function (f) {
        return '<button class="fold-chip' + (cur === f.id ? ' on' : '') + '" data-assign="' + esc(f.id) +
          '" style="' + catVars(f.color) + '">' + icon('folder') + esc(f.name) + '</button>';
      }).join('');
      chips += '<button class="fold-chip add" data-new-folder-for="' + esc(r.key) + '">' +
        icon('plus') + '新建分类</button>';
      foldHtml = '<div class="field-block">' +
        '<div class="field-label"><span>归属分类</span>' +
        '<span style="text-transform:none;letter-spacing:0;font-weight:400">单选 · 一个 skill 只在一个文件夹</span></div>' +
        '<div class="fold-picker">' + chips + '</div>' +
        '<div class="field-help">点 chip 即移动；也可以在列表 / 网格里直接拖拽。</div>' +
        '</div>';
    }

    var coverHtml = '';
    var missingCount = 0;
    if (r.board === 'shared') {
      var linked = r.skill.agents || [];
      var real = r.skill.realIn || [];
      coverHtml = ENABLED.map(function (t) {
        var bad = real.indexOf(t.key) > -1;
        var ok = linked.indexOf(t.key) > -1;
        if (!ok && !bad) missingCount++;
        var cls = bad ? ' is-bad' : ok ? '' : ' is-missing';
        var tip = (bad ? '同名冲突' : ok ? '已分发' : '未分发') + ' · ' + t.dir;
        return '<div class="cover-item' + cls + '" title="' + esc(tip) + '">' +
          '<b>' + esc(t.mark) + '</b><span>' + esc(t.label) + '</span>' +
          icon(bad ? 'alert' : ok ? 'check' : 'minus') + '</div>';
      }).join('');
      if (prefs.showDisabled) {
        var offs = TOOLS.filter(function (t) { return !t.enabled; });
        coverHtml += offs.map(function (t) {
          return '<div class="cover-item is-off" title="未启用分发 · ' + esc(t.dir) + '">' +
            '<b>' + esc(t.mark) + '</b><span>' + esc(t.label) + '</span>' +
            icon('lock') + '</div>';
        }).join('');
      }
    } else {
      var sharedNames = (r.entry.sharedWith || []).map(toolLabelOf);
      coverHtml = '<div class="meta-list">' +
        '<div class="meta-row"><span>来源 agent</span><b>' + esc(r.entry.toolLabel) + ' · ' + esc(r.entry.mark) +
        (r.entry.agentEnabled === false ? '（分发已关）' : '') + '</b></div>' +
        (sharedNames.length
          ? '<div class="meta-row"><span>共读 agent</span><b>' + esc(sharedNames.join('、')) + '</b></div>'
          : '') +
        '<div class="meta-row"><span>来源目录</span><b>' +
        (r.entry.src === 'main' || !r.entry.src ? 'agent 的 skills 主目录'
          : /^[A-Za-z]:/.test(r.entry.src) ? esc(r.entry.src) : '~/' + esc(r.entry.src)) + '</b></div>' +
        '<div class="meta-row"><span>文件数</span><b>' + r.entry.files + ' 个 · ' + fmtBytes(r.entry.bytes) + '</b></div>' +
        (r.entry.sameName
          ? '<div class="meta-row"><span>中央副本</span><b>' + r.entry.storeFiles + ' 个 · ' + fmtBytes(r.entry.storeBytes) + '</b></div>'
          : '<div class="meta-row"><span>中央副本</span><b>不存在</b></div>') +
        '</div>';
    }

    var metaHtml = '';
    if (r.board === 'shared') {
      metaHtml = '<div class="meta-list">' +
        '<div class="meta-row"><span>目录名</span><b>' + esc(r.name) + '</b></div>' +
        '<div class="meta-row"><span>内容哈希</span><b>' + esc(r.skill.hash) + '</b></div>' +
        '<div class="meta-row"><span>入库来源</span><b>' + esc(r.skill.origin) + '</b></div>' +
        '<div class="meta-row"><span>覆盖 agent</span><b>' + (r.skill.agents || []).length + ' / ' + ENABLED.length + '</b></div>' +
        '</div>';
    } else {
      metaHtml = '<div class="meta-list">' +
        '<div class="meta-row"><span>目录名</span><b>' + esc(r.name) + '</b></div>' +
        '<div class="meta-row"><span>归属</span><b>' + esc(r.entry.toolLabel) + '</b></div>' +
        '<div class="meta-row"><span>类型</span><b>' +
        (r.entry.sameName ? '同名但内容不同'
          : r.entry.kind === 'foreign-link' ? '外部链接（真身在 agent 目录之外）'
          : '仅此 agent 有') + '</b></div>' +
        '</div>';
    }

    var path = r.board === 'shared' ? STORE + '\\' + r.name : r.entry.path;

    var diffHtml = '';
    // 同名覆盖行（独有）或与某 agent 实体冲突的共有行都可看差异；
    // diff() 按现算返回，全局 CONFLICT 快照只用来展示计数（对得上才展示）。
    var diffEligible = r.board === 'unique' ? r.entry.sameName : r.conflict;
    if (diffEligible) {
      var diffMatch = CONFLICT && r.name === CONFLICT.skill &&
        (r.board === 'unique' ? CONFLICT.tool === r.entry.tool : true);
      diffHtml = '<div class="field-block"><div class="diff-box">' +
        '<div class="diff-title">' + icon('alert') + '同名副本内容不一致</div>' +
        (diffMatch
          ? '<div class="diff-text">中央 ' + CONFLICT.central.count + ' 个文件 / ' + fmtBytes(CONFLICT.central.bytes) +
            '，本地 ' + CONFLICT.local.count + ' 个文件 / ' + fmtBytes(CONFLICT.local.bytes) +
            '。' + CONFLICT.commonDiffs + ' 个公共文件大小不同，冲突不会自动合并。</div>'
          : '') +
        '<button class="btn btn-sm diff-btn" data-diff="1" data-diff-scope="' + r.board +
        '" data-diff-key="' + esc(r.key) + '">' + icon('eye') + '查看文件差异</button>' +
        '</div></div>';
    }

    var contractHtml = '';
    if (r.board === 'shared' && contractIssue(r)) {
      contractHtml = '<div class="field-block"><div class="diff-box">' +
        '<div class="diff-title">' + icon('alert') + '包内声明名与目录名不一致</div>' +
        '<div class="diff-text">目录名 <b>' + esc(r.name) + '</b> 才是各 agent 实际注册的' +
        '名字（分发就是按它建链接）；SKILL.md 里声明的是 <b>' + esc(contractIssue(r)) +
        '</b>。名字分叉时链接照样全绿，但按第二行那个名字在 agent 里调用一定失败。' +
        '对齐只会改写 SKILL.md 的 name 一行，正文、描述与所有 agent 的链接都不动。</div>' +
        '<button class="btn btn-sm" data-align-name="' + esc(r.name) + '">' +
        icon('check') + '把声明名对齐到目录名</button>' +
        '</div></div>';
    }

    var desc = r.board === 'shared' && r.skill.description
      ? '<div class="field-block"><div class="field-label">描述</div><div class="desc-text">' + esc(r.skill.description) + '</div></div>'
      : '';

    // 收藏信息：已收藏给「收藏于」行；未收藏不占版面（头部星标已是入口）
    var starMetaHtml = isStarredKey(r.key)
      ? '<div class="field-block"><div class="meta-list">' +
        '<div class="meta-row star-row"><span>收藏于</span><b>' + fmtStarredAt(starredAtOf(r.key)) + '</b>' +
        icon('star') + '</div></div></div>'
      : '';

    // 一键收编：实体行与可解析真身的 foreign-link 都给入口（后端
    // resolve_adopt_target 校验真身可读且不在仓库内）；同名覆盖走差异处置。
    // 文案按行形态区分，避免用户以为在动第三方链接的目标文件。
    var promoteHtml = '';
    var promotable = r.entry && (r.entry.kind === 'unique' || r.entry.kind === 'foreign-link');
    if (r.board === 'unique' && promotable && !r.entry.sameName) {
      var linkKind = r.entry.kind === 'foreign-link';
      var align = r.entry.src === 'main' && r.entry.agentEnabled !== false;
      promoteHtml = '<div class="promote-box">' +
        '<div class="promote-title">' + icon('upload') + '收进中央仓库</div>' +
        '<p>' + (linkKind
          ? '链接真身的内容会拷贝进中央仓库成为共有技能（' + r.entry.files + ' 个文件 / ' +
            fmtBytes(r.entry.bytes) + '）。' +
            (align
              ? '「' + esc(r.entry.toolLabel) + '」的分发开关开着，收编后链接改指中央仓库。'
              : '分发开关关着：保留原链接，真身仍在原路径，本工具不删第三方文件。')
          : '把整个目录拷贝进中央仓库成为共有技能（' + r.entry.files + ' 个文件 / ' +
            fmtBytes(r.entry.bytes) + '），之后可统一分发给所有 agent。' +
            (align
              ? '「' + esc(r.entry.toolLabel) + '」的分发开关开着，收编后源副本会按分发方式对齐。'
              : '源目录保留不动，' + esc(r.entry.toolLabel) + ' 不受影响。') +
            '已写过的显示名与备注会一并带过去。') +
        '</p>' +
        '<button class="btn btn-primary" data-promote="1">' + icon('upload') +
        (linkKind ? '按真身收进仓库' : '转为共有技能') + '</button>' +
        '</div>';
    }

    body.innerHTML =
      '<div class="field-block">' +
        '<div class="field-label"><span>显示名称</span>' +
        '<button class="reset-link" data-reset="1">还原</button></div>' +
        '<input class="name-input" id="ins-name" type="text" maxlength="48" value="' + esc(r.display) + '" spellcheck="false">' +
        '<div class="field-help">只改界面里的叫法，磁盘目录名 <b style="font-family:var(--mono)">' + esc(r.name) + '</b> 不变。</div>' +
      '</div>' +
      foldHtml +
      '<div class="field-block">' +
        '<div class="field-label"><span>备注</span></div>' +
        '<textarea class="note-area" id="ins-note" maxlength="400" placeholder="记下这个 skill 的用途、坑、来源…">' + esc(r.note) + '</textarea>' +
        '<div class="note-meta"><span>自动保存到' + (MODE === 'db' ? ' skilllocker.db' : '本机') + '</span><span id="note-count">' + r.note.length + ' / 400</span></div>' +
      '</div>' +
      starMetaHtml +
      contractHtml +
      diffHtml +
      '<div class="divider"></div>' +
      '<div class="field-block">' +
        '<div class="field-label"><span>' + (r.board === 'shared' ? '分发覆盖' : '本地副本') + '</span>' +
        '<span style="text-transform:none;letter-spacing:0;font-weight:400">' +
        (r.board === 'shared'
          ? '已分发 ' + (r.skill.agents || []).length + ' / ' + ENABLED.length
          : esc(r.entry.toolLabel)) + '</span></div>' +
        (r.board === 'shared' ? '<div class="cover-grid">' + coverHtml + '</div>' : coverHtml) +
        (r.board === 'shared' && missingCount
          ? '<div class="field-help">灰色 = 已启用但还没分发；重新打开该 agent 的分发开关即可补齐。</div>'
          : '') +
      '</div>' +
      '<div class="field-block">' + metaHtml + '</div>' +
      '<div class="field-block">' +
        '<div class="field-label"><span>路径</span></div>' +
        '<div class="path-box"><b>' + esc(path) + '</b>' +
        '<button data-copy="' + esc(path) + '" title="复制路径" aria-label="复制路径">' + icon('copy') + '</button></div>' +
      '</div>' +
      desc +
      '<div class="ins-actions">' +
        '<button class="btn" data-open="' + esc(path) + '" data-open-scope="' + r.board +
        '" data-open-key="' + esc(r.key) + '">' + icon('folder') + '打开目录</button>' +
        '<button class="btn" data-copy="' + esc(path) + '">' + icon('copy') + '复制路径</button>' +
      '</div>' +
      promoteHtml +
      (r.board === 'shared' && r.skill && r.skill.isNew
        ? '<div class="ins-import-hint"><b>刚导入</b> — 还没有分发给任何 agent。到「智能体」看板打开开关，或点顶栏刷新后同步。</div>'
        : '') +
      (r.board === 'unique' && r.entry.deletable === false
        ? '<div class="agent-hint ext">' + esc(
            r.entry.kind === 'foreign-link'
              ? '这是指向别处的链接，真身不归本工具管理：可在上方「按真身收进仓库」把内容纳入统一分发；需要移除时只摘链接，不碰真身。'
              : '来自外部目录 ' + r.entry.src + '，由 agent 自己安装/管理：本工具不分发；需要移除时可以卸载（永久删除该目录）。') + '</div>' +
          '<div class="ins-danger">' +
          '<button class="btn btn-danger" data-uninstall="1">' + icon('trash') +
          (r.entry.kind === 'foreign-link' ? '摘除链接（卸载）' : '卸载（删除外部目录）') + '</button>' +
          '<p>' + (r.entry.kind === 'foreign-link'
            ? '移除 junction 链接后 agent 不再加载该 skill；链接真身与中央仓库都不受影响。'
            : '永久删除 ' + esc(r.entry.path) + '（' + r.entry.files + ' 个文件 / ' + fmtBytes(r.entry.bytes) +
              '），不进回收站。该目录由 agent 自己安装，之后可能被它重新安装。') + '</p>' +
          '</div>'
        : '<div class="ins-danger">' +
          '<button class="btn btn-danger" data-delete="1">' + icon('trash') +
          (r.board === 'shared' ? '彻底删除 skill' : '删除本地副本') + '</button>' +
          '<p>' + (r.board === 'shared'
            ? '删除中央仓库实体并撤掉所有 agent 链接，操作不可恢复。'
            : '只删除这个 agent 目录下的副本，中央仓库的同名内容不受影响。') + '</p>' +
        '</div>');
  }

  // 逻辑技能组详情：只做汇总与实例入口。改名 / 备注 / 收编 / 删除都必须
  // 落到具体实例（tool/name），这里不提供会写组 key 的表单。
  function renderGroupInspector(r) {
    var body = document.getElementById('ins-body');
    var g = r.groupData;
    var inst = uniqueRows().filter(function (row) {
      return g.instances.indexOf(row.key) > -1;
    });
    var instHtml = inst.map(function (row) {
      var e = row.entry;
      var kindText = e.sameName ? '同名不同内容'
        : e.kind === 'foreign-link' ? '外部链接'
        : (e.src && e.src !== 'main') ? '外部目录（' + e.src + '）' : '仅此 agent 有';
      return '<div class="meta-list" style="margin-bottom:8px">' +
        '<div class="meta-row"><span>' + esc(e.mark || '') + ' ' + esc(e.toolLabel || e.tool) +
        '</span><b>' + esc(row.display) + '</b></div>' +
        '<div class="meta-row"><span>类型</span><b>' + esc(kindText) + '</b></div>' +
        '<div class="meta-row"><span>文件数</span><b>' + (e.files || 0) + ' 个 · ' +
        fmtBytes(e.bytes) + '</b></div>' +
        '<div class="meta-row"><span>路径</span><b style="font-family:var(--mono);font-size:10.5px">' +
        esc(e.path || '') + '</b></div>' +
        '<div style="display:flex;gap:6px;margin-top:6px">' +
        '<button class="btn btn-sm" data-open-instance="' + esc(row.key) + '">查看实例</button>' +
        '<button class="btn btn-sm" data-copy="' + esc(e.path || '') + '">复制路径</button>' +
        '</div></div>';
    }).join('');
    var diffHtml = '';
    if (g.sameName && inst.length) {
      diffHtml = '<div class="field-block"><div class="diff-box">' +
        '<div class="diff-title">' + icon('alert') + '中央仓库有同名但内容不同的副本</div>' +
        '<div class="diff-text">同名不同内容的实例不会被自动合并；收编会覆盖中央副本，' +
        '请先查看差异后用冲突处置决定保留哪一份。</div>' +
        '<button class="btn btn-sm diff-btn" data-diff="1" data-diff-scope="unique" ' +
        'data-diff-key="' + esc(inst[0].key) + '">' + icon('eye') + '查看文件差异</button>' +
        '</div></div>';
    }
    var note = g.sameName
      ? '同名但内容不同的实例已分成不同组，绝不会被合并成一条。'
      : (g.count > 1 ? '这些副本内容完全一致，合并展示；操作仍按 agent 实例进行。'
                     : '只有一个 agent 装有这份技能。');
    body.innerHTML =
      '<div class="field-block"><div class="meta-list">' +
      '<div class="meta-row"><span>逻辑技能</span><b>' + esc(g.name) + '</b></div>' +
      '<div class="meta-row"><span>实例</span><b>' + g.count + ' 个 · ' +
      esc((g.toolLabels || []).join('、')) + '</b></div>' +
      '<div class="meta-row"><span>内容哈希</span><b>' + esc((g.hash || '').slice(0, 12) || '—') + '</b></div>' +
      '<div class="meta-row"><span>合计</span><b>' + (g.files || 0) + ' 个文件 · ' +
      fmtBytes(g.bytes) + '</b></div>' +
      '</div><div class="field-help">' + esc(note) + '</div></div>' +
      diffHtml +
      '<div class="divider"></div>' +
      '<div class="field-block"><div class="field-label"><span>实例</span>' +
      '<span style="text-transform:none;letter-spacing:0;font-weight:400">点「查看实例」进入该 agent 的那一份</span></div>' +
      instHtml + '</div>';
  }

  function renderAgentInspector(a) {
    var head = document.getElementById('ins-raw-name');
    var body = document.getElementById('ins-body');
    head.textContent = a.key;

    var hint = {
      connected: '已建立 ' + a.linked + ' 个 junction，全部指向中央仓库。关闭开关只删除这些链接，不删除任何 skill 实体。',
      off: '分发已关闭，本机目录里没有本工具建立的链接。打开开关会按中央仓库重新建立。',
      missing: '本机没有检测到 ' + (a.detectDir || a.dir) +
        '。安装该 agent 并至少启动一次后，点“重新探测”即可。'
    }[a.status];

    // 外部来源说明：这些目录由 agent 自己安装/读取，开关管不到，必须写明
    var extHint = '';
    (a.external || []).forEach(function (e) {
      extHint += '<div class="agent-hint ext">' + esc(
        e.path + ' 里另有 ' + e.count + ' 个与中央仓库同名的实体（' +
        e.same + ' 个内容一致、' + e.diff + ' 个不同），是该 agent 自己安装/读取的；' +
        '本工具不分发这里，所以开关关闭后它们仍会被加载；' +
        '需要移除某个 skill 可在独有看板它的详情里卸载。') + '</div>';
    });

    var extMeta = (a.external || []).map(function (e) {
      return '<div class="meta-row"><span>外部来源</span><b>' + esc(e.rel) + ' · ' +
        e.count + ' 个</b></div>';
    }).join('');

    body.innerHTML =
      '<div class="agent-hero ' + a.status + (a.pending ? ' is-pending' : '') + '">' +
        '<span class="agent-hero-mark">' + esc(a.mark) + '</span>' +
        '<div class="agent-hero-main"><b>' + esc(a.label) + '</b>' +
          '<span class="agent-state ' + a.status + (a.pending ? ' is-pending' : '') +
          '"><i></i>' + a.statusText + '</span></div>' +
        agentSwitchHtml(a) +
      '</div>' +
      '<div class="agent-hint">' + esc(hint) + '</div>' +
      extHint +
      '<div class="field-block">' +
        '<div class="field-label"><span>分发详情</span><span class="agent-mode">' + esc(a.mode) + '</span></div>' +
        '<div class="meta-list">' +
          '<div class="meta-row"><span>已链接 skill</span><b>' + a.linked + ' / ' + a.skillsTotal + '</b></div>' +
          (a.conflicts
            ? '<div class="meta-row"><span>同名冲突</span><b class="warn">' + a.conflicts + ' 处未动</b></div>'
            : '') +
          extMeta +
          '<div class="meta-row"><span>分发方式</span><b>' +
            (a.mode === 'copy' ? '实体拷贝' : '目录 junction') + '</b></div>' +
          '<div class="meta-row"><span>检测目录</span><b>' + esc(a.detectDir || '—') + '</b></div>' +
        '</div>' +
      '</div>' +
      '<div class="field-block">' +
        '<div class="field-label"><span>skills 目录</span></div>' +
        '<div class="path-box"><b>' + esc(a.dir || '—') + '</b>' +
        '<button data-copy="' + esc(a.dir) + '" title="复制路径" aria-label="复制路径">' +
        icon('copy') + '</button></div>' +
      '</div>' +
      '<div class="ins-actions">' +
        '<button class="btn" data-agent-open="' + esc(a.key) + '"' +
        (a.status === 'missing' ? ' disabled' : '') + '>' + icon('folder') + '打开目录</button>' +
        '<button class="btn" data-agent-rescan="1">' + icon('refresh') + '重新探测</button>' +
      '</div>';
  }

  /* ---------------------------------------------------------- editing */
  var saveTimer = null;

  function flashSaved() {
    var el = document.getElementById('save-state');
    if (!el) return;
    el.hidden = false;
    el.classList.remove('dirty');
    clearTimeout(el._t);
    el._t = setTimeout(function () { el.hidden = true; }, 1400);
  }

  function markDirty() {
    var el = document.getElementById('save-state');
    if (!el) return;
    el.hidden = false;
    el.classList.add('dirty');
    el.querySelector('span').textContent = '保存中…';
  }

  function commit(key, patch) {
    var m = metaOf(key);
    Object.keys(patch).forEach(function (k) { m[k] = patch[k]; });
    m.updated = Date.now();
    saveMeta(key);
    markDirty();
    renderChrome();
    patchRow(key);
    clearTimeout(saveTimer);
    saveTimer = setTimeout(function () {
      if (MODE === 'db') flushMeta();
      var el = document.getElementById('save-state');
      if (el) el.querySelector('span').textContent = '已保存';
      flashSaved();
    }, 260);
  }

  function patchRow(key) {
    // 列表行与网格文件卡都可能有这个 key：一并原位更新，避免整表重渲染丢焦点
    var esc_key = window.CSS && CSS.escape ? CSS.escape(key) : key;
    var r = currentRows().filter(function (x) { return x.key === key; })[0];
    if (!r) return;
    var m = meta[key];
    var edited = !!(m && (m.display || m.note));

    Array.prototype.forEach.call(
      document.querySelectorAll('.row[data-key="' + esc_key + '"], .file-card[data-key="' + esc_key + '"]'),
      function (row) {
        var nameEl = row.querySelector('.row-name') || row.querySelector('.fc-main b');
        if (nameEl) {
          nameEl.textContent = r.display;
          if (edited && row.classList.contains('row')) {
            var d = document.createElement('span');
            d.className = 'edited-dot';
            d.title = '已编辑';
            nameEl.appendChild(d);
          }
        }
        if (row.classList.contains('row')) {
          var foldEl = row.querySelector('.row-folder');
          if (foldEl) foldEl.innerHTML = folderChipHtml(r.folder);
          var noteEl = row.querySelector('.row-note');
          if (noteEl) {
            noteEl.textContent = r.note || '添加备注…';
            noteEl.classList.toggle('is-empty', !r.note);
          }
        } else {
          var subEl = row.querySelector('.fc-main span');
          if (subEl) {
            if (r.board === 'unique') {
              subEl.textContent = r.entry.toolLabel + (r.entry.sameName ? ' · 同名覆盖' : '');
            } else {
              var f = folderById(r.folder);
              subEl.textContent = f ? '已归档 · ' + f.name : '未归档';
            }
          }
        }
      });
  }

  /* ---------------------------------------------------------- 收藏动作 */
  // 收藏 / 取消收藏唯一入口：行尾星、卡片角星、检查器星标、⋯ 菜单、F 键都走这里。
  // 桌面端随 metaPayload 进 saveMeta 落库（starred_at 由后端只在变星时刻产生）；
  // 原型端整张 meta map 写 localStorage。
  function toggleStar(key) {
    if (String(key || '').indexOf('g#') === 0) {
      toast('info', '收藏按实例保存', '进入组详情选一个 agent 的实例再收藏');
      return;
    }
    var m = metaOf(key);
    var on = !m.starred;
    m.starred = on ? 1 : 0;
    m.starredAt = on ? Date.now() : 0;
    saveMeta(key);
    if (isStarredView() && !on) {
      // 收藏视图内取消：整行右滑淡出后重渲染（计数 / 空态同步）
      var el = document.querySelector('.row[data-key="' + cssEscape(key) + '"], .file-card[data-key="' + cssEscape(key) + '"]');
      if (el) {
        el.classList.add('row-out');
        setTimeout(function () { renderChrome(); renderContent(false); renderInspector(); }, 190);
      } else {
        renderChrome(); renderContent(false); renderInspector();
      }
    } else if (prefs.sort === 'starred') {
      // 收藏优先排序下行的位置会变：整表重渲染
      renderChrome(); renderContent(false); renderInspector();
    } else {
      patchStar(key);
      renderChrome();
      renderInspector(); // 检查器星标态 + 收藏于行
    }
    if (on) starFreshPop(key);
    var label = displayOf(key, key);
    toast(on ? 'star' : 'info',
      on ? '已收藏 ' + label : '已取消收藏 ' + label,
      on ? '收藏夹现有 ' + starCount() + ' 个 · 侧栏 ★ 直达' : '剩余 ' + starCount() + ' 个');
  }

  // 原位更新某个 key 的星标控件（不整表重渲染，保住输入焦点场景）
  function patchStar(key) {
    var on = isStarredKey(key);
    var sel = '[data-star="' + cssEscape(key) + '"]';
    Array.prototype.forEach.call(document.querySelectorAll(sel), function (b) {
      b.classList.toggle('on', on);
      b.title = on ? '取消收藏' : '收藏';
      b.setAttribute('aria-label', b.title);
    });
    var card = document.querySelector('.file-card[data-key="' + cssEscape(key) + '"]');
    if (card) card.classList.toggle('is-starred', on);
  }

  // 重渲染后给「刚点的那颗星」补弹跳 + 光环（旧元素已被重建，动画类要重新挂）
  function starFreshPop(key) {
    var el = document.querySelector('.row-star[data-star="' + cssEscape(key) + '"], .fc-star[data-star="' + cssEscape(key) + '"]');
    if (!el) return;
    el.classList.add('pop', 'burst');
    setTimeout(function () { el.classList.remove('pop', 'burst'); }, 520);
  }

  var clearArmTimer = null;
  function clearStarredArm(btn) {
    if (!btn.classList.contains('arm')) {
      if (!starCount()) { toast('info', '收藏夹本来就是空的', '在行尾点 ☆ 即可收藏'); return; }
      // 两段确认：2.6s 不点第二下自动回弹，防误触
      btn.classList.add('arm');
      btn.innerHTML = icon('alert') + '确认清空？';
      clearArmTimer = setTimeout(function () { disarmClearStar(btn); }, 2600);
      return;
    }
    clearTimeout(clearArmTimer);
    var keys = Object.keys(meta).filter(function (k) { return meta[k] && meta[k].starred; });
    keys.forEach(function (k) {
      meta[k].starred = 0;
      meta[k].starredAt = 0;
      saveMeta(k);
    });
    disarmClearStar(btn);
    renderChrome(); renderContent(false); renderInspector();
    toast('warn', '已清空 ' + keys.length + ' 个收藏', 'skill 本体不受影响，重新点 ☆ 可再收藏');
  }

  function disarmClearStar(btn) {
    btn.classList.remove('arm');
    btn.innerHTML = icon('trash') + '清空收藏';
  }

  function cssEscape(s) {
    return (window.CSS && CSS.escape) ? CSS.escape(s) : String(s).replace(/"/g, '\\"');
  }

  /* ---------------------------------------------------------- diff modal */
  function openDiff(scope, key) {
    if (MODE === 'db' && SKB) {
      SKB.diff(scope || 'shared', key || '').then(function (res) {
        renderDiff(res.conflict);
      }).catch(function (err) {
        toast('bad', '差异读取失败', String(err && err.message || err));
      });
      return;
    }
    if (!CONFLICT) return;
    renderDiff(CONFLICT);
  }

  function renderDiff(c) {
    if (!c) return;
    var cMap = {}, lMap = {}, cFlag = {}, lFlag = {};
    (c.central.files || []).forEach(function (f) {
      cMap[f.name] = f.size;
      if (f.flag) cFlag[f.name] = f.flag;
    });
    (c.local.files || []).forEach(function (f) {
      lMap[f.name] = f.size;
      if (f.flag) lFlag[f.name] = f.flag;
    });

    var names = Object.keys(cMap).concat(Object.keys(lMap).filter(function (n) { return !cMap[n]; }));
    names.sort(function (a, b) {
      var pa = a === 'SKILL.md' ? 0 : a.endsWith('.md') ? 1 : 2;
      var pb = b === 'SKILL.md' ? 0 : b.endsWith('.md') ? 1 : 2;
      return pa - pb || a.localeCompare(b);
    });

    var rows = names.map(function (n) {
      var cs = cMap[n], ls = lMap[n];
      // 后端已按「大小 + 内容哈希」定过 flag（同大小不同内容也报 diff）；
      // 原型快照没有哈希信息，同大小按一致展示
      var flag = cFlag[n] === 'diff' || lFlag[n] === 'diff' ? 'diff'
        : cs == null ? 'local-only'
        : ls == null ? 'central-only' : 'same';
      var label = { 'local-only': '仅本地', 'central-only': '仅中央', 'diff': '不同', 'same': '一致' }[flag];
      return '<div class="diff-row"><span class="n">' + esc(n) + '</span>' +
        '<span class="sz">' + (cs == null ? '—' : fmtBytes(cs)) + '</span>' +
        '<span class="sz">' + (ls == null ? '—' : fmtBytes(ls)) + '</span>' +
        '<span class="fl"><span class="diff-flag ' + flag + '">' + label + '</span></span></div>';
    }).join('');

    openModal(
      '<div class="modal wide">' +
      '<button class="modal-close" data-close="1" aria-label="关闭">' + icon('x') + '</button>' +
      '<div class="modal-head">' +
        '<span class="modal-eyebrow">CONFLICT · ' + esc(c.tool) + ' / ' + esc(c.skill) + '</span>' +
        '<h2>同名 skill，两份内容不一致</h2>' +
      '</div>' +
      '<div class="modal-body">' +
        '<p>中央仓库 <b>' + c.central.count + ' 个文件 / ' + fmtBytes(c.central.bytes) + '</b>，本地 <b>' +
        c.local.count + ' 个文件 / ' + fmtBytes(c.local.bytes) + '</b>。共 ' + c.commonDiffs + ' 个公共文件大小不同。' +
        '这里只做对照，不会自动合并，也不会覆盖任何一份。</p>' +
        '<div class="path-box" style="margin-bottom:4px"><b>' + esc(c.central.path) + '</b></div>' +
        '<div class="path-box"><b>' + esc(c.local.path) + '</b></div>' +
        '<div class="diff-table">' +
          '<div class="diff-head"><span>文件</span><span style="text-align:right">中央</span><span style="text-align:right">本地</span><span style="text-align:right">对比</span></div>' +
          rows +
        '</div>' +
      '</div>' +
      '<div class="modal-foot">' +
        '<button class="btn" data-close="1">关闭</button>' +
        '<button class="btn btn-primary" data-close="1">知道了</button>' +
      '</div>' +
      '</div>'
    );
  }

  /* ---------------------------------------------------------- settings modal */
  function openSettings() {
    var edited = editedCount();
    var html =
      '<div class="modal">' +
      '<button class="modal-close" data-close="1" aria-label="关闭">' + icon('x') + '</button>' +
      '<div class="modal-head"><span class="modal-eyebrow">PREFERENCES</span><h2>设置与数据</h2></div>' +
      '<div class="modal-body">' +
        '<div class="setting-row">' +
          '<div class="setting-main"><b>主题</b><span>深色适合夜间；跟随系统由系统主题决定。</span></div>' +
          '<div class="segmented" id="set-theme">' +
            '<button data-theme="light" class="' + (prefs.theme === 'light' ? 'active' : '') + '">浅色</button>' +
            '<button data-theme="dark" class="' + (prefs.theme === 'dark' ? 'active' : '') + '">深色</button>' +
          '</div>' +
        '</div>' +
        '<div class="setting-row">' +
          '<div class="setting-main"><b>信息密度</b><span>紧凑把行高压到 44px，一屏多看 4 条。</span></div>' +
          '<div class="segmented" id="set-density">' +
            '<button data-density="comfortable" class="' + (prefs.density === 'comfortable' ? 'active' : '') + '">舒适</button>' +
            '<button data-density="compact" class="' + (prefs.density === 'compact' ? 'active' : '') + '">紧凑</button>' +
          '</div>' +
        '</div>' +
        '<div class="setting-row">' +
          '<div class="setting-main"><b>界面动效</b><span>关闭后保留状态反馈，去掉位移与淡入。</span></div>' +
          '<label class="switch"><input type="checkbox" id="set-motion" ' + (prefs.motion ? 'checked' : '') + ' aria-label="界面动效"><i></i></label>' +
        '</div>' +
        '<div class="setting-row">' +
          '<div class="setting-main"><b>显示已停用 agent</b><span>CodeBuddy / GitHub Copilot / Pi 只做只读展示。</span></div>' +
          '<label class="switch"><input type="checkbox" id="set-off" ' + (prefs.showDisabled ? 'checked' : '') + ' aria-label="显示已停用 agent"><i></i></label>' +
        '</div>' +
        '<div class="divider"></div>' +
        '<div class="setting-row">' +
          '<div class="setting-main"><b>我的编辑</b><span>' + edited + ' 条 skill 有显示名或备注改动，' +
            (MODE === 'db' ? '存在 skilllocker.db 的 skill_meta 表。' : '存在浏览器 localStorage。') + '</span></div>' +
          '<button class="btn btn-sm" id="set-export">' + icon('download') + '导出 JSON</button>' +
        '</div>' +
        '<div class="setting-row">' +
          '<div class="setting-main"><b>清空我的编辑</b><span>' +
            (MODE === 'db' ? '清空显示名与备注，归属回到预置分类夹；自建文件夹保留。' : '只清本机 UI 数据，不碰 skilllocker.db 与磁盘上的任何 skill。') +
            '</span></div>' +
          '<button class="btn btn-sm" id="set-clear" style="border-color:color-mix(in srgb,var(--danger) 26%,transparent);color:var(--danger)">' + icon('trash') + '清空</button>' +
        '</div>' +
        '<div class="divider"></div>' +
        '<div class="setting-row">' +
          '<div class="setting-main"><b>中央仓库</b><span>' + SKILLS.length + ' 个 skill · ' + esc(META.storeSize || '—') + ' · 快照 ' + esc(SCAN) + '</span></div>' +
          '<button class="btn btn-sm" data-copy="' + esc(STORE) + '">' + icon('copy') + '复制路径</button>' +
        '</div>' +
        '<div class="setting-row">' +
          '<div class="setting-main"><b>重新探测本机 agent</b><span>新装 agent 后重新读 adapters.json 的目录约定。</span></div>' +
          '<button class="btn btn-sm" id="set-detect">' + icon('refresh') + '探测</button>' +
        '</div>' +
        '<div class="divider"></div>' +
        '<p style="margin:0;font-size:11px;line-height:1.6;color:var(--ink-3)">' +
          (MODE === 'db'
            ? '桌面端：pywebview 直接 import skilllocker，不启 HTTP。显示名 / 备注 / 归属分类夹写入 skill_meta(scope, key) 表；' +
              '「清空我的编辑」只清显示名与备注、归属拉回预置夹，自建文件夹保留。'
            : '原型说明：桌面端用 pywebview 直接 import skilllocker，不启 HTTP。这里的改名 / 备注 / 归属只写 localStorage；' +
              '桌面端对应 skill_meta(scope, key) 表与 set_meta() 写库函数。') +
        '</p>' +
      '</div>' +
      '<div class="modal-foot"><button class="btn btn-primary" data-close="1">完成</button></div>' +
      '</div>';

    openModal(html);

    var root = document.getElementById('modal-root');

    root.querySelector('#set-theme').addEventListener('click', function (e) {
      var b = e.target.closest('[data-theme]');
      if (!b) return;
      prefs.theme = b.getAttribute('data-theme');
      savePrefs(); syncSeg(root, '#set-theme', 'theme', prefs.theme);
      renderChrome();
    });

    root.querySelector('#set-density').addEventListener('click', function (e) {
      var b = e.target.closest('[data-density]');
      if (!b) return;
      prefs.density = b.getAttribute('data-density');
      savePrefs(); syncSeg(root, '#set-density', 'density', prefs.density);
      renderChrome();
      renderList(false);
    });

    root.querySelector('#set-motion').addEventListener('change', function (e) {
      prefs.motion = e.target.checked; savePrefs(); renderChrome();
    });

    root.querySelector('#set-off').addEventListener('change', function (e) {
      prefs.showDisabled = e.target.checked; savePrefs(); renderInspector();
    });

    root.querySelector('#set-export').addEventListener('click', function () {
      var payload = {
        exportedAt: new Date().toISOString(),
        skills: meta,
        folders: FOLDERS
      };
      if (MODE === 'db' && SKB) {
        SKB.exportJson('skilllocker-meta.json', payload).then(function (res) {
          if (res && res.path) toast('ok', '已导出', res.path);
          else toast('info', '已取消', '没有选择保存位置');
        }).catch(function (err) {
          toast('bad', '导出失败', String(err && err.message || err));
        });
        return;
      }
      downloadJSON('skilllocker-meta.json', payload);
      toast('ok', '已导出', '共 ' + editedCount() + ' 条编辑记录');
    });

    root.querySelector('#set-clear').addEventListener('click', function () {
      confirmModal(
        '清空所有编辑？',
        MODE === 'db'
          ? '显示名与备注会清空，归属回到预置分类夹；收藏保留。不会改动磁盘上的任何 skill，也不会删除你自建的文件夹。'
          : '显示名与备注会清空，归属回到预置分类夹；收藏保留。不会改动 skilllocker.db 和磁盘上的任何 skill，自建文件夹保留。',
        '清空',
        function () {
          // 先关弹窗再发请求：连点时确认框还在，一次点击会冒出两个 change
          closeModal();
          if (MODE === 'db' && SKB) {
            SKB.reseedMeta().then(function (res) {
              applyBootstrap(res);
              toast('ok', '已清空', '显示名与备注已清空，归属回到预置分类夹，收藏保留');
            }).catch(function (err) {
              toast('bad', '清空失败', String(err && err.message || err));
            });
            return;
          }
          // 与桌面端 reseed_meta 同口径：只清显示名 / 备注 / 归属，收藏保留
          var stars = {};
          Object.keys(meta).forEach(function (k) {
            if (meta[k] && meta[k].starred) stars[k] = meta[k].starredAt || 0;
          });
          meta = {};
          lsDel(META_KEY);
          savePrefs();
          seedIfNeeded(); // prefs.folders 保留（自建夹不清），归属按规则重新播种
          Object.keys(stars).forEach(function (k) {
            if (!meta[k]) meta[k] = { display: '', note: '', folder: '', updated: 0 };
            meta[k].starred = 1;
            meta[k].starredAt = stars[k];
          });
          saveMeta();
          renderAll(true);
          toast('ok', '已清空', '显示名与备注已清空，归属回到预置分类夹，收藏保留');
        }
      );
    });

    root.querySelector('#set-detect').addEventListener('click', function () {
      closeModal();
      if (MODE === 'db' && SKB) {
        // 与全局刷新共用防重入：并发两条扫描链会各自渲染、各自弹提示，
        // 后端 OPS_LOCK 只是保证不把盘搞坏，救不了界面重复画
        if (refreshBusy) { toast('info', '扫描进行中', '请等待当前扫描完成'); return; }
        refreshBusy = true;
        var stop = startProgress(600);
        refreshTwoPhase(function (res, err) {
          stop();
          refreshBusy = false;
          if (err) { toast('bad', '探测失败', String(err && err.message || err)); return; }
          toast('ok', '探测完成', '识别到 ' + TOOLS.length + ' 个 agent，其中 ' +
            ENABLED.length + ' 个已启用');
        });
        return;
      }
      runProgress(900, function () {
        toast('info', '探测完成', '仍识别到 ' + TOOLS.length + ' 个 agent 目录，其中 ' + ENABLED.length + ' 个已启用');
      });
    });
  }

  function syncSeg(root, sel, attr, value) {
    Array.prototype.forEach.call(root.querySelectorAll(sel + ' button'), function (b) {
      b.classList.toggle('active', b.getAttribute('data-' + attr) === value);
    });
  }

  /* ---------------------------------------------------------- modal / toast */
  // 当前弹窗的取消回调（confirmModal 专用）：Esc 关闭也要走一遍，
  // 否则「关闭分发」这类确认框按 Esc 后复选框停在翻转态，界面撒谎
  var modalOnCancel = null;

  function openModal(html, opts) {
    var root = document.getElementById('modal-root');
    root.innerHTML = '<div class="overlay" data-overlay="1">' + html + '</div>';
    var overlay = root.firstElementChild;
    // locked（导入执行中）：遮罩点击不关闭——后端同步执行不可中断，UI 不许撒谎
    if (!(opts && opts.locked)) {
      overlay.addEventListener('mousedown', function (e) {
        if (e.target === overlay) closeModal();
      });
    }
    modalOnCancel = null;
    document.body.style.overflow = 'hidden';
  }

  function closeModal() {
    document.getElementById('modal-root').innerHTML = '';
    document.body.style.overflow = '';
    modalOnCancel = null;
    impClosed(); // 导入弹窗若开着，同步清它的状态与定时器（所有关闭路径都经过这里）
  }

  function confirmModal(title, text, okLabel, onOk, opts) {
    var danger = !!(opts && opts.danger);
    openModal(
      '<div class="modal">' +
      '<button class="modal-close" data-close="1" aria-label="关闭">' + icon('x') + '</button>' +
      '<div class="modal-head"><span class="modal-eyebrow">CONFIRM</span><h2>' + esc(title) + '</h2></div>' +
      '<div class="modal-body"><p>' + esc(text) + '</p></div>' +
      '<div class="modal-foot">' +
        '<button class="btn" data-close="1">取消</button>' +
        '<button class="btn ' + (danger ? 'btn-danger' : 'btn-primary') + '" id="confirm-ok">' + esc(okLabel) + '</button>' +
      '</div>' +
      '</div>'
    );
    document.getElementById('confirm-ok').addEventListener('click', onOk);
    // 取消 / 右上角关闭 / 点遮罩 / Esc：让调用方回滚自己先改过的界面状态
    if (opts && typeof opts.onCancel === 'function') {
      modalOnCancel = opts.onCancel;
      var cancels = document.querySelectorAll('#modal-root [data-close]');
      for (var i = 0; i < cancels.length; i++) {
        cancels[i].addEventListener('click', opts.onCancel);
      }
      var overlay = document.querySelector('#modal-root .overlay');
      if (overlay) {
        overlay.addEventListener('mousedown', function (e) {
          if (e.target === overlay) opts.onCancel();
        });
      }
    }
  }

  function toast(kind, title, text) {
    var wrap = document.getElementById('toasts');
    var el = document.createElement('div');
    el.className = 'toast';
    var icons = { ok: 'check', info: 'info', warn: 'alert', bad: 'alert', star: 'star' };
    var cls = kind === 'ok' ? '' : kind === 'star' ? ' gold' : ' ' + kind;
    el.innerHTML = '<span class="toast-icon' + cls + '">' + icon(icons[kind] || 'info') + '</span>' +
      '<div class="toast-main"><b>' + esc(title) + '</b>' + (text ? '<span>' + esc(text) + '</span>' : '') + '</div>' +
      '<button class="toast-close" aria-label="关闭">' + icon('x') + '</button>';
    wrap.appendChild(el);
    var kill = function () {
      el.classList.add('out');
      setTimeout(function () { el.remove(); }, 190);
    };
    el.querySelector('.toast-close').addEventListener('click', kill);
    setTimeout(kill, 3200);
  }

  function copyText(text) {
    var done = function () { toast('ok', '路径已复制', text); };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () { fallbackCopy(text, done); });
    } else {
      fallbackCopy(text, done);
    }
  }

  function fallbackCopy(text, done) {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); done(); } catch (e) { toast('bad', '复制失败', '请手动选中路径'); }
    ta.remove();
  }

  function downloadJSON(name, obj) {
    var blob = new Blob([JSON.stringify(obj, null, 2)], { type: 'application/json' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = name;
    a.click();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }

  function runProgress(ms, done) {
    var bar = document.getElementById('progress');
    bar.classList.add('show');
    var fill = bar.querySelector('i');
    fill.style.width = '0%';
    setTimeout(function () { fill.style.width = '72%'; }, 20);
    setTimeout(function () { fill.style.width = '100%'; }, ms * 0.55);
    setTimeout(function () {
      bar.classList.remove('show');
      fill.style.width = '0%';
      done();
    }, ms);
  }

  /* 进度条（立刻开工版）：条子照常亮起来，工作当场开始，返回 stop() 收尾。
     与 runProgress 的区别就是不再把 ms 的动画压在请求前面 —— 旧写法点一下
     刷新要先空等 600ms 才发出桥调用。stop() 保证最短可见 ms，别让一闪而过
     的条子看起来像没响应。 */
  function startProgress(ms) {
    var bar = document.getElementById('progress');
    bar.classList.add('show');
    var fill = bar.querySelector('i');
    fill.style.width = '0%';
    setTimeout(function () { fill.style.width = '72%'; }, 20);
    var t0 = Date.now();
    var closed = false;
    return function () {
      if (closed) return;
      closed = true;
      var left = Math.max(0, ms - (Date.now() - t0));
      setTimeout(function () {
        fill.style.width = '100%';
        setTimeout(function () {
          bar.classList.remove('show');
          fill.style.width = '0%';
        }, 160);
      }, left);
    };
  }

  /* 桌面端两段刷新：第一段（重扫中央仓库 + 共有/智能体看板）先画，
     第二段（独有扫描，本机占一次刷新的一半以上时间）补完再并入。
     cb(res, err)：res 是第二段回包（独有已补齐，meta 是修剪后的最终值）；
     第二段单独失败时 res 回第一段结果 —— 共有看板已经是对的，
     只补一句「独有扫描未完成」。 */
  function refreshTwoPhase(cb) {
    var first = null;
    SKB.rescanShared().then(function (res) {
      first = res;
      applyRefreshSnapshot(res);
      SKB.rescanUnique().then(function (snap) {
        applyUniqueSnapshot(snap);
        // 「扫到几个 skill / 分发了几处链接」是第一段的成果，第二段回包不带：
        // 并进同一份结果，调用方的收尾提示才能报完整这一次刷新做了什么。
        snap.scanned = first.scanned;
        snap.distributed = first.distributed;
        cb(snap, null);
      }, function (err) {
        // 独有段挂了：把「扫描中」的标记收掉，别让它一直挂在界面上不走；
        // 下一轮刷新会重扫独有。
        META.uniquePending = false;
        renderAll(false);
        toast('warn', '独有扫描未完成',
              String(err && err.message || err) + ' · 共有看板已更新');
        cb(first, null);
      });
    }, function (err) { cb(null, err); });
  }

  // 套用刷新回包：整块重渲染，列表滚动位置手动保住（applyBootstrap 会重建行）
  function applyRefreshSnapshot(res) {
    var listBody = document.getElementById('list-body');
    var scrollTop = listBody ? listBody.scrollTop : 0;
    applyBootstrap(res);
    if (listBody) listBody.scrollTop = scrollTop;
  }

  /* 第二段并入：回包只带独有相关字段（后端不再重算共有 / 智能体看板），
     所以这里逐块替换独有行、独有计数与 meta，不走整页 applyBootstrap ——
     那会用第一段的数据再全量画一遍，白渲染一次还可能把滚动/选中抖一下。
     meta 按 key 覆盖：第一段沿用下来的共有计数不能被这份局部回包抹掉。 */
  function applyUniqueSnapshot(res) {
    var d = (res && res.data) || {};
    UNIQUE = d.unique || [];
    UNIQUE_GROUPS = d.unique_groups || localUniqueGroups(UNIQUE);
    CONFLICT = (d.conflict && d.conflict.skill) ? d.conflict : null;
    var frNext = d.firstRun || null;
    var frSig = frNext ? JSON.stringify([frNext.candidate && frNext.candidate.store,
                                        (frNext.adoptable || []).length]) : '';
    if (frSig !== firstRunSig) { firstRunSig = frSig; firstRunDismissed = false; firstRunLogged = false; }
    FIRST_RUN = frNext;
    ['uniqueCount', 'uniqueLogical', 'uniqueLight', 'scanDegraded',
     'uniquePending'].forEach(function (k) {
      if (d.meta && Object.prototype.hasOwnProperty.call(d.meta, k)) META[k] = d.meta[k];
    });
    setMetaMap(res.meta);
    var listBody = document.getElementById('list-body');
    var scrollTop = listBody ? listBody.scrollTop : 0;
    renderAll(false);
    if (listBody) listBody.scrollTop = scrollTop;
    window.__SK_MODE = MODE;
  }

  /* ---------------------------------------------------------- interactions */
  function setBoard(board) {
    if (!/^(shared|unique|agents)$/.test(board)) return;
    // 复位筛选 / 清搜索框 / 默认选中 / 全量渲染都在 setState 的 board 分支
    setState({ board: board });
  }

  function agentByKey(key) {
    var rows = agentRows();
    for (var i = 0; i < rows.length; i++) if (rows[i].key === key) return rows[i];
    return null;
  }

  function requestAgent(key, on, onAbort) {
    var a = agentByKey(key);
    if (!a) return;
    var abort = function () { if (onAbort) onAbort(); };
    if (a.status === 'missing') {
      toast('info', '未安装 ' + a.label, '本机没有检测到 ' + (a.detectDir || a.dir) + '，无法建立链接');
      abort();
      return;
    }
    if (MODE !== 'db' || !SKB) {
      toast('info', '原型模式', '桌面端才会真正建立或删除 junction 链接');
      abort();
      return;
    }

    var showResult = function (res) {
      clearAgentPending(key);
      // 水合前的选中意向：applyBootstrap 会校验该行是否仍存在，失效才回落
      state.selected = key;
      applyBootstrap(res);
      var act = res.action || {};
      if (!on) {
        toast('ok', '已关闭 ' + a.label,
          '撤下 ' + (act.removed || 0) + ' 处链接' +
          (act.kept && act.kept.length ? '，保留 ' + act.kept.length + ' 个冲突副本' : ''));
      } else {
        // 后端每次都重跑同一条 do_sync：本来已连着的是「补齐 / 已在位」，不是没干活
        var n = act.changed || 0;
        var detail = act.repaired
          ? (n ? '补齐 ' + n + ' 处缺失链接' : '链接本来就在位，无需重建')
          : '建立/刷新 ' + n + ' 处链接';
        toast('ok', '已连接 ' + a.label, detail +
          (act.conflicts && act.conflicts.length ? '，' + act.conflicts.length + ' 处冲突未动' : ''));
      }
      renderInspector();
    };
    var fail = function (err) {
      // 先让界面落回后端真值（该行不再挂在「连接中」），再报失败
      clearAgentPending(key);
      renderAgentBoard();
      toast('bad', (on ? '连接失败' : '关闭失败'), String(err && err.message || err));
      abort();
    };
    // 发请求：进入在途 → 立刻发（进度条与请求并行，不再把 650ms 动画压在前面），
    // 回包先收掉在途标记再由数据落定。连着拨多个开关时每个请求各自在途、互不覆盖。
    var send = function () {
      setAgentPending(key, on);
      var stop = startProgress(500);
      SKB.setAgent(key, on).then(function (res) { stop(); showResult(res); },
                                  function (err) { stop(); fail(err); });
    };

    if (!on) {
      confirmModal(
        '关闭 ' + a.label + ' 的分发？',
        '将从 ' + a.dir + ' 删除本工具建立的 ' + a.linked +
        ' 个 junction（只删链接，中央仓库的 skill 实体不动）。随时可以再打开恢复。',
        '关闭分发',
        function () {
          closeModal();
          send();
        },
        { onCancel: abort });
      return;
    }
    send();
  }

  function requestDelete() {
    var r = selectedRow();
    if (!r || r.board === 'agents' || r.group) return; // 组行只做展示，操作按实例走
    if (MODE !== 'db' || !SKB) {
      toast('info', '原型模式', '桌面端才会真的删除磁盘上的 skill');
      return;
    }

    var isUnique = r.board === 'unique';
    var rows = sortRows(filterRows(currentRows()));
    var idx = rows.findIndex(function (x) { return x.key === r.key; });
    var next = idx > -1 ? (rows[idx + 1] || rows[idx - 1] || null) : null;

    var title = isUnique ? '删除这个本地副本？' : '彻底删除 ' + r.display + '？';
    var text = isUnique
      ? '将从 ' + r.entry.toolLabel + ' 的 skills 目录永久删除“' + r.name + '”（' +
        r.entry.files + ' 个文件 / ' + fmtBytes(r.entry.bytes) +
        '）。中央仓库的同名副本不受影响，此操作不可恢复。'
      : '将从中央仓库永久删除“' + r.name + '”目录，并撤掉所有 agent 中指向它的链接。' +
        '手改过的 agent 副本会保留并转为独有技能。此操作不可恢复，也不会进入回收站。';

    confirmModal(title, text, isUnique ? '删除副本' : '彻底删除', function () {
      closeModal();
      runProgress(500, function () {
        SKB.deleteSkill(r.board, r.key).then(function (res) {
          // 水合前的选中意向：指向相邻行，applyBootstrap 校验后生效
          state.selected = next ? next.key : null;
          applyBootstrap(res);
          var act = res.action || {};
          toast('ok', isUnique ? '副本已删除' : 'skill 已删除',
            isUnique
              ? '删除 ' + (act.files || 0) + ' 个文件 · ' + (act.path || '')
              : '删除中央实体 ' + (act.files || 0) + ' 个文件，撤掉 ' + (act.links || 0) + ' 处链接');
        }).catch(function (err) {
          toast('bad', isUnique ? '删除失败' : '彻底删除失败', String(err && err.message || err));
        });
      });
    }, { danger: true });
  }

  function requestUninstall() {
    // 外部来源独有行的「卸载」（deletable=false 的三类：extra 目录 / 容器子目录 /
    // foreign-link）。与「删除本地副本」的区别在后端 uninstall_unique：
    // foreign-link 只摘链接；实体目录永久删除。deletable 行仍走 requestDelete。
    var r = selectedRow();
    if (!r || r.group || r.board !== 'unique' || r.entry.deletable !== false) return;
    if (MODE !== 'db' || !SKB) {
      toast('info', '原型模式', '桌面端才会真的摘除链接 / 删除外部目录');
      return;
    }
    var isLink = r.entry.kind === 'foreign-link';
    var rows = sortRows(filterRows(currentRows()));
    var idx = rows.findIndex(function (x) { return x.key === r.key; });
    var next = idx > -1 ? (rows[idx + 1] || rows[idx - 1] || null) : null;

    var title = isLink ? '摘除这个外部链接？' : '卸载“' + r.display + '”？';
    var text = isLink
      ? '将从 ' + r.entry.toolLabel + ' 目录移除这个 junction 链接，agent 不再加载该 skill。' +
        '链接真身（' + r.entry.path + '）与中央仓库都不受影响。'
      : '将永久删除 ' + r.entry.path + '（' + r.entry.files + ' 个文件 / ' + fmtBytes(r.entry.bytes) +
        '）。该目录由 agent 自己安装/管理，卸载后 agent 更新时可能会重新安装。' +
        '此操作不可恢复、不进回收站。';

    confirmModal(title, text, isLink ? '摘除链接' : '卸载', function () {
      closeModal();
      runProgress(500, function () {
        SKB.uninstallUnique(r.key).then(function (res) {
          state.selected = next ? next.key : null;
          applyBootstrap(res);
          var act = res.action || {};
          toast('ok', isLink ? '链接已摘除' : '已卸载',
            (isLink ? '移除链接 ' : '删除 ' + (act.files || 0) + ' 个文件 · ') + (act.path || ''));
        }).catch(function (err) {
          toast('bad', isLink ? '摘除失败' : '卸载失败', String(err && err.message || err));
        });
      });
    }, { danger: true });
  }

  function requestPromote() {
    // 独有 → 共有的一键收编（右侧栏 promote-box 入口）。前端守卫 + 确认；
    // 收编范围与源副本处置由后端 promote_unique 决定（同名覆盖 / foreign-link
    // 行根本不渲染按钮，后端仍会再拦一次）。
    var r = selectedRow();
    if (!r || r.group || r.board !== 'unique') return;
    var isLink = r.entry.kind === 'foreign-link';
    if (r.entry.kind !== 'unique' && !isLink) return;
    if (MODE !== 'db' || !SKB) {
      toast('info', '原型模式', '桌面端才会真的把技能拷进中央仓库');
      return;
    }
    var toAlign = r.entry.src === 'main' && r.entry.agentEnabled !== false;
    var title = '把“' + r.display + '”' + (isLink ? '按真身收进仓库？' : '转为共有技能？');
    var text = isLink
      ? '将把链接真身（' + r.entry.path + '）的内容拷贝到中央仓库（' + r.entry.files +
        ' 个文件 / ' + fmtBytes(r.entry.bytes) + '），并纳入统一分发。' +
        (toAlign
          ? '「' + r.entry.toolLabel + '」的分发开关开着，收编后链接会改指中央仓库。'
          : '分发开关关着：保留原链接不动，真身仍在原路径，本工具不删第三方文件。')
      : '将把 ' + r.entry.path + ' 拷贝到中央仓库（' + r.entry.files + ' 个文件 / ' +
        fmtBytes(r.entry.bytes) + '），并纳入统一分发。' +
        (toAlign
          ? '「' + r.entry.toolLabel + '」的分发开关开着，源副本会按分发方式对齐。'
          : '源目录保留不动，' + r.entry.toolLabel + ' 不受影响。') +
        '显示名与备注会一并带过去。';
    confirmModal(title, text, isLink ? '按真身收进仓库' : '转为共有', function () {
      closeModal();
      runProgress(700, function () {
        SKB.promoteUnique(r.key).then(function (res) {
          var act = res.action || {};
          applyBootstrap(res); // 独有行已消失：数据先落，选中自愈
          if (act.name) {
            // 转换的终点在共有看板：跳过去选中新技能，让结果可见
            setState({ board: 'shared', selected: act.name });
          }
          toast('ok', act.via === 'link' ? '已按真身收进仓库' : '已转为共有技能',
            '「' + (act.name || r.name) + '」已收进中央仓库' +
            (act.relinked
              ? '，' + r.entry.toolLabel + ' 的' + (act.via === 'link' ? '链接已改指中央仓库' : '源副本已转为链接')
              : (act.via === 'link' ? '；原链接保留（分发开关关着），真身仍在原路径' : '')));
        }).catch(function (err) {
          toast('bad', '转换失败', String(err && err.message || err));
        });
      });
    });
  }

  // 身份契约修复：把包内声明名改成目录名。改的是中央仓库那一份 SKILL.md 的
  // name 一行 —— agent 侧本来就按目录名注册与分发，所以链接、正文、描述都不
  // 受影响，修完不需要再同步一次。
  function alignName(name) {
    if (!name) return;
    confirmModal('把声明名对齐到目录名',
      '将把中央仓库里 ' + name + ' 的 SKILL.md 中 name 一行改写为目录名 ' + name +
      '。正文、描述与其余文件不动，各 agent 的链接也不动。' +
      '改完后第二行显示的名字就和 agent 实际能调用的名字一致。',
      '对齐', function () {
      closeModal();
      runProgress(600, function () {
        SKB.alignName(name).then(function (res) {
          applyBootstrap(res);
          setState({ selected: name });
          toast('ok', '已对齐声明名',
            '「' + name + '」的包内声明名已改为目录名，agent 注册名与界面显示一致了。');
        }).catch(function (err) {
          toast('bad', '对齐失败', String(err && err.message || err));
        });
      });
    });
  }

  /* ---------------------------------------------------------- 导入 skill */
  /* SPEC-skill-import.md：外部目录 → import_preflight 预检 → import_dir 落库。
     状态机 source → review(扫描/报告) → running → done / error，单弹窗内推进；
     running 期间一切关闭路径禁用（后端同步执行不可中断，UI 不许撒谎）。 */
  var imp = { step: null, tab: 'local', demo: null, pre: null, preError: null,
              name: '', folder: '', q: '', pathText: '', timers: [],
              runState: {}, runPct: 0, lastAction: null, lastError: '', browsing: false,
              online: null, onlineBusy: false, fetchName: null,
              fetchDone: 0, fetchTotal: 0 };

  function impClearTimers() {
    imp.timers.forEach(function (t) { clearTimeout(t); });
    imp.timers = [];
  }
  function impLater(fn, ms) { imp.timers.push(setTimeout(fn, ms)); }

  // 所有关闭路径（Esc / 遮罩 / X / cancel）经 closeModal → 这里统一收尸，
  // 防止 imp.step 残留把 Esc 链或拖拽遮罩卡死
  function impClosed() {
    if (!imp.step) return;
    impClearTimers();
    imp.step = null;
    imp.pre = null;
    imp.preError = null;
    imp.demo = null;
    imp.runState = {};
    imp.runPct = 0;
    imp.lastAction = null;
    imp.lastError = '';
    imp.onlineBusy = false;
    if (imp.fetchName && impIsDb()) {
      try { SKB.onlineFetchCancel().catch(function () {}); } catch (e) {}
    }
    imp.fetchName = null;
    imp.fetchDone = 0;
    imp.fetchTotal = 0;
    window.__SK_FETCH_PROGRESS = null;
    hideDropOv();
  }

  function impClose() {
    if (imp.step === 'running') return; // 执行中不可关：Esc/遮罩/X 全部无效
    closeModal();
  }

  function baseNameOf(p) {
    var s = String(p || '').replace(/[\\/]+$/, '');
    var i = Math.max(s.lastIndexOf('\\'), s.lastIndexOf('/'));
    return i > -1 ? s.slice(i + 1) : s;
  }

  // 前端与后端 check_skill_name 同一口径：单层目录名 + Windows 保留名 +
  // 尾点尾空格 + 冒号（name_error 只在键入名 == 预检名时用后端原句）
  function validName(n) {
    if (!n || n === '.' || n === '..') return false;
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(n)) return false;
    if (/^(con|prn|aux|nul|com[1-9]|lpt[1-9])$/i.test(n)) return false;
    if (n !== n.replace(/[. ]+$/, '') || n.indexOf(':') > -1) return false;
    return true;
  }
  var IMP_NAME_ERR = '只能用字母、数字、点、连字符，不能以点/空格结尾，且不能含路径分隔符或冒号';

  function storeHas(n) {
    var t = String(n || '').toLowerCase();
    return SKILLS.some(function (s) { return String(s.name).toLowerCase() === t; });
  }

  function suggestNames(base) {
    var b = String(base || 'skill').replace(/[\\/:*?"<>|. ]+$/g, '').replace(/[\\/:*?"<>|]/g, '').slice(0, 40) || 'skill';
    var d = new Date();
    var stamp = d.getFullYear() + ('0' + (d.getMonth() + 1)).slice(-2) + ('0' + d.getDate()).slice(-2);
    return [b + '-2', b + '-' + stamp, b + '-import'].filter(function (n) { return !storeHas(n); });
  }

  function impHash8(s) {
    var h = 5381, i;
    s = String(s || '');
    for (i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) >>> 0;
    return ('00000000' + h.toString(16)).slice(-8);
  }

  // ---- 原型模式（file://）的内置演示数据：形状与后端 import_preflight 一致 ----
  var IMP_DEMOS = {
    ok: { ok: true, src: 'D:\\downloads\\video-shotcraft-pro', resolved: null,
      name: 'video-shotcraft-pro', name_error: '', has_skill_md: true,
      fm: { name: 'video-shotcraft-pro', description: '按分镜卡创建电影感产品视频：分镜 → 运镜 → 台词 → 渲染（社区增强版）。' },
      files: 12, bytes: 88473, tree: ['SKILL.md', 'references/ ×4', 'scripts/ ×2', 'assets/ ×3'], conflict: 'none' },
    warn: { ok: true, src: 'C:\\Users\\LENOVO\\Desktop\\my-prompt-pack', resolved: null,
      name: 'my-prompt-pack', name_error: '', has_skill_md: false, fm: null,
      files: 3, bytes: 9318, tree: ['prompt.md', 'README.md', 'examples/'], conflict: 'none' },
    dup: { ok: true, src: 'C:\\Users\\LENOVO\\.claude\\skills\\pdf', resolved: 'D:\\ClaudeCode\\.claude\\skills\\pdf',
      name: 'pdf', name_error: '', has_skill_md: true,
      fm: { name: 'pdf', description: 'PDF 读取 / 拆分 / 合并 / 表单全流程。' },
      files: 9, bytes: 131072, tree: ['SKILL.md', 'scripts/ ×5', 'references/ ×2'], conflict: 'same-name' },
    same: { ok: true, src: 'D:\\backup\\store-mirror\\remotion-render', resolved: null,
      name: 'remotion-render', name_error: '', has_skill_md: true,
      fm: { name: 'remotion-render', description: '导出 Remotion 视频。' },
      files: 4, bytes: 12083, tree: ['SKILL.md', 'scripts/render.mjs'], conflict: 'same-content', hash8: '1a2b3c4d' },
    fail: { ok: true, src: 'E:\\usb-backup\\agent-browser', resolved: null,
      name: 'agent-browser', name_error: '', has_skill_md: true,
      fm: { name: 'agent-browser', description: '让 agent 操控真实浏览器：导航、点击、截屏与表单。' },
      files: 15, bytes: 215552, tree: ['SKILL.md', 'scripts/ ×6', 'references/ ×3'], conflict: 'none', failRun: true }
  };
  var IMP_ONLINE = [
    { name: 'skill-creator', source: 'anthropics/skills', installs: '48.2k' },
    { name: 'agent-browser', source: 'vercel-labs/agent-browser', installs: '21.3k' },
    { name: 'slidev-export', source: 'anteri/slidev-tools', installs: '6.8k' },
    { name: 'pdf-toolkit', source: 'moejoy/pdf-toolkit', installs: '3.1k' }
  ];
  function impDemoFromName(name, base) {
    return { ok: true, src: (base || 'C:\\Users\\LENOVO\\Downloads') + '\\' + name, resolved: null,
      name: name, name_error: '', has_skill_md: true,
      fm: { name: name, description: '（演示）解析自 SKILL.md frontmatter 的描述文本。' },
      files: 8, bytes: 43008, tree: ['SKILL.md', 'references/ ×2', 'scripts/'],
      conflict: storeHas(name) ? 'same-name' : 'none' };
  }

  function impIsDb() { return MODE === 'db' && SKB; }

  // skills.sh 的 installs 是原始数字（220593）；原型示例数据是 '48.2k' 字符串
  function fmtInstalls(n) {
    if (n == null || n === '') return '—';
    var v = Number(n);
    if (!isFinite(v)) return String(n);
    if (v >= 1000) return (Math.round(v / 100) / 10).toString().replace(/\.0$/, '') + 'k';
    return String(v);
  }

  function impOnlineSearch() {
    if (!impIsDb() || imp.onlineBusy) return;
    var q = String(imp.q || '').trim();
    if (!q) { toast('warn', '先输入关键词', '如 remotion、pdf、browser'); return; }
    imp.onlineBusy = true;
    impRender();
    SKB.onlineSearch(q).then(function (res) {
      imp.onlineBusy = false;
      if (!imp.step) return; // 弹窗已关：结果留到下次打开再用
      imp.online = (res && res.data) || [];
      impRender();
      var el = document.getElementById('imp-q');
      if (el) { el.focus(); }
      if (!imp.online.length) toast('info', '没有命中', 'skills.sh 上没有找到「' + q + '」，换个关键词试试');
    }).catch(function (err) {
      imp.onlineBusy = false;
      if (imp.step) impRender();
      toast('bad', '在线检索失败', String(err && err.message || err) + ' — 检查网络后可重试');
    });
  }

  // 键入名的冲突判定：== 预检名 → 信预检（含 same-content / hash8 与 name_error）；
  // 改了名 → 前端用 SKILLS 现值判同名（同内容只有后端能证，改了名视为不同源）
  function impValidate() {
    var n = imp.name;
    var out = { ok: false, nameErr: '', conflict: false, same: false };
    if (!imp.pre) return out;
    if (!n) { out.nameErr = '名称不能为空'; return out; }
    if (!validName(n)) { out.nameErr = IMP_NAME_ERR; return out; }
    var preNameEq = imp.pre.name && n.toLowerCase() === String(imp.pre.name).toLowerCase();
    if (preNameEq && imp.pre.name_error) { out.nameErr = imp.pre.name_error; return out; }
    var cf = preNameEq ? (imp.pre.conflict || 'none') : (storeHas(n) ? 'same-name' : 'none');
    if (cf === 'same-content') out.same = true;
    else if (cf === 'same-name') out.conflict = true;
    else out.ok = true;
    return out;
  }

  /* ---------- 渲染 ---------- */
  function impStepsHtml(cur) {
    var defs = [['1', '选择来源'], ['2', '预检命名'], ['3', '导入完成']];
    var order = ['source', 'review', 'finish'];
    var ci = order.indexOf(cur);
    return '<div class="imp-steps">' + defs.map(function (d, i) {
      var st = i < ci ? ' done' : (i === ci ? ' cur' : '');
      return '<span class="imp-stp' + st + '"><i></i>' + d[0] + ' · ' + d[1] + '</span>';
    }).join('') + '</div>';
  }
  function impHead(title, stepKey, closable) {
    return '<div class="modal-head"><span class="modal-eyebrow">IMPORT SKILL</span>' +
      '<h2>' + esc(title) + '</h2>' + impStepsHtml(stepKey) +
      (closable ? '<button class="modal-close" data-act="imp-close" aria-label="关闭">' + icon('x') + '</button>' : '') +
      '</div>';
  }

  function impSourceHtml() {
    var isDb = impIsDb();
    var tabLocal =
      '<div data-src-pane="local"' + (imp.tab === 'local' ? '' : ' hidden') + '>' +
        '<div class="dz" id="imp-dz" data-act="imp-dz" role="button" tabindex="0">' +
          '<span class="dz-ic">' + icon('upload') + '</span>' +
          '<b>把文件夹拖到这里</b>' +
          '<span>松手后自动预检 — 也可以直接拖进主窗口任意位置</span>' +
        '</div>' +
        '<div class="imp-label"><span>来源路径</span><span style="letter-spacing:0">桌面端「浏览」调原生目录选择</span></div>' +
        '<div class="path-row">' +
          '<input class="path-input" id="imp-path" type="text" value="' + esc(imp.pathText || '') + '" placeholder="D:\\downloads\\my-skill\\" spellcheck="false" aria-label="来源路径">' +
          '<button class="btn" data-act="imp-browse">' + icon('folder-open') + '浏览…</button>' +
          '<button class="btn btn-primary" data-act="imp-use-path">预检</button>' +
        '</div>' +
        (isDb ? '' :
          '<div class="demo-strip"><span class="demo-cap">原型直达</span>' +
            '<button class="demo-chip" data-act="imp-demo" data-demo="ok">' + icon('check') + '标准 skill</button>' +
            '<button class="demo-chip" data-act="imp-demo" data-demo="warn">' + icon('alert') + '缺 SKILL.md</button>' +
            '<button class="demo-chip" data-act="imp-demo" data-demo="dup">' + icon('bad') + '同名冲突</button>' +
            '<button class="demo-chip" data-act="imp-demo" data-demo="same">' + icon('info') + '同名·内容一致</button>' +
            '<button class="demo-chip" data-act="imp-demo" data-demo="fail">' + icon('spin') + '写入失败演练</button>' +
          '</div>') +
      '</div>';
    var onlineRows = impIsDb() ? (imp.online || []) : IMP_ONLINE;
    var rows = onlineRows.map(function (r, i) {
      var busy = imp.fetchName === (r.name + '|' + (r.source || ''));
      return '<div class="sr-row">' +
        '<span class="fc-mark">' + esc(mark(r.name)) + '</span>' +
        '<span class="sr-main"><b>' + esc(r.name) + '</b><span>' + esc(r.source || 'skills.sh') + ' · skills.sh</span></span>' +
        '<span class="sr-inst">' + icon('download') + esc(fmtInstalls(r.installs)) + '</span>' +
        '<button class="btn btn-sm btn-ghost" data-act="imp-online-page" data-i="' + i + '">' + icon('ext-link') + '页面</button>' +
        (busy ? '<button class="btn btn-sm btn-ghost" data-act="imp-fetch-cancel">取消</button>' : '') +
        '<button class="btn btn-sm btn-primary" data-act="imp-fetch" data-i="' + i + '"' +
          (imp.fetchName ? ' disabled' : '') + '>' +
          (busy
            ? '<span class="icon" style="display:inline-flex;animation:sk-spin 0.9s linear infinite">' + icon('spin') + '</span>'
              + '<span class="sk-fetch-label">'
              + (imp.fetchTotal ? '下载 ' + imp.fetchDone + '/' + imp.fetchTotal : '解析中')
              + '</span>'
            : '获取') +
        '</button>' +
        '</div>';
    }).join('');
    var onlineEmpty = (impIsDb() && !onlineRows.length)
      ? '<div class="sr-row" style="justify-content:center;min-height:44px"><span style="color:var(--ink-3)">输入关键词搜索 skills.sh 社区索引，如 remotion、pdf、browser</span></div>'
      : '';
    var tabOnline =
      '<div data-src-pane="online"' + (imp.tab === 'online' ? '' : ' hidden') + '>' +
        '<div class="path-row">' +
          '<input class="path-input" id="imp-q" type="text" value="' + esc(imp.q) + '" placeholder="搜索 skills.sh，如 remotion、pdf…" spellcheck="false" aria-label="搜索 skills.sh">' +
          '<button class="btn" data-act="imp-search"' + (imp.onlineBusy ? ' disabled' : '') + '>' +
            (imp.onlineBusy
              ? '<span class="icon" style="display:inline-flex;animation:sk-spin 0.9s linear infinite">' + icon('spin') + '</span>搜索中'
              : icon('search') + '搜索') +
          '</button>' +
        '</div>' +
        '<div class="sr-list" id="sr-list">' + rows + onlineEmpty + '</div>' +
        '<div class="note-bar info">' + icon('info') +
          '<span><b>「获取」= 先下载到临时目录，再走标准预检导入。</b>下载失败可重试，不会直接写仓库。</span></div>' +
      '</div>';
    return impHead('把 skill 导入中央仓库', 'source', true) +
      '<div class="modal-body">' +
        '<div class="src-tabs">' +
          '<button class="src-tab' + (imp.tab === 'local' ? ' on' : '') + '" data-act="imp-tab" data-tab="local">' +
            '<span class="st-top">' + icon('hard-drive') + '本地文件夹</span>' +
            '<span class="st-sub">磁盘 / U 盘 / 其他 agent 的 skills 目录</span></button>' +
          '<button class="src-tab' + (imp.tab === 'online' ? ' on' : '') + '" data-act="imp-tab" data-tab="online">' +
            '<span class="st-top">' + icon('globe') + '在线检索</span>' +
            '<span class="st-sub">skills.sh 社区索引<span class="mini-badge">需联网</span></span></button>' +
        '</div>' +
        tabLocal + tabOnline +
      '</div>' +
      '<div class="modal-foot"><span class="foot-note">导入 = 复制进中央仓库，不改源目录</span>' +
      '<button class="btn" data-act="imp-close">取消</button></div>';
  }

  function impConflictPanelHtml(name, show) {
    return '<div class="panel danger" data-role="panel-conflict"' + (show ? '' : ' hidden') + '>' +
      '<div class="panel-t">' + icon('bad') + '<span>中央仓库已有同名 skill：<span class="pv-name">' + esc(name) + '</span></span></div>' +
      '<p>继续导入会失败（import_dir 拒绝同名）。换一个名字，或先到共有看板处理现有的同名 skill。</p>' +
      '<div class="sug-chips">' + suggestNames(name).map(function (n) {
        return '<button class="sug-chip" data-act="imp-sug" data-name="' + esc(n) + '">' + esc(n) + '</button>';
      }).join('') + '</div></div>';
  }
  function impSamePanelHtml(name, show) {
    return '<div class="panel same" data-role="panel-same"' + (show ? '' : ' hidden') + '>' +
      '<div class="panel-t">' + icon('ok') + '<span>仓库已有同名且内容完全一致</span></div>' +
      '<p>全部文件哈希一致（<span class="r-mono">' + impHash8(name) + '</span>…），无需重复导入。若想覆盖，请先删除仓库中的同名 skill。</p>' +
      '<button class="btn btn-sm" data-act="imp-skip">知道了，跳过</button></div>';
  }
  function impNameErrHtml(txt) {
    return '<div class="field-err" data-role="name-err"' + (txt ? '' : ' hidden') + '>' + icon('alert') + '<span>' + esc(txt || '') + '</span></div>';
  }
  function impNameHelpHtml(show) {
    return '<div class="field-help" data-role="name-help"' + (show ? '' : ' hidden') + '>仅限单层目录名：字母 / 数字 / 点 / 连字符，不能含路径分隔符（与 import_dir 同一规则）。</div>';
  }

  function impReviewHtml() {
    var srcPath = (imp.demo && imp.demo.path) || '';
    var srcLine = '<div class="src-row-line">' + icon('folder') + '<code title="' + esc(srcPath) + '">' + esc(srcPath) + '</code>' +
      (imp.pre && imp.pre.resolved ? '<span class="res-pill" title="源是链接，已解析到真实目录">' + icon('link') + '已解析链接</span>' : '') +
      '<button class="btn btn-sm btn-ghost" data-act="imp-back">' + icon('arrow-left') + '重新选择</button></div>';
    if (!imp.pre) {
      return impHead('预检与命名', 'review', true) +
        '<div class="modal-body">' + srcLine +
        '<div class="hero" style="padding-bottom:14px"><span class="hero-ic" style="background:var(--surface-2);color:var(--ink-3)"><span style="display:inline-flex;' +
        (document.body.classList.contains('reduce-motion') ? '' : 'animation:sk-spin 0.9s linear infinite;') + '">' + icon('spin') + '</span></span>' +
        '<b style="font-size:13px">正在扫描来源目录</b><span>解析 SKILL.md · 统计文件数与大小 · 比对同名冲突…</span></div>' +
        '</div><div class="modal-foot"><button class="btn" data-act="imp-close">取消</button></div>';
    }
    if (imp.pre.ok === false || imp.preError) {
      var reason = esc(imp.preError || imp.pre.reason || '预检失败');
      return impHead('预检与命名', 'review', true) +
        '<div class="modal-body">' + srcLine +
          '<div class="hero bad"><span class="hero-ic">' + icon('bad') + '</span>' +
            '<h3 style="font-size:14px">预检未通过</h3><span>' + reason + '</span></div>' +
          '<div class="note-bar warn" style="margin-top:12px">' + icon('alert') +
            '<span><b>仓库没有变化。</b>确认来源后点「重新选择」换一个目录。</span></div>' +
        '</div>' +
        '<div class="modal-foot"><span class="foot-note">预检失败</span>' +
          '<button class="btn" data-act="imp-close">取消</button>' +
          '<button class="btn btn-primary" data-act="imp-back">' + icon('arrow-left') + '重新选择</button></div>';
    }
    var d = imp.pre;
    var fmRows =
      '<div class="rep-row"><span>SKILL.md</span><div>' + (d.has_skill_md
        ? '<span class="r-mono" style="color:var(--success);display:inline-flex;align-items:center;gap:5px">' + icon('ok') + '已解析 frontmatter</span>'
        : '<span class="r-mono" style="color:var(--warning);display:inline-flex;align-items:center;gap:5px">' + icon('alert') + '未找到</span>') + '</div></div>' +
      (d.fm ? '<div class="rep-row"><span>名称</span><div><span class="r-mono">' + esc(d.fm.name) + '</span></div></div>' +
        '<div class="rep-row"><span>描述</span><div><div class="r-desc">' + esc(d.fm.description) + '</div></div></div>' : '') +
      '<div class="rep-row"><span>规模</span><div><span class="r-mono">' + d.files + ' 个文件 · ' + fmtBytes(d.bytes) + '</span></div></div>' +
      '<div class="rep-row"><span>结构</span><div><div class="rep-chips">' +
        (d.tree || []).map(function (t) { return '<span class="fc-chip">' + esc(t) + '</span>'; }).join('') + '</div></div></div>';
    var report = '<div class="rep">' + fmRows + '</div>';
    if (!d.has_skill_md) {
      report += '<div class="note-bar warn">' + icon('alert') +
        '<span><b>未找到 SKILL.md</b> — 这个目录大概率不是标准 skill，agent 可能不识别。仍可导入，建议先确认来源。</span></div>';
    }
    if (d.resolved) {
      report += '<div class="note-bar info">' + icon('link') +
        '<span><b>源是链接（junction / symlink），已解析到真实目录：</b><span style="font-family:var(--mono);font-size:10.5px">' + esc(d.resolved) + '</span>。导入复制的是真实内容，链接本身不受影响。</span></div>';
    }
    var v = impValidate();
    var nameBlock =
      '<div class="imp-label"><span>Skill 名称（仓库目录名）</span><span style="letter-spacing:0;text-transform:none">默认取目录名</span></div>' +
      '<div class="name-wrap"><input class="imp-input' + (v.nameErr ? ' err' : '') + '" id="imp-name" type="text" value="' + esc(imp.name) + '" spellcheck="false" aria-label="Skill 名称"></div>' +
      impNameErrHtml(v.nameErr) + impNameHelpHtml(!v.nameErr) +
      impConflictPanelHtml(imp.name, v.conflict) + impSamePanelHtml(imp.name, v.same);
    var folderChips = ['<button class="fold-chip dashed' + (imp.folder === '' ? ' on' : '') + '" data-act="imp-folder" data-f="">' +
      icon('inbox') + '未归档</button>'].concat(FOLDERS.map(function (f) {
        return '<button class="fold-chip' + (imp.folder === f.id ? ' on' : '') + '" data-act="imp-folder" data-f="' + esc(f.id) + '" style="' + catVars(f.color) + '">' +
          '<span class="catdot"></span>' + esc(f.name) + '</button>';
      })).join('');
    var canGo = v.ok && !v.conflict && !v.same;
    return impHead('预检与命名', 'review', true) +
      '<div class="modal-body">' + srcLine + report +
        nameBlock +
        '<div class="dest-box">' + icon('hard-drive') + '<span>目标位置&nbsp;</span><b>' + esc(STORE + '\\' + (imp.name || '…')) + '</b></div>' +
        '<div class="imp-label" style="margin-top:13px"><span>导入后归档（可选）</span><span style="letter-spacing:0;text-transform:none">也可以之后拖拽归档</span></div>' +
        '<div class="fold-picker">' + folderChips + '</div>' +
        '<div class="flow-hint"><b>复制</b> 到 .skpart 临时目录' + icon('chevron-right') + '<b>原子改名</b>落库' +
          icon('chevron-right') + '<b>重新扫描</b>' + icon('chevron-right') + '到「智能体」页分发</div>' +
      '</div>' +
      '<div class="modal-foot"><span class="foot-note">' + (d.has_skill_md ? '预检通过' : '带警告：缺 SKILL.md') + '</span>' +
        '<button class="btn" data-act="imp-close">取消</button>' +
        '<button class="btn btn-primary" data-act="imp-start"' + (canGo ? '' : ' disabled') + '>' + icon('download') + '导入到仓库</button></div>';
  }

  function impRunningHtml() {
    var steps = [
      { id: 'copy', label: '复制到临时目录', small: '.skpart-' + impHash8(imp.name).slice(0, 4) + '…' },
      { id: 'rename', label: '原子改名落库', small: 'os.replace' },
      { id: 'scan', label: '重新扫描并注册', small: 'rescan()' }
    ];
    var st = imp.runState || {};
    var rows = steps.map(function (s) {
      var cls = st[s.id] || '';
      var ic = cls === 'ok' ? icon('ok') : icon('spin');
      return '<div class="run-step ' + cls + '"><span class="run-ic">' + ic + '</span><span>' + s.label + '</span><small>' + s.small + '</small></div>';
    }).join('');
    return impHead('正在导入', 'review', false) +
      '<div class="modal-body"><div class="src-row-line">' + icon('folder') + '<code>' + esc((imp.demo && imp.demo.path) || '') + '</code></div>' +
      '<div class="run-steps" style="margin-top:12px">' + rows + '</div>' +
      '<div class="imp-prog"><i id="imp-prog-i" style="width:' + (imp.runPct || 0) + '%"></i></div>' +
      '<div class="field-help" style="margin-top:9px">执行期间不能关闭窗口或再次修改名称；失败会回滚临时目录，仓库保持原样。</div>' +
      '</div><div class="modal-foot"><button class="btn" disabled>执行中…</button></div>';
  }

  function impDoneHtml() {
    var act = imp.lastAction || {};
    var f = folderById(imp.folder);
    var row = null;
    for (var i = 0; i < SKILLS.length; i++) if (SKILLS[i].name === act.name) row = SKILLS[i];
    var linked = row && row.agents ? row.agents.length : 0;
    return impHead('导入完成', 'finish', false) +
      '<div class="modal-body">' +
        '<div class="hero ok"><span class="hero-ic">' + icon('ok') + '</span>' +
          '<h3>' + esc(act.name || imp.name) + '</h3><span>已进入中央仓库，并按分发开关同步给 agent</span></div>' +
        '<div class="meta-list" style="margin-top:12px">' +
          '<div class="meta-row"><span>位置</span><b>' + esc(act.path || (STORE + '\\' + imp.name)) + '</b></div>' +
          '<div class="meta-row"><span>规模</span><b>' + (act.files != null ? act.files + ' 个文件 · ' + fmtBytes(act.bytes) : '—') + '</b></div>' +
          '<div class="meta-row"><span>归档</span><b>' + (f ? esc(f.name) : '未归档（可拖拽归档）') + '</b></div>' +
          '<div class="meta-row"><span>分发</span><b>' + linked + '/' + ENABLED.length + ' agent' +
            (linked ? ' — 已同步' : (ENABLED.length ? ' — 有冲突或失败，见智能体页' : ' — 暂无已连接的 agent')) + '</b></div>' +
        '</div>' +
        '<div class="cta-row">' +
          '<button class="btn" data-act="imp-finish">完成</button>' +
          '<button class="btn btn-primary" data-act="imp-agents">' + icon('bot') + '去分发</button>' +
        '</div>' +
      '</div>';
  }

  function impErrorHtml() {
    return impHead('导入失败', 'finish', false) +
      '<div class="modal-body">' +
        '<div class="hero bad"><span class="hero-ic">' + icon('bad') + '</span>' +
          '<h3 style="font-size:14px">写入中央仓库失败</h3><span>' + esc(imp.lastError || '未知错误') + '</span></div>' +
        '<div class="note-bar warn" style="margin-top:12px">' + icon('alert') +
          '<span><b>仓库没有变化。</b>临时目录 .skpart-* 已回滚；可以重试、换来源，或检查目标磁盘。</span></div>' +
        '<div class="cta-row">' +
          '<button class="btn" data-act="imp-back">' + icon('arrow-left') + '返回修改</button>' +
          '<button class="btn btn-primary" data-act="imp-retry">' + icon('refresh') + '重试</button>' +
        '</div>' +
      '</div>';
  }

  function impRender() {
    var html = imp.step === 'source' ? impSourceHtml()
      : imp.step === 'review' ? impReviewHtml()
      : imp.step === 'running' ? impRunningHtml()
      : imp.step === 'done' ? impDoneHtml()
      : impErrorHtml();
    openModal('<div class="modal imp-modal" role="dialog" aria-modal="true" aria-label="导入 skill">' + html + '</div>',
      { locked: imp.step === 'running' });
    var dz = document.getElementById('imp-dz');
    if (dz) {
      dz.addEventListener('dragover', function (e) {
        e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; dz.classList.add('hot');
      });
      dz.addEventListener('dragleave', function () { dz.classList.remove('hot'); });
      dz.addEventListener('drop', function (e) {
        e.preventDefault(); e.stopPropagation(); dz.classList.remove('hot'); impHandleFileDrop(e);
      });
    }
    var nameInput = document.getElementById('imp-name');
    if (nameInput) nameInput.focus();
  }

  function impOpen(step) {
    impClearTimers();
    imp.step = step || 'source';
    impRender();
  }

  /* ---------- 预检 / 执行 ---------- */
  function impGoPreflight(src, nameHint) {
    src = String(src || '').trim();
    if (!src) { toast('warn', '先填来源路径', '或把文件夹拖进来'); return; }
    impClearTimers();
    imp.demo = { path: src };
    imp.pre = null;
    imp.preError = null;
    imp.name = nameHint || baseNameOf(src);
    imp.folder = '';
    imp.q = '';
    imp.step = 'review';
    impRender();
    if (impIsDb()) {
      SKB.importPreflight(src).then(function (res) {
        // 用户可能已「重新选择」或改了路径：过期回包直接丢弃
        if (imp.step !== 'review' || !imp.demo || imp.demo.path !== src) return;
        imp.pre = res.data || null;
        if (imp.pre && imp.pre.ok === false) imp.preError = imp.pre.reason || '预检失败';
        impRender();
      }).catch(function (err) {
        if (imp.step !== 'review' || !imp.demo || imp.demo.path !== src) return;
        imp.preError = String(err && err.message || err);
        impRender();
      });
      return;
    }
    impLater(function () {
      if (imp.step !== 'review' || !imp.demo || imp.demo.path !== src) return;
      imp.pre = impDemoFromName(imp.name);
      imp.pre.src = src;
      impRender();
    }, 620);
  }

  function impLoadDemo(key, base) {
    var pre = key instanceof Object ? key : IMP_DEMOS[key];
    if (!pre) return;
    impClearTimers();
    imp.demo = { path: pre.src };
    imp.pre = null;
    imp.name = (pre.fm && pre.fm.name) || baseNameOf(pre.src);
    imp.folder = '';
    imp.q = '';
    imp.step = 'review';
    impRender();
    impLater(function () {
      if (imp.step !== 'review') return;
      imp.pre = pre;
      impRender();
    }, 620);
  }

  function impMarkImported(nm) {
    if (!nm) return;
    for (var i = 0; i < SKILLS.length; i++) {
      if (SKILLS[i].name === nm) SKILLS[i].isNew = true;
    }
  }

  function impStartRun() {
    var src = imp.demo && imp.demo.path;
    if (!src || !imp.pre) return;
    var v = impValidate();
    if (!v.ok || v.conflict || v.same) return;
    imp.step = 'running';
    imp.runState = { copy: 'doing' };
    imp.runPct = 8;
    impRender();
    if (impIsDb()) {
      SKB.importSkill(src, imp.name, imp.folder).then(function (res) {
        var finished = imp.step === 'running'; // 被强关过就只落数据，不再弹完成页
        imp.runState = { copy: 'ok', rename: 'ok', scan: 'ok' };
        imp.runPct = 100;
        imp.lastAction = res.action || { name: imp.name, path: STORE + '\\' + imp.name,
          files: 0, bytes: 0, folder: imp.folder || '' };
        applyBootstrap(res); // 返回即含新 boards+meta（rescan 已在后端跑完）
        impMarkImported(imp.lastAction.name);
        state.folder = null;
        state.selected = imp.lastAction.name || imp.name;
        renderChrome(); renderContent(false); renderInspector();
        if (finished) { imp.step = 'done'; impRender(); }
        var dist = imp.lastAction.distributed || 0;
        var distErr = (imp.lastAction.distributeErrors || []).length;
        toast('ok', '已导入中央仓库', (imp.lastAction.name || imp.name) + ' · ' +
          imp.lastAction.files + ' 个文件 · ' + (imp.folder ? '已归档' : '未归档') +
          (dist ? ' · 已分发 ' + dist + ' 个 agent' : '') +
          (distErr ? ' · ' + distErr + ' 个 agent 分发失败' : ''));
      }).catch(function (err) {
        imp.lastError = String(err && err.message || err);
        if (imp.step === 'running') { imp.step = 'error'; impRender(); }
        else toast('bad', '导入失败', imp.lastError);
      });
      return;
    }
    // 原型模式：三段 setTimeout 动画 + 内存插入（不写真实仓库，toast 明示）
    var set = function (id, cls) { imp.runState[id] = cls; };
    impLater(function () { set('copy', 'ok'); imp.runPct = 42; impRender(); }, 780);
    impLater(function () { set('rename', 'doing'); impRender(); }, 900);
    impLater(function () { set('rename', 'ok'); imp.runPct = 68; impRender(); }, 1560);
    impLater(function () { set('scan', 'doing'); impRender(); }, 1700);
    impLater(function () { set('scan', 'ok'); imp.runPct = 100; impRender(); }, 2350);
    impLater(function () {
      if (imp.pre && imp.pre.failRun) { imp.step = 'error'; impRender(); return; }
      var name = imp.name;
      var pre = imp.pre || {};
      SKILLS.unshift({ name: name, label: (pre.fm && pre.fm.name) || name,
        description: (pre.fm && pre.fm.description) || '', isNew: true });
      metaOf(name).folder = imp.folder || '';
      state.folder = null;
      state.selected = name;
      imp.lastAction = { name: name, path: STORE + '\\' + name,
        files: pre.files || 0, bytes: pre.bytes || 0, folder: imp.folder || '' };
      imp.step = 'done';
      renderAll();
      impRender();
      toast('ok', '已导入（原型模式）', name + ' · 数据来自内置快照，未写真实仓库');
    }, 2650);
  }

  /* ---------- 弹窗内事件（委托在 #modal-root 上，重渲染不重挂） ---------- */
  function impModalClick(e) {
    var t = e.target.closest('[data-act]');
    if (!t) return;
    var act = t.getAttribute('data-act');
    if (act === 'imp-overlay') { if (e.target === t && imp.step !== 'running') impClose(); return; }
    if (act === 'imp-close') { impClose(); return; }
    if (act === 'imp-tab') { imp.tab = t.getAttribute('data-tab'); impRender(); return; }
    if (act === 'imp-browse') {
      if (impIsDb()) {
        if (imp.browsing) return;
        imp.browsing = true;
        SKB.chooseDir().then(function (res) {
          imp.browsing = false;
          var p = res && res.data;
          if (!p) return; // 用户取消
          imp.pathText = p;
          var inp = document.getElementById('imp-path');
          if (inp) inp.value = p;
          impGoPreflight(p);
        }).catch(function (err) {
          imp.browsing = false;
          toast('bad', '选择目录失败', String(err && err.message || err));
        });
      } else {
        toast('info', '原型环境没有原生目录选择', '桌面端此处调 chooseDir()；下面给了一排示例直达');
      }
      return;
    }
    if (act === 'imp-use-path') {
      var inp = document.getElementById('imp-path');
      var p = inp ? inp.value.trim() : '';
      if (!p) { toast('warn', '先填来源路径', '或点下方示例直达'); return; }
      impGoPreflight(p);
      return;
    }
    if (act === 'imp-demo') { impLoadDemo(t.getAttribute('data-demo')); return; }
    if (act === 'imp-dz') { toast('info', '拖文件夹到这里', '或填路径点「预检」；原型可点下方示例直达'); return; }
    if (act === 'imp-search') {
      if (impIsDb()) { impOnlineSearch(); return; }
      var list = document.getElementById('sr-list');
      if (list) list.style.opacity = '0.45';
      impLater(function () { impRender(); }, 420);
      return;
    }
    if (act === 'imp-online-page') {
      var pageRows = impIsDb() ? (imp.online || []) : IMP_ONLINE;
      var prow = pageRows[parseInt(t.getAttribute('data-i'), 10)];
      if (impIsDb() && prow && prow.page_url) {
        SKB.openUrl(prow.page_url).catch(function (err) {
          toast('bad', '打开页面失败', String(err && err.message || err));
        });
      } else {
        toast('info', '外链（原型不跳转）', '桌面端用系统浏览器打开 skills.sh 页面');
      }
      return;
    }
    if (act === 'imp-fetch-cancel') {
      t.disabled = true;
      SKB.onlineFetchCancel().then(function (res) {
        var ok = res && res.data && res.data.cancelled;
        toast(ok ? 'info' : 'warn', ok ? '正在取消' : '没有进行中的下载',
          ok ? '当前请求结束后停止，仓库不会变化' : '');
      }).catch(function (err) {
        toast('bad', '取消失败', String(err && err.message || err));
      });
      return;
    }
    if (act === 'imp-fetch') {
      var fetchRows = impIsDb() ? (imp.online || []) : IMP_ONLINE;
      var row = fetchRows[parseInt(t.getAttribute('data-i'), 10)];
      if (!row) return;
      if (imp.fetchName) return; // 一次只跑一个下载
      if (!impIsDb()) {
        // 原型演示：假下载 1.1s → 内置预检数据
        t.disabled = true;
        t.innerHTML = '<span class="icon" style="display:inline-flex;animation:sk-spin 0.9s linear infinite">' + icon('spin') + '</span>下载中';
        impLater(function () {
          impLoadDemo(impDemoFromName(row.name, '%TEMP%\\skilllocker-dl'));
          toast('ok', '已下载到临时目录', row.name + ' — 继续预检命名');
        }, 1100);
        return;
      }
      imp.fetchName = row.name + '|' + (row.source || '');
      imp.fetchDone = 0;
      imp.fetchTotal = 0;
      window.__SK_FETCH_PROGRESS = function (p) {
        imp.fetchDone = p.done || 0;
        imp.fetchTotal = p.total || 0;
        if (!imp.step || !imp.fetchName) return;
        // 只改文本节点，不重渲染整个弹窗：每来一个进度就 impRender() 会让
        // WebView 整窗重绘，用户看到的是「每加一个文件闪一下」。
        var lbl = document.querySelector('#sr-list .sk-fetch-label');
        if (lbl) {
          lbl.textContent = imp.fetchTotal
            ? '下载 ' + imp.fetchDone + '/' + imp.fetchTotal : '解析中';
        }
      };
      impRender();
      SKB.onlineFetch(row.source, row.name).then(function (res) {
        var d = (res && res.data) || {};
        var ms = d.timings && +d.timings.total_ms;
        imp.fetchName = null;
        imp.fetchDone = 0;
        imp.fetchTotal = 0;
        window.__SK_FETCH_PROGRESS = null;
        if (!imp.step) return;
        toast('ok', '已下载到临时目录',
          (d.name || row.name) + ' · ' + (d.files || 0) + ' 个文件'
          + (ms > 0 ? ' · ' + (ms / 1000).toFixed(1) + 's' : '') + ' — 继续预检');
        impGoPreflight(d.path, d.name || row.name);
      }).catch(function (err) {
        var msg = String(err && err.message || err);
        var cancelled = msg.indexOf('已取消') >= 0;
        imp.fetchName = null;
        imp.fetchDone = 0;
        imp.fetchTotal = 0;
        window.__SK_FETCH_PROGRESS = null;
        if (imp.step) impRender();
        if (cancelled) toast('info', '已取消', '仓库没有变化，可重新获取');
        else toast('bad', '下载失败', msg + ' — 仓库没有变化，可重试');
      });
      return;
    }
    if (act === 'imp-back') { impClearTimers(); imp.step = 'source'; imp.pre = null; imp.preError = null; impRender(); return; }
    if (act === 'imp-sug') {
      var n = t.getAttribute('data-name');
      imp.name = n;
      var ni = document.getElementById('imp-name');
      if (ni) ni.value = n;
      impRender();
      return;
    }
    if (act === 'imp-skip') { impClose(); toast('info', '已跳过', '仓库里的同名 skill 内容一致，无需导入'); return; }
    if (act === 'imp-folder') { imp.folder = t.getAttribute('data-f'); impRender(); return; }
    if (act === 'imp-start' || act === 'imp-retry') { impStartRun(); return; }
    if (act === 'imp-finish') { impClose(); return; }
    if (act === 'imp-agents') {
      impClose();
      setBoard('agents');
      toast('info', '已切到智能体', '「' + (imp.lastAction && imp.lastAction.name || imp.name) + '」待分发：打开各 agent 的开关后点刷新同步');
      return;
    }
  }

  function impModalInput(e) {
    if (e.target.id === 'imp-name') {
      // 键入时只做原位更新，绝不整弹窗重渲染（会丢焦点 / 光标 / IME 组合态）
      imp.name = e.target.value;
      var v = impValidate();
      e.target.classList.toggle('err', !!v.nameErr);
      var errEl = document.querySelector('#modal-root [data-role="name-err"]');
      if (errEl) {
        errEl.hidden = !v.nameErr;
        var esp = errEl.querySelector('span');
        if (esp) esp.textContent = v.nameErr || '';
      }
      var helpEl = document.querySelector('#modal-root [data-role="name-help"]');
      if (helpEl) helpEl.hidden = !!v.nameErr;
      var cp = document.querySelector('#modal-root [data-role="panel-conflict"]');
      if (cp) {
        cp.hidden = !v.conflict;
        if (v.conflict) {
          cp.outerHTML = impConflictPanelHtml(imp.name, true);
        }
      }
      var sp = document.querySelector('#modal-root [data-role="panel-same"]');
      if (sp) {
        sp.hidden = !v.same;
        if (v.same) {
          var h = sp.querySelector('.r-mono');
          if (h) h.textContent = impHash8(imp.name);
        }
      }
      var dest = document.querySelector('#modal-root .dest-box b');
      if (dest) dest.textContent = STORE + '\\' + (imp.name || '…');
      var startBtn = document.querySelector('#modal-root [data-act="imp-start"]');
      if (startBtn) startBtn.disabled = !(v.ok && !v.conflict && !v.same);
      return;
    }
    if (e.target.id === 'imp-path') { imp.pathText = e.target.value; return; }
    if (e.target.id === 'imp-q') { imp.q = e.target.value; return; }
  }

  function impModalKeydown(e) {
    if (e.target.id === 'imp-path' && e.key === 'Enter') {
      var btn = document.querySelector('#modal-root [data-act="imp-use-path"]');
      if (btn) btn.click();
    }
    if (e.target.id === 'imp-q' && e.key === 'Enter') {
      var sb = document.querySelector('#modal-root [data-act="imp-search"]');
      if (sb) sb.click();
    }
  }

  /* ---------- 全窗拖拽（三级路径降级，拿不到路径绝不假装有） ---------- */
  var dropOv = null, dragDepth = 0;
  function impHasFiles(e) {
    return e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types || [], 'Files') > -1;
  }
  function impShowDropOv() {
    if (dropOv || imp.step === 'running') return;
    dropOv = document.createElement('div');
    dropOv.className = 'drop-ov';
    dropOv.innerHTML = '<div class="drop-card"><span class="dz-ic">' + icon('upload') + '</span>' +
      '<b>松手，导入到 skilllocker</b><span>文件夹会被复制进中央仓库，源目录不动</span></div>';
    document.body.appendChild(dropOv);
  }
  function hideDropOv() {
    dragDepth = 0;
    if (dropOv) { dropOv.remove(); dropOv = null; }
  }
  function impDropPayload(e) {
    var dt = e.dataTransfer;
    if (!dt) return null;
    try {
      // 1) File.path（pywebview / Electron 系注入的全路径）
      if (dt.files && dt.files.length && dt.files[0].path) return { path: String(dt.files[0].path) };
    } catch (err) {}
    try {
      // 2) pywebview 自定义 mime：部分平台给全路径数组
      var raw = dt.getData('application/x-pywebview-drag-drop') || dt.getData('pywebview-drag-drop');
      if (raw) {
        var arr = JSON.parse(raw);
        if (arr && arr.length && arr[0]) return { path: String(arr[0]) };
      }
    } catch (err) {}
    // 3) 只有名字（WebView2 安全限制）——如实降级，不假装有路径
    var name = '';
    try {
      if (dt.files && dt.files.length) name = dt.files[0].name;
      else if (dt.items && dt.items.length && dt.items[0].getAsFile) {
        var f = dt.items[0].getAsFile();
        if (f) name = f.name;
      }
    } catch (err) {}
    return name ? { name: name } : null;
  }
  function impHandleFileDrop(e) {
    if (!impHasFiles(e)) return;
    var got = impDropPayload(e);
    hideDropOv();
    if (imp.step === 'running') return;
    if (!got) { toast('warn', '没有识别到文件夹', '请改用「浏览」选择目录'); return; }
    if (!document.getElementById('modal-root').firstElementChild) impOpen('source');
    if (imp.step !== 'source') return;
    if (got.path) {
      impGoPreflight(got.path);
      toast('info', '已捕获拖入的目录', baseNameOf(got.path) + ' — 进入预检');
    } else {
      toast('warn', '拖拽只拿到了名字：' + got.name,
        '浏览器安全限制拿不到完整路径 —— 请把路径粘贴到下面，或点「浏览」选择');
      var inp = document.getElementById('imp-path');
      if (inp) inp.focus();
    }
  }
  function bindImportDragAndDrop() {
    document.addEventListener('dragenter', function (e) {
      // 弹窗开着用弹窗内投放区；执行中不显示遮罩（导入不可打断的暗示之一）
      if (!impHasFiles(e) || imp.step === 'running') return;
      if (document.getElementById('modal-root').firstElementChild) return;
      dragDepth++;
      impShowDropOv();
    });
    document.addEventListener('dragleave', function () {
      if (!dropOv) return;
      dragDepth--;
      if (dragDepth <= 0) hideDropOv();
    });
    document.addEventListener('dragover', function (e) {
      if (dropOv) e.preventDefault(); // 允许 drop：遮罩在场时接管整窗
    });
    document.addEventListener('drop', function (e) {
      // 内部行拖拽归档（bindDragAndDrop）不归这里管；只接外部文件投递
      if (!impHasFiles(e)) return;
      if (!dropOv && !document.getElementById('imp-dz')) return;
      e.preventDefault();
      e.stopPropagation();
      impHandleFileDrop(e);
    });
  }

  function resetKey(key) {
    // 「还原」只清显示名 / 备注；归属文件夹保持不动（不再删整行）
    var m = metaOf(key);
    m.display = '';
    m.note = '';
    m.updated = 0;
    delete dirtyKeys[key];
    delete metaRetries[key];
    saveMeta(key);
    renderChrome();
    renderContent(false);
    renderInspector();
    flashSaved();
    toast('info', '已还原', '显示名与备注回到默认值，归属分类保持不变');
  }

  /* ---------------------------------------------------------- 文件夹动作 */
  // 归属落库 / 本地保存 + 三处界面同步（侧栏计数、列表行、检查器 chip）
  function assignSkill(key, folderId, silent) {
    if (scopeOf(key) !== 'shared') return;
    var m = metaOf(key);
    var target = folderId || '';
    var changed = (m.folder || '') !== target;
    m.folder = target;
    m.updated = Date.now();
    saveMeta(key);
    flashSaved();
    renderChrome();
    renderContent(false);
    renderInspector();
    if (changed && !silent) {
      var f = folderById(target);
      var label = (m.display || key);
      toast('ok', '已移动', '「' + label + '」→ ' + (f ? '「' + f.name + '」' : '未归档'));
    }
  }

  function openFolder(id) {
    // 收起 ⋯ 菜单、chrome/内容/检查器刷新都在 setState 的 folder 分支
    setState({ folder: id === 'all' ? null : id });
  }

  // 组行「查看实例」：进入该 agent 文件夹并选中那一条实例（tool/name）
  function openGroupInstance(key) {
    var tool = String(key || '').split('/')[0];
    if (!tool) return;
    setState({ folder: tool, selected: key });
    insScrollTop();
  }

  function setViewMode(mode) {
    if (mode !== 'grid' && mode !== 'list') return;
    if (viewOf(state.board) === mode) return;
    prefs.views[state.board] = mode;
    savePrefs();
    renderChrome();
    renderContent(false);
  }

  function setSort() {
    prefs.sort = prefs.sort === 'name' ? 'edited' : prefs.sort === 'edited' ? 'starred' : 'name';
    savePrefs();
    renderChrome();
    renderContent(false);
  }

  /* ---------------------------------------------------------- 文件夹 CRUD */
  function folderModal(mode, folderId, assignKey) {
    var f = folderId ? folderById(folderId) : null;
    var color = f ? f.color : PALETTE[0];
    var name = f ? f.name : '';
    openModal(
      '<div class="modal">' +
      '<button class="modal-close" data-close="1" aria-label="关闭">' + icon('x') + '</button>' +
      '<div class="modal-head"><span class="modal-eyebrow">' + (f ? 'RENAME FOLDER' : 'NEW FOLDER') + '</span>' +
      '<h2>' + (f ? '重命名分类' : '新建分类文件夹') + '</h2></div>' +
      '<div class="modal-body">' +
      '<div class="field-label"><span>名称</span></div>' +
      '<input class="name-input" id="fm-name" type="text" maxlength="12" placeholder="例如：自动化 / 写作 / 数据…" value="' + esc(name) + '" spellcheck="false">' +
      '<div class="field-label" style="margin-top:12px"><span>颜色</span></div>' +
      '<div class="palette" id="fm-palette">' +
      PALETTE.map(function (c) {
        return '<button data-color="' + c + '" class="' + (c === color ? 'on' : '') +
          '" style="--pc:var(--' + (c === 'green' ? 'success' : c) + ')" aria-label="' + c + '"></button>';
      }).join('') +
      '</div>' +
      '<p style="margin-top:10px;font-size:11px">' +
      (f
        ? '分类名与颜色只影响展示；其中 ' + skillsInFolder(f.id).length + ' 个 skill 的归属保持不变。'
        : (assignKey
          ? '创建后会把「' + esc((meta[assignKey] && meta[assignKey].display) || assignKey) + '」直接归入这个分类。'
          : '创建后是一个空文件夹，把 skill 拖进来或用 ⋯ 菜单移动。')) +
      '</p></div>' +
      '<div class="modal-foot"><button class="btn" data-close="1">取消</button>' +
      '<button class="btn btn-primary" id="fm-ok">' + (f ? '保存' : '创建') + '</button></div>' +
      '</div>');
    var picked = color;
    document.getElementById('fm-palette').addEventListener('click', function (e) {
      var b = e.target.closest('[data-color]');
      if (!b) return;
      picked = b.getAttribute('data-color');
      Array.prototype.forEach.call(this.querySelectorAll('button'), function (x) {
        x.classList.toggle('on', x === b);
      });
    });
    var input = document.getElementById('fm-name');
    input.focus();
    document.getElementById('fm-ok').addEventListener('click', function () {
      var val = input.value.trim().slice(0, 12);
      if (!val) { toast('warn', '名称不能为空', '给分类起个名字再保存'); return; }
      if (input.disabled) return; // Enter 双触发兜底：document 级 keydown 也会点 OK
      if (MODE === 'db' && SKB) {
        input.disabled = true;
        var p = f ? SKB.folderRename(f.id, val, picked) : SKB.folderCreate(val, picked);
        p.then(function (res) {
          closeModal();
          applyBootstrap(res);
          if (!f && assignKey && res.folder) assignSkill(assignKey, res.folder.id, true);
          toast('ok', f ? '分类已更新' : '分类已创建', val);
        }).catch(function (err) {
          input.disabled = false;
          toast('bad', f ? '保存失败' : '创建失败', String(err && err.message || err));
        });
        return;
      }
      if (f) {
        f.name = val;
        f.color = picked;
        savePrefs();
        closeModal();
        renderAll(false);
        toast('ok', '分类已更新', val);
        return;
      }
      var id = 'f-' + Date.now().toString(36).slice(-6);
      prefs.folders.push({ id: id, name: val, color: picked, position: prefs.folders.length });
      FOLDERS = prefs.folders;
      savePrefs();
      closeModal();
      if (assignKey) {
        assignSkill(assignKey, id, true);
        toast('ok', '分类已创建', val + ' · 「' + ((meta[assignKey] && meta[assignKey].display) || assignKey) + '」已归入');
      } else {
        renderAll(false);
        toast('ok', '分类已创建', val + ' · 空文件夹');
      }
    });
  }

  function confirmDeleteFolder(f) {
    if (!f) return;
    var n = skillsInFolder(f.id).length;
    confirmModal(
      '删除分类「' + f.name + '」？',
      '其中 ' + n + ' 个 skill 将回到未归档，skill 文件本身不受影响；此操作只删除分类，不删除任何 skill。' +
        '也可以先把里面的 skill 拖到其他文件夹。',
      '删除分类',
      function () {
        closeModal();
        if (MODE === 'db' && SKB) {
          SKB.folderDelete(f.id).then(function (res) {
            if (state.folder === f.id) state.folder = null;
            applyBootstrap(res);
            toast('ok', '分类已删除', '「' + f.name + '」里的 ' +
              ((res.action && res.action.moved) || 0) + ' 个 skill 已回到未归档');
          }).catch(function (err) {
            toast('bad', '删除失败', String(err && err.message || err));
          });
          return;
        }
        prefs.folders = prefs.folders.filter(function (x) { return x.id !== f.id; });
        FOLDERS = prefs.folders;
        Object.keys(meta).forEach(function (key) {
          if (meta[key] && meta[key].folder === f.id) {
            meta[key].folder = '';
            meta[key].updated = Date.now();
          }
        });
        saveMeta();
        savePrefs();
        if (state.folder === f.id) state.folder = null;
        renderAll(false);
        toast('ok', '分类已删除', '「' + f.name + '」里的 ' + n + ' 个 skill 已回到未归档');
      },
      { danger: true });
  }

  /* ---------------------------------------------------------- ⋯ 快捷菜单 */
  // 菜单挂在 body 上：不能用 root 委托，点击处理统一放 document
  var ctxEl = null;
  var ctxSkill = null;

  function closeCtx() {
    if (ctxEl) { ctxEl.remove(); ctxEl = null; }
  }

  function openCtx(items, x, y) {
    closeCtx();
    ctxEl = document.createElement('div');
    ctxEl.className = 'ctx-menu';
    ctxEl.innerHTML = items.map(function (it) {
      if (it === '-') return '<div class="ctx-sep"></div>';
      if (it.label === '移动到…') return '<div class="ctx-label">移动到…</div>';
      return '<button class="ctx-item' + (it.danger ? ' danger' : '') + (it.gold ? ' star-item' : '') +
        (it.disabled ? ' current' : '') +
        '" data-ctx="' + esc(it.act || '') + '"' + (it.arg ? ' data-arg="' + esc(it.arg) + '"' : '') + '>' +
        (it.dot ? '<span class="catdot" style="--c:' + it.dot + '"></span>' : icon(it.ic || 'folder')) +
        esc(it.label) + (it.badge ? '<b>' + esc(it.badge) + '</b>' : '') + '</button>';
    }).join('');
    document.body.appendChild(ctxEl);
    var r = ctxEl.getBoundingClientRect();
    ctxEl.style.left = Math.min(x, window.innerWidth - r.width - 10) + 'px';
    ctxEl.style.top = Math.min(y, window.innerHeight - r.height - 10) + 'px';
  }

  function skillMenu(key, x, y) {
    if (scopeOf(key) !== 'shared') return;
    ctxSkill = key;
    var cur = (meta[key] && meta[key].folder) || '';
    var items = [
      { label: isStarredKey(key) ? '取消收藏' : '收藏', ic: 'star', act: 'star', gold: true },
      { label: '移动到…', ic: 'folder' }
    ];
    FOLDERS.forEach(function (f) {
      items.push({
        label: f.name, dot: COLORS[f.color][0], act: 'move', arg: f.id,
        badge: cur === f.id ? '当前' : String(skillsInFolder(f.id).length),
        disabled: cur === f.id
      });
    });
    items.push('-');
    items.push({ label: '移出分类（回到未归档）', ic: 'inbox', act: 'unfile', disabled: !cur });
    openCtx(items, x, y);
  }

  function folderMenu(folderId, x, y) {
    openCtx([
      { label: '重命名', ic: 'pencil', act: 'rename', arg: folderId },
      { label: '更改颜色', ic: 'folder', act: 'recolor', arg: folderId },
      '-',
      { label: '删除分类', ic: 'trash', danger: true, act: 'del', arg: folderId }
    ], x, y);
  }

  /* ---------------------------------------------------------- 拖拽归档 */
  // 行 / 文件卡 → 文件夹卡片或侧栏夹 = 归档；→ 面包屑「全部技能」= 移出。
  // 只有 shared 行带 draggable；独有 / 智能体看板不出现文件夹元素。
  function bindDragAndDrop() {
    var dragSkill = null;
    document.addEventListener('dragstart', function (e) {
      var src = e.target.closest && e.target.closest('[data-key][draggable]');
      if (!src) return;
      var key = src.getAttribute('data-key');
      if (scopeOf(key) !== 'shared') return;
      dragSkill = key;
      src.classList.add('is-dragging');
      try {
        e.dataTransfer.setData('text/plain', 'skill:' + dragSkill);
        e.dataTransfer.effectAllowed = 'move';
      } catch (err) {}
    });
    document.addEventListener('dragend', function () {
      Array.prototype.forEach.call(
        document.querySelectorAll('.is-dragging, .drop-hint'),
        function (el) { el.classList.remove('is-dragging', 'drop-hint'); });
      dragSkill = null;
    });
    function dropTarget(el) { return el.closest && el.closest('[data-drop-folder], [data-drop-unfile]'); }
    document.addEventListener('dragover', function (e) {
      var t = dropTarget(e.target);
      if (!t || !dragSkill) return;
      e.preventDefault();
      t.classList.add('drop-hint');
    });
    document.addEventListener('dragleave', function (e) {
      var t = dropTarget(e.target);
      if (t) t.classList.remove('drop-hint');
    });
    document.addEventListener('drop', function (e) {
      var t = dropTarget(e.target);
      if (!t || !dragSkill) return;
      e.preventDefault();
      var data = '';
      try { data = e.dataTransfer.getData('text/plain'); } catch (err) {}
      var key = dragSkill || (data.indexOf('skill:') === 0 ? data.slice(6) : '');
      t.classList.remove('drop-hint');
      dragSkill = null;
      if (!key || scopeOf(key) !== 'shared') return;
      if (t.hasAttribute('data-drop-unfile')) assignSkill(key, '');
      else assignSkill(key, t.getAttribute('data-drop-folder') || '');
    });
  }

  function renderAll(animate) {
    renderChrome();
    renderContent(animate);
    renderUniquePending();
    renderInspector();
  }

  /* 两段式刷新的第二段还在扫：独有看板的数据是上一轮的，标题行如实标一句
     「独有扫描中…」，别让沿用下来的数字看起来像本次结论。判据只看后端标记：
     uniquePending 只可能由第一段的回包置真，第二段回来自然消失。 */
  function renderUniquePending() {
    if (!META.uniquePending || state.board !== 'unique') return;
    var sub = document.getElementById('page-sub');
    if (sub) sub.textContent += ' · 独有扫描中…';
  }

  /* ---------------------------------------------------------- 全局刷新 */
  var refreshBusy = false;

  // 全局刷新：桌面端走两段（rescan_shared 先出共有看板，rescan_unique 再补独有），
  // 进度条与请求并行，不再空等那 600ms 动画；原型只重渲染内置快照。
  // 忙碌时按钮转圈并忽略重复点击；看板、筛选、分类与选中由 applyBootstrap
  // 尽量保留，列表滚动位置手动恢复。
  function requestRefresh() {
    if (refreshBusy) return;
    refreshBusy = true;
    var btn = document.getElementById('refresh-btn');
    if (btn) btn.classList.add('is-busy');
    var done = function () {
      refreshBusy = false;
      if (btn) btn.classList.remove('is-busy');
    };
    if (MODE !== 'db' || !SKB) {
      runProgress(600, function () {
        renderAll(true);
        done();
        toast('info', '已刷新', '原型模式：重新渲染内置快照');
      });
      return;
    }
    var stop = startProgress(600);
    refreshTwoPhase(function (res, err) {
      stop();
      done();
      if (err) { toast('bad', '刷新失败', String(err && err.message || err)); return; }
      var dist = res.distributed || {};
      var distN = dist.changed || 0;
      var distErr = (dist.errors || []).length;
      toast('ok', '已刷新', '扫描到 ' + (res.scanned != null ? res.scanned : SKILLS.length) +
        ' 个 skill · ' + agentCounts(agentRows()).connected + ' 个 agent 已连接 · ' +
        uniqueLogicalCount() + ' 个独有技能' +
        (distN ? ' · 新增/变更已分发 ' + distN + ' 处链接' : '') +
        (distErr ? ' · ' + distErr + ' 个 agent 分发失败' : ''));
    });
  }

  /* ---------------------------------------------------------- window resize */
  // 无边框窗口的缩放自己在 JS 里算：grip 记下按下的屏幕坐标，pointermove 时按
  // screenX/screenY 的绝对位移求目标矩形，交给后端 SetWindowPos。
  // 不走系统原生 hit-test（WM_NCLBUTTONDOWN）：WebView2 持有鼠标捕获，跨线程发消息
  // 拿不到按住状态，实测拖动后窗口尺寸 delta 恒为 0。
  var WIN_MIN = { w: 1180, h: 720 };
  var dragState = null;

  function resizeTarget(edge, dx, dy) {
    var b = dragState.bounds;
    var left = b.x, top = b.y, right = b.x + b.w, bottom = b.y + b.h;
    if (edge.indexOf('e') > -1) right += dx;
    if (edge.indexOf('w') > -1) left += dx;
    if (edge.indexOf('s') > -1) bottom += dy;
    if (edge.indexOf('n') > -1) top += dy;
    if (right - left < WIN_MIN.w) {
      if (edge.indexOf('w') > -1) left = right - WIN_MIN.w;
      else right = left + WIN_MIN.w;
    }
    if (bottom - top < WIN_MIN.h) {
      if (edge.indexOf('n') > -1) top = bottom - WIN_MIN.h;
      else bottom = top + WIN_MIN.h;
    }
    return {
      x: Math.round(left), y: Math.round(top),
      w: Math.round(right - left), h: Math.round(bottom - top)
    };
  }

  // 一次只放一个调用在飞：连续拖动只保留最新目标，避免 60Hz 打爆跨线程桥
  function resizePump() {
    var st = dragState;
    if (!st || st.inflight || !st.target || !SKB) return;
    var b = st.target;
    st.target = null;
    st.inflight = true;
    SKB.winSetBounds(b.x, b.y, b.w, b.h).then(function () {
      st.inflight = false;
      resizePump();
    }, function (err) {
      st.inflight = false;
      endResize();
      toast('bad', '缩放失败', String(err && err.message || err));
    });
  }

  function endResize() {
    var st = dragState;
    dragState = null;
    document.body.classList.remove('resizing');
    if (!st || !st.grip) return;
    try { st.grip.releasePointerCapture(st.pid); } catch (e) {}
  }

  function beginResize(e, grip) {
    if (MODE !== 'db' || !SKB || !SKB.winBounds) return;
    if (e.button) return;
    e.preventDefault();
    var pid = e.pointerId;
    // 顶部/边角手柄抢走按下点时，窗口只会缩放不会移动；日志里必须能看见，
    // 否则「拖不动」会被误判成「收不到 pointerdown」。
    uiLog('resize.down', { edge: grip.getAttribute('data-resize') || '',
                           x: e.screenX, y: e.screenY,
                           trusted: e.isTrusted });
    dragState = {
      edge: grip.getAttribute('data-resize') || '',
      grip: grip, pid: pid,
      sx: e.screenX, sy: e.screenY,
      lx: e.screenX, ly: e.screenY,
      bounds: null, target: null, inflight: false, done: false
    };
    try { grip.setPointerCapture(pid); } catch (err) {}
    document.body.classList.add('resizing');
    SKB.winBounds().then(function (res) {
      var st = dragState;
      if (!st || st.pid !== pid) return;
      if (res && res.maximized) { uiLog('resize.max', res); endResize(); return; }
      st.bounds = res && res.bounds;
      if (!st.bounds) { endResize(); return; }
      st.target = resizeTarget(st.edge, st.lx - st.sx, st.ly - st.sy);
      if (st.done) {
        // 已经松手了：补发一次终值再收尾（与标题栏拖动同理）
        SKB.winSetBounds(st.target.x, st.target.y, st.target.w, st.target.h)
          .catch(function () {});
        endResize();
        return;
      }
      resizePump();
    }, function (err) {
      endResize();
      toast('bad', '缩放失败', String(err && err.message || err));
    });
  }

  /* ---------------------------------------------------------- window move */
  // 标题栏整条可拖：空白区 / 品牌区按下即移动；输入框、按钮、窗口三键不参与。
  // 同样用 screenX/screenY 绝对位移 + SetWindowPos，避免依赖 pywebview 的 drag-region。
  var moveState = null;
  // 最大化态的还原阈值（CSS 像素）：按下后拖过这个距离才算「真的在拖」，
  // 才还原窗口跟随光标；单击 / 轻微抖动不还原 —— 缩小只认双击标题栏。
  var DRAG_RESTORE_PX = 6;

  // 诊断：真实手拖出问题时，kit/skilllocker.log 能说清断在哪一步
  function uiLog(kind, payload) {
    if (MODE !== 'db' || !SKB || !SKB.logUi) return;
    try { SKB.logUi(kind, JSON.stringify(payload || {})); } catch (err) {}
  }

  function moveTarget(dx, dy) {
    var b = moveState.bounds;
    return {
      x: Math.round(b.x + dx), y: Math.round(b.y + dy),
      w: Math.round(b.w), h: Math.round(b.h)
    };
  }

  function movePump() {
    var st = moveState;
    if (!st || st.inflight || !st.target || !SKB) return;
    var b = st.target;
    st.target = null;
    st.inflight = true;
    SKB.winSetBounds(b.x, b.y, b.w, b.h).then(function () {
      st.inflight = false;
      movePump();
    }, function (err) {
      st.inflight = false;
      uiLog('move.fail', { target: b, err: String(err && err.message || err) });
      endMove();
      toast('bad', '移动失败', String(err && err.message || err));
    });
  }

  function endMove() {
    var st = moveState;
    moveState = null;
    document.body.classList.remove('moving');
    if (!st || !st.bar) return;
    try { st.bar.releasePointerCapture(st.pid); } catch (e) {}
  }

  // 基准外框到手：最大化先挂起（等拖动阈值），还原后按 lx/ly 决定继续跟随还是补发终值
  function moveFrom(res, pid) {
    var st = moveState;
    if (!st || st.pid !== pid) return;
    if (res && res.maximized) {
      // 松手前基准矩形才回来（快速点按）：只有位移已越过阈值才补还原，
      // 纯单击（含 ≤ 阈值的抖动）什么都不做，直接收尾
      if (st.done) {
        if (Math.abs(st.lx - st.sx) > DRAG_RESTORE_PX ||
            Math.abs(st.ly - st.sy) > DRAG_RESTORE_PX) {
          uiLog('move.restore', res);
          SKB.winRestore().then(function (r2) {
            moveFrom(r2, pid);
          }, function (err) {
            uiLog('move.fail', { why: 'restore', err: String(err && err.message || err) });
            endMove();
            toast('bad', '移动失败', String(err && err.message || err));
          });
        } else {
          uiLog('move.click', { at: [st.sx, st.sy] });
          endMove();
        }
        return;
      }
      // 还按着：挂起还原，交给 pointermove 的阈值判定
      if (!st.zmax) {
        st.zmax = true;
        uiLog('move.maxpend', { at: [st.sx, st.sy] });
      }
      return;
    }
    st.bounds = res && res.bounds;
    if (!st.bounds) { uiLog('move.nobounds', res); endMove(); return; }
    uiLog('move.begin', { bounds: st.bounds, from: [st.sx, st.sy], done: st.done });
    st.target = moveTarget(st.lx - st.sx, st.ly - st.sy);
    if (st.done) {
      // 已经松手了：补发一次终值再收尾
      SKB.winSetBounds(st.target.x, st.target.y, st.target.w, st.target.h)
        .catch(function () {});
      endMove();
      return;
    }
    movePump();
  }

  function beginMove(e, bar) {
    if (MODE !== 'db' || !SKB || !SKB.winBounds) {
      uiLog('move.skip', { why: 'no-bridge', mode: MODE, has: !!SKB,
                           target: String(e.target && e.target.className || '') });
      return;
    }
    if (e.button) return;
    if (e.target.closest && e.target.closest('button, input, textarea, select, a, .gsearch, .title-actions')) {
      uiLog('move.skip', { why: 'control', target: String(e.target.className || e.target.tagName) });
      return;
    }
    e.preventDefault();
    var pid = e.pointerId;
    // lx/ly 始终记录最新光标位置：基准外框（winBounds）是跨线程异步的，
    // 用户快速拖一下就松手时它可能还没回来，不能因此丢掉整段位移。
    moveState = {
      bar: bar, pid: pid,
      sx: e.screenX, sy: e.screenY,
      lx: e.screenX, ly: e.screenY,
      bounds: null, target: null, inflight: false, done: false, logged: false,
      zmax: false  // 基准矩形回来时是最大化：挂起还原，拖过阈值才真的还原
    };
    document.body.classList.add('moving');
    uiLog('move.down', { x: e.screenX, y: e.screenY, pid: pid,
                         target: String(e.target.className || e.target.tagName),
                         trusted: e.isTrusted });
    try { bar.setPointerCapture(pid); } catch (err) {}
    SKB.winBounds().then(function (res) {
      moveFrom(res, pid);
    }, function (err) {
      uiLog('move.fail', { why: 'bounds', err: String(err && err.message || err) });
      endMove();
      toast('bad', '移动失败', String(err && err.message || err));
    });
  }

  /* ---------------------------------------------------------- events */
  function bind() {
    bindDragAndDrop();
    bindImportDragAndDrop();

    // 导入弹窗（SPEC-skill-import.md）：顶栏入口 + 弹窗内事件委托 + Ctrl+I
    var impTop = document.getElementById('btn-import-top');
    if (impTop) impTop.addEventListener('click', function () { impOpen('source'); });
    var mroot = document.getElementById('modal-root');
    mroot.addEventListener('click', impModalClick);
    mroot.addEventListener('input', impModalInput);
    mroot.addEventListener('keydown', impModalKeydown);

    document.addEventListener('pointerdown', function (e) {
      var grip = e.target.closest && e.target.closest('[data-resize]');
      if (grip) { beginResize(e, grip); return; }
      var bar = e.target.closest && e.target.closest('.titlebar');
      if (bar) beginMove(e, bar);
    }, true);

    document.addEventListener('pointermove', function (e) {
      var mv = moveState;
      if (mv && e.pointerId === mv.pid) {
        mv.lx = e.screenX; mv.ly = e.screenY;
        // 最大化挂起还原：位移越过阈值才算拖动，此刻才还原并继续跟随；
        // 单击 / 轻微抖动永远走不到这里，窗口保持最大化
        if (mv.zmax && !mv.done &&
            (Math.abs(mv.lx - mv.sx) > DRAG_RESTORE_PX ||
             Math.abs(mv.ly - mv.sy) > DRAG_RESTORE_PX)) {
          mv.zmax = false;
          uiLog('move.restore', { dx: mv.lx - mv.sx, dy: mv.ly - mv.sy });
          SKB.winRestore().then(function (r2) {
            moveFrom(r2, mv.pid);
          }, function (err) {
            uiLog('move.fail', { why: 'restore', err: String(err && err.message || err) });
            endMove();
            toast('bad', '移动失败', String(err && err.message || err));
          });
        }
        if (mv.bounds && !mv.done) {
          mv.target = moveTarget(mv.lx - mv.sx, mv.ly - mv.sy);
          if (!mv.logged) {
            mv.logged = true;
            uiLog('move.first', { from: [mv.sx, mv.sy], to: [mv.lx, mv.ly],
                                  target: mv.target });
          }
          movePump();
        }
      }
      var st = dragState;
      if (!st || e.pointerId !== st.pid) return;
      st.lx = e.screenX; st.ly = e.screenY;
      if (!st.bounds || st.done) return;
      st.target = resizeTarget(st.edge, st.lx - st.sx, st.ly - st.sy);
      resizePump();
    }, true);

    function finishResize(e) {
      var st = dragState;
      if (!st || e.pointerId !== st.pid) return;
      st.lx = e.screenX; st.ly = e.screenY;
      // 最后一次位移可能还在队列里：直接补发一次终值，再收尾
      if (st.bounds && SKB && SKB.winSetBounds) {
        var fin = resizeTarget(st.edge, st.lx - st.sx, st.ly - st.sy);
        SKB.winSetBounds(fin.x, fin.y, fin.w, fin.h).catch(function () {});
        endResize();
        return;
      }
      st.done = true;  // 基准外框还没回来：等 winBounds 落地后补一次终值
    }
    document.addEventListener('pointerup', finishResize, true);
    document.addEventListener('pointercancel', finishResize, true);

    function finishMove(e) {
      var mv = moveState;
      if (!mv || e.pointerId !== mv.pid) return;
      mv.lx = e.screenX; mv.ly = e.screenY;
      // 最大化挂起还原时松手 = 一次单击：不还原、不写外框，直接收尾
      if (mv.zmax) {
        uiLog('move.click', { at: [mv.sx, mv.sy] });
        endMove();
        return;
      }
      if (mv.bounds && SKB && SKB.winSetBounds) {
        var fin = moveTarget(mv.lx - mv.sx, mv.ly - mv.sy);
        SKB.winSetBounds(fin.x, fin.y, fin.w, fin.h).catch(function () {});
        uiLog('move.end', { to: [mv.lx, mv.ly], bounds: mv.bounds, target: fin });
        endMove();
        return;
      }
      mv.done = true;  // 基准外框还没回来：等 winBounds 落地后补一次终值
    }
    document.addEventListener('pointerup', finishMove, true);
    document.addEventListener('pointercancel', finishMove, true);

    // 双击标题栏空白处 = 最大化/还原（Windows 习惯）；按钮、搜索框、LOCAL 胶囊不接管
    document.addEventListener('dblclick', function (e) {
      if (MODE !== 'db' || !SKB || !SKB.winMax) return;
      if (!e.target.closest || !e.target.closest('.titlebar')) return;
      if (e.target.closest('button, input, textarea, select, a, .gsearch, .title-actions')) return;
      SKB.winMax().catch(function (err) {
        toast('bad', '窗口操作失败', String(err && err.message || err));
      });
    });

    // 分发开关：change 是唯一可靠入口。无论点中覆盖在开关上的透明 input，
    // 还是点到 label/装饰 i（label 会把点击转发给 input），checked 都已翻成目标状态，
    // 一次按下只产生一个 change，不会重复请求。
    document.addEventListener('change', function (e) {
      var box = e.target;
      if (!box || box.tagName !== 'INPUT' || !box.closest) return;
      var sw = box.closest('[data-agent-switch]');
      if (!sw || box.disabled) return;
      var key = sw.getAttribute('data-agent-switch');
      var a = agentByKey(key);
      if (!a) return;
      requestAgent(key, box.checked, function revert() {
        // 取消 / 失败：开关回到数据模型的值，别让界面撒谎
        if (box.isConnected) box.checked = !!a.enabled;
      });
    });

    document.addEventListener('click', function (e) {
      var t = e.target;

      // ⋯ 快捷菜单：先于导航 / 行选中判断（按钮在行与卡片内部）
      var moreF = t.closest('[data-more-folder]');
      if (moreF) {
        folderMenu(moreF.getAttribute('data-more-folder'), e.clientX, e.clientY);
        return;
      }
      var moreS = t.closest('[data-more-skill]');
      if (moreS) {
        skillMenu(moreS.getAttribute('data-more-skill'), e.clientX, e.clientY);
        return;
      }
      // 菜单开着时点别处：关闭菜单，但这次点击继续走正常逻辑（选中行等）
      if (ctxEl) {
        var ci = t.closest('[data-ctx]');
        if (ci) {
          var act = ci.getAttribute('data-ctx');
          var arg = ci.getAttribute('data-arg');
          var skillKey = ctxSkill;
          closeCtx();
          ctxSkill = null;
          if (act === 'move' && arg && skillKey) assignSkill(skillKey, arg);
          if (act === 'unfile' && skillKey) assignSkill(skillKey, '');
          if (act === 'star' && skillKey) toggleStar(skillKey);
          if (act === 'rename' && arg) folderModal('edit', arg);
          if (act === 'recolor' && arg) folderModal('edit', arg);
          if (act === 'del' && arg) confirmDeleteFolder(folderById(arg));
          return;
        }
        if (!t.closest('.ctx-menu')) closeCtx();
      }

      // ---- 收藏（v0.3.1）----
      // 星标按钮（行尾 / 卡片角）：先于行选中 return，不会误选行
      var starBtn = t.closest('[data-star]');
      if (starBtn) { toggleStar(starBtn.getAttribute('data-star')); return; }
      // 检查器星标（常驻按钮，跟随当前选中行）
      if (t.closest('#ins-star')) { if (state.selected) toggleStar(state.selected); return; }
      // 面包屑「清空收藏」：两段确认
      var clearBtn = t.closest('[data-clear-starred]');
      if (clearBtn) { clearStarredArm(clearBtn); return; }
      // 侧栏「收藏」导航：跨看板入口，永远落回共有看板的收藏虚拟夹
      if (t.closest('#nav-starred')) {
        if (state.board !== 'shared') setState({ board: 'shared', folder: 'starred' });
        else openFolder('starred');
        return;
      }

      var boardEl = t.closest('[data-board]');
      if (boardEl) { setBoard(boardEl.getAttribute('data-board')); return; }


      // 分发开关只由下方的 change 事件驱动：这里拦下，避免同时触发行选中。
      // 不能在 click 里读 !box.checked —— 点击冒到 document 时 checked 已被翻转，
      // 再取反正好反了（点“开”着的开关却又发一次 open，界面看起来就是关不掉）。
      if (t.closest('[data-agent-switch]')) return;

      var statusEl = t.closest('[data-agent-status]');
      if (statusEl) {
        setState({ agentStatus: statusEl.getAttribute('data-agent-status') });
        return;
      }

      var agentOpen = t.closest('[data-agent-open]');
      if (agentOpen) {
        var agentKey = agentOpen.getAttribute('data-agent-open');
        if (MODE === 'db' && SKB) {
          SKB.openDir('agent', agentKey).then(function (res) {
            if (res && res.path) toast('ok', '已打开目录', res.path);
          }).catch(function (err) {
            toast('bad', '打开失败', String(err && err.message || err));
          });
        } else {
          toast('info', '打开目录', '桌面端会调用 explorer.exe 打开该 agent 的 skills 目录');
        }
        return;
      }

      if (t.closest('[data-agent-rescan]')) {
        if (MODE === 'db' && SKB) {
          // 与 requestRefresh 共用 refreshBusy 防重入：连点会并发多个 rescan、
          // 重复 applyBootstrap 和双 toast（refreshBusy 与 #refresh-btn 的
          // is-busy 类解耦，这里不会让工具栏按钮误转圈）
          if (refreshBusy) { toast('info', '扫描进行中', '请等待当前扫描完成'); return; }
          refreshBusy = true;
          var doneRs = function () { refreshBusy = false; };
          var stopRs = startProgress(600);
          refreshTwoPhase(function (res, err) {
            stopRs();
            doneRs();
            if (err) { toast('bad', '重新探测失败', String(err && err.message || err)); return; }
            toast('ok', '探测完成', '识别到 ' + agentCounts(agentRows()).connected +
              ' 个已连接 agent');
          });
        } else {
          toast('info', '重新探测', '桌面端才会真的扫描本机目录');
        }
        return;
      }

      // ---- 分类文件夹导航 / CRUD ----
      var folderNav = t.closest('[data-folder-nav]');
      if (folderNav) { openFolder(folderNav.getAttribute('data-folder-nav')); return; }
      var folderCard = t.closest('[data-folder-card]');
      if (folderCard) { openFolder(folderCard.getAttribute('data-folder-card')); return; }
      if (t.closest('[data-crumb-root]')) { openFolder('all'); return; }
      var jumpEl = t.closest('[data-jump-folder]');
      if (jumpEl) { openFolder(jumpEl.getAttribute('data-jump-folder')); return; }

      if (t.closest('#grid-new-folder') || t.closest('#side-add-folder')) { folderModal('create'); return; }
      var newFor = t.closest('[data-new-folder-for]');
      if (newFor) { folderModal('create', null, newFor.getAttribute('data-new-folder-for')); return; }
      var renEl = t.closest('[data-rename-folder]');
      if (renEl) { folderModal('edit', renEl.getAttribute('data-rename-folder')); return; }
      var delEl = t.closest('[data-del-folder]');
      if (delEl) { confirmDeleteFolder(folderById(delEl.getAttribute('data-del-folder'))); return; }

      // 检查器归属 chip：单选即移动
      var assign = t.closest('[data-assign]');
      if (assign && state.selected) {
        assignSkill(state.selected, assign.getAttribute('data-assign') || '');
        return;
      }

      var instEl = t.closest('[data-open-instance]');
      if (instEl) { openGroupInstance(instEl.getAttribute('data-open-instance')); return; }
      if (t.closest('[data-delete]')) { requestDelete(); return; }
      if (t.closest('[data-uninstall]')) { requestUninstall(); return; }
      if (t.closest('[data-promote]')) { requestPromote(); return; }

      if (t.closest('[data-close]')) { closeModal(); return; }
      if (t.closest('.modal-close')) { closeModal(); return; }

      // 行 / 文件卡选中（两者都带 data-key）
      var row = t.closest('.row, .file-card');
      if (row && row.getAttribute('data-key')) {
        setState({ selected: row.getAttribute('data-key') }, { insTop: true });
        return;
      }

      if (t.closest('[data-reset]') && state.selected) { resetKey(state.selected); return; }

      var copyEl = t.closest('[data-copy]');
      if (copyEl) {
        var copyTextValue = copyEl.getAttribute('data-copy');
        if (MODE === 'db' && SKB) {
          SKB.copyPath(copyTextValue).then(function () {
            toast('ok', '已复制', copyTextValue);
          }).catch(function () {
            copyText(copyTextValue);
          });
        } else {
          copyText(copyTextValue);
        }
        return;
      }

      var openEl = t.closest('[data-open]');
      if (openEl) {
        if (MODE === 'db' && SKB) {
          SKB.openDir(openEl.getAttribute('data-open-scope'), openEl.getAttribute('data-open-key'))
            .then(function (res) {
              if (res && res.path) toast('ok', '已打开目录', res.path);
            })
            .catch(function (err) {
              toast('bad', '打开失败', String(err && err.message || err));
            });
        } else {
          toast('info', '打开目录', '桌面端会调用 explorer.exe 定位到该路径（原型不执行）');
        }
        return;
      }

      var alignEl = t.closest('[data-align-name]');
      if (alignEl) { alignName(alignEl.getAttribute('data-align-name')); return; }

      var diffEl = t.closest('[data-diff]');
      if (diffEl) {
        openDiff(diffEl.getAttribute('data-diff-scope'), diffEl.getAttribute('data-diff-key'));
        return;
      }

      // 视图切换（顶栏分段）：prefs.view 持久化
      var viewBtn = t.closest('#view-seg [data-view-mode]');
      if (viewBtn) { setViewMode(viewBtn.getAttribute('data-view-mode')); return; }

      if (t.closest('#reset-filter-btn')) {
        document.getElementById('gsearch').value = '';
        setState({ q: '', agentStatus: 'all', folder: null });
        return;
      }

      if (t.closest('#refresh-btn')) { requestRefresh(); return; }

      if (t.closest('#sort-btn')) { setSort(); return; }

      if (t.closest('#density-btn')) {
        prefs.density = prefs.density === 'compact' ? 'comfortable' : 'compact';
        savePrefs(); renderChrome(); renderContent(false);
        return;
      }

      if (t.closest('#open-settings')) { openSettings(); return; }

      if (t.closest('#theme-toggle')) {
        prefs.theme = prefs.theme === 'dark' ? 'light' : 'dark';
        savePrefs(); renderChrome();
        return;
      }

      if (t.closest('#win-max')) {
        if (MODE === 'db' && SKB) {
          SKB.winMax().catch(function (err) {
            toast('bad', '窗口操作失败', String(err && err.message || err));
          });
        } else {
          document.getElementById('app-window').classList.toggle('maximized');
        }
        return;
      }

      if (t.closest('#win-min')) {
        if (MODE === 'db' && SKB) {
          SKB.winMin().catch(function () {});
        } else {
          toast('info', '最小化', '原型不接管窗口，桌面端交给 pywebview');
        }
        return;
      }

      if (t.closest('#win-close')) {
        if (MODE === 'db' && SKB) {
          SKB.winClose().catch(function () {});
        } else {
          toast('warn', '关闭窗口', '桌面端会退出进程，原型里只提示');
        }
        return;
      }
    });

    document.addEventListener('input', function (e) {
      var t = e.target;

      if (t.id === 'gsearch') {
        var nextQ = t.value.trim();
        // 与原型一致：搜索时强制回根（文件夹内不做搜索过滤）
        setState({ q: nextQ, folder: nextQ ? null : state.folder });
        return;
      }

      if (t.id === 'ins-name' && state.selected) {
        metaOf(state.selected).display = t.value.trim();
        commit(state.selected, {});
        return;
      }

      if (t.id === 'ins-note' && state.selected) {
        metaOf(state.selected).note = t.value;
        var counter = document.getElementById('note-count');
        if (counter) counter.textContent = t.value.length + ' / 400';
        commit(state.selected, {});
      }
    });

    document.addEventListener('keydown', function (e) {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        var s = document.getElementById('gsearch');
        if (s) { s.focus(); s.select(); }
        return;
      }
      if ((e.ctrlKey || e.metaKey) && e.key === ',') {
        e.preventDefault();
        openSettings();
        return;
      }
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'i') {
        // Ctrl+I：导入 skill（SPEC-skill-import.md，与 Ctrl+K 并列）
        e.preventDefault();
        impOpen('source');
        return;
      }
      if ((e.key === 'f' || e.key === 'F') && !e.ctrlKey && !e.metaKey && !e.altKey) {
        // F：收藏 / 取消当前选中行；输入框里打字不触发（IME / 光标优先）
        var aeF = document.activeElement;
        if (aeF && (aeF.tagName === 'INPUT' || aeF.tagName === 'TEXTAREA')) return;
        if (state.selected && state.board !== 'agents') {
          e.preventDefault();
          toggleStar(state.selected);
        }
        return;
      }
      if (e.key === 'Enter') {
        if (e.target && e.target.id === 'fm-name') {
          var ok = document.getElementById('fm-ok');
          if (ok) { e.preventDefault(); ok.click(); }
        }
        return;
      }
      if (e.key === 'Escape') {
        // 导入弹窗优先：非 running → 关；running → 一切关闭路径禁用，
        // 连下面的通用 modal-root 检查都不能进（后端不可中断，UI 不许撒谎）
        if (imp.step) {
          if (imp.step !== 'running') impClose();
          return;
        }
        // Esc 链：弹窗 → ⋯ 菜单 → 清搜索 → 退出文件夹
        if (document.getElementById('modal-root').firstElementChild) {
          var onCancel = modalOnCancel;
          closeModal(); // 先清回调再触发，避免回调里再开新弹窗被误判成同一层
          if (onCancel) onCancel();
          return;
        }
        if (ctxEl) { closeCtx(); return; }
        if (state.q) {
          document.getElementById('gsearch').value = '';
          setState({ q: '' });
          return;
        }
        if (state.folder) openFolder('all');
        return;
      }
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        // 输入框和备注框里方向键是移动光标 / 选字的，不能抢去切行
        // （renderInspector 会重建整个检查器，丢光标与 IME 组合状态）
        var ae = document.activeElement;
        if (ae && (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA')) return;
        var rows = Array.prototype.slice.call(document.querySelectorAll('.row, .file-card'));
        if (!rows.length) return;
        var idx = rows.findIndex(function (r) { return r.classList.contains('is-selected'); });
        var next = e.key === 'ArrowDown' ? Math.min(rows.length - 1, idx + 1) : Math.max(0, idx - 1);
        e.preventDefault();
        setState({ selected: rows[next].getAttribute('data-key') });
        // 重渲染后旧元素已 detached（直接 scrollIntoView 是 no-op，老实现因此
        // 键盘导航从不跟滚）：按高亮类现查当前元素再滚
        var el = document.querySelector('.row.is-selected, .file-card.is-selected');
        if (el) el.scrollIntoView({ block: 'nearest' });
      }
    });

    // hover tooltip for cover segments
    document.addEventListener('mouseover', function (e) {
      var seg = e.target.closest && e.target.closest('.cover-seg');
      var tip = document.getElementById('tooltip');
      if (!seg) { tip.classList.remove('show'); return; }
      tip.innerHTML = '<b>' + esc(seg.getAttribute('title') || '') + '</b>';
      tip.classList.add('show');
      var box = seg.getBoundingClientRect();
      tip.style.left = Math.min(window.innerWidth - 260, box.left + 4) + 'px';
      tip.style.top = (box.bottom + 8) + 'px';
    });
  }

  /* ---------------------------------------------------------- boot overlay */
  /* 启动编排时钟（SPEC-boot-animation.md · 方案 A）。纯展示层：不改数据流，
     只消费 hydrate 的 payload。编排时钟为主，真实 R（bootstrap 成功且 start()
     首帧渲染完成）只负责提前收尾（剩余时间轴提速 ~1.2s 走完）或延后收尾
     （停在 94% 等待）；8s 硬上限保证 overlay 在任何路径下都必然消失。 */
  var BootFX = (function () {
    var TOTAL = 4940;                      // 1× 满速时间轴总长（ms）
    var OK_T = 4420;                       // 「就绪」相位；R 未到则停在前一相位等待
    var AGENTS_T = 2960;                   // 探测 agent 分发相位（chips/parse 到此为止）
    var CNT_FROM = 860, CNT_TO = 2960;     // 计数器 easeOutCubic 窗口
    var HARD_CAP = 8000;                   // 桥卡死硬上限：到点强制走就绪 + reveal
    var CHIP_MS = 118, CHIP_MAX = 14, PARSE_MS = 150, MARK_MS = 135;
    var REVEAL_GAP = 520;                  // 就绪 → reveal（与 4420→4940 同距）
    // chip 是纯装饰：颜色按 A→Z 序循环取（规格 §3.2 的六个分类色）
    var CHIP_COLORS = ['--violet', '--accent', '--warning', '--rose', '--teal', '--slate'];

    var el = null, winEl = null;
    var phEl, cntEl, lineEl, fillEl, chipsEl, agentsEl;
    var iv = 0, revealTimer = 0;
    var disabled = false, reduce = false, fullSpeed = false;
    var begun = false, rSeen = false, okFired = false, finished = false;
    // 桌面壳里「桥还没接上」的等待态：boot() 通过 awaitBridge() 交进来。
    // 有它的时候，HARD_CAP 到点不再 reveal 一个空壳，而是停在 94% 如实报等待秒数。
    var bridgeWait = null, connSecs = -1;
    var vt = 0, lastWall = 0, scale = 1, beginWall = 0, idx = 0, okVt = 0;
    var tgt = { n: 0, u: 0, m: 0, f: 0, py: '', store: '', size: '' };
    var marks = [], chips = [], parseSeq = [];
    var chipI = 0, nextChipVt = 0, parseI = 0, nextParseVt = 0, markI = 0, nextMarkVt = 0;
    var cntCur = 0;

    // 事件表：hold 相位（就绪）必须等真实 R 放行，慢机时停在 94% 等待
    var PHASES = [
      { t: 0, ph: '正在启动', pct: 6 },
      { t: 460, ph: '打开中央仓库', pct: 14 },
      { t: 860, ph: '扫描技能目录', pct: 30 },
      { t: 2060, ph: '解析 SKILL.md', pct: 60 },
      { t: AGENTS_T, ph: '探测 agent 分发', pct: 78 },
      { t: 3960, ph: '载入分类与元数据', pct: 94 },
      { t: OK_T, ph: '就绪', pct: 100, hold: true }
    ];

    function enabledTools(tools) {
      return (tools || []).filter(function (t) { return t && t.enabled && t.mark; });
    }

    function applyData(d) {
      var meta = (d && d.meta) || {};
      var skills = (d && d.skills) || [];
      var en = enabledTools(d && d.tools);
      tgt = {
        n: meta.sharedCount != null ? meta.sharedCount : skills.length,
        // 两段式刷新的第一段 / 开关切换的回包都没扫独有：这时独有计数是「还不知道」，
        // 不是 0。启动动画还在跑时若按 0 收，动画里的独有数字会被一次刷新刷没；
        // 保留上一轮目标值，第二段回来后自然更正。
        u: meta.uniqueCount != null ? meta.uniqueCount
          : ((meta.uniquePending || d.uniqueDeferred) ? tgt.u : 0),
        m: meta.agentCount != null ? meta.agentCount : en.length,
        f: ((d && d.folders) || []).length,
        py: meta.python || '',
        store: meta.store || STORE,
        size: meta.storeSize || ''
      };
      marks = en.map(function (t) {
        return { key: t.key || '', mark: t.mark, dir: t.dir || '' };
      });
      // A→Z 取用；hash 后端只有 12 位，缺了用序号派生兜底
      parseSeq = skills.map(function (s) {
        return { name: s.label || s.name || '', hash: String(s.hash || '') };
      }).sort(function (a, b) {
        return a.name.toLowerCase() < b.name.toLowerCase() ? -1 : 1;
      });
    }

    function marksKey(list) {
      return list.map(function (m) { return m.mark; }).join(',');
    }

    function renderMarks() {
      if (!agentsEl) return;
      agentsEl.innerHTML = '';
      for (var i = 0; i < marks.length; i++) {
        var s = document.createElement('span');
        s.className = 'boot-agent';
        s.textContent = marks[i].mark;
        agentsEl.appendChild(s);
      }
      markI = 0;
    }

    function setPhase(text) { if (phEl) phEl.textContent = text; }
    function setLine(text) { if (lineEl) lineEl.textContent = text; }
    function setPct(p) { if (fillEl) fillEl.style.width = p + '%'; }
    function setCnt(n) { if (cntEl) cntEl.textContent = n + ' / ' + tgt.n; }

    function kernelLine() {
      return 'kernel ready' + (tgt.py ? ' · python ' + tgt.py : '');
    }

    function firePhase(p) {
      setPhase(p.ph);
      setPct(p.pct);
      if (p.t === 0) setLine(kernelLine());
      else if (p.t === 460) setLine(tgt.store + (tgt.size ? ' · ' + tgt.size : ''));
      else if (p.t === 860) {
        setLine('scan store … ' + tgt.n + ' skills · ' + tgt.u + ' unique');
        nextChipVt = p.t;
      } else if (p.t === 2060) {
        nextParseVt = p.t;
      } else if (p.t === AGENTS_T) {
        if (chipsEl) chipsEl.innerHTML = '';
        nextMarkVt = p.t;
      } else if (p.t === 3960) {
        // 字段口径按规格 §4 数据表：F=folders.length，M=meta.agentCount
        setLine('folders ×' + tgt.f + ' · meta ×' + tgt.m + ' · prefs ok');
      } else if (p.t === OK_T) {
        okFired = true;
        okVt = vt;
        cntCur = tgt.n; // ok 后计数器锁定在精确终值，不再被追赶平滑改写
        if (el) el.classList.add('ok');
        setCnt(tgt.n);
        setLine(tgt.n + ' 个技能就绪 · ' + tgt.m + ' 个 agent 已连接');
      }
    }

    function addChip() {
      var c = chips[chipI];
      if (!c || !c.name || !chipsEl) return;
      var color = CHIP_COLORS[chipI % CHIP_COLORS.length];
      chipI++;
      var s = document.createElement('span');
      s.className = 'boot-chip';
      s.innerHTML = '<b style="--c:var(' + color + ')">'
        + esc(c.name.slice(0, 2).toUpperCase()) + '</b>' + esc(c.name);
      chipsEl.appendChild(s);
      // 只保留 14 个在显 chip：标记最老的「未退场」chip（firstElementChild 可能
      // 已经是 .out 退场中的，续期无效果，可见数会悄悄爬过上限）
      var visible = chipsEl.querySelectorAll('.boot-chip:not(.out)');
      for (var v = 0; visible.length - v > CHIP_MAX; v++) {
        (function (node) {
          node.classList.add('out');
          setTimeout(function () { if (node.parentNode) node.parentNode.removeChild(node); }, 240);
        })(visible[v]);
      }
    }

    // 计数器目标：860→2960 easeOutCubic；真实数据到手改目标后由追赶平滑过渡
    function cntTarget() {
      if (vt <= CNT_FROM) return 0;
      var p = Math.min(1, (vt - CNT_FROM) / (CNT_TO - CNT_FROM));
      return tgt.n * (1 - Math.pow(1 - p, 3));
    }

    function tick() {
      if (finished || !el || !el.isConnected) { stop(); return; } // data.js 缺失时 overlay 已随 body 重建被移走
      var w = Date.now();
      vt += (w - lastWall) * scale; // 隐藏窗口定时器被节流也不丢时长：按墙钟差推进
      lastWall = w;
      if (!rSeen && w - beginWall > HARD_CAP) {
        if (bridgeWait) {
          // 桥还没落地就 reveal，用户看到的是「界面正常但全空」——比继续等更误导。
          // 停在 94% 报真实等待秒数；但留一个绝对上限，绝不把动画永久卡死。
          if (w - bridgeWait.t0 > bridgeWait.cap) {
            bridgeWait = null;          // 兜底：桥这条 promise 本身也不 settle 时照常收尾
          } else {
            if (connSecs < 0) { setPhase('等待桌面端'); setPct(94); }
            var secs = Math.floor((w - bridgeWait.t0) / 1000);
            if (secs !== connSecs) {
              connSecs = secs;
              setLine('正在连接桌面端… 已等待 ' + (secs + 1) + ' 秒');
            }
            return;                     // 相位/装饰/计数器都冻在当前值，只更新等待文案
          }
        }
        rSeen = true;
        vt = Math.max(vt, OK_T);    // 强制走就绪：下一 tick 放行 hold 相位
      }
      var p;
      while (idx < PHASES.length && (p = PHASES[idx]).t <= vt && (rSeen || !p.hold)) {
        firePhase(p);
        idx++;
      }
      if (chipsEl && vt >= CNT_FROM && vt < AGENTS_T) {
        while (chipI < chips.length && vt >= nextChipVt) { addChip(); nextChipVt += CHIP_MS; }
      }
      if (vt > CNT_FROM && cntEl && !okFired) {
        var want = cntTarget();
        cntCur += (want - cntCur) * 0.35;
        if (Math.abs(want - cntCur) < 0.6) cntCur = want;
        setCnt(Math.round(cntCur));
      }
      if (lineEl && vt >= 2060 && vt < AGENTS_T && parseSeq.length) {
        var row;
        while (vt >= nextParseVt) {
          row = parseSeq[parseI % parseSeq.length];
          parseI++;
          setLine('parse ' + row.name + '/SKILL.md → '
            + (row.hash || ('000000000000' + ((parseI * 2654435761) >>> 0).toString(16)).slice(-12)));
          nextParseVt += PARSE_MS;
        }
      }
      if (agentsEl && vt >= AGENTS_T) {
        while (markI < marks.length && vt >= nextMarkVt) {
          if (agentsEl.children[markI]) agentsEl.children[markI].classList.add('on');
          // vt 跳进（定时器合并/提速）可能已放行就绪相位：日志行让位给就绪文案
          if (!okFired) setLine('link ' + marks[markI].key + ' → ' + marks[markI].dir + ' … ok');
          markI++;
          nextMarkVt += MARK_MS;
        }
      }
      if (okFired && vt >= okVt + REVEAL_GAP) reveal();
    }

    function stop() {
      if (iv) { clearInterval(iv); iv = 0; }
    }

    function reveal() {
      if (finished) return;
      finished = true;
      stop();
      if (revealTimer) { clearTimeout(revealTimer); revealTimer = 0; }
      if (winEl) {
        winEl.classList.remove('booting');
        winEl.classList.add('ready');   // 触发 [data-boot-rv] 级联进场
      }
      if (el) {
        el.classList.add('done');
        setTimeout(function () { if (el) el.hidden = true; }, 320);
      }
    }

    function reduceMotion() {
      try {
        if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return true;
      } catch (e) {}
      return prefs.motion === false;
    }

    function begin() {
      if (begun) return;
      begun = true;
      el = document.getElementById('boot');
      winEl = document.getElementById('app-window');
      if (!el || !winEl) { finished = true; return; }
      if (/[?&]noboot=1/.test(location.search)) { disable(); return; }
      reduce = reduceMotion();
      if (reduce) return;   // 即时模式：静态层留到 R + 200ms，由 ready/fail 收尾
      // file:// 原型（无 desktop=1）：满速 1× 编排（可展示），不加速。
      // 判据不能用 !SKB —— bridge.js 在原型下也导出 SKILLLOCKER_BRIDGE（ready 为 null）
      fullSpeed = !/[?&]desktop=1/.test(location.search);
      phEl = el.querySelector('.boot-ph');
      cntEl = el.querySelector('.boot-cnt');
      lineEl = el.querySelector('.boot-line');
      fillEl = el.querySelector('.boot-track i');
      chipsEl = el.querySelector('.boot-chips');
      agentsEl = el.querySelector('.boot-agents');
      applyData(window.SKILLLOCKER_DATA || {});
      chips = parseSeq;     // chip 流用快照预跑，真实数据到手后不回换（规格 §10.1）
      renderMarks();
      setCnt(0);
      setLine(kernelLine());
      beginWall = lastWall = Date.now();
      el.classList.add('run');
      iv = setInterval(tick, 40);
    }

    /* 桥接等待态：boot() 把 SKB.ready 这条 promise 交进来。
       - 接上了：撤掉等待态，并把硬上限的计时起点推到这一刻 —— 桥接花掉的 20 秒
         不该再算进 bootstrap 的 8s 预算，否则刚连上就被硬上限抢先 reveal。
       - 判死（null）：撤掉等待态，接下来由 boot() 负责整页重载或出错误页。
       capMs 是「连这条 promise 本身都不 settle」的绝对兜底，默认 26s：
       覆盖首轮 20s 窗口 + 余量（重试轮 8s 更短，同一个值够用）。 */
    function awaitBridge(promise, capMs) {
      if (!promise || typeof promise.then !== 'function' || finished || disabled) return;
      bridgeWait = { t0: Date.now(), cap: capMs || 26000 };
      connSecs = -1;
      promise.then(function (api) {
        if (!bridgeWait) return;
        bridgeWait = null;
        connSecs = -1;
        if (api) { beginWall = lastWall = Date.now(); }
      }, function () { bridgeWait = null; });
    }

    function data(d) {
      if (disabled || finished || !d) return;
      var before = marksKey(marks);
      applyData(d);
      if (!reduce && marksKey(marks) !== before) renderMarks();
    }

    function ready() {
      if (disabled || finished || rSeen) return;
      rSeen = true;
      bridgeWait = null;   // R 都到了，桥接等待态没有意义（等待文案也不该再挂）
      connSecs = -1;
      if (reduce) { revealTimer = setTimeout(reveal, 200); return; }
      if (fullSpeed) return;  // 原型不加速：R 必然早到，让 1× 时间轴走满
      // 常态（规格 §5）：剩余时间轴提速，总时长 ≈ max(R + 1.2s, 1.8s)；
      // 下限分母是为 1.8s 地板服务的（R < 600ms 时把收尾托到 1.8s）
      var remaining = TOTAL - vt;
      var wallLeft = Math.max(1200, 1800 - (Date.now() - beginWall));
      scale = Math.min(4, Math.max(1, remaining / wallLeft));
    }

    function disable() {
      disabled = true;
      finished = true;
      bridgeWait = null;
      stop();
      if (revealTimer) { clearTimeout(revealTimer); revealTimer = 0; }
      if (el) el.hidden = true;
      // 不加 ready：主界面直接可见、零过渡，与 v0.2.0 行为完全一致
      if (winEl) winEl.classList.remove('booting');
    }

    return {
      begin: begin,
      awaitBridge: awaitBridge,
      data: data,
      ready: ready,
      fail: ready,  // 降级路径等价就绪：overlay 同样立即收尾，绝不陪等
      disable: disable
    };
  })();

  /* ---------------------------------------------------------- init */
  function loadLocalMeta() {
    var raw = lsGet(META_KEY, null);
    meta = (raw && typeof raw === 'object') ? raw : {};
  }

  function start() {
    // 允许 index.html?board=unique|agents 直达某个看板（截图 / 深链接用）
    var qp = /[?&]board=(unique|shared|agents)/.exec(location.search);
    if (qp) {
      state.board = qp[1];
      prefs.board = state.board;
      savePrefs();
    }
    seedIfNeeded();
    var rows = currentRows();
    if (!state.selected && rows.length) {
      state.selected = rows[0].key;
    }
    bind();
    renderAll(true);
    document.documentElement.setAttribute('data-mode', MODE);
    window.__SK_MODE = MODE;
    window.__SK_READY = true;
    // 启动留痕：skilllocker.log 里没有 boot 行 = 跑的还是旧进程/旧代码，先别怀疑拖动逻辑
    uiLog('boot', { mode: MODE, vw: window.innerWidth, vh: window.innerHeight });
  }

  /* 快照序号防线。后端每份整套看板都在 OPS_LOCK 内、算完之后发号
     （见 kit/desktop.py 的 _boards_payload），所以号大 = 描述的世界新。
     「谁后到谁生效」是错的：连着打开多个智能体时，先出发那笔的回包完全可能最后
     才落地，而它只认识自己那一刻已经 enabled 的那一个 —— 于是已经连上的其余几个
     被一份旧快照整体刷回「已关闭」（用户报的「只剩一个连上，剩下的还得重新打开」）。
     旧快照只丢数据、不丢这次操作的结果：action 照常用、在途标记照收，界面照常重画。
     原型 / 内置快照没有 seq，不参与这套判断。 */
  var LAST_SNAPSHOT_SEQ = 0;

  function isStaleSnapshot(res) {
    var seq = (res && typeof res.seq === 'number') ? res.seq : 0;
    if (!seq) return false;
    if (seq < LAST_SNAPSHOT_SEQ) return true;
    LAST_SNAPSHOT_SEQ = seq;
    return false;
  }

  function applyBootstrap(res) {
    if (MODE === 'error') return; // 启动错误页：任何刷新入口都不再往空界面上渲染
    if (isStaleSnapshot(res)) {   // 晚到的旧快照：数据保持最新那份，只重画一次
      renderAll(false);
      return;
    }
    hydrate(res.data);
    setMetaMap(res.meta);
    // config.json 损坏退回默认配置：与 scanDegraded 同一套显性化口径，
    // 别让用户在毫不知情下看着「另一个仓库」的数据
    if (META.loadError) toast('warn', '配置未生效', META.loadError);
    if (META.seedNote && SKILLS.length) toast('ok', '中央仓库已就绪', META.seedNote);
    // 当前所在的文件夹若已被删除回根（共有=folder id，独有=agent key）；
    // 选中行由 renderList 兜底回落
    if (state.folder && state.folder !== 'none') {
      if (state.board === 'unique' ? !agentByKey(state.folder) : !folderById(state.folder)) {
        state.folder = null;
      }
    }
    var rows = currentRows();
    var keep = rows.filter(function (r) { return r.key === state.selected; })[0];
    if (!keep) state.selected = rows.length ? rows[0].key : null;
    renderAll(true);
    window.__SK_MODE = MODE;
  }

  function bootLocal(data) {
    if (!data) {
      document.body.innerHTML = '<p style="padding:40px;font-family:sans-serif">data.js 未加载，界面无法渲染。</p>';
      return;
    }
    MODE = 'local';
    hydrate(data);
    loadLocalMeta();
    start();
    BootFX.ready(); // start() 返回即 R；file:// 原型由此放行 1× 满速时间轴
  }

  /* 启动错误页（桌面壳已连上、后端 bootstrap 失败）：全屏覆盖，明确失败原因，
     绝不退回内置演示快照冒充真实数据。重试 = 重跑 boot 链路。 */
  function renderBootError(err) {
    var detail = String(err && err.message || err || '未知错误');
    document.documentElement.setAttribute('data-mode', 'error');
    window.__SK_MODE = 'error';
    var box = document.createElement('div');
    box.id = 'boot-error';
    box.innerHTML =
      '<div class="boot-error-card">' +
        '<div class="boot-error-icon">!</div>' +
        '<h1>后端启动失败</h1>' +
        '<p class="boot-error-sub">skilllocker 后端在初始化时出错，界面无法连接真实数据库。</p>' +
        '<pre class="boot-error-detail">' + esc(detail) + '</pre>' +
        '<p class="boot-error-hint">详细原因见 <b>%LOCALAPPDATA%\\SkillLocker\\skilllocker.log</b>；' +
        '排除后点「重试」重新加载。</p>' +
        '<button id="boot-error-retry" type="button">重试</button>' +
      '</div>';
    document.body.appendChild(box);
    box.querySelector('#boot-error-retry').addEventListener('click', function () {
      location.reload();
    });
  }

  function boot() {
    BootFX.begin(); // 编排时钟起点＝脚本执行（首帧前）；noboot / reduce-motion 在这里分叉
    if (!SKB) { bootLocal(window.SKILLLOCKER_DATA); return; }
    // 桥这条 promise 交给启动编排：桌面壳里它可能要 20 秒（重试轮 8 秒）才判死，
    // 期间动画停在 94% 报等待秒数，而不是按硬上限 reveal 出一个空壳。
    BootFX.awaitBridge(SKB.ready);
    SKB.ready.then(function (api) {
      if (!api) {
        BootFX.fail();
        if (SKB.isDesktopShell && SKB.isDesktopShell()) {
          // 桌面壳里注入超时与 bootstrap 失败同等对待：错误页 + 重试（reload），
          // 绝不退回演示快照 —— 那会让用户以为在改真库，实际只是内存里的假数据。
          // 首次安装时文件刚落地，WebView2 冷启动 + 杀软扫描可能拖过注入窗口：
          // 自动重载一次（桥热了通常就接上了），仍失败才显示错误页。
          if (!/[?&]bridgeRetry=1/.test(location.search)) {
            location.href = location.href +
              (location.href.indexOf('?') > -1 ? '&' : '?') + 'bridgeRetry=1';
            return;
          }
          MODE = 'error';
          // 文案要如实交代「已经自动重试过、两轮各等多久」：用户在这页之前已经
          // 对着启动动画等了 20+8 秒，不说清楚只会以为程序卡死。
          renderBootError(new Error("pywebview 注入超时：桌面壳已启动但桥未接上。"
            + "已自动重试 1 次（首轮等 20 秒、重试轮等 8 秒）仍未接上，"
            + "通常是 WebView2 冷启动过慢或后端进程未能启动，点「重试」重新加载"));
          return;
        }
        bootLocal(window.SKILLLOCKER_DATA);
        return;
      }
      MODE = 'db';
      SKB.bootstrap().then(function (res) {
        document.body.classList.add('desktop');
        hydrate(res.data);
        setMetaMap(res.meta);
        start();
        // 首启自动建库留痕：toast + 状态栏（META.seedNote 只在真正跑过的那次有值）。
        // 0 收成时不弹 toast（空库引导面板里已有说明），避免“检查了但没建”的噪音。
        if (META.seedNote && SKILLS.length) toast('ok', '中央仓库已就绪', META.seedNote);
        BootFX.ready(); // R = bootstrap 成功且 start() 首帧渲染完成
      }).catch(function (err) {
        BootFX.fail();
        if (SKB.isDesktop()) {  // 桌面壳已连上但后端报错：明确失败，绝不给演示数据
          MODE = 'error';
          renderBootError(err);
          return;
        }
        // 非桌面原型（file:// 直开）：保留只读快照降级 + toast
        MODE = 'local';
        toast('bad', '后端连接失败', String(err && err.message || err) + '，已退回内置快照');
        bootLocal(window.SKILLLOCKER_DATA);
      });
    });
  }

  window.__SK_APP = {
    state: state,
    rows: currentRows,
    agents: agentRows,
    meta: function () { return meta; },
    refresh: applyBootstrap
  };

  boot();
})();
