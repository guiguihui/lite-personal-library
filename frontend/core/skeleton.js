/**
 * 轻量个人知识库 — Skeleton
 * 布局形状的 shimmer 占位(非圆形 spinner,遵循 taste-skill)。
 * 独立 IIFE,不依赖其他 core 模块内部函数。
 *
 * 用法:
 *   LqdSkeleton.list({ count: 5, itemHeight: 32 })      → 返回 HTML 字符串
 *   LqdSkeleton.text({ lines: 3 })                      → 返回 HTML 字符串
 *   LqdSkeleton.block({ width: '100%', height: 400 })   → 返回 HTML 字符串
 *   LqdSkeleton.replace(container, skeletonHTML)        → 替换容器内容
 *   LqdSkeleton.stop(container)                          → 清空容器
 *
 * shimmer 动 background-position;reduced-motion 下由 foundation.css 全局兜底降为静态。
 */
(function () {
  'use strict';

  function shimmerClass() {
    return 'lqd-skeleton-shimmer';
  }

  function block(options) {
    options = options || {};
    var width = options.width || '100%';
    var height = options.height || 16;
    var radius = options.radius || 'var(--radius-sm)';
    return '<div class="lqd-skeleton ' + shimmerClass() + '" style="width:' + width + ';height:' + height + 'px;border-radius:' + radius + '"></div>';
  }

  function text(options) {
    options = options || {};
    var lines = options.lines || 3;
    var width = options.width || '100%';
    var html = '';
    for (var i = 0; i < lines; i++) {
      var w = (i === lines - 1) ? '60%' : width;
      html += block({ width: w, height: 12, radius: 'var(--radius-sm)' });
    }
    return '<div class="lqd-skeleton-text" style="display:flex;flex-direction:column;gap:var(--space-1)">' + html + '</div>';
  }

  function list(options) {
    options = options || {};
    var count = options.count || 5;
    var itemHeight = options.itemHeight || 32;
    var html = '';
    for (var i = 0; i < count; i++) {
      html += '<div class="lqd-skeleton-item" style="display:flex;align-items:center;gap:var(--space-2);padding:var(--space-1) var(--space-2)">';
      html += block({ width: 20, height: 20, radius: '50%' });
      html += block({ width: '70%', height: 12, radius: 'var(--radius-sm)' });
      html += '</div>';
    }
    return '<div class="lqd-skeleton-list">' + html + '</div>';
  }

  function card(options) {
    options = options || {};
    var lines = options.lines || 3;
    var html = '<div class="lqd-skeleton-card" style="padding:var(--space-3);border:1px solid var(--border);border-radius:var(--radius-md)">';
    html += block({ width: '40%', height: 16, radius: 'var(--radius-sm)' });
    html += '<div style="height:var(--space-2)"></div>';
    for (var i = 0; i < lines; i++) {
      var w = (i === lines - 1) ? '60%' : '100%';
      html += block({ width: w, height: 12, radius: 'var(--radius-sm)' });
    }
    html += '</div>';
    return html;
  }

  function replace(container, html) {
    if (!container) return;
    container.innerHTML = html || '';
  }

  function stop(container) {
    if (!container) return;
    container.innerHTML = '';
  }

  window.LqdSkeleton = {
    block: block,
    text: text,
    list: list,
    card: card,
    replace: replace,
    stop: stop
  };
})();
