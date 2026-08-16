/**
 * 轻量个人知识库 — Tooltip
 * 自定义 tooltip,替代原生 title=。hover/focus 触发,有延迟。
 * 独立 IIFE,不依赖其他 core 模块内部函数。
 *
 * 用法:
 *   LqdTooltip.attach(el, { text: '删除', position: 'top' })
 *   LqdTooltip.detach(el)
 *
 * 工作流 H 会把约 8 处原生 title= 迁移到此。
 * z-index: var(--z-overlay)。
 */
(function () {
  'use strict';

  var TOOLTIP_ID = 'lqd-tooltip';
  var SHOW_DELAY = 500;
  var HIDE_DELAY = 100;
  var showTimer = null;
  var hideTimer = null;
  var currentTarget = null;

  function ensureEl() {
    var existing = document.getElementById(TOOLTIP_ID);
    if (existing) return existing;
    var el = document.createElement('div');
    el.id = TOOLTIP_ID;
    el.className = 'lqd-tooltip';
    el.setAttribute('role', 'tooltip');
    el.setAttribute('hidden', '');
    document.body.appendChild(el);
    return el;
  }

  function position(el, target, pref) {
    var rect = target.getBoundingClientRect();
    var tipRect = el.getBoundingClientRect();
    var margin = 8;
    var pos = pref;

    // 自动翻转:空间不足时翻到对侧
    if (pos === 'top' && rect.top < tipRect.height + margin) pos = 'bottom';
    if (pos === 'bottom' && window.innerHeight - rect.bottom < tipRect.height + margin) pos = 'top';
    if (pos === 'left' && rect.left < tipRect.width + margin) pos = 'right';
    if (pos === 'right' && window.innerWidth - rect.right < tipRect.width + margin) pos = 'left';

    var top, left;
    if (pos === 'top') {
      top = rect.top - tipRect.height - margin;
      left = rect.left + rect.width / 2 - tipRect.width / 2;
    } else if (pos === 'bottom') {
      top = rect.bottom + margin;
      left = rect.left + rect.width / 2 - tipRect.width / 2;
    } else if (pos === 'left') {
      top = rect.top + rect.height / 2 - tipRect.height / 2;
      left = rect.left - tipRect.width - margin;
    } else {
      top = rect.top + rect.height / 2 - tipRect.height / 2;
      left = rect.right + margin;
    }

    // 边界裁剪
    left = Math.max(margin, Math.min(left, window.innerWidth - tipRect.width - margin));
    top = Math.max(margin, Math.min(top, window.innerHeight - tipRect.height - margin));

    el.style.top = top + 'px';
    el.style.left = left + 'px';
    el.setAttribute('data-position', pos);
  }

  function show(target, text, pref) {
    if (!text) return;
    clearTimeout(hideTimer);
    hideTimer = null;
    clearTimeout(showTimer);
    showTimer = setTimeout(function () {
      var el = ensureEl();
      el.textContent = text;
      el.removeAttribute('hidden');
      currentTarget = target;
      requestAnimationFrame(function () {
        position(el, target, pref);
        el.classList.add('lqd-tooltip--visible');
      });
    }, SHOW_DELAY);
  }

  function hide() {
    clearTimeout(showTimer);
    showTimer = null;
    hideTimer = setTimeout(function () {
      var el = ensureEl();
      el.classList.remove('lqd-tooltip--visible');
      el.setAttribute('hidden', '');
      currentTarget = null;
    }, HIDE_DELAY);
  }

  function attach(el, options) {
    if (!el) return;
    options = options || {};
    var text = options.text || '';
    var pref = options.position || 'top';

    el.setAttribute('data-lqd-tooltip', text);
    if (el.hasAttribute('title')) {
      el.removeAttribute('title'); // 移除原生 title,避免双 tooltip
    }

    function onEnter() { show(el, text, pref); }
    function onLeave() { hide(); }
    function onFocus() { show(el, text, pref); }
    function onBlur() { hide(); }

    el.addEventListener('mouseenter', onEnter);
    el.addEventListener('mouseleave', onLeave);
    el.addEventListener('focusin', onFocus);
    el.addEventListener('focusout', onBlur);

    el._lqdTooltip = {
      onEnter: onEnter, onLeave: onLeave, onFocus: onFocus, onBlur: onBlur,
      text: text, pref: pref
    };
  }

  function detach(el) {
    if (!el || !el._lqdTooltip) return;
    var h = el._lqdTooltip;
    el.removeEventListener('mouseenter', h.onEnter);
    el.removeEventListener('mouseleave', h.onLeave);
    el.removeEventListener('focusin', h.onFocus);
    el.removeEventListener('focusout', h.onBlur);
    delete el._lqdTooltip;
    el.removeAttribute('data-lqd-tooltip');
    if (currentTarget === el) hide();
  }

  function detachAll() {
    var all = document.querySelectorAll('[data-lqd-tooltip]');
    all.forEach(detach);
  }

  window.LqdTooltip = {
    attach: attach,
    detach: detach,
    detachAll: detachAll
  };
})();
