/**
 * 轻量个人知识库 — Theme Manager
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
