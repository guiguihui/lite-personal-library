/**
 * 轻量个人知识库 — Tab Context Menu
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

  function attach(tabEl, tabId) {
    if (!tabEl || !tabId) return;
    tabEl.addEventListener('contextmenu', function (e) {
      e.preventDefault();
      show(tabEl, tabId, e.clientX, e.clientY);
    });
  }

  function show(tabEl, tabId, x, y) {
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
    setTimeout(function () {
      document.addEventListener('click', onOutsideClick);
      document.addEventListener('keydown', onKey);
    }, 0);
  }

  window.LqdTabContextMenu = {
    attach: attach,
    close: closeMenu
  };
})();
