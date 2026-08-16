/**
 * 轻量个人知识库 — Spinner
 * 把现有 refresh 图标(icons.js)加 .lqd-spin 类做旋转,用于内联加载态。
 * 独立 IIFE,不依赖其他 core 模块内部函数。
 *
 * 用法:
 *   LqdSpinner.inline(el)        → 给 el(含 refresh svg)加旋转
 *   LqdSpinner.overlay(container) → 在容器上盖一层旋转遮罩
 *   TqdSpinner.stop(el)           → 移除旋转
 *
 * rotation 是 transform,允许;reduced-motion 下由 foundation.css 全局兜底冻结。
 */
(function () {
  'use strict';

  var SPIN_CLASS = 'lqd-spin';
  var overlayCount = 0;

  function inline(el) {
    if (!el) return;
    el.classList.add(SPIN_CLASS);
  }

  function stop(el) {
    if (!el) return;
    el.classList.remove(SPIN_CLASS);
    var overlay = el.querySelector('.lqd-spinner-overlay');
    if (overlay) overlay.parentNode.removeChild(overlay);
  }

  function overlay(container) {
    if (!container) return;
    container.style.position = container.style.position || 'relative';
    var existing = container.querySelector('.lqd-spinner-overlay');
    if (existing) return;
    var overlay = document.createElement('div');
    overlay.className = 'lqd-spinner-overlay';
    overlay.setAttribute('aria-hidden', 'true');
    var spinner = document.createElement('div');
    spinner.className = 'lqd-spinner-icon ' + SPIN_CLASS;
    if (window.LqdIcons) spinner.innerHTML = window.LqdIcons.icon('refresh');
    overlay.appendChild(spinner);
    container.appendChild(overlay);
    overlayCount++;
    return overlay;
  }

  function stopOverlay(container) {
    if (!container) return;
    var overlay = container.querySelector('.lqd-spinner-overlay');
    if (overlay) overlay.parentNode.removeChild(overlay);
  }

  window.LqdSpinner = {
    inline: inline,
    overlay: overlay,
    stop: stop,
    stopOverlay: stopOverlay
  };
})();
