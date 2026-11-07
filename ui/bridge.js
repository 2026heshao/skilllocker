/* ============================================================
   skilllocker · desktop bridge
   pywebview 注入 window.pywebview.api 后，把 Python 方法包成
   带 {ok:false} 归一化的 Promise；普通浏览器里 ready 直接给 null。
   ============================================================ */
(function () {
  'use strict';

  window.__SK_ERRORS = [];
  window.addEventListener('error', function (e) {
    window.__SK_ERRORS.push(String(e.message || e.error || 'error'));
  });
  window.addEventListener('unhandledrejection', function (e) {
    window.__SK_ERRORS.push(String((e.reason && e.reason.message) || e.reason || 'rejection'));
  });

  var api = null;
  // desktop=1 只说明「跑在桌面壳里」；壳里桥也可能接不上（注入超时），
  // app.js 的 boot() 需要区分这两种失败，把它提到模块作用域并暴露
  var desktopHint = /[?&]desktop=1/.test(location.search);
  var ready = new Promise(function (resolve) {
    // pywebview 在页面加载完成后才注入 window.pywebview，
    // 用 URL 上的 desktop=1 区分桌面壳与 file:// 原型，避免原型白等。
    var settled = false;
    var tick = null;
    function settle(a) {
      if (settled) return;
      settled = true;
      if (tick) clearInterval(tick);
      api = a || null;
      resolve(api);
    }
    function fromInjection() {
      if (window.pywebview && window.pywebview.api) {
        settle(window.pywebview.api);
        return true;
      }
      return false;
    }
    if (!desktopHint) { settle(null); return; }
    if (fromInjection()) return;
    window.addEventListener('pywebviewready', fromInjection, { once: true });
    // 首次安装 / 刚解压完的 exe 会被 Defender 扫描，WebView2 冷启动可能明显
    // 超过 6s；注入窗口给到 20s，避免新用户第一次打开就停在「注入超时」。
    var INJECT_TIMEOUT = 20000;
    var tries = 0;
    tick = setInterval(function () {
      if (fromInjection()) return;
      // 轮询负责尽早命中（pywebviewready 事件兜不住时）；超时判定交给下面
      // INJECT_TIMEOUT 那一发，这里只管别比它早判死
      if (++tries > Math.floor(INJECT_TIMEOUT / 100) - 2) settle(null);
    }, 100);
    setTimeout(function () { if (!fromInjection()) settle(null); }, INJECT_TIMEOUT);
  });

  // 桥调用超时：pywebview 的 promise 在后端线程死锁 / 窗口已销毁时可能永不
  // settle（如 rescan 卡住 → refreshBusy 永久 true，刷新按钮永久禁用）。
  // 重量级接口（全盘扫描 / 建删 junction）给宽时限，轻量接口 30s 兜底。
  var DEFAULT_TIMEOUT = 30000;
  var CALL_TIMEOUT = {
    bootstrap: 180000,
    rescan: 180000,
    delete_skill: 180000,
    uninstall_unique: 180000,
    promote_unique: 180000,
    adopt_store: 180000,
    adopt_local_skills: 600000,  // 批量拷贝 + 一次 rescan，按仓库大小给足预算
    set_agent: 180000,
    reseed_meta: 120000,
    diff: 60000,
    export_json: 60000,
    import_preflight: 60000,
    import_skill: 180000,
    choose_dir: 600000,  // 等用户在原生对话框里选目录，不能短
    online_search: 60000,
    online_fetch: 240000  // 后端下载预算 180s + 清单解析；后端先超时，避免前端假失败
  };

  function call(name) {
    var args = Array.prototype.slice.call(arguments, 1);
    return ready.then(function (a) {
      if (!a) throw new Error('桌面端未连接');
      var fn = a[name];
      if (typeof fn !== 'function') throw new Error('缺少接口 ' + name);
      var ms = CALL_TIMEOUT[name] || DEFAULT_TIMEOUT;
      return new Promise(function (resolve, reject) {
        var timer = setTimeout(function () {
          reject(new Error('接口 ' + name + ' ' + Math.round(ms / 1000) + ' 秒无响应'));
        }, ms);
        fn.apply(a, args).then(function (res) {
          clearTimeout(timer);
          if (res && res.ok === false) reject(new Error(res.error || '后端返回失败'));
          else resolve(res);
        }, function (err) {
          clearTimeout(timer);
          reject(err);
        });
      });
    });
  }

  window.SKILLLOCKER_BRIDGE = {
    ready: ready,
    isDesktop: function () { return !!api; },
    isDesktopShell: function () { return desktopHint; },
    bootstrap: function () { return call('bootstrap'); },
    saveMeta: function (scope, key, fields) { return call('save_meta', scope, key, fields); },
    clearMeta: function (scope, key) { return call('clear_meta', scope, key); },
    deleteSkill: function (scope, key) { return call('delete_skill', scope, key); },
    uninstallUnique: function (key) { return call('uninstall_unique', key); },
    promoteUnique: function (key) { return call('promote_unique', key); },
    adoptStore: function (path) { return call('adopt_store', path); },
    adoptLocalSkills: function () { return call('adopt_local_skills'); },
    chooseDir: function () { return call('choose_dir'); },
    importPreflight: function (src, name) { return call('import_preflight', src, name); },
    importSkill: function (src, name, folder) { return call('import_skill', src, name, folder); },
    onlineSearch: function (q, limit) { return call('online_search', q, limit); },
    onlineFetch: function (source, name) { return call('online_fetch', source, name); },
    onlineFetchCancel: function () { return call('online_fetch_cancel'); },
    openUrl: function (url) { return call('open_url', url); },
    reseedMeta: function () { return call('reseed_meta'); },
    folderCreate: function (name, color) { return call('folder_create', name, color); },
    folderRename: function (id, name, color) { return call('folder_rename', id, name, color); },
    folderDelete: function (id) { return call('folder_delete', id); },
    diff: function (scope, key) { return call('diff', scope, key); },
    rescan: function () { return call('rescan'); },
    exportJson: function (name, payload) { return call('export_json', name, payload); },
    copyPath: function (text) { return call('copy_path', text); },
    openDir: function (scope, key) { return call('open_dir', scope, key); },
    setAgent: function (key, on) { return call('set_agent', key, !!on); },
    winMin: function () { return call('win_min'); },
    winMax: function () { return call('win_max'); },
    winBounds: function () { return call('win_bounds'); },
    winSetBounds: function (x, y, w, h) { return call('win_set_bounds', x, y, w, h); },
    winRestore: function () { return call('win_restore'); },
    logUi: function (kind, detail) { return call('log_ui', kind, detail); },
    winClose: function () { return call('win_close'); }
  };
})();
