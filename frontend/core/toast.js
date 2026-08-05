/**
 * LQ-D — Toast
 * 瞬时非阻塞通知堆叠。独立 IIFE,不依赖其他 core 模块的内部函数。
 *
 * 用法:
 *   LqdToast.show({ message: '已保存', type: 'success', duration: 4000 })
 *   LqdToast.show({ message: '删除失败', type: 'error', action: { label: '重试', onClick: fn } })
 *
 * 动画仅 transform/opacity;reduced-motion 下滑入降为淡入。
 * 发事件: toast:shown / toast:dismissed(可选订阅)。
 */
(function () {
  'use strict';

  var STORAGE_KEY = 'lqd-toast-container';
  var DEFAULT_DURATION = 4000;
  var STACK_GAP = 8;
  var nextId = 1;
  var timers = {};

  function ensureContainer() {
    var existing = document.getElementById(STORAGE_KEY);
    if (existing) return existing;
    var container = document.createElement('div');
    container.id = STORAGE_KEY;
    container.className = 'lqd-toast-container';
    container.setAttribute('aria-live', 'polite');
    container.setAttribute('role', 'status');
    document.body.appendChild(container);
    return container;
  }

  function iconForType(type) {
    if (!window.LqdIcons) return '';
    if (type === 'success') return window.LqdIcons.icon('check');
    if (type === 'error' || type === 'danger') return window.LqdIcons.icon('alert');
    if (type === 'warning') return window.LqdIcons.icon('alert');
    if (type === 'info') return window.LqdIcons.icon('info');
    return '';
  }

  function dismiss(id) {
    var container = ensureContainer();
    var el = container.querySelector('[data-toast-id="' + id + '"]');
    if (!el) return;
    if (timers[id]) {
      clearTimeout(timers[id]);
      delete timers[id];
    }
    el.classList.add('lqd-toast--leaving');
    var removed = false;
    function remove() {
      if (removed) return;
      removed = true;
      if (el.parentNode) el.parentNode.removeChild(el);
      if (window.LqdEvents) window.LqdEvents.emit('toast:dismissed', { id: id });
    }
    // 动画结束后移除;reduced-motion 下 transition 接近 0,setTimeout 兜底
    el.addEventListener('transitionend', remove, { once: true });
    setTimeout(remove, 400);
  }

  function show(options) {
    options = options || {};
    var container = ensureContainer();
    var id = 'toast-' + (nextId++);
    var type = options.type || 'info';
    var duration = typeof options.duration === 'number' ? options.duration : DEFAULT_DURATION;

    var el = document.createElement('div');
    el.className = 'lqd-toast lqd-toast--' + type;
    el.setAttribute('data-toast-id', id);
    el.setAttribute('role', 'status');

    var iconHtml = iconForType(type);
    if (iconHtml) {
      var iconEl = document.createElement('span');
      iconEl.className = 'lqd-toast-icon';
      iconEl.innerHTML = iconHtml;
      el.appendChild(iconEl);
    }

    var msgEl = document.createElement('span');
    msgEl.className = 'lqd-toast-message';
    msgEl.textContent = options.message || '';
    el.appendChild(msgEl);

    if (options.action && options.action.label && typeof options.action.onClick === 'function') {
      var actionBtn = document.createElement('button');
      actionBtn.className = 'lqd-toast-action';
      actionBtn.textContent = options.action.label;
      actionBtn.addEventListener('click', function () {
        try { options.action.onClick(); } catch (e) { /* 忽略 */ }
        dismiss(id);
      });
      el.appendChild(actionBtn);
    }

    var closeBtn = document.createElement('button');
    closeBtn.className = 'lqd-toast-close';
    closeBtn.setAttribute('aria-label', '关闭通知');
    closeBtn.innerHTML = window.LqdIcons ? window.LqdIcons.icon('close') : '×';
    closeBtn.addEventListener('click', function () { dismiss(id); });
    el.appendChild(closeBtn);

    container.appendChild(el);
    // 触发入场:下一帧加 entering 类
    requestAnimationFrame(function () { el.classList.add('lqd-toast--entering'); });

    if (duration > 0) {
      timers[id] = setTimeout(function () { dismiss(id); }, duration);
    }

    if (window.LqdEvents) window.LqdEvents.emit('toast:shown', { id: id, type: type, message: options.message });
    return id;
  }

  function clear() {
    var container = ensureContainer();
    var ids = [];
    container.querySelectorAll('[data-toast-id]').forEach(function (el) {
      ids.push(el.getAttribute('data-toast-id'));
    });
    ids.forEach(dismiss);
  }

  window.LqdToast = {
    show: show,
    dismiss: dismiss,
    clear: clear
  };
})();
