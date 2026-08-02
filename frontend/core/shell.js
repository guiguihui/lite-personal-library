/**
 * LQ-D — Shell
 * 应用骨架:Activity Bar + Sidebar + Main(Tab Bar + Body) + Overview + Status Bar。
 * 负责初始化所有 core 模块并协调视图状态。
 */
(function () {
  'use strict';

  var ACTIVITIES = ['chat', 'library', 'manage', 'upload', 'config'];
  // 只有聊天页需要左侧历史会话面板;Overview 默认折叠(不挤占主区),
  // 用户可通过 Activity Bar 底部的切换按钮手动展开。
  var ACTIVITIES_WITH_SIDEBAR = ['chat'];
  var ACTIVITIES_WITH_OVERVIEW = [];
  var OVERVIEW_COLLAPSE_KEY_PREFIX = 'lqd-overview-collapsed:';
  var ACTIVITY_LABELS = {
    chat: '聊天',
    library: '文档库',
    manage: '索引管理',
    upload: '上传',
    config: '配置'
  };

  var state = {
    initialized: false,
    sidebarCollapsed: false,
    overviewCollapsed: false,
    els: {}
  };

  function $(id) { return document.getElementById(id); }

  // ── Activity Bar 渲染 ──────────────────────────────────────────────────
  function renderActivityBar() {
    var container = state.els.activityBar;
    if (!container) return;
    container.innerHTML = '';

    var activity = window.LqdStore ? window.LqdStore.get('activity') : 'chat';

    ACTIVITIES.forEach(function (a) {
      var icon = document.createElement('div');
      icon.className = 'lqd-activity-icon' + (a === activity ? ' active' : '');
      icon.setAttribute('data-activity', a);
      icon.setAttribute('role', 'button');
      icon.setAttribute('tabindex', '0');
      icon.setAttribute('aria-label', ACTIVITY_LABELS[a]);
      icon.setAttribute('aria-pressed', String(a === activity));
      icon.innerHTML = window.LqdIcons ? window.LqdIcons.icon(a) : '';
      icon.addEventListener('click', function () { setActivity(a); });
      icon.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          setActivity(a);
        }
      });
      container.appendChild(icon);
    });

    var spacer = document.createElement('div');
    spacer.className = 'lqd-activity-spacer';
    container.appendChild(spacer);

    var toggleOverview = document.createElement('div');
    toggleOverview.className = 'lqd-activity-icon';
    toggleOverview.setAttribute('role', 'button');
    toggleOverview.setAttribute('tabindex', '0');
    toggleOverview.setAttribute('aria-label', '切换概览面板');
    toggleOverview.setAttribute('aria-pressed', String(state.overviewCollapsed));
    toggleOverview.innerHTML = window.LqdIcons ? window.LqdIcons.icon(state.overviewCollapsed ? 'expand' : 'collapse') : '';
    toggleOverview.addEventListener('click', function () { toggleOverviewPanel(); });
    toggleOverview.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        toggleOverviewPanel();
      }
    });
    container.appendChild(toggleOverview);
  }

  // ── Activity 切换 ──────────────────────────────────────────────────────
  function setActivity(activity) {
    if (ACTIVITIES.indexOf(activity) === -1) return;

    var current = window.LqdStore ? window.LqdStore.get('activity') : 'chat';
    if (activity === current) return;

    window.LqdStore.set('activity', activity);

    // 更新 Activity Bar 高亮
    var icons = state.els.activityBar.querySelectorAll('.lqd-activity-icon[data-activity]');
    icons.forEach(function (icon) {
      icon.classList.toggle('active', icon.getAttribute('data-activity') === activity);
    });

    // 触发 Sidebar/Overview 重新渲染
    window.LqdEvents.emit('activity:changed', { activity: activity, previous: current });

    // 打开或切换到对应类型的标签(先切标签,再基于新标签更新面板标题)
    if (window.LqdTabs) {
      var tabs = window.LqdTabs.list();
      var existing = null;
      for (var i = 0; i < tabs.length; i++) {
        if (tabs[i].type === activity) { existing = tabs[i]; break; }
      }
      if (existing) {
        window.LqdTabs.activate(existing.id);
      } else {
        var titleMap = {
          chat: '新对话',
          library: '文档库',
          manage: '索引管理',
          upload: '上传',
          config: '配置'
        };
        window.LqdTabs.open({ type: activity, title: titleMap[activity] || activity });
      }
    }

    // 更新 Sidebar / Overview 标题(基于切换后的活动标签)
    var activeTab = window.LqdTabs ? window.LqdTabs.active() : null;
    var component = activeTab ? window.LqdTabs.getComponent(activeTab.type) : null;
    var sidebarActions = [];
    if (component && typeof component.getSidebarActions === 'function') {
      sidebarActions = component.getSidebarActions();
    }
    if (window.LqdSidebar) window.LqdSidebar.renderHeader(ACTIVITY_LABELS[activity], sidebarActions);
    if (window.LqdOverview) window.LqdOverview.renderHeader(ACTIVITY_LABELS[activity]);

    // 只有聊天页需要两侧面板;其他页面折叠 Sidebar/Overview,让主区占满
    syncPanelsForActivity(activity);
  }

  function reflectActiveTab(tab) {
    if (!tab || ACTIVITIES.indexOf(tab.type) === -1) return;
    var activity = tab.type;
    var current = window.LqdStore ? window.LqdStore.get('activity') : 'chat';
    if (activity !== current) {
      window.LqdStore.set('activity', activity);
      var icons = state.els.activityBar.querySelectorAll('.lqd-activity-icon[data-activity]');
      icons.forEach(function (icon) {
        icon.classList.toggle('active', icon.getAttribute('data-activity') === activity);
      });
      window.LqdEvents.emit('activity:changed', { activity: activity, previous: current });
    }
    var component = window.LqdTabs ? window.LqdTabs.getComponent(tab.type) : null;
    var sidebarActions = component && typeof component.getSidebarActions === 'function' ? component.getSidebarActions() : [];
    if (window.LqdSidebar) window.LqdSidebar.renderHeader(ACTIVITY_LABELS[activity], sidebarActions);
    if (window.LqdOverview) window.LqdOverview.renderHeader(ACTIVITY_LABELS[activity]);
    syncPanelsForActivity(activity);
  }
  function toggleSidebar() {
    state.sidebarCollapsed = !state.sidebarCollapsed;
    if (state.els.shell) {
      state.els.shell.setAttribute('data-sidebar-collapsed', state.sidebarCollapsed);
    }
  }

  function toggleOverviewPanel() {
    state.overviewCollapsed = !state.overviewCollapsed;
    if (state.els.shell) {
      state.els.shell.setAttribute('data-overview-collapsed', state.overviewCollapsed);
    }
    // 持久化当前 activity 的折叠偏好
    var activity = window.LqdStore ? window.LqdStore.get('activity') : 'chat';
    try {
      localStorage.setItem(OVERVIEW_COLLAPSE_KEY_PREFIX + activity, String(state.overviewCollapsed));
    } catch (e) { /* localStorage 不可用时静默 */ }
    renderActivityBar();
  }

  function readOverviewPreference(activity) {
    try {
      var v = localStorage.getItem(OVERVIEW_COLLAPSE_KEY_PREFIX + activity);
      if (v === 'true') return true;
      if (v === 'false') return false;
    } catch (e) { /* 忽略 */ }
    return null; // 未设置偏好
  }

  function syncPanelsForActivity(activity) {
    var needsSidebar = ACTIVITIES_WITH_SIDEBAR.indexOf(activity) !== -1;
    var needsOverview = ACTIVITIES_WITH_OVERVIEW.indexOf(activity) !== -1;

    state.sidebarCollapsed = !needsSidebar;
    // Overview:默认折叠(不挤占主区);仅当该 activity 支持且用户曾手动展开时才展开
    if (needsOverview) {
      var pref = readOverviewPreference(activity);
      state.overviewCollapsed = (pref === null) ? false : pref;
    } else {
      // 不在支持列表:尊重用户手动偏好(若曾展开过),否则折叠
      var pref2 = readOverviewPreference(activity);
      state.overviewCollapsed = (pref2 === null) ? true : pref2;
    }

    if (state.els.shell) {
      state.els.shell.setAttribute('data-sidebar-collapsed', String(!needsSidebar));
      state.els.shell.setAttribute('data-overview-collapsed', String(state.overviewCollapsed));
    }
    renderActivityBar();
  }

  // ── 初始化 ────────────────────────────────────────────────────────────
  function init() {
    if (state.initialized) return;

    var shell = $('lqd-shell');
    if (!shell) return;

    state.els.shell = shell;
    state.els.activityBar = $('lqd-activity-bar');
    state.els.sidebar = $('lqd-sidebar');
    state.els.sidebarHeader = $('lqd-sidebar-header');
    state.els.sidebarBody = $('lqd-sidebar-body');
    state.els.main = $('lqd-main');
    state.els.tabBar = $('lqd-tab-bar');
    state.els.mainBody = $('lqd-main-body');
    state.els.overview = $('lqd-overview');
    state.els.overviewHeader = $('lqd-overview-header');
    state.els.overviewBody = $('lqd-overview-body');
    state.els.statusBar = $('lqd-status-bar');

    // 主题初始化(在 theme.js 已做,这里再确保一次)
    if (window.LqdTheme) window.LqdTheme.init();

    // 初始化各 core 模块
    if (window.LqdEvents) { /* events 无 init */ }
    if (window.LqdStore) { /* store 无 init */ }
    if (window.LqdIcons) { /* icons 无 init */ }
    if (window.LqdSidebar) window.LqdSidebar.init();
    if (window.LqdOverview) window.LqdOverview.init();
    if (window.LqdStatusBar) window.LqdStatusBar.init();
    if (window.LqdCommands) window.LqdCommands.init();
    if (window.LqdTabs) window.LqdTabs.init();

    // 渲染 Activity Bar
    renderActivityBar();

    // 设置初始 Sidebar/Overview 标题,并根据初始 activity 同步两侧面板显隐
    var initialActivity = window.LqdStore ? window.LqdStore.get('activity') : 'chat';
    var initialTab = window.LqdTabs ? window.LqdTabs.active() : null;
    if (initialTab && ACTIVITIES.indexOf(initialTab.type) !== -1 && initialActivity !== initialTab.type) {
      initialActivity = initialTab.type;
      if (window.LqdStore) window.LqdStore.set('activity', initialActivity);
      renderActivityBar();
    }
    var initialComponent = initialTab ? window.LqdTabs.getComponent(initialTab.type) : null;
    var initialActions = [];
    if (initialComponent && typeof initialComponent.getSidebarActions === 'function') {
      initialActions = initialComponent.getSidebarActions();
    }
    if (window.LqdSidebar) window.LqdSidebar.renderHeader(ACTIVITY_LABELS[initialActivity], initialActions);
    if (window.LqdOverview) window.LqdOverview.renderHeader(ACTIVITY_LABELS[initialActivity]);
    syncPanelsForActivity(initialActivity);

    // 监听 store 变化以重新渲染 Activity Bar
    if (window.LqdStore && typeof window.LqdStore.subscribe === 'function') {
      window.LqdStore.subscribe('activity', function () {
        renderActivityBar();
      });
    }

    // 监听标签切换,让 activity 与活动标签保持一致(用户点击 tab 时也能更新侧栏)
    if (window.LqdEvents) {
      window.LqdEvents.on('tab:activated', function (payload) { reflectActiveTab(payload && payload.tab); });
      window.LqdEvents.on('tab:opened', function (payload) { reflectActiveTab(payload && payload.tab); });
    }

    state.initialized = true;
  }

  // ── 暴露 API ───────────────────────────────────────────────────────────
  window.LqdShell = {
    init: init,
    setActivity: setActivity,
    reflectActiveTab: reflectActiveTab,
    toggleSidebar: toggleSidebar,
    toggleOverview: toggleOverviewPanel,
    getActivity: function () { return window.LqdStore ? window.LqdStore.get('activity') : 'chat'; },
    getSidebarBody: function () { return state.els.sidebarBody; },
    getMainBody: function () { return state.els.mainBody; },
    getOverviewBody: function () { return state.els.overviewBody; },
    refreshSidebar: function () {
      if (window.LqdSidebar) window.LqdSidebar.refresh();
    },
    refreshOverview: function () {
      if (window.LqdOverview) window.LqdOverview.refresh();
    }
  };

  document.addEventListener('DOMContentLoaded', init);
})();
