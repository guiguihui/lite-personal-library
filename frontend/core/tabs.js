/**
 * LQ-D — Tabs
 * 多标签页管理器。所有可标签化视图通过 register() 注册组件。
 */
(function () {
  'use strict';

  var registry = {};
  var nextId = 1;
  var currentMountedId = null; // 防止重复挂载同一标签
  var closedTabsStack = [];
  var CLOSED_STACK_MAX = 20;

  function $(id) { return document.getElementById(id); }

  function getContainer() {
    return $('lqd-main-body');
  }

  function getBar() {
    return $('lqd-tab-bar');
  }

  function generateId() {
    return 'tab-' + (nextId++);
  }

  function getTabs() {
    return window.LqdStore.get('tabs') || [];
  }

  function setTabs(tabs) {
    window.LqdStore.set('tabs', tabs);
  }

  function getActiveId() {
    return window.LqdStore.get('activeTabId');
  }

  function setActiveId(id) {
    window.LqdStore.set('activeTabId', id);
  }

  function findTab(id) {
    var tabs = getTabs();
    for (var i = 0; i < tabs.length; i++) {
      if (tabs[i].id === id) return tabs[i];
    }
    return null;
  }

  function findTabIndex(id) {
    var tabs = getTabs();
    for (var i = 0; i < tabs.length; i++) {
      if (tabs[i].id === id) return i;
    }
    return -1;
  }

  function register(type, component) {
    if (!type || !component) return;
    registry[type] = component;
  }

  function getComponent(type) {
    return registry[type];
  }

  function renderBar() {
    var bar = getBar();
    if (!bar) return;
    bar.innerHTML = '';
    var tabs = getTabs();
    var activeId = getActiveId();

    tabs.forEach(function (tab) {
      var comp = getComponent(tab.type);
      if (!comp) return;

      var el = document.createElement('div');
      el.className = 'lqd-tab' + (tab.id === activeId ? ' active' : '');
      el.setAttribute('data-tab-id', tab.id);
      el.setAttribute('aria-label', tab.title || '');
      el.setAttribute('role', 'tab');
      el.setAttribute('tabindex', '0');
      el.setAttribute('aria-selected', tab.id === activeId ? 'true' : 'false');
      if (window.LqdTooltip && tab.title) {
        window.LqdTooltip.attach(el, { text: tab.title, position: 'bottom' });
      }

      var iconName = typeof comp.getIcon === 'function' ? comp.getIcon(tab) : tab.type;
      var iconHtml = window.LqdIcons ? window.LqdIcons.icon(iconName) : '';

      var titleEl = document.createElement('span');
      titleEl.className = 'lqd-tab-title';
      titleEl.textContent = (typeof comp.getTitle === 'function' ? comp.getTitle(tab) : tab.title) || '未命名';

      var closeBtn = document.createElement('button');
      closeBtn.className = 'lqd-tab-close';
      closeBtn.setAttribute('aria-label', '关闭标签');
      closeBtn.innerHTML = window.LqdIcons ? window.LqdIcons.icon('close') : '×';
      closeBtn.addEventListener('click', function (e) {
        e.stopPropagation();
        close(tab.id);
      });

      if (iconHtml) {
        var iconWrap = document.createElement('span');
        iconWrap.className = 'lqd-tab-icon';
        iconWrap.innerHTML = iconHtml;
        el.appendChild(iconWrap);
      }
      el.appendChild(titleEl);
      el.appendChild(closeBtn);

      el.addEventListener('click', function () { activate(tab.id); });
      el.addEventListener('mousedown', function (e) {
        if (e.button === 1) {
          e.preventDefault();
          close(tab.id);
        }
      });
      // 右键菜单(工作流 F)
      if (window.LqdTabContextMenu) {
        window.LqdTabContextMenu.attach(el, tab.id, tab.type);
      }
      // 键盘:Enter/Space 激活(工作流 H 的 a11y 配套)
      el.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          activate(tab.id);
        }
      });

      bar.appendChild(el);
    });
  }

  function mountTab(tab) {
    var comp = getComponent(tab.type);
    if (!comp || typeof comp.mount !== 'function') {
      if (window.console) window.console.error('[LqdTabs] mountTab no component for', tab.type);
      return;
    }

    var container = getContainer();
    if (!container) return;

    // 防止重复挂载:同一标签已挂载则跳过
    if (currentMountedId === tab.id) return;

    // 先卸载当前显示的标签(如果有)
    var currentId = getActiveId();
    if (currentId && currentId !== tab.id) {
      var currentTab = findTab(currentId);
      if (currentTab) unmountTab(currentTab, false);
    }

    container.innerHTML = '';
    try {
      comp.mount(container, tab);
      currentMountedId = tab.id;
    } catch (e) {
      var errMsg = e && e.message ? e.message : String(e);
      if (window.console && window.console.error) {
        window.console.error('[LqdTabs] mount error for', tab.type, tab.id, e);
      }
      container.innerHTML = '<div class="lqd-empty">标签加载失败<br><small style="color:var(--fg-tertiary)">' + tab.type + ': ' + errMsg + '</small></div>';
      currentMountedId = null;
    }
  }

  function unmountTab(tab, clearContainer) {
    var comp = getComponent(tab.type);
    if (comp && typeof comp.unmount === 'function') {
      try {
        var container = getContainer();
        comp.unmount(container, tab);
      } catch (e) {
        if (window.console && window.console.error) {
          window.console.error('[LqdTabs] unmount error', tab, e);
        }
      }
    }
    if (clearContainer) {
      var container = getContainer();
      if (container) container.innerHTML = '';
    }
    if (currentMountedId === tab.id) {
      currentMountedId = null;
    }
  }

  function open(options) {
    options = options || {};
    var type = options.type;
    if (!type || !registry[type]) {
      if (window.console && window.console.error) {
        window.console.error('[LqdTabs] unknown tab type:', type);
      }
      return null;
    }

    // Singleton 标签:同类型只允许一个,已存在则激活复用
    // (chat 除外——Codex 式多会话,每个 chat 标签独立会话,见 P0-4)
    var SINGLETON_TYPES = {};
    if (SINGLETON_TYPES[type]) {
      var existingTabs = getTabs();
      for (var i = 0; i < existingTabs.length; i++) {
        if (existingTabs[i].type === type) {
          // 只在确实需要切换时才调用 activate,避免重复挂载
          if (getActiveId() !== existingTabs[i].id) {
            activate(existingTabs[i].id);
          }
          return existingTabs[i].id;
        }
      }
    }

    var tabs = getTabs();
    var comp = registry[type];
    var title = options.title || (typeof comp.getTitle === 'function' ? comp.getTitle(options.state || {}) : '未命名');

    var tab = {
      id: options.id || generateId(),
      type: type,
      title: title,
      state: options.state || {}
    };

    tabs.push(tab);
    setTabs(tabs);
    setActiveId(tab.id);
    renderBar();
    mountTab(tab);

    window.LqdEvents.emit('tab:opened', { tab: tab });
    return tab.id;
  }

  function activate(id) {
    var tab = findTab(id);
    if (!tab) return;

    var currentId = getActiveId();
    if (currentId === id) return;

    if (currentId) {
      var currentTab = findTab(currentId);
      if (currentTab) unmountTab(currentTab, false);
    }

    setActiveId(id);
    renderBar();
    mountTab(tab);

    window.LqdEvents.emit('tab:activated', { tab: tab });
  }

  function pushClosed(tab) {
    if (!tab) return;
    closedTabsStack.push({
      id: tab.id,
      type: tab.type,
      title: tab.title,
      state: tab.state ? JSON.parse(JSON.stringify(tab.state)) : {}
    });
    if (closedTabsStack.length > CLOSED_STACK_MAX) {
      closedTabsStack.splice(0, closedTabsStack.length - CLOSED_STACK_MAX);
    }
  }

  function reopenLastClosed() {
    if (!closedTabsStack.length) return null;
    var entry = closedTabsStack.pop();
    if (!entry || !entry.type || !registry[entry.type]) return null;
    return open({ type: entry.type, title: entry.title, state: entry.state });
  }

  function canReopenClosed() {
    return closedTabsStack.length > 0;
  }

  function doClose(id) {
    var idx = findTabIndex(id);
    if (idx === -1) return;

    var tabs = getTabs();
    var tab = tabs[idx];

    pushClosed(tab);
    unmountTab(tab, false);
    tabs.splice(idx, 1);
    setTabs(tabs);

    var activeId = getActiveId();
    if (activeId === id) {
      var nextTab = tabs[Math.min(idx, tabs.length - 1)];
      if (nextTab) {
        setActiveId(nextTab.id);
        renderBar();
        mountTab(nextTab);
        // 关闭后聚焦到新激活的标签元素(此前不聚焦任何东西)
        focusTabElement(nextTab.id);
      } else {
        setActiveId(null);
        renderBar();
        var container = getContainer();
        if (container) container.innerHTML = '';
        // 没有标签时打开默认 chat
        open({ type: 'chat', title: '新对话' });
      }
    } else {
      renderBar();
    }

    window.LqdEvents.emit('tab:closed', { tab: tab });
  }

  function close(id) {
    var tab = findTab(id);
    if (!tab) return;

    var comp = getComponent(tab.type);
    var dirty = comp && typeof comp.isDirty === 'function' ? !!comp.isDirty(tab) : false;

    if (!dirty) {
      doClose(id);
      return;
    }

    var proceed = function (ok) {
      if (ok) doClose(id);
    };

    if (window.LqdModal && typeof window.LqdModal.confirm === 'function') {
      var result = window.LqdModal.confirm({
        title: '关闭标签',
        message: '当前标签有未保存内容,确定关闭?',
        confirmLabel: '关闭',
        cancelLabel: '取消',
        danger: true
      });
      if (result && typeof result.then === 'function') {
        result.then(proceed, function () { /* 忽略 */ });
      } else {
        proceed(!!result);
      }
    } else if (window.confirm('当前标签有未保存内容,确定关闭?')) {
      doClose(id);
    }
  }

  function focusTabElement(tabId) {
    var bar = getBar();
    if (!bar) return;
    var el = bar.querySelector('[data-tab-id="' + tabId + '"]');
    if (el) {
      try { el.focus(); } catch (e) { /* 忽略 */ }
    }
  }

  function closeOthers(id) {
    var tabs = getTabs();
    var keep = findTab(id);
    if (!keep) return;

    tabs.forEach(function (tab) {
      if (tab.id !== id) unmountTab(tab, false);
    });

    setTabs([keep]);
    activate(id);
    window.LqdEvents.emit('tab:closed-others', { keep: keep });
  }

  function closeAll() {
    var tabs = getTabs();
    tabs.forEach(function (tab) { unmountTab(tab, false); });
    setTabs([]);
    setActiveId(null);
    renderBar();
    var container = getContainer();
    if (container) container.innerHTML = '';
    open({ type: 'chat', title: '新对话' });
    window.LqdEvents.emit('tab:closed-all', {});
  }

  function closeToRight(id) {
    var idx = findTabIndex(id);
    if (idx === -1) return;
    var tabs = getTabs();
    var keep = tabs[idx];
    var toClose = tabs.slice(idx + 1);
    toClose.forEach(function (tab) { unmountTab(tab, false); });
    setTabs(tabs.slice(0, idx + 1));
    activate(id);
    window.LqdEvents.emit('tab:close-to-right', { keep: keep, closed: toClose.length });
  }

  function active() {
    return findTab(getActiveId());
  }

  function list() {
    return getTabs().slice();
  }

  function updateTabState(id, updater) {
    var tabs = getTabs();
    for (var i = 0; i < tabs.length; i++) {
      if (tabs[i].id === id) {
        if (typeof updater === 'function') {
          updater(tabs[i]);
        } else {
          Object.assign(tabs[i].state, updater);
        }
        setTabs(tabs);
        renderBar();
        return;
      }
    }
  }

  function init() {
    // 先尝试从 localStorage 恢复标签(工作流 F);若无再开默认 chat
    var restored = false;
    if (window.LqdTabPersistence && typeof window.LqdTabPersistence.init === 'function') {
      try { restored = window.LqdTabPersistence.init(); } catch (e) { restored = false; }
    }
    // restore 已挂载活动标签,这里只需补上 renderBar
    if (restored) {
      renderBar();
      return;
    }
    renderBar();
    var tabs = getTabs();
    if (!tabs.length) {
      open({ type: 'chat', title: '新对话' });
    } else if (getActiveId()) {
      var tab = findTab(getActiveId());
      if (tab) mountTab(tab);
    }
  }

  window.LqdTabs = {
    register: register,
    open: open,
    activate: activate,
    close: close,
    closeOthers: closeOthers,
    closeAll: closeAll,
    closeToRight: closeToRight,
    active: active,
    list: list,
    updateTabState: updateTabState,
    getComponent: getComponent,
    reopenLastClosed: reopenLastClosed,
    canReopenClosed: canReopenClosed,
    init: init
  };
})();
