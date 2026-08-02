/**
 * LQ-D — Tabs
 * 多标签页管理器。所有可标签化视图通过 register() 注册组件。
 */
(function () {
  'use strict';

  var registry = {};

  var mountedTabId = null;

  function $(id) { return document.getElementById(id); }

  function getContainer() {
    return $('lqd-main-body');
  }

  function getBar() {
    return $('lqd-tab-bar');
  }

  function generateId() {
    return window.LqdTabIds.next(function (id) {
      return !!findTab(id);
    });
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
      el.setAttribute('data-tab-type', tab.type);
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
        window.LqdTabContextMenu.attach(el, tab.id);
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
    if (!comp || typeof comp.mount !== 'function') return;

    var container = getContainer();
    if (!container) return;

    // 先卸载当前显示的标签(如果有)
    var currentId = getActiveId();
    if (currentId && currentId !== tab.id) {
      var currentTab = findTab(currentId);
      if (currentTab) unmountTab(currentTab, false);
    }

    container.innerHTML = '';
    try {
      comp.mount(container, tab);
      mountedTabId = tab.id;
    } catch (e) {
      mountedTabId = null;
      if (window.console && window.console.error) {
        window.console.error('[LqdTabs] mount error', tab, e);
      }
      container.innerHTML = '<div class="lqd-empty">标签加载失败</div>';
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
    if (mountedTabId === tab.id) mountedTabId = null;
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

    var tabs = getTabs();
    var comp = registry[type];
    var title = options.title || (typeof comp.getTitle === 'function' ? comp.getTitle(options.state || {}) : '未命名');

    var tabId = options.id || generateId();
    if (options.id) window.LqdTabIds.reserve(options.id);

    var tab = {
      id: tabId,
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
    if (currentId === id) {
      if (mountedTabId !== id) mountTab(tab);
      return;
    }

    if (currentId) {
      var currentTab = findTab(currentId);
      if (currentTab) unmountTab(currentTab, false);
    }

    setActiveId(id);
    renderBar();
    mountTab(tab);

    window.LqdEvents.emit('tab:activated', { tab: tab });
  }

  function close(id) {
    var idx = findTabIndex(id);
    if (idx === -1) return;

    var tabs = getTabs();
    var tab = tabs[idx];

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
    init: init
  };
})();
