/**
 * 轻量个人知识库 — Manage Builder & Ingest helpers (placeholder)
 *
 * 当前 manage.js 为单文件实现，index.js 直接调用 window.initManage / window.YuuManage.buildIndex。
 * 本文件保留为扩展点，未来可拆分 builder/ingest 逻辑至此。
 */
(function () {
  'use strict';

  window.LqdManageBuilder = {
    build: function (mode) {
      if (window.YuuManage && typeof window.YuuManage.buildIndex === 'function') {
        window.YuuManage.buildIndex(mode || 'full');
      }
    }
  };

  window.LqdManageIngest = {
    start: function (file, meta) {
      // 未来实现：直接调用 /api/ingest/full 并轮询
      if (window.console && window.console.error) {
        window.console.error('[LqdManageIngest] start not implemented', file, meta);
      }
    }
  };
})();
