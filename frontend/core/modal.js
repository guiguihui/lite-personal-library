/**
 * LQ-D — Modal
 * 可访问的模态对话框,替代原生 alert()/confirm()。
 * 独立 IIFE,焦点陷阱逻辑有意自包含(不与 command-palette.js 共享,避免耦合)。
 *
 * 用法:
 *   await LqdModal.alert({ title: '上传日志', message: '<pre>...</pre>', confirmLabel: '关闭' })
 *   const ok = await LqdModal.confirm({ title: '清除', message: '...', danger: true })
 *
 * 特性: role="dialog" aria-modal="true";焦点陷阱;Esc/背景点击取消;关闭后焦点回触发元素。
 * 发事件: modal:opened / modal:closed(可选订阅)。
 */
(function () {
  'use strict';

  var STORAGE_ID = 'lqd-modal-root';
  var FOCUSABLE_SELECTOR = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
  var lastFocused = null;
  var activeResolver = null;
  var backdropClickEnabled = false;

  function ensureRoot() {
    var existing = document.getElementById(STORAGE_ID);
    if (existing) return existing;
    var root = document.createElement('div');
    root.id = STORAGE_ID;
    root.className = 'lqd-modal-root';
    root.setAttribute('hidden', '');
    document.body.appendChild(root);
    return root;
  }

  function isReducedMotion() {
    return window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  function focusFirst() {
    var root = ensureRoot();
    var focusable = root.querySelector('[autofocus]') || root.querySelector(FOCUSABLE_SELECTOR);
    if (focusable) {
      try { focusable.focus(); } catch (e) { /* 忽略 */ }
    }
  }

  // 焦点陷阱:Tab/Shift+Tab 在对话框内循环(约 15 行,有意复制,不与 command-palette 共享)
  function trapKeydown(e) {
    if (e.key === 'Escape') {
      e.preventDefault();
      close(false);
      return;
    }
    if (e.key !== 'Tab') return;
    var root = ensureRoot();
    var focusable = Array.prototype.slice.call(root.querySelectorAll(FOCUSABLE_SELECTOR));
    if (!focusable.length) {
      e.preventDefault();
      return;
    }
    var first = focusable[0];
    var last = focusable[focusable.length - 1];
    var active = document.activeElement;
    if (e.shiftKey) {
      if (active === first || !root.contains(active)) {
        e.preventDefault();
        try { last.focus(); } catch (err) { /* 忽略 */ }
      }
    } else {
      if (active === last) {
        e.preventDefault();
        try { first.focus(); } catch (err) { /* 忽略 */ }
      }
    }
  }

  function onBackdropClick(e) {
    if (!backdropClickEnabled) return;
    if (e.target === ensureRoot()) {
      close(false);
    }
  }

  function close(result) {
    var root = ensureRoot();
    if (root.hasAttribute('hidden')) return;

    document.removeEventListener('keydown', trapKeydown);
    root.removeEventListener('click', onBackdropClick);

    root.classList.add('lqd-modal-root--leaving');
    var finished = false;
    function finalize() {
      if (finished) return;
      finished = true;
      root.innerHTML = '';
      root.setAttribute('hidden', '');
      root.classList.remove('lqd-modal-root--leaving');
      if (lastFocused) {
        try { lastFocused.focus(); } catch (e) { /* 忽略 */ }
        lastFocused = null;
      }
      if (window.LqdEvents) window.LqdEvents.emit('modal:closed', { result: result });
    }
    if (isReducedMotion()) {
      finalize();
    } else {
      root.addEventListener('transitionend', finalize, { once: true });
      setTimeout(finalize, 320);
    }

    if (activeResolver) {
      var resolve = activeResolver;
      activeResolver = null;
      resolve(result);
    }
  }

  function render(options) {
    options = options || {};
    var root = ensureRoot();
    root.innerHTML = '';

    var dialog = document.createElement('div');
    dialog.className = 'lqd-modal' + (options.danger ? ' lqd-modal--danger' : '');
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    dialog.setAttribute('aria-labelledby', 'lqd-modal-title');

    if (options.title) {
      var title = document.createElement('div');
      title.className = 'lqd-modal-title';
      title.id = 'lqd-modal-title';
      title.textContent = options.title;
      dialog.appendChild(title);
    }

    if (options.message !== undefined && options.message !== null) {
      var body = document.createElement('div');
      body.className = 'lqd-modal-body';
      // message 可为 HTML(如日志 <pre>);调用方负责转义用户内容
      body.innerHTML = options.message;
      dialog.appendChild(body);
    }

    var actions = document.createElement('div');
    actions.className = 'lqd-modal-actions';

    if (options.cancelLabel !== null && options.cancelLabel !== false) {
      var cancelBtn = document.createElement('button');
      cancelBtn.className = 'lqd-btn';
      cancelBtn.textContent = options.cancelLabel || '取消';
      cancelBtn.addEventListener('click', function () { close(false); });
      actions.appendChild(cancelBtn);
    }

    var confirmBtn = document.createElement('button');
    confirmBtn.className = 'lqd-btn lqd-btn--' + (options.danger ? 'danger' : 'primary');
    confirmBtn.textContent = options.confirmLabel || '确定';
    confirmBtn.setAttribute('autofocus', '');
    confirmBtn.addEventListener('click', function () { close(true); });
    actions.appendChild(confirmBtn);

    dialog.appendChild(actions);
    root.appendChild(dialog);

    root.removeAttribute('hidden');
    requestAnimationFrame(function () { root.classList.add('lqd-modal-root--visible'); });
  }

  function alert(options) {
    return new Promise(function (resolve) {
      lastFocused = document.activeElement;
      activeResolver = resolve;
      backdropClickEnabled = true;
      var opts = Object.assign({}, options, { cancelLabel: null });
      render(opts);
      document.addEventListener('keydown', trapKeydown);
      ensureRoot().addEventListener('click', onBackdropClick);
      if (window.LqdEvents) window.LqdEvents.emit('modal:opened', { kind: 'alert' });
      // alert 模式只确认,无取消;焦点放确认按钮
      requestAnimationFrame(focusFirst);
    });
  }

  function confirm(options) {
    return new Promise(function (resolve) {
      lastFocused = document.activeElement;
      activeResolver = resolve;
      backdropClickEnabled = true;
      render(options);
      document.addEventListener('keydown', trapKeydown);
      ensureRoot().addEventListener('click', onBackdropClick);
      if (window.LqdEvents) window.LqdEvents.emit('modal:opened', { kind: 'confirm' });
      requestAnimationFrame(focusFirst);
    });
  }

  window.LqdModal = {
    alert: alert,
    confirm: confirm,
    close: function () { close(false); }
  };
})();
