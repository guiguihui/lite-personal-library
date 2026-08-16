/**
 * 轻量个人知识库 — Overview
 * 右侧上下文面板,根据当前 Activity 和活动标签渲染。
 */
(function () {
  'use strict';

  function $(id) { return document.getElementById(id); }

  function getBody() {
    return $('lqd-overview-body');
  }

  function getHeader() {
    return $('lqd-overview-header');
  }

  function renderHeader(title) {
    var header = getHeader();
    if (!header) return;
    header.textContent = title || '上下文';
  }

  function renderForActivity(activity) {
    var body = getBody();
    var header = getHeader();
    if (!body || !header) return;

    body.innerHTML = '';

    var activeTab = window.LqdTabs ? window.LqdTabs.active() : null;
    var component = activeTab ? window.LqdTabs.getComponent(activeTab.type) : null;

    // 如果当前 Activity 有对应注册组件,优先让组件渲染 Overview
    var activityComponent = registry[activity];
    if (activityComponent && typeof activityComponent.renderOverview === 'function') {
      activityComponent.renderOverview(body, activeTab);
      return;
    }

    // 否则让当前活动标签的组件渲染 Overview
    if (component && typeof component.renderOverview === 'function') {
      component.renderOverview(body, activeTab);
      return;
    }

    body.innerHTML = '<div class="lqd-empty">选择标签以查看上下文</div>';
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

  window.LqdOverview = {
    register: register,
    refresh: refresh,
    renderHeader: renderHeader,
    getBody: getBody,
    init: init
  };
})();
