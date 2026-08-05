/**
 * LQ-D — Tab Context Menu
 * 标签右键菜单:关闭 / 关闭其他 / 关闭右侧 / 关闭全部。
 * 独立 IIFE,不依赖 tabs.js 内部函数。
 *
 * 跨模块通信:调 LqdTabs.close/closeOthers/closeAll/closeToRight(公共导出)。
 * z-index: var(--z-overlay)。
 */
(function () {
  'use strict';

  var MENU_ID = 'lqd-tab-context-menu';
  var currentMenu = null;

  function ensureMenuEl() {
    var existing = document.getElementById(MENU_ID);
    if (existing) return existing;
    var el = document.createElement('div');
    el.id = MENU_ID;
    el.className = 'lqd-tab-context-menu';
    el.setAttribute('role', 'menu');
    el.setAttribute('hidden', '');
    document.body.appendChild(el);
    return el;
  }

  function closeMenu() {
    var el = ensureMenuEl();
    el.setAttribute('hidden', '');
    el.innerHTML = '';
    currentMenu = null;
    document.removeEventListener('click', onOutsideClick);
    document.removeEventListener('keydown', onKey);
  }

  function onOutsideClick(e) {
    var el = ensureMenuEl();
    if (el && !el.contains(e.target)) closeMenu();
  }

  function onKey(e) {
    if (e.key === 'Escape') closeMenu();
  }

  function createItem(label, onClick) {
    var item = document.createElement('div');
    item.className = 'lqd-tab-context-menu-item';
    item.setAttribute('role', 'menuitem');
    item.setAttribute('tabindex', '0');
    item.textContent = label;
    item.addEventListener('click', function () {
      closeMenu();
      try { onClick(); } catch (e) { /* 忽略 */ }
    });
    item.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        closeMenu();
        try { onClick(); } catch (err) { /* 忽略 */ }
      }
    });
    return item;
  }

  function attach(tabEl, tabId, tabType) {
    if (!tabEl || !tabId) return;
    tabEl.addEventListener('contextmenu', function (e) {
      e.preventDefault();
      e.stopPropagation();
      show(tabEl, tabId, tabType, e.clientX, e.clientY);
    });
  }

  function show(tabEl, tabId, tabType, x, y) {
    var el = ensureMenuEl();
    el.innerHTML = '';

    el.appendChild(createItem('关闭', function () {
      if (window.LqdTabs) window.LqdTabs.close(tabId);
    }));
    el.appendChild(createItem('关闭其他', function () {
      if (window.LqdTabs && window.LqdTabs.closeOthers) window.LqdTabs.closeOthers(tabId);
    }));
    el.appendChild(createItem('关闭右侧', function () {
      if (window.LqdTabs && window.LqdTabs.closeToRight) window.LqdTabs.closeToRight(tabId);
    }));
    el.appendChild(createItem('关闭全部', function () {
      if (window.LqdTabs && window.LqdTabs.closeAll) window.LqdTabs.closeAll();
    }));

    // chat 标签:追加"复制对话"
    if (tabType === 'chat') {
      var sep = document.createElement('div');
      sep.className = 'lqd-tab-context-menu-sep';
      el.appendChild(sep);

      el.appendChild(createItem('复制对话', function () {
        copyConversation(tabId);
      }));
    }

    el.style.left = x + 'px';
    el.style.top = y + 'px';
    el.removeAttribute('hidden');
    requestAnimationFrame(function () { el.classList.add('lqd-tab-context-menu--visible'); });

    // 边界裁剪
    var rect = el.getBoundingClientRect();
    if (rect.right > window.innerWidth) {
      el.style.left = (window.innerWidth - rect.width - 8) + 'px';
    }
    if (rect.bottom > window.innerHeight) {
      el.style.top = (window.innerHeight - rect.height - 8) + 'px';
    }

    currentMenu = { tabId: tabId };
    // 阻止右键后紧随的 click 冒泡到 document 触发 onOutsideClick 关闭菜单
    function suppressClick(e) {
      e.stopPropagation();
      document.removeEventListener('click', suppressClick, true);
    }
    document.addEventListener('click', suppressClick, true);
    setTimeout(function () {
      document.removeEventListener('click', suppressClick, true);
      document.addEventListener('click', onOutsideClick);
      document.addEventListener('keydown', onKey);
    }, 0);
  }

  function copyConversation(tabId) {
    var messages = [];
    // 尝试从 LqdChatSession 读取
    if (window.LqdChatSession && typeof window.LqdChatSession.loadCurrent === 'function') {
      messages = window.LqdChatSession.loadCurrent(tabId);
    }
    // 也尝试直接从 sessionStorage 读(不同 tab key 对应不同 session)
    if (!messages || !messages.length) {
      try {
        var raw = sessionStorage.getItem('lqd_chat_session_' + (tabId || 'default'));
        if (raw) {
          var parsed = JSON.parse(raw);
          if (Array.isArray(parsed) && parsed.length) messages = parsed;
        }
      } catch (_) { /* ignore */ }
    }

    if (!messages || !messages.length) {
      if (window.LqdToast) window.LqdToast.show({ message: '暂无对话内容', type: 'warning', duration: 2000 });
      return;
    }

    var text = '';
    for (var i = 0; i < messages.length; i++) {
      var role = messages[i].role === 'user' ? 'User' : 'Assistant';
      text += role + ': ' + messages[i].content + '\n\n';
    }
    text = text.trim();

    // 复制到剪贴板
    function fallbackCopy(t) {
      var ta = document.createElement('textarea');
      ta.value = t;
      ta.style.position = 'fixed';
      ta.style.left = '-9999px';
      ta.style.top = '-9999px';
      document.body.appendChild(ta);
      ta.select();
      try {
        document.execCommand('copy');
        document.body.removeChild(ta);
        if (window.LqdToast) window.LqdToast.show({ message: '对话已复制到剪贴板', type: 'success', duration: 2500 });
      } catch (err) {
        document.body.removeChild(ta);
        if (window.LqdToast) window.LqdToast.show({ message: '复制失败', type: 'error', duration: 3000 });
      }
    }

    if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
      navigator.clipboard.writeText(text).then(function () {
        if (window.LqdToast) window.LqdToast.show({ message: '对话已复制到剪贴板', type: 'success', duration: 2500 });
      }).catch(function () {
        fallbackCopy(text);
      });
    } else {
      fallbackCopy(text);
    }
  }

  window.LqdTabContextMenu = {
    attach: attach,
    close: closeMenu
  };
})();
