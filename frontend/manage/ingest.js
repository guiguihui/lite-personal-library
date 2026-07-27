/**
 * LQ-D — Manage Ingest helpers (placeholder)
 *
 * 当前 manage.js 为单文件实现，index.js 直接调用 window.initManage。
 * 本文件保留为扩展点。
 */
(function () {
  'use strict';

  // 与 builder.js 合并导出，避免重复
  if (!window.LqdManageIngest) {
    window.LqdManageIngest = {
      start: function () {}
    };
  }
})();
