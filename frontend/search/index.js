/**
 * LQ-D — Search Tab Component
 * 全局搜索结果视图，注册到 LqdTabs，由命令面板 search:global 打开。
 */
(function () {
  'use strict';

  var DEFAULT_LIMIT = 20;

  function $(id) { return document.getElementById(id); }

  function escapeHtml(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function icon(name) {
    return window.LqdIcons && typeof window.LqdIcons.icon === 'function' ? window.LqdIcons.icon(name) : '';
  }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function getBase() {
    return (window.LQD_CHAT_BASE || '/').replace(/\/$/, '');
  }

  // ── 标签组件接口 ───────────────────────────────────────────────────────────
  function mount(container, tab) {
    var root = el('div', 'lqd-search');
    root.innerHTML =
      '<div class="lqd-search-header">' +
        '<input type="text" class="lqd-form-input lqd-search-input" placeholder="搜索图书馆...">' +
        '<button class="lqd-btn lqd-search-btn" aria-label="搜索">' + icon('search') + '</button>' +
      '</div>' +
      '<div class="lqd-search-results"></div>';
    container.appendChild(root);

    var input = root.querySelector('.lqd-search-input');
    var results = root.querySelector('.lqd-search-results');
    var btn = root.querySelector('.lqd-search-btn');

    if (tab && tab.state && tab.state.query) {
      input.value = tab.state.query;
      doSearch(tab.state.query, results);
    } else {
      results.appendChild(el('div', 'lqd-search-empty', '输入关键词后按 Enter 搜索'));
    }

    btn.addEventListener('click', function () { doSearch(input.value, results); });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') doSearch(input.value, results);
    });
  }

  function unmount(container, tab) {
    var root = container.querySelector('.lqd-search');
    if (root) root.remove();
  }

  function getTitle(tab) {
    if (tab && tab.state && tab.state.query) {
      return '搜索: ' + tab.state.query.slice(0, 12);
    }
    return '全局搜索';
  }

  function getIcon() { return 'search'; }

  // ── 搜索逻辑 ─────────────────────────────────────────────────────────────
  function doSearch(query, resultsEl) {
    query = (query || '').trim();
    if (!query) {
      resultsEl.innerHTML = '';
      resultsEl.appendChild(el('div', 'lqd-search-empty', '输入关键词后按 Enter 搜索'));
      return;
    }

    resultsEl.innerHTML = (window.LqdSkeleton ? window.LqdSkeleton.list({ count: 6, itemHeight: 48 }) : '<div class="lqd-loading">搜索中...</div>');
    var url = getBase() + '/api/search?q=' + encodeURIComponent(query) + '&limit=' + DEFAULT_LIMIT;

    fetch(url)
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      })
      .then(function (data) {
        renderResults(data, query, resultsEl);
      })
      .catch(function (e) {
        renderError(resultsEl, e.message);
      });
  }

  function renderError(resultsEl, message) {
    resultsEl.innerHTML = '';
    resultsEl.appendChild(el('div', 'lqd-search-error', '搜索失败: ' + escapeHtml(message)));
  }

  function renderResults(data, query, resultsEl) {
    resultsEl.innerHTML = '';
    var results = data && data.results ? data.results : [];
    if (!results.length) {
      resultsEl.appendChild(el('div', 'lqd-search-empty', '未找到 "' + escapeHtml(query) + '" 的结果'));
      return;
    }

    resultsEl.appendChild(el('div', 'lqd-search-count', '共 ' + results.length + ' 条结果'));

    for (var i = 0; i < results.length; i++) {
      var r = results[i];
      var item = el('div', 'lqd-search-result');
      var breadcrumb = r.breadcrumb || '';
      var meta = (r.doc_type || r.type || '') + (breadcrumb ? ' · ' + breadcrumb : '');
      item.innerHTML =
        '<div class="lqd-search-result-title">' + escapeHtml(r.title || '无标题') + '</div>' +
        '<div class="lqd-search-result-meta">' + escapeHtml(meta) + '</div>' +
        '<div class="lqd-search-result-snippet">' + escapeHtml((r.text || '').slice(0, 200)) + (r.text && r.text.length > 200 ? '…' : '') + '</div>';
      item.addEventListener('click', (function (result) {
        return function () { openResult(result); };
      })(r));
      resultsEl.appendChild(item);
    }
  }

  function openResult(result) {
    if (window.LqdEvents) {
      window.LqdEvents.emit('search:result:selected', result);
    }
    if (window.LqdLibrary && typeof window.LqdLibrary.openDoc === 'function') {
      window.LqdLibrary.openDoc(result.doc_type || result.type, result.slug || result.doc_id, result.node_id);
    }
  }

  // ── 打开标签 ─────────────────────────────────────────────────────────────
  function open(query) {
    if (!window.LqdTabs) {
      if (window.console && window.console.error) {
        window.console.error('[LqdSearch] LqdTabs not available');
      }
      return;
    }

    var tabs = window.LqdTabs.list();
    for (var i = 0; i < tabs.length; i++) {
      if (tabs[i].type === 'search') {
        window.LqdTabs.close(tabs[i].id);
        break;
      }
    }

    window.LqdTabs.open({
      type: 'search',
      title: query ? '搜索: ' + query.slice(0, 12) : '全局搜索',
      state: query ? { query: query } : {}
    });
  }

  var LqdSearch = {
    type: 'search',
    getTitle: getTitle,
    getIcon: getIcon,
    mount: mount,
    unmount: unmount,
    open: open
  };

  window.LqdSearch = LqdSearch;

  function tryRegister() {
    if (window.LqdTabs) window.LqdTabs.register('search', LqdSearch);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', tryRegister);
  } else {
    tryRegister();
  }
})();
