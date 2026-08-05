/**
 * LQ-D — Shell
 * 应用骨架:Activity Bar + Sidebar + Main(Tab Bar + Body) + Overview + Status Bar。
 * 负责初始化所有 core 模块并协调视图状态。
 */
(function () {
  'use strict';

  var ACTIVITIES = ['chat', 'library', 'manage', 'upload', 'config', 'filesearch'];
  // Codex 布局:侧栏为全局导航 + 对话列表,所有视图常驻;Overview 隐藏。
  var ACTIVITIES_WITH_SIDEBAR = ACTIVITIES.slice();
  var ACTIVITIES_WITH_OVERVIEW = [];
  var OVERVIEW_COLLAPSE_KEY_PREFIX = 'lqd-overview-collapsed:';
  var ACTIVITY_LABELS = {
    chat: '聊天',
    library: '文档库',
    manage: '索引管理',
    upload: '上传',
    config: '配置',
    filesearch: '本机检索'
  };
  // Codex 侧栏主导航(新对话为特殊动作,其余为视图切换)
  var SIDE_NAV = [
    { id: 'new-chat', label: '新对话', icon: 'edit' },
    { id: 'library', label: '文档库', icon: 'book' },
    { id: 'manage', label: '索引管理', icon: 'tools' },
    { id: 'upload', label: '上传', icon: 'upload' },
    { id: 'filesearch', label: '本机检索', icon: 'search' },
    { id: 'config', label: '配置', icon: 'config' }
  ];

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

    // M6 修复:侧栏切换按钮 — 此前 toggleSidebar 已暴露但无 UI 入口,
    // 非 chat activity 下侧栏永久折叠且不可达。
    var toggleSidebar = document.createElement('div');
    toggleSidebar.className = 'lqd-activity-icon';
    toggleSidebar.setAttribute('role', 'button');
    toggleSidebar.setAttribute('tabindex', '0');
    toggleSidebar.setAttribute('aria-label', '切换侧栏面板');
    toggleSidebar.setAttribute('aria-pressed', String(state.sidebarCollapsed));
    toggleSidebar.innerHTML = window.LqdIcons ? window.LqdIcons.icon(state.sidebarCollapsed ? 'expand' : 'collapse') : '';
    toggleSidebar.addEventListener('click', function () { toggleSidebarPanel(); });
    toggleSidebar.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        toggleSidebarPanel();
      }
    });
    container.appendChild(toggleSidebar);

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

  // ── Codex 侧栏:品牌行 / 主导航 / 底部状态 ─────────────────────────────
  function renderSidebarChrome() {
    renderTitlebar();
    renderSideBrand();
    renderSideNav();
    renderSideFooter();
  }

  // ── 顶栏:窗口控制 + 前进/后退 + 应用菜单 ─────────────────────────────
  var viewHistory = [];
  var viewFuture = [];
  var navigatingHistory = false;
  var openDropdown = null;

  function closeDropdown() {
    if (openDropdown) {
      openDropdown.remove();
      openDropdown = null;
      var open = state.els.titlebarMenus && state.els.titlebarMenus.querySelector('.lqd-tb-menu.open');
      if (open) open.classList.remove('open');
      document.removeEventListener('click', onDocClickCloseDropdown, true);
      document.removeEventListener('keydown', onEscCloseDropdown, true);
    }
  }

  function onDocClickCloseDropdown(e) {
    if (openDropdown && !openDropdown.contains(e.target) &&
        !(e.target.closest && e.target.closest('.lqd-tb-menu'))) {
      closeDropdown();
    }
  }

  function onEscCloseDropdown(e) {
    if (e.key === 'Escape') {
      e.stopPropagation();
      closeDropdown();
    }
  }

  function menuItems() {
    var themeMode = window.LqdTheme ? window.LqdTheme.get() : 'auto';
    var status = (window.LqdStore && window.LqdStore.get('status')) || {};
    return {
      'File': [
        { label: '新对话', shortcut: '', action: function () { if (window.LqdChat) window.LqdChat.openNewChat(); } },
        { label: '上传文档', action: function () { setActivity('upload'); } },
        { label: '配置', action: function () { setActivity('config'); } },
        { sep: true },
        { label: '关闭当前标签', shortcut: 'Ctrl+W', action: function () {
          var tab = window.LqdTabs ? window.LqdTabs.active() : null;
          if (tab) window.LqdTabs.close(tab.id);
        } }
      ],
      'Edit': [
        { label: '复制', shortcut: 'Ctrl+C', action: function () { document.execCommand('copy'); } },
        { label: '全选', shortcut: 'Ctrl+A', action: function () {
          var el = document.activeElement;
          if (el && (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT')) el.select();
          else document.execCommand('selectAll');
        } },
        { sep: true },
        { label: '清空当前对话', action: function () { if (window.LqdChat) window.LqdChat.openNewChat(); } }
      ],
      'View': [
        { label: '文档库', action: function () { setActivity('library'); } },
        { label: '索引管理', action: function () { setActivity('manage'); } },
        { label: '命令面板', shortcut: 'Ctrl+K', action: function () { if (window.LqdCommands) window.LqdCommands.toggle(); } },
        { sep: true },
        { label: '侧栏显示 / 隐藏', action: function () { toggleSidebarPanel(); } },
        { sep: true },
        { label: '主题:跟随系统', check: themeMode === 'auto', action: function () { window.LqdTheme.set('auto'); } },
        { label: '主题:浅色', check: themeMode === 'light', action: function () { window.LqdTheme.set('light'); } },
        { label: '主题:深色', check: themeMode === 'dark', action: function () { window.LqdTheme.set('dark'); } }
      ],
      'Help': [
        { label: '关于 LQ-D', action: function () {
          if (window.LqdModal) {
            window.LqdModal.alert({
              title: '关于 LQ-D',
              message: 'LQ-D v' + (status.version || '0.1.0') + ' · 个人数字图书馆' + (status.provider ? '<br>' + status.provider + ' / ' + (status.model || '') : '')
            });
          }
        } },
        { label: '快捷键', action: function () {
          if (window.LqdModal) {
            window.LqdModal.alert({
              title: '快捷键',
              message: 'Ctrl+K 命令面板<br>Ctrl+W 关闭标签<br>Enter 发送 · Shift+Enter 换行'
            });
          }
        } }
      ]
    };
  }

  function openMenuDropdown(menuBtn, name) {
    closeDropdown();
    menuBtn.classList.add('open');
    var items = menuItems()[name] || [];
    var dd = document.createElement('div');
    dd.className = 'lqd-tb-dropdown';
    dd.setAttribute('role', 'menu');

    items.forEach(function (item) {
      if (item.sep) {
        var sep = document.createElement('div');
        sep.className = 'lqd-tb-drop-sep';
        dd.appendChild(sep);
        return;
      }
      var btn = document.createElement('button');
      btn.className = 'lqd-tb-drop-item';
      btn.setAttribute('role', 'menuitem');
      btn.innerHTML =
        '<span class="lqd-tb-drop-check">' + (item.check && window.LqdIcons ? window.LqdIcons.icon('check') : '') + '</span>' +
        '<span>' + item.label + '</span>' +
        (item.shortcut ? '<span class="lqd-tb-drop-shortcut">' + item.shortcut + '</span>' : '');
      btn.addEventListener('click', function () {
        closeDropdown();
        item.action();
      });
      dd.appendChild(btn);
    });

    document.body.appendChild(dd);
    var r = menuBtn.getBoundingClientRect();
    dd.style.top = (r.bottom + 4) + 'px';
    dd.style.left = Math.max(4, r.left) + 'px';
    openDropdown = dd;
    document.addEventListener('click', onDocClickCloseDropdown, true);
    document.addEventListener('keydown', onEscCloseDropdown, true);
  }

  function syncHistoryButtons() {
    if (state.els.tbBack) state.els.tbBack.disabled = viewHistory.length === 0;
    if (state.els.tbForward) state.els.tbForward.disabled = viewFuture.length === 0;
  }

  function renderTitlebar() {
    var left = state.els.titlebarLeft;
    var menus = state.els.titlebarMenus;
    var right = state.els.titlebarRight;
    if (!left || !menus || !right) return;
    left.innerHTML = '';
    menus.innerHTML = '';
    right.innerHTML = '';

    function tbBtn(iconName, label, onClick) {
      var b = document.createElement('button');
      b.className = 'lqd-tb-btn';
      b.setAttribute('aria-label', label);
      b.innerHTML = window.LqdIcons ? window.LqdIcons.icon(iconName) : '';
      b.addEventListener('click', onClick);
      if (window.LqdTooltip) window.LqdTooltip.attach(b, { text: label, position: 'bottom' });
      left.appendChild(b);
      return b;
    }

    tbBtn('panel-left', '显示 / 隐藏侧栏', function () { toggleSidebarPanel(); });
    state.els.tbBack = tbBtn('arrow-left', '后退', function () {
      if (!viewHistory.length) return;
      var current = window.LqdStore.get('activity');
      viewFuture.push(current);
      var prev = viewHistory.pop();
      navigatingHistory = true;
      setActivity(prev);
      navigatingHistory = false;
      syncHistoryButtons();
    });
    state.els.tbForward = tbBtn('arrow-right', '前进', function () {
      if (!viewFuture.length) return;
      var current = window.LqdStore.get('activity');
      viewHistory.push(current);
      var next = viewFuture.pop();
      navigatingHistory = true;
      setActivity(next);
      navigatingHistory = false;
      syncHistoryButtons();
    });
    syncHistoryButtons();

    ['File', 'Edit', 'View', 'Help'].forEach(function (name) {
      var m = document.createElement('button');
      m.className = 'lqd-tb-menu';
      m.textContent = name;
      m.setAttribute('aria-haspopup', 'menu');
      m.addEventListener('click', function (e) {
        e.stopPropagation();
        if (m.classList.contains('open')) closeDropdown();
        else openMenuDropdown(m, name);
      });
      menus.appendChild(m);
    });

    // 窗口控制按钮:pywebview api 就绪后立即渲染;未就绪则等 pywebviewready 事件补渲。
    // 修复:api 方法在 before_load 后注入,DOMContentLoaded 时分支可能为 false 导致按钮缺失。
    function renderWindowControls() {
      if (!right || right.children.length) return;
      if (!(window.pywebview && window.pywebview.api)) return;
      [['minimize', '最小化', 'minimize'], ['maximize', '最大化', 'toggle_maximized'], ['close', '关闭', 'close']].forEach(function (cfg) {
        var b = document.createElement('button');
        b.className = 'lqd-tb-btn lqd-tb-win';
        b.setAttribute('aria-label', cfg[1]);
        b.innerHTML = window.LqdIcons ? window.LqdIcons.icon(cfg[0]) : '';
        b.addEventListener('click', function () {
          try {
            var api = window.pywebview.api;
            if (typeof api[cfg[2]] === 'function') api[cfg[2]]();
          } catch (e) { /* 忽略 */ }
        });
        right.appendChild(b);
      });
    }
    renderWindowControls();
    if (!right.children.length) {
      document.addEventListener('pywebviewready', renderWindowControls, { once: true });
      // 兜底:api 方法注入时机因版本而异,短轮询最多 5s 内补齐
      var tries = 0;
      var timer = setInterval(function () {
        renderWindowControls();
        if (right.children.length || ++tries >= 10) clearInterval(timer);
      }, 500);
    }

    // frameless 窗口拖动 + 双击最大化。
    // 注:-webkit-app-region: drag 只对 Electron 生效,pywebview 需经 JS Bridge
    // 调 Win32(WM_NCLBUTTONDOWN + HTCAPTION)让系统接管拖动。
    var titlebar = state.els.titlebarLeft && state.els.titlebarLeft.parentElement;
    if (titlebar && !titlebar._dragBound) {
      titlebar._dragBound = true;
      titlebar.addEventListener('mousedown', function (e) {
        // 只响应左键,且不拦截按钮/菜单/输入上的点击
        if (e.button !== 0) return;
        if (e.target.closest('button, input, textarea, select, a')) return;
        try {
          var api = window.pywebview && window.pywebview.api;
          if (api && typeof api.drag_window === 'function') {
            // 系统级拖动:Windows 原生模态循环接管,跟手无卡顿
            api.drag_window();
          }
        } catch (err) { /* 忽略 */ }
      });
      titlebar.addEventListener('dblclick', function (e) {
        if (e.target.closest('button')) return;
        try {
          var api = window.pywebview && window.pywebview.api;
          if (api && typeof api.toggle_maximized === 'function') api.toggle_maximized();
        } catch (err) { /* 忽略 */ }
      });
    }
  }

  function renderSideBrand() {
    var el = state.els.sideBrand;
    if (!el) return;
    el.innerHTML =
      '<button class="lqd-side-brand-name" aria-label="LQ-D 知识库" title="LQ-D 个人知识库">' +
        '<span class="lqd-brand-logo">' + (window.LqdIcons ? window.LqdIcons.icon('logo') : '') + '</span>' +
        '<span class="lqd-brand-text">LQ-D</span>' +
      '</button>';
    var search = document.createElement('button');
    search.className = 'lqd-side-brand-search';
    search.setAttribute('aria-label', '搜索或命令 (Ctrl+K)');
    search.innerHTML = window.LqdIcons ? window.LqdIcons.icon('search') : '';
    search.addEventListener('click', function (e) {
      // 阻止冒泡:command-palette 的 document 级"点击外部关闭"监听
      // 会在同一次点击里立即把刚打开的面板关掉。
      e.stopPropagation();
      if (window.LqdCommands && typeof window.LqdCommands.toggle === 'function') {
        window.LqdCommands.toggle();
      }
    });
    if (window.LqdTooltip) window.LqdTooltip.attach(search, { text: '搜索 / 命令面板 (Ctrl+K)', position: 'bottom' });
    el.appendChild(search);
  }

  function renderSideNav() {
    var nav = state.els.sideNav;
    if (!nav) return;
    nav.innerHTML = '';
    var activity = window.LqdStore ? window.LqdStore.get('activity') : 'chat';

    SIDE_NAV.forEach(function (item) {
      var btn = document.createElement('button');
      btn.className = 'lqd-side-nav-item';
      if (item.id !== 'new-chat' && item.id === activity) btn.classList.add('active');
      if (item.id === 'new-chat' && activity === 'chat') btn.classList.add('active');
      btn.setAttribute('data-nav-id', item.id);
      btn.innerHTML =
        (window.LqdIcons ? window.LqdIcons.icon(item.icon) : '') +
        '<span>' + item.label + '</span>';
      btn.addEventListener('click', function () {
        if (item.id === 'new-chat') {
          if (window.LqdChat && typeof window.LqdChat.openNewChat === 'function') {
            window.LqdChat.openNewChat();
          } else {
            setActivity('chat');
          }
        } else {
          setActivity(item.id);
        }
      });
      nav.appendChild(btn);
    });
  }

  function syncSideNavActive() {
    var nav = state.els.sideNav;
    if (!nav) return;
    var activity = window.LqdStore ? window.LqdStore.get('activity') : 'chat';
    nav.querySelectorAll('.lqd-side-nav-item').forEach(function (btn) {
      var id = btn.getAttribute('data-nav-id');
      var active = (id === 'new-chat' && activity === 'chat') || id === activity;
      btn.classList.toggle('active', active);
    });
  }

  function renderSideFooter() {
    var el = state.els.sideFooter;
    if (!el) return;
    var status = (window.LqdStore && window.LqdStore.get('status')) || {};
    var fetchError = window.LqdStore ? window.LqdStore.get('statusFetchError') : null;

    var dotClass = 'lqd-side-footer-dot';
    var text = '索引未构建';
    if (fetchError) {
      dotClass += ' lqd-side-footer-dot--failed';
      text = '状态获取失败';
    } else if (status.indexRunning) {
      dotClass += ' lqd-side-footer-dot--running';
      text = '索引构建中';
    } else if (status.indexReady) {
      dotClass += ' lqd-side-footer-dot--ready';
      text = '索引就绪';
    } else if (status.ingestRunning) {
      dotClass += ' lqd-side-footer-dot--running';
      text = '入库中';
    }
    if (status.provider) {
      text += ' · ' + status.provider + (status.model ? ' / ' + status.model : '');
    }

    var themeIcon = 'monitor';
    var themeMode = window.LqdTheme ? window.LqdTheme.get() : 'auto';
    if (themeMode === 'light') themeIcon = 'sun';
    else if (themeMode === 'dark') themeIcon = 'moon';

    el.innerHTML =
      '<button class="lqd-side-footer-gear" aria-label="配置">' +
        (window.LqdIcons ? window.LqdIcons.icon('config') : '') +
      '</button>' +
      '<div class="lqd-side-footer-status">' +
        '<span class="' + dotClass + '"></span>' +
        '<span></span>' +
      '</div>';
    el.querySelector('.lqd-side-footer-status span:last-child').textContent = text;
    var gear = el.querySelector('.lqd-side-footer-gear');
    gear.addEventListener('click', function () { setActivity('config'); });
    if (window.LqdTooltip) window.LqdTooltip.attach(gear, { text: '配置', position: 'top' });

    var themeBtn = document.createElement('button');
    themeBtn.className = 'lqd-side-footer-theme';
    themeBtn.setAttribute('aria-label', '切换主题');
    themeBtn.innerHTML = window.LqdIcons ? window.LqdIcons.icon(themeIcon) : '';
    themeBtn.addEventListener('click', function () {
      var modes = ['auto', 'light', 'dark'];
      var current = window.LqdTheme ? window.LqdTheme.get() : 'auto';
      var next = modes[(modes.indexOf(current) + 1) % modes.length];
      if (window.LqdTheme) window.LqdTheme.set(next);
    });
    if (window.LqdTooltip) window.LqdTooltip.attach(themeBtn, { text: '切换主题', position: 'top' });
    el.appendChild(themeBtn);
  }

  // ── Activity 切换 ──────────────────────────────────────────────────────
  function setActivity(activity) {
    if (ACTIVITIES.indexOf(activity) === -1) return;

    var current = window.LqdStore ? window.LqdStore.get('activity') : 'chat';
    if (activity === current) return;

    // 前进/后退历史(历史导航自身不再入栈)
    if (!navigatingHistory) {
      viewHistory.push(current);
      if (viewHistory.length > 50) viewHistory.shift();
      viewFuture = [];
      syncHistoryButtons();
    }

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

  function toggleSidebar() {
    state.sidebarCollapsed = !state.sidebarCollapsed;
    if (state.els.shell) {
      state.els.shell.setAttribute('data-sidebar-collapsed', state.sidebarCollapsed);
    }
  }

  // M6:侧栏切换面板(含 UI 刷新,与 toggleOverviewPanel 对称)
  function toggleSidebarPanel() {
    state.sidebarCollapsed = !state.sidebarCollapsed;
    if (state.els.shell) {
      state.els.shell.setAttribute('data-sidebar-collapsed', String(state.sidebarCollapsed));
    }
    renderActivityBar();
    // 侧栏显隐变化后重新渲染内容(组件可能需要适应宽度)
    if (window.LqdSidebar) window.LqdSidebar.refresh();
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

  // ── 侧栏拖拽伸缩 ──────────────────────────────────────────────────────
  var RESIZE_KEY = 'lqd-sidebar-width';
  var MIN_WIDTH = 180;
  var MAX_WIDTH = 500;
  var DEFAULT_WIDTH = 260;

  function initSidebarResize() {
    var handle = document.getElementById('lqd-resize-handle');
    if (!handle) {
      console.warn('[Shell] resize handle not found in DOM');
      return;
    }
    console.log('[Shell] resize handle found, init drag');

    // 恢复保存的宽度
    var saved = localStorage.getItem(RESIZE_KEY);
    if (saved) {
      var w = parseInt(saved, 10);
      if (w >= MIN_WIDTH && w <= MAX_WIDTH) {
        setSidebarWidth(w);
      }
    }

    var dragging = false;
    var startX = 0;
    var startW = 0;

    handle.addEventListener('mousedown', function (e) {
      e.preventDefault();
      dragging = true;
      startX = e.clientX;
      startW = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--sidebar-width'));
      handle.classList.add('resizing');
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';
    });

    document.addEventListener('mousemove', function (e) {
      if (!dragging) return;
      var delta = e.clientX - startX;
      var newW = Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, startW + delta));
      setSidebarWidth(newW);
    });

    document.addEventListener('mouseup', function () {
      if (!dragging) return;
      dragging = false;
      handle.classList.remove('resizing');
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      // 持久化到 localStorage
      var currentW = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--sidebar-width'));
      try { localStorage.setItem(RESIZE_KEY, String(Math.round(currentW))); } catch (_) {}
    });
  }

  function setSidebarWidth(w) {
    document.documentElement.style.setProperty('--sidebar-width', w + 'px');
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
    state.els.titlebarLeft = $('lqd-titlebar-left');
    state.els.titlebarMenus = $('lqd-titlebar-menus');
    state.els.titlebarRight = $('lqd-titlebar-right');
    state.els.sidebar = $('lqd-sidebar');
    state.els.sideBrand = $('lqd-side-brand');
    state.els.sideNav = $('lqd-side-nav');
    state.els.sideFooter = $('lqd-side-footer');
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

    // 侧栏拖拽伸缩
    initSidebarResize();

    // P3-17: 全局快捷键 Ctrl+Shift+F → 打开全局搜索
    document.addEventListener('keydown', function (e) {
      if ((e.ctrlKey || e.metaKey) && e.shiftKey && (e.key === 'F' || e.key === 'f')) {
        e.preventDefault();
        if (window.LqdSearch && typeof window.LqdSearch.open === 'function') {
          window.LqdSearch.open('');
        }
      }
    });

    // 渲染 Activity Bar(隐藏)与 Codex 侧栏
    renderActivityBar();
    renderSidebarChrome();

    // 设置初始 Sidebar/Overview 标题,并根据初始 activity 同步两侧面板显隐
    var initialActivity = window.LqdStore ? window.LqdStore.get('activity') : 'chat';
    var initialTab = window.LqdTabs ? window.LqdTabs.active() : null;
    var initialComponent = initialTab ? window.LqdTabs.getComponent(initialTab.type) : null;
    var initialActions = [];
    if (initialComponent && typeof initialComponent.getSidebarActions === 'function') {
      initialActions = initialComponent.getSidebarActions();
    }
    if (window.LqdSidebar) window.LqdSidebar.renderHeader(ACTIVITY_LABELS[initialActivity], initialActions);
    if (window.LqdOverview) window.LqdOverview.renderHeader(ACTIVITY_LABELS[initialActivity]);
    syncPanelsForActivity(initialActivity);

    // 监听 store 变化以重新渲染 Activity Bar 与侧栏状态
    if (window.LqdStore && typeof window.LqdStore.subscribe === 'function') {
      window.LqdStore.subscribe('activity', function () {
        renderActivityBar();
        syncSideNavActive();
      });
      window.LqdStore.subscribe('status', function () {
        renderSideFooter();
      });
      window.LqdStore.subscribe('statusFetchError', function () {
        renderSideFooter();
      });
    }
    if (window.LqdEvents) {
      window.LqdEvents.on('theme:changed', function () {
        renderSideFooter();
      });
      window.LqdEvents.on('settings:loaded', function () {
        renderSideFooter();
      });
    }

    // 监听标签切换,让 activity 与活动标签保持一致(用户点击 tab 时也能更新侧栏)
    if (window.LqdEvents) {
      function syncActivityFromTab(tab) {
        if (!tab || !tab.type) return;
        var currentActivity = window.LqdStore ? window.LqdStore.get('activity') : 'chat';
        if (tab.type !== currentActivity && ACTIVITIES.indexOf(tab.type) !== -1) {
          setActivity(tab.type);
        }
      }
      window.LqdEvents.on('tab:activated', function (payload) { syncActivityFromTab(payload && payload.tab); });
      window.LqdEvents.on('tab:opened', function (payload) { syncActivityFromTab(payload && payload.tab); });
    }

    state.initialized = true;
  }

  // ── 暴露 API ───────────────────────────────────────────────────────────
  window.LqdShell = {
    init: init,
    setActivity: setActivity,
    toggleSidebar: toggleSidebarPanel,
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
