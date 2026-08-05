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

  var LqdUpload = {
    type: 'upload',
    getTitle: getTitle,
    getIcon: getIcon,
    mount: mount,
    unmount: unmount,
    renderSidebar: renderSidebar,
    renderOverview: renderOverview
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
