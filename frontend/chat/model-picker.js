/**
 * LQ-D — Model Picker
 *
 * 模型选择器组件:显示在 composer 底部操作条,替代静态 .lqd-chat-model。
 * 功能:模型切换、连通性状态展示、定期自动检测、手动刷新。
 *
 * 用法: window.LqdModelPicker.create(container, { onChange: fn })
 */
(function () {
  'use strict';

  var BASE = (window.LQD_CHAT_BASE || '').replace(/\/+$/, '');
  var CHECK_INTERVAL = 60000; // 自动检测间隔 60s
  var STATUS_LABELS = {
    available: '可用',
    auth_error: 'Key 无效',
    rate_limited: '限流中',
    unavailable: '不可用',
    unreachable: '无法连接',
    no_key: '未配置',
    checking: '检测中…'
  };

  var dropEl = null;
  var btnEl = null;
  var checkTimer = null;
  var checkResults = {};
  var onModelChange = null;

  function escHtml(s) {
    if (!s) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  // ── Status dot SVG ──────────────────────────────────────────────────────
  function statusDot(status) {
    var color = 'var(--fg-tertiary)'; // default gray
    if (status === 'available') color = 'var(--success)';
    else if (status === 'auth_error' || status === 'unavailable') color = 'var(--danger)';
    else if (status === 'rate_limited') color = 'var(--warning)';
    else if (status === 'unreachable') color = 'var(--danger)';
    else if (status === 'checking') color = 'var(--warning)';
    return '<span class="lqd-model-dot" style="background:' + color + '" title="' + (STATUS_LABELS[status] || status) + '"></span>';
  }

  // ── Dropdown ────────────────────────────────────────────────────────────
  function hideDropdown() {
    if (dropEl) { dropEl.remove(); dropEl = null; }
    document.removeEventListener('click', onOutsideClick);
  }

  function onOutsideClick(e) {
    if (dropEl && !dropEl.contains(e.target) && (!btnEl || !btnEl.contains(e.target))) {
      hideDropdown();
    }
  }

  function showDropdown(e, providers, activeProvider) {
    hideDropdown();

    var dd = document.createElement('div');
    dd.className = 'lqd-model-dropdown';
    dd.setAttribute('role', 'listbox');
    dd.setAttribute('aria-label', '选择模型');

    var header = document.createElement('div');
    header.className = 'lqd-model-dd-header';
    header.textContent = '选择模型';
    dd.appendChild(header);

    var list = document.createElement('div');
    list.className = 'lqd-model-dd-list';

    var names = Object.keys(providers);
    for (var i = 0; i < names.length; i++) {
      var name = names[i];
      var p = providers[name];
      var res = checkResults[name] || { status: 'unknown' };
      var isActive = name === activeProvider;

      var row = document.createElement('div');
      row.className = 'lqd-model-dd-item' + (isActive ? ' lqd-model-dd-item--active' : '');
      row.setAttribute('role', 'option');
      row.setAttribute('aria-selected', String(isActive));
      row.setAttribute('data-provider', name);

      var errorHint = '';
      if (res.error && res.status !== 'available' && res.status !== 'checking') {
        errorHint = '<span class="lqd-model-dd-error" title="' + escHtml(res.error) + '">' + escHtml(res.error) + '</span>';
      }

      var latencyStr = res.latency_ms > 0 ? res.latency_ms + 'ms' : '';

      row.innerHTML =
        statusDot(res.status) +
        '<span class="lqd-model-dd-name">' + escHtml(name) + '</span>' +
        '<span class="lqd-model-dd-model">' + escHtml(p.model || '—') + '</span>' +
        '<span class="lqd-model-dd-latency">' + latencyStr + '</span>' +
        errorHint;

      (function (providerName) {
        row.addEventListener('click', function () {
          hideDropdown();
          if (providerName !== activeProvider && onModelChange) {
            onModelChange(providerName);
          }
        });
      })(name);

      list.appendChild(row);
    }

    dd.appendChild(list);

    // 底部操作栏:刷新按钮
    var footer = document.createElement('div');
    footer.className = 'lqd-model-dd-footer';
    var refreshBtn = document.createElement('button');
    refreshBtn.className = 'lqd-model-dd-refresh';
    refreshBtn.innerHTML = (window.LqdIcons ? window.LqdIcons.icon('refresh') : '↻') + ' 刷新状态';
    refreshBtn.addEventListener('click', function (e) {
      e.stopPropagation();
      runCheck().then(function () {
        // 用新结果重绘下拉
        showDropdown(e, providers, activeProvider);
      });
    });
    footer.appendChild(refreshBtn);
    dd.appendChild(footer);

    document.body.appendChild(dd);
    dropEl = dd;

    // 定位:放在按钮上方
    var btnRect = btnEl.getBoundingClientRect();
    dd.style.position = 'fixed';
    dd.style.left = Math.max(8, btnRect.left) + 'px';
    // 下拉菜单显示在按钮上方
    var ddHeight = 340; // 预估高度
    if (btnRect.top - ddHeight > 40) {
      dd.style.bottom = (window.innerHeight - btnRect.top + 4) + 'px';
    } else {
      dd.style.top = (btnRect.bottom + 4) + 'px';
    }
    // 确保不超出右侧
    var ddRect = dd.getBoundingClientRect();
    if (ddRect.right > window.innerWidth - 8) {
      dd.style.left = (window.innerWidth - ddRect.width - 8) + 'px';
    }

    setTimeout(function () {
      document.addEventListener('click', onOutsideClick);
    }, 0);
  }

  // ── Connectivity check ──────────────────────────────────────────────────
  function runCheck() {
    // 更新所有已知结果为 checking
    var names = Object.keys(checkResults);
    for (var i = 0; i < names.length; i++) {
      checkResults[names[i]] = { status: 'checking', error: '', latency_ms: 0 };
    }
    updateButton();

    return fetch(BASE + '/api/llm/check', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' }
    })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (data) {
      if (data && data.results) {
        for (var i = 0; i < data.results.length; i++) {
          checkResults[data.results[i].provider] = data.results[i];
        }
      }
      updateButton();
      return data;
    })
    .catch(function () {
      updateButton();
    });
  }

  function startAutoCheck() {
    stopAutoCheck();
    runCheck();
    checkTimer = setInterval(runCheck, CHECK_INTERVAL);
  }

  function stopAutoCheck() {
    if (checkTimer) { clearInterval(checkTimer); checkTimer = null; }
  }

  // ── Button update ───────────────────────────────────────────────────────
  function updateButton() {
    if (!btnEl) return;
    var settings = window.LqdSettings;
    var provider = settings.get('provider') || '';
    var model = settings.get('model') || '';
    var res = checkResults[provider] || { status: 'unknown' };

    var labelEl = btnEl.querySelector('.lqd-model-label');
    var dotEl = btnEl.querySelector('.lqd-model-dot');

    if (labelEl) {
      labelEl.textContent = provider + (model ? ' / ' + model : '');
    }
    if (dotEl) {
      dotEl.style.background = dotColor(res.status);
      dotEl.setAttribute('title', STATUS_LABELS[res.status] || res.status);
    }
  }

  function dotColor(status) {
    if (status === 'available') return 'var(--success)';
    if (status === 'auth_error' || status === 'unavailable' || status === 'unreachable') return 'var(--danger)';
    if (status === 'rate_limited' || status === 'checking') return 'var(--warning)';
    return 'var(--fg-tertiary)';
  }

  // ── Create ──────────────────────────────────────────────────────────────
  function create(container, options) {
    options = options || {};
    onModelChange = options.onChange || null;

    // 替换容器中的 .lqd-chat-model 元素(如果已存在)
    var existing = container.querySelector('.lqd-model-picker');
    if (existing) return existing;

    var modelSpan = container.querySelector('.lqd-chat-model');
    if (!modelSpan) return null;

    // 创建按钮
    btnEl = document.createElement('button');
    btnEl.className = 'lqd-model-picker';
    btnEl.setAttribute('aria-label', '选择模型');
    btnEl.setAttribute('aria-haspopup', 'listbox');
    btnEl.innerHTML =
      '<span class="lqd-model-dot"></span>' +
      '<span class="lqd-model-label"></span>' +
      '<span class="lqd-model-chevron">' + (window.LqdIcons ? window.LqdIcons.icon('chevron-down') : '▾') + '</span>';

    // 点击切换下拉
    btnEl.addEventListener('click', function (e) {
      e.stopPropagation();
      if (dropEl) { hideDropdown(); return; }
      // 加载设置并显示下拉
      window.LqdSettings.load().then(function () {
        var providers = window.LqdSettings._cache ? window.LqdSettings._cache._providers || {} : {};
        var active = window.LqdSettings.get('provider');
        showDropdown(e, providers, active);
      });
    });

    // 替换原有的 model span
    modelSpan.parentNode.replaceChild(btnEl, modelSpan);

    // 初始状态
    updateButton();

    // 监听设置变化
    if (window.LqdEvents) {
      window.LqdEvents.on('settings:loaded', function () {
        updateButton();
      });
    }
    if (window.LqdStore && typeof window.LqdStore.subscribe === 'function') {
      window.LqdStore.subscribe('status', function () {
        updateButton();
      });
    }

    // 启动自动检测
    startAutoCheck();

    return btnEl;
  }

  function refresh() {
    runCheck();
  }

  function destroy() {
    stopAutoCheck();
    hideDropdown();
    btnEl = null;
    checkResults = {};
  }

  window.LqdModelPicker = {
    create: create,
    refresh: refresh,
    destroy: destroy,
    runCheck: runCheck
  };
})();
