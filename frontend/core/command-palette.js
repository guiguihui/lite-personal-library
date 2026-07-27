/**
 * LQ-D — Command Palette
 * 命令面板:Cmd/Ctrl+K 呼出,支持静态命令与动态提供者。
 */
(function () {
  'use strict';

  var commands = [];
  var providers = [];
  var selectedIndex = 0;
  var filtered = [];
  var isOpen = false;
  var lastFocused = null;

  // 焦点陷阱选择器(有意自包含,不与 modal.js 共享,避免耦合)
  var CP_FOCUSABLE_SELECTOR = 'input:not([disabled]), [data-index]:not([hidden]), button:not([disabled])';

  function $(id) { return document.getElementById(id); }

  function escapeHtml(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  function getEl() {
    return $('lqd-command-palette');
  }

  function getInput() {
    return $('lqd-cp-input');
  }

  function getList() {
    return $('lqd-cp-list');
  }

  function register(command) {
    commands.push(command);
  }

  function registerProvider(provider) {
    providers.push(provider);
  }

  function normalize(str) {
    return String(str || '').toLowerCase();
  }

  function score(query, item) {
    var q = normalize(query);
    var title = normalize(item.title);
    var meta = normalize(item.meta || '');
    if (title.startsWith(q)) return 3;
    if (title.indexOf(q) !== -1) return 2;
    if (meta.indexOf(q) !== -1) return 1;
    return 0;
  }

  async function collectItems(query) {
    var all = commands.slice();
    for (var i = 0; i < providers.length; i++) {
      try {
        var items = await providers[i](query);
        if (items && items.length) all = all.concat(items);
      } catch (e) {
        if (window.console && window.console.error) {
          window.console.error('[LqdCommands] provider error', e);
        }
      }
    }
    return all;
  }

  async function filter(query) {
    var all = await collectItems(query);
    var q = normalize(query);
    if (!q) {
      filtered = all.filter(function (item) { return !item.hidden; }).slice(0, 50);
    } else {
      filtered = all
        .map(function (item) { return { item: item, s: score(q, item) }; })
        .filter(function (x) { return x.s > 0; })
        .sort(function (a, b) { return b.s - a.s; })
        .map(function (x) { return x.item; })
        .slice(0, 50);
    }
    selectedIndex = 0;
    renderList();
  }

  function renderList() {
    var list = getList();
    if (!list) return;
    list.innerHTML = '';

    if (!filtered.length) {
      list.innerHTML = '<div class="lqd-cp-empty">无匹配命令</div>';
      return;
    }

    filtered.forEach(function (item, idx) {
      var el = document.createElement('div');
      el.className = 'lqd-cp-item' + (idx === selectedIndex ? ' active' : '');
      el.setAttribute('data-index', idx);

      var iconName = item.icon || 'command';
      var iconHtml = window.LqdIcons ? window.LqdIcons.icon(iconName) : '';

      el.innerHTML =
        (iconHtml ? '<span class="lqd-cp-item-icon">' + iconHtml + '</span>' : '') +
        '<span class="lqd-cp-item-title">' + escapeHtml(item.title) + '</span>' +
        (item.meta ? '<span class="lqd-cp-item-meta">' + escapeHtml(item.meta) + '</span>' : '');

      el.addEventListener('click', function () { execute(item); });
      el.addEventListener('mouseenter', function () {
        selectedIndex = idx;
        renderList();
      });

      list.appendChild(el);
    });
  }

  function execute(item) {
    if (!item) return;
    close();
    try {
      item.action(item);
    } catch (e) {
      if (window.console && window.console.error) {
        window.console.error('[LqdCommands] execute error', item, e);
      }
    }
  }

  function open() {
    var el = getEl();
    if (!el) return;
    isOpen = true;
    lastFocused = document.activeElement;
    el.setAttribute('role', 'dialog');
    el.setAttribute('aria-modal', 'true');
    el.setAttribute('aria-label', '命令面板');
    el.removeAttribute('hidden');
    el.classList.add('lqd-command-palette--visible');
    var input = getInput();
    if (input) {
      input.value = '';
      input.focus();
    }
    filter('');
  }

  function close() {
    var el = getEl();
    if (!el) return;
    isOpen = false;
    el.setAttribute('hidden', '');
    el.classList.remove('lqd-command-palette--visible');
    var input = getInput();
    if (input) input.blur();
    if (lastFocused) {
      try { lastFocused.focus(); } catch (e) { /* 忽略 */ }
      lastFocused = null;
    }
  }

  // 焦点陷阱:Tab/Shift+Tab 在面板内循环(约 15 行,有意复制,不与 modal.js 共享)
  function trapFocus(e) {
    if (!isOpen) return;
    if (e.key === 'Tab') {
      var el = getEl();
      if (!el) return;
      var focusable = Array.prototype.slice.call(el.querySelectorAll(CP_FOCUSABLE_SELECTOR));
      if (!focusable.length) { e.preventDefault(); return; }
      var first = focusable[0];
      var last = focusable[focusable.length - 1];
      var active = document.activeElement;
      if (e.shiftKey) {
        if (active === first) { e.preventDefault(); try { last.focus(); } catch (err) { /* 忽略 */ } }
      } else {
        if (active === last) { e.preventDefault(); try { first.focus(); } catch (err) { /* 忽略 */ } }
      }
    }
  }

  function toggle() {
    if (isOpen) close();
    else open();
  }

  function handleKeyDown(e) {
    if (!isOpen) return;

    if (e.key === 'Escape') {
      e.preventDefault();
      close();
      return;
    }

    if (e.key === 'ArrowDown') {
      e.preventDefault();
      selectedIndex = (selectedIndex + 1) % Math.max(filtered.length, 1);
      renderList();
      return;
    }

    if (e.key === 'ArrowUp') {
      e.preventDefault();
      selectedIndex = (selectedIndex - 1 + Math.max(filtered.length, 1)) % Math.max(filtered.length, 1);
      renderList();
      return;
    }

    if (e.key === 'Enter') {
      e.preventDefault();
      if (filtered[selectedIndex]) execute(filtered[selectedIndex]);
      return;
    }
  }

  function handleInput(e) {
    filter(e.target.value);
  }

  function init() {
    var el = getEl();
    if (!el) return;

    var input = getInput();
    var list = getList();

    if (input) {
      input.addEventListener('input', handleInput);
      input.addEventListener('keydown', handleKeyDown);
    }

    // 焦点陷阱(全局 keydown,仅 isOpen 时生效)
    document.addEventListener('keydown', trapFocus);

    document.addEventListener('keydown', function (e) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        toggle();
      }
      // Ctrl/Cmd+W 关闭当前标签(设计文档 §8 声称已实现,实际此前缺失)
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'w') {
        e.preventDefault();
        var tab = window.LqdTabs ? window.LqdTabs.active() : null;
        if (tab && window.LqdTabs) window.LqdTabs.close(tab.id);
      }
    });

    document.addEventListener('click', function (e) {
      if (isOpen && el && !el.contains(e.target)) {
        close();
      }
    });

    registerDefaultCommands();
  }

  function registerDefaultCommands() {
    // Activity 切换
    ['chat', 'library', 'manage', 'upload', 'config'].forEach(function (activity) {
      register({
        id: 'activity:' + activity,
        title: '切换到 ' + getActivityLabel(activity),
        icon: activity,
        action: function () {
          if (window.LqdShell) window.LqdShell.setActivity(activity);
        }
      });
    });

    // 新建标签
    register({
      id: 'tab:new:chat',
      title: '新建对话',
      icon: 'chat',
      action: function () {
        if (window.LqdChat && typeof window.LqdChat.openNewChat === 'function') {
          window.LqdChat.openNewChat();
        } else if (window.LqdTabs) {
          window.LqdTabs.open({ type: 'chat', title: '新对话' });
        }
      }
    });

    register({
      id: 'tab:close',
      title: '关闭当前标签',
      icon: 'close',
      action: function () {
        var tab = window.LqdTabs ? window.LqdTabs.active() : null;
        if (tab) window.LqdTabs.close(tab.id);
      }
    });

    // 主题切换
    ['light', 'dark', 'auto'].forEach(function (mode) {
      register({
        id: 'theme:' + mode,
        title: '切换主题: ' + (mode === 'light' ? '浅色' : mode === 'dark' ? '深色' : '自动'),
        icon: mode === 'light' ? 'sun' : mode === 'dark' ? 'moon' : 'monitor',
        action: function () {
          if (window.LqdTheme) window.LqdTheme.set(mode);
        }
      });
    });

    // 触发索引构建
    register({
      id: 'index:build',
      title: '触发增量索引构建',
      icon: 'refresh',
      action: function () {
        if (window.LqdManage && typeof window.LqdManage.buildIndex === 'function') {
          window.LqdManage.buildIndex('incremental');
        }
      }
    });

    // 清空历史
    register({
      id: 'history:clear',
      title: '清空历史对话',
      icon: 'trash',
      action: function () {
        if (window.LqdChatSession && typeof window.LqdChatSession.clearAll === 'function') {
          window.LqdChatSession.clearAll();
        }
      }
    });

    // 全局搜索
    register({
      id: 'search:global',
      title: '全局文本搜索',
      icon: 'search',
      action: function () {
        // 打开命令面板搜索,实际由搜索标签处理
        if (window.LqdSearch) window.LqdSearch.open();
      }
    });
  }

  function getActivityLabel(activity) {
    var labels = {
      chat: '聊天',
      library: '文档库',
      manage: '索引管理',
      upload: '上传',
      config: '配置'
    };
    return labels[activity] || activity;
  }

  // 动态提供者:最近会话
  registerProvider(async function (query) {
    if (window.LqdChatSession && typeof window.LqdChatSession.getAll === 'function') {
      var sessions = window.LqdChatSession.getAll();
      var q = normalize(query);
      return sessions
        .filter(function (s) {
          if (!q) return true;
          return normalize(s.title).indexOf(q) !== -1;
        })
        .slice(0, 10)
        .map(function (s) {
          return {
            id: 'chat:recent:' + s.id,
            title: s.title || '未命名对话',
            meta: '历史对话',
            icon: 'history',
            action: function () {
              if (window.LqdChat && typeof window.LqdChat.openSession === 'function') {
                window.LqdChat.openSession(s.id);
              }
            }
          };
        });
    }
    return [];
  });

  // 动态提供者:文档标题
  registerProvider(async function (query) {
    if (!query || !window.LqdLibrary || typeof window.LqdLibrary.searchDocs !== 'function') return [];
    try {
      var docs = await window.LqdLibrary.searchDocs(query);
      return docs.slice(0, 10).map(function (d) {
        return {
          id: 'library:open:' + d.type + ':' + d.slug,
          title: d.title || d.slug,
          meta: (d.type === 'books' ? '书籍' : d.type === 'papers' ? '论文' : '笔记'),
          icon: d.type === 'books' ? 'book' : d.type === 'papers' ? 'paper' : 'note',
          action: function () {
            if (window.LqdLibrary && typeof window.LqdLibrary.openDoc === 'function') {
              window.LqdLibrary.openDoc(d.type, d.slug);
            }
          }
        };
      });
    } catch (e) {
      return [];
    }
  });

  window.LqdCommands = {
    register: register,
    registerProvider: registerProvider,
    open: open,
    close: close,
    toggle: toggle,
    filter: filter,
    execute: execute,
    init: init
  };
})();
