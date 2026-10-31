/* ============================================================
   skillkit · desktop bridge
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
  var ready = new Promise(function (resolve) {
    // pywebview 在页面加载完成后才注入 window.pywebview，
    // 用 URL 上的 desktop=1 区分桌面壳与 file:// 原型，避免原型白等。
    var desktopHint = /[?&]desktop=1/.test(location.search);
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
    var tries = 0;
    tick = setInterval(function () {
      if (fromInjection()) return;
      if (++tries > 15) settle(null);
    }, 100);
    setTimeout(function () { if (!fromInjection()) settle(null); }, 6000);
  });

  function call(name) {
    var args = Array.prototype.slice.call(arguments, 1);
    return ready.then(function (a) {
      if (!a) throw new Error('桌面端未连接');
      var fn = a[name];
      if (typeof fn !== 'function') throw new Error('缺少接口 ' + name);
      return fn.apply(a, args).then(function (res) {
        if (res && res.ok === false) throw new Error(res.error || '后端返回失败');
        return res;
      });
    });
  }

  window.SKILLKIT_BRIDGE = {
    ready: ready,
    isDesktop: function () { return !!api; },
    bootstrap: function () { return call('bootstrap'); },
    saveMeta: function (scope, key, fields) { return call('save_meta', scope, key, fields); },
    clearMeta: function (scope, key) { return call('clear_meta', scope, key); },
    deleteSkill: function (scope, key) { return call('delete_skill', scope, key); },
    reseedMeta: function () { return call('reseed_meta'); },
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
