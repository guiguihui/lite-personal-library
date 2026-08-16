/**
 * 轻量个人知识库 — Sidebar
 * 根据当前 Activity 调度左侧 Sidebar 内容。
 */
(function () {
  'use strict';

  function $(id) { return document.getElementById(id); }

  function getBody() {
    return $('lqd-sidebar-body');
  }

  function getHeader() {
    return $('lqd-sidebar-header');
  }

  function renderHeader(title, actionButtons) {
    var header = getHeader();
    if (!header) return;
    header.innerHTML = '';

    var titleEl = document.createElement('span');
    titleEl.textContent = title || '';
    header.appendChild(titleEl);

    if (actionButtons && actionButtons.length) {
      var actions = document.createElement('div');
      actions.className = 'lqd-sidebar-actions';
      actionButtons.forEach(function (btn) {
        var b = document.createElement('button');
        b.setAttribute('aria-label', btn.title || btn.label || '');
        b.innerHTML = window.LqdIcons ? window.LqdIcons.icon(btn.icon) : (btn.label || '');
        b.addEventListener('click', btn.onClick);
        actions.appendChild(b);
      });
      header.appendChild(actions);
    }
  }

  function renderForActivity(activity) {
    var body = getBody();
    var header = getHeader();
    if (!body || !header) return;

    body.innerHTML = '';

    var activeTab = window.LqdTabs ? window.LqdTabs.active() : null;
    var component = activeTab ? window.LqdTabs.getComponent(activeTab.type) : null;

    // 如果当前 Activity 有对应注册组件,优先让组件渲染 Sidebar
    var activityComponent = registry[activity];
    if (activityComponent && typeof activityComponent.renderSidebar === 'function') {
      activityComponent.renderSidebar(body);
      return;
    }

    // 否则让当前活动标签的组件渲染 Sidebar
    if (component && typeof component.renderSidebar === 'function') {
      component.renderSidebar(body);
      return;
    }

    body.innerHTML = '<div class="lqd-empty">暂无内容</div>';
  }

  var registry = {};

  function register(activity, component) {
    registry[activity] = component;
  }

  function refresh() {
    var activity = window.LqdStore ? window.LqdStore.get('activity') : 'chat';
    renderForActivity(activity);
  }

  function init() {
    window.LqdEvents.on('activity:changed', function (payload) {
      renderForActivity(payload.activity);
    });
    window.LqdEvents.on('tab:activated', function () {
      refresh();
    });
    window.LqdEvents.on('tab:opened', function () {
      refresh();
    });
    refresh();
  }

  window.LqdSidebar = {
    register: register,
    refresh: refresh,
    renderHeader: renderHeader,
    getBody: getBody,
    init: init
  };
})();
