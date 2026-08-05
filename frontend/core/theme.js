/**
 * LQ-D — Theme Manager
 * 无闪烁主题初始化与切换。
 */
(function () {
  'use strict';

  var THEME_KEY = 'lqd-theme';
  var LEGACY_KEY = 'book-theme';

  function getEffectiveTheme(saved) {
    if (saved === 'light' || saved === 'dark') return saved;
    if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) return 'dark';
    return 'light';
  }

  function applyEffective() {
    var mode = get();
    document.documentElement.setAttribute('data-effective-theme', getEffectiveTheme(mode));
  }

  function bindSystemThemeListener() {
    if (!window.matchMedia) return;
    var mql = window.matchMedia('(prefers-color-scheme: dark)');
    var handler = function () {
      // 仅 auto 模式下跟随系统;手动 light/dark 不变
      if (get() !== 'auto') return;
      applyEffective();
      if (window.LqdEvents && typeof window.LqdEvents.emit === 'function') {
        window.LqdEvents.emit('theme:changed', { mode: 'auto', effective: getEffectiveTheme('auto') });
      }
    };
    if (typeof mql.addEventListener === 'function') {
      mql.addEventListener('change', handler);
    } else if (typeof mql.addListener === 'function') {
      mql.addListener(handler);
    }
  }

  function init() {
    var saved = localStorage.getItem(THEME_KEY);
    if (!saved) {
      var legacy = localStorage.getItem(LEGACY_KEY);
      if (legacy) {
        saved = legacy;
        localStorage.setItem(THEME_KEY, saved);
      }
    }
    saved = (saved === 'light' || saved === 'dark') ? saved : 'auto';
    document.documentElement.setAttribute('data-theme', saved);
    document.documentElement.setAttribute('data-effective-theme', getEffectiveTheme(saved));
    bindSystemThemeListener();
  }

  function set(mode) {
    if (mode !== 'light' && mode !== 'dark' && mode !== 'auto') return;
    localStorage.setItem(THEME_KEY, mode);
    document.documentElement.setAttribute('data-theme', mode);
    document.documentElement.setAttribute('data-effective-theme', getEffectiveTheme(mode));
    if (window.LqdEvents && typeof window.LqdEvents.emit === 'function') {
      window.LqdEvents.emit('theme:changed', { mode: mode, effective: getEffectiveTheme(mode) });
    }
  }

  function get() {
    return localStorage.getItem(THEME_KEY) || 'auto';
  }

  function getEffective() {
    return getEffectiveTheme(get());
  }

  window.LqdTheme = {
    init: init,
    set: set,
    get: get,
    getEffective: getEffective
  };
})();
