/**
 * LQ-D — Sidebar
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
    if (!body) return;

    body.innerHTML = '';

    // Codex 布局:侧栏固定为全局对话列表(chat 组件的 renderSidebar),
    // 不随当前视图切换内容。
    var chatComponent = registry.chat;
    if (chatComponent && typeof chatComponent.renderSidebar === 'function') {
      chatComponent.renderSidebar(body);
      return;
    }

    body.innerHTML = '<div class="lqd-empty">暂无对话</div>';
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
    // 会话归档/恢复/删除后刷新对话列表
    window.LqdEvents.on('chat:session:changed', function () {
      refresh();
    });
    window.LqdEvents.on('chat:history:changed', function () {
      refresh();
    });
    window.LqdEvents.on('chat:session:restored', function () {
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
