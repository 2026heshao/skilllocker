/* ============================================================
   skillkit · Skill Library — logic
   桌面端：bridge.js → pywebview → skillkit.py，数据与编辑直接走 SQLite。
   原型：file:// 打开时退回 data.js 快照 + localStorage。
   ============================================================ */
(function () {
  'use strict';

  var SKB = window.SKILLKIT_BRIDGE || null;
  var D = null;
  var MODE = 'local';
  var META = {};
  var SKILLS = [];
  var TOOLS = [];
  var ENABLED = [];
  var UNIQUE = [];
  var AGENTS = [];
  var CONFLICT = null;

  var STORE = 'D:\\skillkit\\store';
  var SCAN = '22:16';

  function hydrate(data) {
    D = data || {};
    META = D.meta || {};
    SKILLS = D.skills || [];
    TOOLS = D.tools || [];
    ENABLED = TOOLS.filter(function (t) { return t.enabled; });
    UNIQUE = D.unique || [];
    AGENTS = D.agents || [];
    CONFLICT = (D.conflict && D.conflict.skill) ? D.conflict : null;
    STORE = META.store || STORE;
    SCAN = META.scanAt || SCAN;
  }

  // agent key -> 显示名；共享目录提示 / 详情栏共读行都用它
  function toolLabelOf(key) {
    for (var i = 0; i < TOOLS.length; i++) if (TOOLS[i].key === key) return TOOLS[i].label || key;
    return key;
  }

  /* ---------------------------------------------------------- storage */
  var META_KEY = 'skillkit.library.meta.v1';
  var PREF_KEY = 'skillkit.library.prefs.v1';

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

  /* ---------------------------------------------------------- categories */
  var CATS = [
    { id: 'design', label: '设计', color: 'violet' },
    { id: 'frontend', label: '前端', color: 'accent' },
    { id: 'doc', label: '文档', color: 'warning' },
    { id: 'video', label: '视频', color: 'rose' },
    { id: 'memory', label: '记忆', color: 'teal' },
    { id: 'eng', label: '工程', color: 'slate' },
    { id: 'search', label: '检索', color: 'green' }
  ];

  var COLORS = {
    violet: ['var(--violet)', 'var(--violet-soft)'],
    accent: ['var(--accent)', 'var(--accent-soft)'],
    warning: ['var(--warning)', 'var(--warning-soft)'],
    rose: ['var(--rose)', 'var(--rose-soft)'],
    teal: ['var(--teal)', 'var(--teal-soft)'],
    slate: ['var(--slate)', 'var(--slate-soft)'],
    green: ['var(--success)', 'var(--success-soft)']
  };

  var CUSTOM_PALETTE = ['violet', 'teal', 'accent', 'rose', 'warning', 'slate', 'green'];

  var prefs = lsGet(PREF_KEY, null) || {
    theme: 'light',
    density: 'comfortable',
    motion: true,
    showDisabled: true,
    sort: 'name',
    board: 'shared',
    customCats: [],
    seeded: false
  };
  if (!prefs.customCats) prefs.customCats = [];

  var meta = {};
  var dirtyKeys = {};

  function savePrefs() { lsSet(PREF_KEY, prefs); }

  // 桌面端：编辑落到 skillkit.db 的 skill_meta 表；原型（file://）：写 localStorage。
  // 写库不做去抖：单机 SQLite 写入很轻，且避免隐藏窗口被节流时丢编辑。
  function scopeOf(key) { return key.indexOf('/') > -1 ? 'unique' : 'shared'; }

  function setMetaMap(map) { meta = map || {}; }

  function saveMeta(key) {
    if (MODE === 'db') {
      if (key) dirtyKeys[key] = 1;
      flushMeta();
      return true;
    }
    return lsSet(META_KEY, meta);
  }

  function flushMeta() {
    if (MODE !== 'db' || !SKB) return;
    var keys = Object.keys(dirtyKeys);
    if (!keys.length) return;
    dirtyKeys = {};
    keys.forEach(function (key) {
      var m = meta[key] || {};
      SKB.saveMeta(scopeOf(key), key, {
        tool: scopeOf(key) === 'unique' ? key.split('/')[0] : '',
        display: m.display || '',
        note: m.note || '',
        cats: m.cats || []
      }).catch(function (err) {
        dirtyKeys[key] = 1;
        toast('bad', '保存失败', String(err && err.message || err));
      });
    });
  }

  var state = {
    board: /^(shared|unique|agents)$/.test(prefs.board || '') ? prefs.board : 'shared',
    cat: 'all',
    agentStatus: 'all',
    q: '',
    selected: null
  };

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

  function catById(id) {
    for (var i = 0; i < CATS.length; i++) if (CATS[i].id === id) return CATS[i];
    return null;
  }

  function allCats() {
    var out = CATS.slice();
    prefs.customCats.forEach(function (c, i) {
      out.push({ id: c.id, label: c.label, color: CUSTOM_PALETTE[i % CUSTOM_PALETTE.length], custom: true });
    });
    return out;
  }

  function catOf(id) {
    var list = allCats();
    for (var i = 0; i < list.length; i++) if (list[i].id === id) return list[i];
    return null;
  }

  function catVars(color) {
    var pair = COLORS[color] || COLORS.slate;
    return '--c:' + pair[0] + ';--cs:' + pair[1];
  }

  function catChip(id, extraCls) {
    var c = catOf(id);
    if (!c) return '';
    return '<span class="cat ' + (extraCls || '') + '" style="' + catVars(c.color) + '"><i></i>' + esc(c.label) + '</span>';
  }

  function metaOf(key) {
    if (!meta[key]) meta[key] = { display: '', note: '', cats: [], updated: 0 };
    if (!meta[key].cats) meta[key].cats = [];
    return meta[key];
  }

  function displayOf(key, fallback) {
    var m = meta[key];
    return (m && m.display) ? m.display : fallback;
  }

  // 只有「显示名 / 备注 / 分类偏离默认值」才算用户编辑过；预置分类不算。
  function editedCount() {
    return Object.keys(meta).filter(function (k) {
      var m = meta[k];
      if (!m) return false;
      if (m.display || m.note) return true;
      var cats = m.cats || [];
      if (m.seed) return !(cats.length === 1 && cats[0] === m.seed);
      return false;
    }).length;
  }

  function fmtBytes(n) {
    if (!n) return '0 B';
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(n < 10240 ? 1 : 0) + ' KB';
    return (n / 1048576).toFixed(1) + ' MB';
  }

  function seedCat(name) {
    var n = String(name).toLowerCase();
    if (/remotion|video/.test(n)) return 'video';
    if (/mnemosyne|memory/.test(n)) return 'memory';
    if (/aihot|ai hot/.test(n)) return 'search';
    if (/pdf|docx|xlsx|pptx/.test(n)) return 'doc';
    if (/^web-|webapp/.test(n)) return 'frontend';
    if (/design|taste|stitch|minimalist|brutalist|brandkit|imagegen|image-to-code|redesign|ui-new|ui-design|soft-skill/.test(n)) return 'design';
    return 'eng';
  }

  function seedIfNeeded() {
    if (MODE === 'db') return; // 桌面端由 skillkit.seed_meta() 在首启播种
    if (prefs.seeded) return;
    SKILLS.forEach(function (s) {
      var seed = seedCat(s.name);
      if (!meta[s.name]) meta[s.name] = { display: '', note: '', cats: [seed], seed: seed, updated: 0 };
      else {
        if (!meta[s.name].cats || !meta[s.name].cats.length) meta[s.name].cats = [seed];
        if (!meta[s.name].seed) meta[s.name].seed = meta[s.name].cats.length === 1 ? meta[s.name].cats[0] : '';
      }
    });
    UNIQUE.forEach(function (u) {
      var key = u.tool + '/' + u.name;
      var seed = u.sameName ? 'doc' : 'eng';
      if (!meta[key]) meta[key] = { display: '', note: '', cats: [seed], seed: seed, updated: 0 };
      else if (!meta[key].seed) meta[key].seed = meta[key].cats && meta[key].cats.length === 1 ? meta[key].cats[0] : '';
    });
    prefs.seeded = true;
    savePrefs();
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
        cats: (m.cats || []).slice(),
        note: m.note || '',
        updated: m.updated || 0,
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
        cats: (m.cats || []).slice(),
        note: m.note || '',
        updated: m.updated || 0,
        entry: u,
        conflict: u.sameName
      };
    });
  }

  function currentRows() {
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
      var def = AGENT_STATUS[st];
      return {
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
        statusText: def.label,
        rank: def.rank,
        dot: def.dot,
        linked: a.linked || 0,
        conflicts: a.conflicts || 0,
        skillsTotal: a.skillsTotal == null ? SKILLS.length : a.skillsTotal,
        // agent 自己安装/读取的外部目录（如 ~/.agents/skills），本工具只展示不管理
        external: a.external || []
      };
    });
  }

  function agentCounts(rows) {
    var c = { all: rows.length, connected: 0, off: 0, missing: 0 };
    rows.forEach(function (r) { c[r.status] = (c[r.status] || 0) + 1; });
    return c;
  }

  function rowSecondary(r) {
    if (r.board === 'unique') {
      return r.entry.toolLabel + ' · ' + r.entry.files + ' files';
    }
    var m = meta[r.key] || {};
    if (m.display && m.display !== r.name) return r.name;
    if (r.label && r.label !== r.name) return r.label;
    return '';
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
    var catText = r.cats.map(function (id) {
      var c = catOf(id);
      return c ? c.label : '';
    }).join(' ');
    if (r.board === 'unique') {
      return [r.label, r.entry ? r.entry.toolLabel : '', r.entry ? r.entry.mark : '', r.note, catText].join(' ');
    }
    return [r.label, r.note, catText, r.skill ? r.skill.description : ''].join(' ');
  }

  function passesFilters(r) {
    if (r.board === 'agents') {
      return state.agentStatus === 'all' || r.status === state.agentStatus;
    }
    if (state.cat === 'none') return !r.cats.length;
    if (state.cat !== 'all') return r.cats.indexOf(state.cat) > -1;
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
    } else {
      out.sort(function (a, b) {
        return String(a.display).localeCompare(String(b.display), 'zh-Hans-CN');
      });
    }
    return out;
  }

  /* ---------------------------------------------------------- render: chrome */
  function renderChrome() {
    document.body.dataset.view = state.board;

    document.getElementById('store-path').textContent = STORE;
    document.getElementById('store-path').title = STORE;
    document.getElementById('store-count').textContent = SKILLS.length;
    document.getElementById('store-size').textContent = META.storeSize || '54.4 MB';
    document.getElementById('store-health').style.width =
      (100 - (UNIQUE.length / (SKILLS.length + UNIQUE.length)) * 100).toFixed(1) + '%';

    var sharedCount = SKILLS.length;
    var uniqueCount = UNIQUE.length;
    var agentStats = agentCounts(agentRows());

    document.getElementById('nav-shared').textContent = sharedCount;
    document.getElementById('nav-unique').textContent = uniqueCount;
    document.getElementById('nav-agents').textContent = agentStats.all;
    document.getElementById('stat-agents').textContent = agentStats.connected;
    document.getElementById('stat-edited').textContent = editedCount();
    document.getElementById('stat-cats').textContent = allCats().length;
    document.getElementById('stat-unique').textContent = uniqueCount;
    document.getElementById('sb-edited').textContent = editedCount();
    document.getElementById('sb-health').textContent = sharedCount + ' 共有 · ' + uniqueCount + ' 独有';
    document.getElementById('sb-path').textContent = STORE;
    document.getElementById('sb-path').title = STORE;
    document.getElementById('ver-scan').textContent = SCAN;
    document.getElementById('sb-scan').textContent = SCAN;

    document.getElementById('tab-shared-num').textContent = sharedCount;
    document.getElementById('tab-unique-num').textContent = uniqueCount;
    document.getElementById('tab-agents-num').textContent = agentStats.all;
    document.getElementById('tab-shared-em').textContent = ENABLED.length + ' / ' + ENABLED.length + ' agents';
    document.getElementById('tab-agents-em').textContent = agentStats.connected + ' 已连接';
    var overridden = UNIQUE.filter(function (u) { return u.sameName; }).length;
    document.getElementById('tab-unique-em').textContent =
      (uniqueCount - overridden) + (overridden ? ' + ' + overridden + ' 同名覆盖' : '');

    var colName = document.getElementById('col-name');
    var colMid = document.getElementById('col-mid');
    var colRight = document.getElementById('col-right');
    var colCover = document.getElementById('col-cover');
    var emptyTitle = document.getElementById('empty-title');
    var emptySub = document.getElementById('empty-sub');

    if (state.board === 'shared') {
      document.getElementById('page-title').textContent = '共有技能';
      document.getElementById('page-sub').textContent =
        sharedCount + ' 个 skill 同时存在于 ' + ENABLED.length + ' 个 agent · 全部指向中央仓库';
      colName.textContent = 'SKILL';
      colMid.textContent = '分类';
      colRight.textContent = '备注';
      colCover.textContent = '分发覆盖';
      emptyTitle.textContent = '没有匹配的 skill';
      emptySub.textContent = '换个关键词，或把筛选切回“全部”';
      document.getElementById('list-foot-hint').textContent = '点任意一行，在右侧改显示名、分类与备注';
      document.getElementById('list-foot-count').textContent = sharedCount + ' 条';
    } else if (state.board === 'unique') {
      document.getElementById('page-title').textContent = '独有技能';
      document.getElementById('page-sub').textContent =
        '只存在于某一个 agent · ' + (uniqueCount - overridden) + ' 个中央仓库没有，' + overridden + ' 个同名但内容不同';
      colName.textContent = 'SKILL';
      colMid.textContent = '分类';
      colRight.textContent = '备注';
      colCover.textContent = '来源';
      emptyTitle.textContent = '没有匹配的 skill';
      emptySub.textContent = '换个关键词，或把筛选切回“全部”';
      document.getElementById('list-foot-hint').textContent = '这些 skill 不会被中央仓库覆盖，改名与备注只影响展示';
      document.getElementById('list-foot-count').textContent = uniqueCount + ' 条';
    } else {
      document.getElementById('page-title').textContent = '智能体';
      document.getElementById('page-sub').textContent =
        '本机检测到 ' + (agentStats.connected + agentStats.off) + ' 个 agent · ' +
        agentStats.connected + ' 已连接、' + agentStats.off + ' 已关闭 · 另有 ' +
        agentStats.missing + ' 个未安装';
      colName.textContent = 'AGENT';
      colMid.textContent = '状态';
      colRight.textContent = '目录';
      colCover.textContent = '分发开关';
      emptyTitle.textContent = '没有匹配的 agent';
      emptySub.textContent = '换个关键词，或把状态筛选切回“全部”';
      document.getElementById('list-foot-hint').textContent = '开关只建/删 junction 链接，中央仓库的 skill 实体不受影响';
      document.getElementById('list-foot-count').textContent = agentStats.all + ' 个 agent';
    }

    document.getElementById('side-cat-label').textContent =
      state.board === 'agents' ? '状态' : '分类';
    document.getElementById('side-cats').setAttribute(
      'aria-label', state.board === 'agents' ? '按状态筛选' : '分类筛选');
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

    document.getElementById('sort-label').textContent = prefs.sort === 'edited' ? '最近编辑' : '名称 A→Z';
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

  function renderFilters(rows) {
    var total = currentRows().length;

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
      document.getElementById('side-cats').innerHTML = sideHtml;
      updateFoot(rows.length, total);
      return;
    }

    var counts = { all: total };
    var catCounts = {};
    rows.forEach(function (r) {
      r.cats.forEach(function (id) { catCounts[id] = (catCounts[id] || 0) + 1; });
    });

    // sidebar categories
    var side = '<button class="nav-item cat-nav ' + (state.cat === 'all' ? 'active' : '') + '" data-cat="all">' +
      '<span class="catdot" style="--c:var(--ink-3)"></span>全部<b class="nav-badge">' + counts.all + '</b></button>';
    allCats().forEach(function (c) {
      var n = catCounts[c.id] || 0;
      if (!n && state.cat !== c.id) return;
      side += '<button class="nav-item cat-nav ' + (state.cat === c.id ? 'active' : '') + '" data-cat="' + c.id + '">' +
        '<span class="catdot" style="--c:' + COLORS[c.color][0] + '"></span>' + esc(c.label) +
        '<b class="nav-badge">' + n + '</b></button>';
    });
    document.getElementById('side-cats').innerHTML = side;

    updateFoot(rows.length, total);
  }

  /* ---------------------------------------------------------- render: list */
  function renderList(animate) {
    var all = currentRows();
    var rows = sortRows(filterRows(all));

    // 过滤后选中项失效时，自动选中第一条结果；无结果则清空选中。
    var selChanged = false;
    if (!rows.length) {
      if (state.selected) { state.selected = null; selChanged = true; }
    } else if (!rows.some(function (r) { return r.key === state.selected; })) {
      state.selected = rows[0].key;
      selChanged = true;
    }
    if (selChanged) renderInspector();

    renderFilters(all);

    var body = document.getElementById('list-body');
    var empty = document.getElementById('list-empty');

    if (!rows.length) {
      body.innerHTML = '';
      body.style.display = 'none';
      empty.hidden = false;
      return;
    }

    body.style.display = '';
    empty.hidden = true;

    if (state.board === 'agents') {
      body.innerHTML = renderAgentRows(rows, animate);
      updateFoot(rows.length, all.length);
      return;
    }

    var html;
    if (state.board === 'unique') {
      html = uniqueGroupedHtml(rows, animate);
    } else {
      html = rows.map(function (r, i) { return skillRowHtml(r, i, animate); }).join('');
    }
    // 脚部计数与智能体看板对齐：有筛选 / 搜索时显示「筛出 x / y」
    updateFoot(rows.length, all.length);

    body.innerHTML = html;
  }

  // 单条 skill 行（共有 / 独有看板共用）
  function skillRowHtml(r, i, animate) {
    var secondary = rowSecondary(r);
    var isSel = state.selected === r.key;
    var conflictFlag;
    if (r.board === 'shared') {
      conflictFlag = r.conflict ? '<span class="mini-flag conflict">' + icon('alert') + '冲突</span>' : '';
    } else if (r.entry.sameName) {
      conflictFlag = '<span class="mini-flag conflict">同名覆盖</span>';
    } else if (r.entry.kind === 'foreign-link') {
      conflictFlag = '<span class="mini-flag conflict">' + icon('alert') + '外部链接</span>';
    } else {
      conflictFlag = '<span class="mini-flag unique">独占</span>';
    }
    if (r.board === 'unique' && r.entry.src && r.entry.src !== 'main') {
      conflictFlag += '<span class="mini-flag ext" title="来自外部目录 ' + esc(r.entry.src) +
        '，本工具只展示不删除">外部</span>';
    }

    var cats = r.cats.length
      ? r.cats.slice(0, 2).map(function (id) { return catChip(id); }).join('') +
        (r.cats.length > 2 ? '<span class="cat more">+' + (r.cats.length - 2) + '</span>' : '')
      : '<span class="cat none">未分类</span>';

    var note = r.note
      ? '<span class="row-note">' + esc(r.note) + '</span>'
      : '<span class="row-note is-empty">添加备注…</span>';

    var cover;
    if (r.board === 'shared') {
      var segs = ENABLED.map(function (t) {
        var bad = (r.skill.realIn || []).indexOf(t.key) > -1;
        return '<span class="cover-seg ' + (bad ? 'bad' : '') + '" title="' + esc(t.label) + ' · ' + (bad ? '同名冲突' : 'junction') + '"></span>';
      }).join('');
      var okCount = ENABLED.length - (r.conflict ? 1 : 0);
      cover = '<div class="row-cover"><div class="cover-bar">' + segs + '</div>' +
        '<span class="cover-text ' + (r.conflict ? 'bad' : '') + '">' + okCount + '/' + ENABLED.length + '</span></div>';
    } else {
      var sharedNames = (r.entry.sharedWith || []).map(toolLabelOf);
      var sharedTip = sharedNames.length
        ? ' title="' + esc(sharedNames.join('、') + ' 也读取该目录') + '"' : '';
      cover = '<div class="row-agent"><span class="agent-chip"' + sharedTip + '>' + esc(r.entry.mark) + ' ' + esc(r.entry.toolLabel) + '</span>' +
        '<span class="count-text" style="margin-left:auto">' + r.entry.files + ' 文件</span></div>';
    }

    return '<div class="row' + (isSel ? ' is-selected' : '') + (r.conflict ? ' is-conflict' : '') + (animate ? '' : ' no-anim') +
      '" data-key="' + esc(r.key) + '" data-row-board="' + r.board + '" role="button" tabindex="0"' +
      (animate ? ' style="animation-delay:' + Math.min(i, 14) * 12 + 'ms"' : '') + '>' +
      // 序号列：按当前列表顺序 1..N（筛选 / 搜索 / 改排序后重新从 1 编），纯展示
      '<span class="row-mark row-num">' + (i + 1) + '</span>' +
      '<div class="row-main"><div class="row-title"><span class="row-name">' + esc(r.display) +
      (meta[r.key] && (meta[r.key].display || meta[r.key].note || (meta[r.key].cats || []).length > 1) ? '<span class="edited-dot" title="已编辑"></span>' : '') +
      '</span>' + conflictFlag + '</div>' +
      (secondary ? '<span class="row-label">' + esc(secondary) + '</span>' : '') + '</div>' +
      '<div class="row-cats">' + cats + '</div>' +
      note +
      cover +
      '<span class="row-go">' + icon('chevron-right') + '</span>' +
      '</div>';
  }

  // 独有技能看板：同一 agent 的 skill 归为一组，组与组之间插一条 agent 分割线。
  // 组顺序按 UNIQUE 里 agent 的出现顺序（后端扫描顺序）；rows 已按当前排序稳定排好，
  // 分组是稳定划分，不打乱组内次序。分割线不是 .row，行选中与键盘导航不会命中它。
  function uniqueGroupedHtml(rows, animate) {
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
    var num = 0;
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
      g.forEach(function (r) { html += skillRowHtml(r, num++, animate); });
    });
    return html;
  }

  function agentSwitchHtml(a) {
    if (a.status === 'missing') {
      return '<label class="switch is-disabled" title="未安装，无法开启">' +
        '<input type="checkbox" disabled aria-label="分发开关"><i></i></label>';
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
        }).join('\n') + '\n本工具不分发、不删除这些目录，开关不影响它们';
        extHtml = '<span class="ext-badge" title="' + esc(extTip) +
          '">外部 ' + extTotal + '</span>';
      }
      return '<div class="row agent-row' + (isSel ? ' is-selected' : '') +
        (r.status === 'missing' ? ' is-missing' : '') + (animate ? '' : ' no-anim') +
        '" data-key="' + esc(r.key) + '" data-row-board="agents" role="button" tabindex="0"' +
        (animate ? ' style="animation-delay:' + Math.min(i, 14) * 12 + 'ms"' : '') + '>' +
        '<span class="row-mark">' + esc(r.mark) + '</span>' +
        '<div class="row-main"><div class="row-title"><span class="row-name">' +
        esc(r.label) + '</span>' + extHtml + '</div>' +
        '<span class="row-label">' + esc(r.key) + '</span></div>' +
        '<div><span class="agent-state ' + r.status + '"><i></i>' + r.statusText +
        (r.status === 'connected' ? ' ' + r.linked : '') + '</span></div>' +
        '<div class="row-links" title="' + esc(r.dir) + '">' + esc(dirText) + '</div>' +
        '<div class="row-switch">' + agentSwitchHtml(r) + '</div>' +
        '<span class="row-go">' + icon('chevron-right') + '</span>' +
        '</div>';
    }).join('');
  }

  /* ---------------------------------------------------------- render: inspector */
  function selectedRow() {
    var rows = currentRows();
    for (var i = 0; i < rows.length; i++) if (rows[i].key === state.selected) return rows[i];
    return null;
  }

  function renderInspector() {
    var r = selectedRow();
    var body = document.getElementById('ins-body');
    var head = document.getElementById('ins-raw-name');
    var saveState = document.getElementById('save-state');

    if (!r) {
      head.textContent = '—';
      saveState.hidden = true;
      body.innerHTML = '<div class="ins-empty">' +
        '<div class="empty-icon">' + icon(state.board === 'agents' ? 'bot' : 'pencil') + '</div>' +
        '<b style="font-size:12.5px;color:var(--ink)">' +
        (state.board === 'agents' ? '选一个 agent' : '选一个 skill') + '</b>' +
        '<span style="font-size:11px">' +
        (state.board === 'agents'
          ? '左侧点任意一行，这里可以查看目录并切换分发开关'
          : '左侧点任意一行，这里可以改显示名、分类与备注') +
        '</span></div>';
      return;
    }

    saveState.hidden = true;
    if (r.board === 'agents') {
      renderAgentInspector(r);
      return;
    }
    head.textContent = r.name;

    var catsHtml = allCats().map(function (c) {
      var on = r.cats.indexOf(c.id) > -1;
      return '<button class="cat-chip ' + (on ? 'on' : '') + '" data-toggle-cat="' + c.id + '" style="' + catVars(c.color) + '" aria-pressed="' + on + '">' +
        '<i></i>' + esc(c.label) + (on ? icon('check') : '') + '</button>';
    }).join('');

    var coverHtml = '';
    if (r.board === 'shared') {
      var tools = r.skill.agents || [];
      coverHtml = ENABLED.map(function (t) {
        var bad = (r.skill.realIn || []).indexOf(t.key) > -1;
        return '<div class="cover-item' + (bad ? ' is-bad' : '') + '" title="' + esc(t.dir) + '">' +
          '<b>' + esc(t.mark) + '</b><span>' + esc(t.label) + '</span>' +
          icon(bad ? 'alert' : 'check') + '</div>';
      }).join('');
      if (prefs.showDisabled) {
        var offs = TOOLS.filter(function (t) { return !t.enabled; });
        coverHtml += offs.map(function (t) {
          return '<div class="cover-item" style="opacity:.55" title="' + esc(t.dir) + '">' +
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

    var desc = r.board === 'shared' && r.skill.description
      ? '<div class="field-block"><div class="field-label">描述</div><div class="desc-text">' + esc(r.skill.description) + '</div></div>'
      : '';

    body.innerHTML =
      '<div class="field-block">' +
        '<div class="field-label"><span>显示名称</span>' +
        '<button class="reset-link" data-reset="1">还原</button></div>' +
        '<input class="name-input" id="ins-name" type="text" maxlength="48" value="' + esc(r.display) + '" spellcheck="false">' +
        '<div class="field-help">只改界面里的叫法，磁盘目录名 <b style="font-family:var(--mono)">' + esc(r.name) + '</b> 不变。</div>' +
      '</div>' +
      '<div class="field-block">' +
        '<div class="field-label"><span>分类</span><span style="text-transform:none;letter-spacing:0;font-weight:400">可多选</span></div>' +
        '<div class="cat-picker">' + catsHtml + '</div>' +
        '<div class="cat-add-row">' +
          '<input class="cat-add" id="cat-add" type="text" maxlength="12" placeholder="新增分类并应用…" spellcheck="false">' +
          '<button class="btn btn-sm" id="cat-add-btn">' + icon('plus') + '添加</button>' +
        '</div>' +
      '</div>' +
      '<div class="field-block">' +
        '<div class="field-label"><span>备注</span></div>' +
        '<textarea class="note-area" id="ins-note" maxlength="400" placeholder="记下这个 skill 的用途、坑、来源…">' + esc(r.note) + '</textarea>' +
        '<div class="note-meta"><span>自动保存到' + (MODE === 'db' ? ' skillkit.db' : '本机') + '</span><span id="note-count">' + r.note.length + ' / 400</span></div>' +
      '</div>' +
      diffHtml +
      '<div class="divider"></div>' +
      '<div class="field-block">' +
        '<div class="field-label"><span>' + (r.board === 'shared' ? '分发覆盖' : '本地副本') + '</span>' +
        '<span style="text-transform:none;letter-spacing:0;font-weight:400">' + (r.board === 'shared' ? ENABLED.length + ' 个 agent' : esc(r.entry.toolLabel)) + '</span></div>' +
        (r.board === 'shared' ? '<div class="cover-grid">' + coverHtml + '</div>' : coverHtml) +
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
      (r.board === 'unique' && r.entry.deletable === false
        ? '<div class="agent-hint ext">' + esc(
            r.entry.kind === 'foreign-link'
              ? '这是指向 agent 外部目录的链接，真身不归本工具管理：只展示，不删除、不分发。'
              : '来自外部目录 ' + r.entry.src + '，由 agent 自己安装/管理：本工具不删除、不分发。') + '</div>'
        : '<div class="ins-danger">' +
          '<button class="btn btn-danger" data-delete="1">' + icon('trash') +
          (r.board === 'shared' ? '彻底删除 skill' : '删除本地副本') + '</button>' +
          '<p>' + (r.board === 'shared'
            ? '删除中央仓库实体并撤掉所有 agent 链接，操作不可恢复。'
            : '只删除这个 agent 目录下的副本，中央仓库的同名内容不受影响。') + '</p>' +
        '</div>');
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
        '本工具不分发、不删除这里，所以开关关闭后它们仍会被加载。') + '</div>';
    });

    var extMeta = (a.external || []).map(function (e) {
      return '<div class="meta-row"><span>外部来源</span><b>' + esc(e.rel) + ' · ' +
        e.count + ' 个</b></div>';
    }).join('');

    body.innerHTML =
      '<div class="agent-hero ' + a.status + '">' +
        '<span class="agent-hero-mark">' + esc(a.mark) + '</span>' +
        '<div class="agent-hero-main"><b>' + esc(a.label) + '</b>' +
          '<span class="agent-state ' + a.status + '"><i></i>' + a.statusText + '</span></div>' +
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
    var row = document.querySelector('.row[data-key="' + (window.CSS && CSS.escape ? CSS.escape(key) : key) + '"]');
    if (!row) return;
    var r = currentRows().filter(function (x) { return x.key === key; })[0];
    if (!r) return;

    var nameEl = row.querySelector('.row-name');
    if (nameEl) {
      var dot = nameEl.querySelector('.edited-dot');
      nameEl.textContent = r.display;
      var m = meta[key];
      if (m && (m.display || m.note || (m.cats || []).length > 1)) {
        var d = document.createElement('span');
        d.className = 'edited-dot';
        d.title = '已编辑';
        nameEl.appendChild(d);
      }
    }

    var catsEl = row.querySelector('.row-cats');
    if (catsEl) {
      catsEl.innerHTML = r.cats.length
        ? r.cats.slice(0, 2).map(function (id) { return catChip(id); }).join('') +
          (r.cats.length > 2 ? '<span class="cat more">+' + (r.cats.length - 2) + '</span>' : '')
        : '<span class="cat none">未分类</span>';
    }

    var noteEl = row.querySelector('.row-note');
    if (noteEl) {
      noteEl.textContent = r.note || '添加备注…';
      noteEl.classList.toggle('is-empty', !r.note);
    }
  }

  function toggleCat(key, id) {
    var m = metaOf(key);
    var idx = m.cats.indexOf(id);
    if (idx > -1) m.cats.splice(idx, 1);
    else m.cats.push(id);
    m.updated = Date.now();
    saveMeta(key);
    renderList(false);
    renderInspector();
    renderChrome();
    flashSaved();
  }

  function addCategory(key, raw) {
    var label = String(raw || '').trim().slice(0, 12);
    if (!label) return;
    var found = allCats().filter(function (c) { return c.label === label; })[0];
    var id;
    if (found) {
      id = found.id;
    } else {
      id = 'c-' + Date.now().toString(36).slice(-5);
      prefs.customCats.push({ id: id, label: label });
      savePrefs();
    }
    if (metaOf(key).cats.indexOf(id) < 0) metaOf(key).cats.push(id);
    metaOf(key).updated = Date.now();
    saveMeta(key);
    renderList(false);
    renderInspector();
    renderChrome();
    toast('ok', '分类已添加', '“' + label + '” 已应用到当前 skill');
  }

  function resetKey(key) {
    delete meta[key];
    delete dirtyKeys[key];
    if (MODE === 'db' && SKB) {
      SKB.clearMeta(scopeOf(key), key).catch(function (err) {
        toast('bad', '还原失败', String(err && err.message || err));
      });
    } else {
      saveMeta(key);
    }
    renderList(false);
    renderInspector();
    renderChrome();
    flashSaved();
    toast('info', '已还原', '显示名、分类与备注都回到默认值');
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
    var cMap = {}, lMap = {};
    c.central.files.forEach(function (f) { cMap[f.name] = f.size; });
    c.local.files.forEach(function (f) { lMap[f.name] = f.size; });

    var names = Object.keys(cMap).concat(Object.keys(lMap).filter(function (n) { return !cMap[n]; }));
    names.sort(function (a, b) {
      var pa = a === 'SKILL.md' ? 0 : a.endsWith('.md') ? 1 : 2;
      var pb = b === 'SKILL.md' ? 0 : b.endsWith('.md') ? 1 : 2;
      return pa - pb || a.localeCompare(b);
    });

    var rows = names.map(function (n) {
      var cs = cMap[n], ls = lMap[n];
      var flag = cs == null ? 'local-only' : ls == null ? 'central-only' : (cs !== ls ? 'diff' : 'same');
      var label = { 'local-only': '仅本地', 'central-only': '仅中央', 'diff': '大小不同', 'same': '一致' }[flag];
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
          '<div class="setting-main"><b>我的编辑</b><span>' + edited + ' 条 skill 有显示名、分类或备注改动，' +
            (MODE === 'db' ? '存在 skillkit.db 的 skill_meta 表。' : '存在浏览器 localStorage。') + '</span></div>' +
          '<button class="btn btn-sm" id="set-export">' + icon('download') + '导出 JSON</button>' +
        '</div>' +
        '<div class="setting-row">' +
          '<div class="setting-main"><b>清空我的编辑</b><span>' +
            (MODE === 'db' ? '清空 skill_meta 并恢复预置分类，不碰磁盘上的 skill。' : '只清本机 UI 数据，不碰 skillkit.db 与磁盘。') +
            '</span></div>' +
          '<button class="btn btn-sm" id="set-clear" style="border-color:color-mix(in srgb,var(--danger) 26%,transparent);color:var(--danger)">' + icon('trash') + '清空</button>' +
        '</div>' +
        '<div class="divider"></div>' +
        '<div class="setting-row">' +
          '<div class="setting-main"><b>中央仓库</b><span>' + SKILLS.length + ' 个 skill · ' + esc(META.storeSize || '54.4 MB') + ' · 快照 ' + esc(SCAN) + '</span></div>' +
          '<button class="btn btn-sm" data-copy="' + esc(STORE) + '">' + icon('copy') + '复制路径</button>' +
        '</div>' +
        '<div class="setting-row">' +
          '<div class="setting-main"><b>重新探测本机 agent</b><span>新装 agent 后重新读 adapters.json 的目录约定。</span></div>' +
          '<button class="btn btn-sm" id="set-detect">' + icon('refresh') + '探测</button>' +
        '</div>' +
        '<div class="divider"></div>' +
        '<p style="margin:0;font-size:11px;line-height:1.6;color:var(--ink-3)">' +
          (MODE === 'db'
            ? '桌面端：pywebview 直接 import skillkit，不启 HTTP。显示名 / 备注 / 分类写入 skill_meta(scope, key) 表；' +
              '重置删除行，清空则清表后重新播种预置分类。'
            : '原型说明：桌面端用 pywebview 直接 import skillkit，不启 HTTP。这里的改名 / 备注 / 分类只写 localStorage；' +
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
        categories: allCats()
      };
      if (MODE === 'db' && SKB) {
        SKB.exportJson('skillkit-meta.json', payload).then(function (res) {
          if (res && res.path) toast('ok', '已导出', res.path);
          else toast('info', '已取消', '没有选择保存位置');
        }).catch(function (err) {
          toast('bad', '导出失败', String(err && err.message || err));
        });
        return;
      }
      downloadJSON('skillkit-meta.json', payload);
      toast('ok', '已导出', '共 ' + editedCount() + ' 条编辑记录');
    });

    root.querySelector('#set-clear').addEventListener('click', function () {
      confirmModal(
        '清空所有编辑？',
        MODE === 'db'
          ? '显示名、分类、备注都会回到预置值。只清 skill_meta 表，不会改动磁盘上的任何 skill。'
          : '显示名、分类、备注都会回到默认值。不会改动 skillkit.db 和磁盘上的任何 skill。',
        '清空',
        function () {
          if (MODE === 'db' && SKB) {
            SKB.reseedMeta().then(function (res) {
              applyBootstrap(res);
              closeModal();
              toast('ok', '已清空', '编辑记录已重置为预置分类');
            }).catch(function (err) {
              toast('bad', '清空失败', String(err && err.message || err));
            });
            return;
          }
          meta = {};
          lsDel(META_KEY);
          prefs.seeded = false;
          savePrefs();
          seedIfNeeded();
          renderAll(true);
          closeModal();
          toast('ok', '已清空', '编辑记录已重置为初始分类');
        }
      );
    });

    root.querySelector('#set-detect').addEventListener('click', function () {
      closeModal();
      if (MODE === 'db' && SKB) {
        runProgress(600, function () {
          SKB.rescan().then(function (res) {
            applyBootstrap(res);
            toast('ok', '探测完成', '识别到 ' + TOOLS.length + ' 个 agent，其中 ' + ENABLED.length + ' 个已启用');
          }).catch(function (err) {
            toast('bad', '探测失败', String(err && err.message || err));
          });
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
  function openModal(html) {
    var root = document.getElementById('modal-root');
    root.innerHTML = '<div class="overlay" data-overlay="1">' + html + '</div>';
    var overlay = root.firstElementChild;
    overlay.addEventListener('mousedown', function (e) {
      if (e.target === overlay) closeModal();
    });
    document.body.style.overflow = 'hidden';
  }

  function closeModal() {
    document.getElementById('modal-root').innerHTML = '';
    document.body.style.overflow = '';
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
    // 取消 / 右上角关闭 / 点遮罩：让调用方回滚自己先改过的界面状态
    if (opts && typeof opts.onCancel === 'function') {
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
    var icons = { ok: 'check', info: 'info', warn: 'alert', bad: 'alert' };
    var cls = kind === 'ok' ? '' : ' ' + kind;
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

  /* ---------------------------------------------------------- interactions */
  function setBoard(board) {
    if (!/^(shared|unique|agents)$/.test(board) || state.board === board) return;
    state.board = board;
    prefs.board = board;
    savePrefs();
    state.cat = 'all';
    state.agentStatus = 'all';
    state.q = '';
    var search = document.getElementById('gsearch');
    if (search) search.value = '';
    state.selected = null;
    var rows = currentRows();
    if (rows.length) state.selected = rows[0].key;
    renderChrome();
    renderList(true);
    renderInspector();
    var body = document.getElementById('list-body');
    if (body) body.scrollTop = 0;
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
      state.selected = key;
      applyBootstrap(res);
      var act = res.action || {};
      if (!on) {
        toast('ok', '已关闭 ' + a.label,
          '撤下 ' + (act.removed || 0) + ' 处链接' +
          (act.kept && act.kept.length ? '，保留 ' + act.kept.length + ' 个冲突副本' : ''));
      } else {
        toast('ok', '已连接 ' + a.label,
          '建立/刷新 ' + (act.changed || 0) + ' 处链接' +
          (act.conflicts && act.conflicts.length ? '，' + act.conflicts.length + ' 处冲突未动' : ''));
      }
      renderInspector();
    };
    var fail = function (err) {
      toast('bad', (on ? '连接失败' : '关闭失败'), String(err && err.message || err));
      abort();
    };

    if (!on) {
      confirmModal(
        '关闭 ' + a.label + ' 的分发？',
        '将从 ' + a.dir + ' 删除本工具建立的 ' + a.linked +
        ' 个 junction（只删链接，中央仓库的 skill 实体不动）。随时可以再打开恢复。',
        '关闭分发',
        function () {
          closeModal();
          runProgress(500, function () {
            SKB.setAgent(key, false).then(showResult).catch(fail);
          });
        },
        { onCancel: abort });
      return;
    }
    runProgress(650, function () {
      SKB.setAgent(key, true).then(showResult).catch(fail);
    });
  }

  function requestDelete() {
    var r = selectedRow();
    if (!r || r.board === 'agents') return;
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

  function setCat(id) {
    state.cat = id;
    renderList(false);
  }

  function setSort() {
    prefs.sort = prefs.sort === 'name' ? 'edited' : 'name';
    savePrefs();
    renderChrome();
    renderList(false);
  }

  function renderAll(animate) {
    renderChrome();
    renderList(animate);
    renderInspector();
  }

  /* ---------------------------------------------------------- 全局刷新 */
  var refreshBusy = false;

  // 全局刷新：桌面端 rescan()（同步 adapters.json + 重扫中央仓库）后整套重渲染；
  // 原型只重渲染内置快照。忙碌时按钮转圈并忽略重复点击；看板、筛选、
  // 分类与选中由 applyBootstrap 尽量保留，列表滚动位置手动恢复。
  function requestRefresh() {
    if (refreshBusy) return;
    refreshBusy = true;
    var btn = document.getElementById('refresh-btn');
    if (btn) btn.classList.add('is-busy');
    var done = function () {
      refreshBusy = false;
      if (btn) btn.classList.remove('is-busy');
    };
    runProgress(600, function () {
      if (MODE === 'db' && SKB) {
        SKB.rescan().then(function (res) {
          var listBody = document.getElementById('list-body');
          var scrollTop = listBody ? listBody.scrollTop : 0;
          applyBootstrap(res);
          if (listBody) listBody.scrollTop = scrollTop;
          done();
          toast('ok', '已刷新', '扫描到 ' + (res.scanned != null ? res.scanned : SKILLS.length) +
            ' 个 skill · ' + agentCounts(agentRows()).connected + ' 个 agent 已连接');
        }).catch(function (err) {
          done();
          toast('bad', '刷新失败', String(err && err.message || err));
        });
        return;
      }
      renderAll(true);
      done();
      toast('info', '已刷新', '原型模式：重新渲染内置快照');
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

  // 诊断：真实手拖出问题时，kit/ui-drag.log 能说清断在哪一步
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

  // 基准外框到手：最大化先还原再拖，然后按 lx/ly 决定是继续跟随还是补发终值
  function moveFrom(res, pid) {
    var st = moveState;
    if (!st || st.pid !== pid) return;
    if (res && res.maximized) {
      uiLog('move.restore', res);
      SKB.winRestore().then(function (r2) {
        moveFrom(r2, pid);
      }, function (err) {
        uiLog('move.fail', { why: 'restore', err: String(err && err.message || err) });
        endMove();
        toast('bad', '移动失败', String(err && err.message || err));
      });
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
      bounds: null, target: null, inflight: false, done: false, logged: false
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

      var boardEl = t.closest('[data-board]');
      if (boardEl) { setBoard(boardEl.getAttribute('data-board')); return; }


      // 分发开关只由下方的 change 事件驱动：这里拦下，避免同时触发行选中。
      // 不能在 click 里读 !box.checked —— 点击冒到 document 时 checked 已被翻转，
      // 再取反正好反了（点“开”着的开关却又发一次 open，界面看起来就是关不掉）。
      if (t.closest('[data-agent-switch]')) return;

      var statusEl = t.closest('[data-agent-status]');
      if (statusEl) {
        state.agentStatus = statusEl.getAttribute('data-agent-status');
        renderList(false);
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
          runProgress(600, function () {
            SKB.rescan().then(function (res) {
              applyBootstrap(res);
              toast('ok', '探测完成', '识别到 ' + agentCounts(agentRows()).connected +
                ' 个已连接 agent');
            }).catch(function (err) {
              toast('bad', '重新探测失败', String(err && err.message || err));
            });
          });
        } else {
          toast('info', '重新探测', '桌面端才会真的扫描本机目录');
        }
        return;
      }

      var catEl = t.closest('[data-cat]');
      if (catEl) { setCat(catEl.getAttribute('data-cat')); return; }

      if (t.closest('[data-delete]')) { requestDelete(); return; }

      if (t.closest('[data-close]')) { closeModal(); return; }
      if (t.closest('.modal-close')) { closeModal(); return; }

      var row = t.closest('.row');
      if (row) {
        state.selected = row.getAttribute('data-key');
        renderList(false);
        renderInspector();
        var ins = document.getElementById('ins-body');
        if (ins) ins.scrollTop = 0;
        return;
      }

      var toggle = t.closest('[data-toggle-cat]');
      if (toggle && state.selected) { toggleCat(state.selected, toggle.getAttribute('data-toggle-cat')); return; }

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

      var diffEl = t.closest('[data-diff]');
      if (diffEl) {
        openDiff(diffEl.getAttribute('data-diff-scope'), diffEl.getAttribute('data-diff-key'));
        return;
      }

      if (t.closest('#reset-filter-btn')) {
        state.q = '';
        state.cat = 'all';
        state.agentStatus = 'all';
        document.getElementById('gsearch').value = '';
        renderList(false);
        return;
      }

      if (t.closest('#refresh-btn')) { requestRefresh(); return; }

      if (t.closest('#sort-btn')) { setSort(); return; }

      if (t.closest('#density-btn')) {
        prefs.density = prefs.density === 'compact' ? 'comfortable' : 'compact';
        savePrefs(); renderChrome(); renderList(false);
        return;
      }

      if (t.closest('#open-settings')) { openSettings(); return; }

      if (t.closest('#cat-add-btn')) {
        var input = document.getElementById('cat-add');
        if (input && state.selected) { addCategory(state.selected, input.value); input.value = ''; }
        return;
      }

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
        state.q = t.value.trim();
        renderList(false);
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
      if (e.key === 'Enter') {
        if (e.target && e.target.id === 'cat-add' && state.selected) {
          addCategory(state.selected, e.target.value);
          e.target.value = '';
        }
        return;
      }
      if (e.key === 'Escape') {
        if (document.getElementById('modal-root').firstElementChild) {
          closeModal();
          return;
        }
        if (state.q) {
          state.q = '';
          document.getElementById('gsearch').value = '';
          renderList(false);
        }
      }
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        if (document.activeElement && document.activeElement.tagName === 'INPUT') return;
        var rows = Array.prototype.slice.call(document.querySelectorAll('.row'));
        if (!rows.length) return;
        var idx = rows.findIndex(function (r) { return r.classList.contains('is-selected'); });
        var next = e.key === 'ArrowDown' ? Math.min(rows.length - 1, idx + 1) : Math.max(0, idx - 1);
        e.preventDefault();
        state.selected = rows[next].getAttribute('data-key');
        renderList(false);
        renderInspector();
        rows[next].scrollIntoView({ block: 'nearest' });
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
      var pdf = rows.filter(function (r) { return r.name === 'pdf'; })[0];
      state.selected = (pdf || rows[0]).key;
    }
    bind();
    renderAll(true);
    document.documentElement.setAttribute('data-mode', MODE);
    window.__SK_MODE = MODE;
    window.__SK_READY = true;
    // 启动留痕：ui-drag.log 里没有 boot 行 = 跑的还是旧进程/旧代码，先别怀疑拖动逻辑
    uiLog('boot', { mode: MODE, vw: window.innerWidth, vh: window.innerHeight });
  }

  function applyBootstrap(res) {
    hydrate(res.data);
    setMetaMap(res.meta);
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
  }

  function boot() {
    if (!SKB) { bootLocal(window.SKILLKIT_DATA); return; }
    SKB.ready.then(function (api) {
      if (!api) { bootLocal(window.SKILLKIT_DATA); return; }
      MODE = 'db';
      SKB.bootstrap().then(function (res) {
        document.body.classList.add('desktop');
        hydrate(res.data);
        setMetaMap(res.meta);
        start();
      }).catch(function (err) {
        MODE = 'local';
        toast('bad', '后端连接失败', String(err && err.message || err) + '，已退回内置快照');
        bootLocal(window.SKILLKIT_DATA);
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
