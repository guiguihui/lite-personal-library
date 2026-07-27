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
        '<div class="library-tree" id="library-tree"></div>' +
        '<div class="library-reader" id="library-reader"></div>' +
      '</div>';

    if (typeof window.initLibrary === 'function') {
      window.initLibrary();
    }

    // 如果 tab 携带了要打开的文档，恢复它
    if (tab.state && tab.state.type && tab.state.slug) {
      if (typeof window.selectDoc === 'function') {
        window.selectDoc(tab.state.type, tab.state.slug);
      }
    }
  }

  function unmount(container, tab) {
    // 无需特殊清理
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
