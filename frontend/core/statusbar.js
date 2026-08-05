/**
 * LQ-D — StatusBar
 * 底部状态栏:索引状态、模型名称、版本号、主题切换。
 */
(function () {
  'use strict';

  var REFRESH_INTERVAL = 30000;
  var timer = null;

  function $(id) { return document.getElementById(id); }

  function getEl() {
    return $('lqd-status-bar');
  }

  function getBase() {
    return (window.LQD_CHAT_BASE || '/').replace(/\/$/, '');
  }

  function escapeHtml(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function render(status) {
    status = status || window.LqdStore.get('status') || {};
    var el = getEl();
    if (!el) return;

    var fetchError = window.LqdStore.get('statusFetchError');

    var indexDotClass = 'lqd-status-bar-dot';
    var indexText = '索引未构建';
    if (fetchError) {
      indexDotClass += ' lqd-status-bar-dot--failed';
      indexText = '状态获取失败';
    } else if (status.indexRunning) {
      indexDotClass += ' lqd-status-bar-dot--running';
      indexText = '索引构建中';
    } else if (status.indexReady) {
      indexDotClass += ' lqd-status-bar-dot--ready';
      indexText = '索引就绪';
    } else if (status.ingestRunning) {
      indexDotClass += ' lqd-status-bar-dot--running';
      indexText = '入库中';
    }

    var themeIcon = 'monitor';
    var themeMode = window.LqdTheme ? window.LqdTheme.get() : 'auto';
    if (themeMode === 'light') themeIcon = 'sun';
    else if (themeMode === 'dark') themeIcon = 'moon';

    el.innerHTML =
      '<div class="lqd-status-bar-left lqd-tabular">' +
        '<div class="lqd-status-bar-item' + (fetchError ? ' lqd-status-bar-item--error' : '') + '" id="lqd-status-index-item">' +
          '<span class="' + indexDotClass + '"></span>' +
          '<span>' + escapeHtml(indexText) + '</span>' +
        '</div>' +
      '</div>' +
      '<div class="lqd-status-bar-center lqd-tabular">' +
        (status.provider ? '<div class="lqd-status-bar-item">模型: ' + escapeHtml(status.provider) + ' / ' + escapeHtml(status.model || '-') + '</div>' : '') +
      '</div>' +
      '<div class="lqd-status-bar-right lqd-tabular">' +
        '<div class="lqd-status-bar-item">v' + escapeHtml(status.version || '0.1.0') + '</div>' +
        '<button class="lqd-status-bar-item lqd-btn lqd-btn--ghost lqd-btn--sm" id="lqd-status-theme-btn" aria-label="切换主题">' +
          (window.LqdIcons ? window.LqdIcons.icon(themeIcon) : '') +
        '</button>' +
      '</div>';

    var themeBtn = $('lqd-status-theme-btn');
    if (themeBtn) {
      if (window.LqdTooltip) window.LqdTooltip.attach(themeBtn, { text: '切换主题', position: 'top' });
      themeBtn.addEventListener('click', function () {
        var modes = ['auto', 'light', 'dark'];
        var current = window.LqdTheme ? window.LqdTheme.get() : 'auto';
        var next = modes[(modes.indexOf(current) + 1) % modes.length];
        window.LqdTheme.set(next);
        render();
      });
    }

    // 失败时给索引项挂 tooltip 说明
    var indexItem = $('lqd-status-index-item');
    if (indexItem && fetchError && window.LqdTooltip) {
      window.LqdTooltip.attach(indexItem, { text: '状态获取失败: ' + fetchError, position: 'top' });
    }
  }

  async function fetchStatus() {
    try {
      var r = await fetch(getBase() + '/api/status');
      if (!r.ok) throw new Error('HTTP ' + r.status);
      var data = await r.json();
      var status = {
        indexReady: !!data.index_ready,
        indexRunning: !!data.index_running,
        ingestRunning: !!data.ingest_running,
        provider: data.active_provider || '',
        model: data.model || '',
        version: data.version || '0.1.0'
      };
      window.LqdStore.set('status', status);
      window.LqdStore.set('statusFetchError', null);
      render(status);
    } catch (e) {
      // 不再静默吞错:记录错误,渲染失败点,发事件
      var msg = (e && e.message) || '未知错误';
      window.LqdStore.set('statusFetchError', msg);
      if (window.LqdEvents) window.LqdEvents.emit('status:fetch-error', { message: msg });
      render();
    }
  }

  function startPolling() {
    if (timer) clearInterval(timer);
    fetchStatus();
    timer = setInterval(fetchStatus, REFRESH_INTERVAL);
  }

  function stopPolling() {
    if (timer) {
      clearInterval(timer);
      timer = null;
    }
    // L3: 移除 visibilitychange 监听器,避免多次 init/destroy 后累积
    if (_visibilityHandler) {
      document.removeEventListener('visibilitychange', _visibilityHandler);
      _visibilityHandler = null;
    }
  }

  // L3: 提取为命名函数,以便 removeEventListener
  var _visibilityHandler = null;

  function init() {
    render();
    startPolling();

    // 窗口重获焦点时重试(此前 fetch 失败会留下陈旧状态)
    _visibilityHandler = function () {
      if (!document.hidden && window.LqdStore.get('statusFetchError')) {
        fetchStatus();
      }
    };
    document.addEventListener('visibilitychange', _visibilityHandler);

    window.LqdEvents.on('index:status', function (payload) {
      var status = window.LqdStore.get('status');
      if (payload.indexReady !== undefined) status.indexReady = !!payload.indexReady;
      if (payload.indexRunning !== undefined) status.indexRunning = !!payload.indexRunning;
      if (payload.ingestRunning !== undefined) status.ingestRunning = !!payload.ingestRunning;
      window.LqdStore.set('status', status);
      render(status);
    });

    window.LqdEvents.on('settings:loaded', function (payload) {
      var status = window.LqdStore.get('status');
      if (payload.provider !== undefined) status.provider = payload.provider;
      if (payload.model !== undefined) status.model = payload.model;
      window.LqdStore.set('status', status);
      render(status);
    });

    window.LqdEvents.on('theme:changed', function () {
      render();
    });
  }

  window.LqdStatusBar = {
    init: init,
    render: render,
    refresh: fetchStatus,
    startPolling: startPolling,
    stopPolling: stopPolling
  };
})();
