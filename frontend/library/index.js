/**
 * LQ-D — Library Tab Component
 *
 * 包装原有 library.js 的三栏阅读器，适配 LQ-D core 标签框架。
 */
(function () {
  'use strict';

  function mount(container, tab) {
    container.innerHTML =
      '<div class="library-layout" id="library-layout-' + tab.id + '">' +
        '<div class="library-shelf" id="library-shelf"></div>' +
        '<div class="library-resize-handle" id="library-resize-h1-' + tab.id + '" style="left:calc(var(--library-shelf-width) - 4px)"></div>' +
        '<div class="library-tree" id="library-tree"></div>' +
        '<div class="library-resize-handle" id="library-resize-h2-' + tab.id + '" style="left:calc(var(--library-shelf-width) + var(--library-tree-width) - 4px)"></div>' +
        '<div class="library-reader" id="library-reader"></div>' +
      '</div>';

    if (typeof window.initLibrary === 'function') {
      window.initLibrary();
    }

    // 初始化 Library 内部栏间拖拽伸缩
    if (typeof initLibraryResize === 'function') {
      initLibraryResize(tab.id);
    }

    // 如果 tab 携带了要打开的文档，恢复它
    if (tab.state && tab.state.type && tab.state.slug) {
      if (typeof window.selectDoc === 'function') {
        window.selectDoc(tab.state.type, tab.state.slug);
      }
    }
  }

  function unmount(container, tab) {
    // H1: 解绑事件监听 + M2: 取消进行中 fetch
    if (typeof window.destroyLibrary === 'function') {
      window.destroyLibrary();
    }
  }

  function getTitle(tab) {
    return tab.title || '文档库';
  }

  function getIcon() {
    return 'library';
  }

  function renderSidebar(container) {
    container.innerHTML =
      '<div class="lqd-sidebar-section-title">文档库</div>' +
      '<div class="lqd-empty">在 Main 区选择文档类型与书籍</div>';
  }

  function renderOverview(container, tab) {
    container.innerHTML =
      '<div class="lqd-overview-section-title">当前文档</div>' +
      '<div class="lqd-empty">选择章节后显示元信息</div>';
  }

  var LqdLibrary = {
    type: 'library',
    getTitle: getTitle,
    getIcon: getIcon,
    mount: mount,
    unmount: unmount,
    renderSidebar: renderSidebar,
    renderOverview: renderOverview
  };

  window.LqdLibrary = LqdLibrary;

  // ── Library 内部栏间拖拽伸缩 ──────────────────────────────────────────
  var _LIB_RESIZE_SAVED = {};
  var _LIB_MIN_W = 140;

  function _loadLibWidth(key, fallback) {
    try {
      var v = localStorage.getItem(key);
      if (v) {
        var w = parseInt(v, 10);
        if (w >= _LIB_MIN_W) return w;
      }
    } catch (_) {}
    return fallback;
  }

  function _saveLibWidth(key, w) {
    try { localStorage.setItem(key, String(Math.round(w))); } catch (_) {}
  }

  function _makeLibResizeHandle(handleId, layoutEl, cssVar, storageKey, isFirst) {
    var handle = document.getElementById(handleId);
    if (!handle) return;
    var dragging = false;
    var startX = 0;
    var startW = 0;

    // 恢复保存的宽度
    var saved = _loadLibWidth(storageKey, null);
    if (saved) {
      layoutEl.style.setProperty(cssVar, saved + 'px');
      // 更新第二个手柄的 left 偏移需要在两个都初始化后做,此处暂不处理
    }

    handle.addEventListener('mousedown', function (e) {
      e.preventDefault();
      dragging = true;
      startX = e.clientX;
      startW = parseFloat(getComputedStyle(layoutEl).getPropertyValue(cssVar));
      handle.classList.add('resizing');
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';
    });

    document.addEventListener('mousemove', function (e) {
      if (!dragging) return;
      var newW = Math.max(_LIB_MIN_W, startW + e.clientX - startX);
      layoutEl.style.setProperty(cssVar, newW + 'px');

      // 同时更新另一手柄的 left（两个手柄相互联动）
      var handles = layoutEl.querySelectorAll('.library-resize-handle');
      if (handles.length === 2) {
        handles[0].style.left = 'calc(var(--library-shelf-width) - 4px)';
        handles[1].style.left = 'calc(var(--library-shelf-width) + var(--library-tree-width) - 4px)';
      }
    });

    document.addEventListener('mouseup', function () {
      if (!dragging) return;
      dragging = false;
      handle.classList.remove('resizing');
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      var w = parseFloat(getComputedStyle(layoutEl).getPropertyValue(cssVar));
      _saveLibWidth(storageKey, w);
    });
  }

  function initLibraryResize(tabId) {
    var layoutEl = document.getElementById('library-layout-' + tabId);
    if (!layoutEl) return;

    var savedShelf = _loadLibWidth('lqd-lib-shelf-w', null);
    var savedTree = _loadLibWidth('lqd-lib-tree-w', null);
    if (savedShelf) layoutEl.style.setProperty('--library-shelf-width', savedShelf + 'px');
    if (savedTree) layoutEl.style.setProperty('--library-tree-width', savedTree + 'px');

    _makeLibResizeHandle(
      'library-resize-h1-' + tabId, layoutEl,
      '--library-shelf-width', 'lqd-lib-shelf-w'
    );
    _makeLibResizeHandle(
      'library-resize-h2-' + tabId, layoutEl,
      '--library-tree-width', 'lqd-lib-tree-w'
    );
  }

  window.initLibraryResize = initLibraryResize;

  function tryRegister() {
    if (window.LqdTabs) window.LqdTabs.register('library', LqdLibrary);
    if (window.LqdSidebar) window.LqdSidebar.register('library', LqdLibrary);
    if (window.LqdOverview) window.LqdOverview.register('library', LqdLibrary);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', tryRegister);
  } else {
    tryRegister();
  }
})();
