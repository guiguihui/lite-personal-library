/**
 * 轻量个人知识库 — Library Sidebar & Reader helpers (placeholder)
 *
 * 当前 library.js 为单文件实现，index.js 直接调用 window.initLibrary。
 * 本文件保留为扩展点，未来可在此实现纯 Sidebar/Reader 的细粒度组件。
 */
(function () {
  'use strict';

  window.LqdLibrarySidebar = {
    renderShelf: function () {
      if (typeof window.initLibrary === 'function') window.initLibrary();
    }
  };

  window.LqdLibraryReader = {
    openDoc: function (type, slug) {
      if (typeof window.selectDoc === 'function') window.selectDoc(type, slug);
    }
  };
})();
