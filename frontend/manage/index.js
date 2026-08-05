/**
 * LQ-D — Manage Tab Component
 *
 * 包装原有 manage.js 的索引构建与入库流水线面板。
 */
(function () {
  'use strict';

  function mount(container, tab) {
    container.innerHTML = '<div class="lqd-manage" id="manage-panel"></div>';
    if (typeof window.initManage === 'function') {
      window.initManage();
    }
    // M8: 恢复被暂停的轮询(若任务仍在运行)
    if (window.YuuManage && typeof window.YuuManage.resumePolls === 'function') {
      window.YuuManage.resumePolls();
    }
  }

  function unmount(container, tab) {
    // M8: 暂停轮询,避免切走后 setInterval 持续发悬空 fetch
    if (window.YuuManage && typeof window.YuuManage.pausePolls === 'function') {
      window.YuuManage.pausePolls();
    }
  }

  function getTitle(tab) {
    return tab.title || '索引管理';
  }

  function getIcon() {
    return 'manage';
  }

  function renderSidebar(container) {
    container.innerHTML =
      '<div class="lqd-sidebar-section-title">索引管理</div>' +
      '<div class="lqd-empty">在 Main 区触发构建或入库</div>';
  }

  function renderOverview(container, tab) {
    container.innerHTML =
      '<div class="lqd-overview-section-title">快捷操作</div>' +
      '<div class="lqd-overview-section-body">' +
        '<button class="lqd-btn lqd-btn--primary lqd-btn--block" id="lqd-manage-quick-build">全量构建</button>' +
      '</div>';

    var btn = container.querySelector('#lqd-manage-quick-build');
    if (btn) {
      btn.addEventListener('click', function () {
        if (window.YuuManage && typeof window.YuuManage.buildIndex === 'function') {
          window.YuuManage.buildIndex('full');
        }
        if (window.LqdTabs) {
          var active = window.LqdTabs.active();
          if (active && active.type === 'manage') window.LqdTabs.activate(active.id);
        }
      });
    }
  }

  var LqdManage = {
    type: 'manage',
    getTitle: getTitle,
    getIcon: getIcon,
    mount: mount,
    unmount: unmount,
    renderSidebar: renderSidebar,
    renderOverview: renderOverview
  };

  window.LqdManage = LqdManage;

  function tryRegister() {
    if (window.LqdTabs) window.LqdTabs.register('manage', LqdManage);
    if (window.LqdSidebar) window.LqdSidebar.register('manage', LqdManage);
    if (window.LqdOverview) window.LqdOverview.register('manage', LqdManage);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', tryRegister);
  } else {
    tryRegister();
  }
})();
