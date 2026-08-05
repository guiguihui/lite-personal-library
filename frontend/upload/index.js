/**
 * LQ-D — Upload Tab Component
 *
 * 包装原有 upload.js 的上传队列。
 */
(function () {
  'use strict';

  function mount(container, tab) {
    container.innerHTML = '<div class="lqd-upload" id="lqd-upload-root"></div>';
    if (window.YuuUpload && typeof window.YuuUpload.init === 'function') {
      window.YuuUpload.init(container.querySelector('#lqd-upload-root'));
    }
  }

  function unmount(container, tab) {}

  function getTitle(tab) {
    return tab.title || '上传';
  }

  function getIcon() {
    return 'upload';
  }

  function renderSidebar(container) {
    container.innerHTML =
      '<div class="lqd-sidebar-section-title">上传队列</div>' +
      '<div id="lqd-upload-sidebar-body"><div class="lqd-empty">暂无任务</div></div>';

    if (window.YuuUpload && typeof window.YuuUpload.renderQueue === 'function') {
      window.YuuUpload.renderQueue(container.querySelector('#lqd-upload-sidebar-body'));
    }
  }

  function renderOverview(container, tab) {
    container.innerHTML =
      '<div class="lqd-overview-section-title">上传</div>' +
      '<div class="lqd-empty">拖拽或选择文件开始入库</div>';
  }

  // P3-18: 对外暴露"接收拖入文件" — 聊天区拖拽文件时调用
  function addFiles(files) {
    if (!files || !files.length) return;
    if (window.YuuUpload && typeof window.YuuUpload.handleDropped === 'function') {
      window.YuuUpload.handleDropped(files);
      return;
    }
    // 兜底:打开上传页由用户手动选
    if (window.LqdTabs) {
      window.LqdTabs.open({ type: 'upload', title: '上传' });
    }
  }

  var LqdUpload = {
    type: 'upload',
    getTitle: getTitle,
    getIcon: getIcon,
    mount: mount,
    unmount: unmount,
    renderSidebar: renderSidebar,
    renderOverview: renderOverview,
    addFiles: addFiles
  };

  window.LqdUpload = LqdUpload;

  function tryRegister() {
    if (window.LqdTabs) window.LqdTabs.register('upload', LqdUpload);
    if (window.LqdSidebar) window.LqdSidebar.register('upload', LqdUpload);
    if (window.LqdOverview) window.LqdOverview.register('upload', LqdUpload);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', tryRegister);
  } else {
    tryRegister();
  }
})();
