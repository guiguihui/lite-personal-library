/**
 * LQ-D — Tab Persistence
 * 标签状态跨刷新持久化。设计文档 §12 此前推迟,现补齐。
 * 独立 IIFE,不依赖 tabs.js 内部函数。
 *
 * 跨模块通信:读 LqdStore.get('tabs')/('activeTabId')(公共 API);
 *           恢复调 LqdTabs.open(公共 API);监听 store:changed(公共事件)。
 * 只持久化 {id,type,title,state} 的可序列化子集;跳过含函数的 state。
 */
(function () {
  'use strict';

  var STORAGE_KEY = 'lqd-tabs';

  function isSerializable(value) {
    if (value === null || value === undefined) return true;
    var t = typeof value;
    if (t === 'function') return false;
    if (t !== 'object') return true;
    try {
      JSON.stringify(value);
      return true;
    } catch (e) {
      return false;
    }
  }

  function sanitizeState(state) {
    if (!state || typeof state !== 'object') return {};
    var clean = {};
    var keys = Object.keys(state);
    for (var i = 0; i < keys.length; i++) {
      var k = keys[i];
      // 跳过内部下划线字段和不可序列化的值
      if (k.charAt(0) === '_') continue;
      if (isSerializable(state[k])) clean[k] = state[k];
    }
    return clean;
  }

  function save() {
    if (!window.LqdStore) return;
    var tabs = window.LqdStore.get('tabs') || [];
    var activeId = window.LqdStore.get('activeTabId');
    var serializable = tabs.map(function (t) {
      return {
        id: t.id,
        type: t.type,
        title: t.title,
        state: sanitizeState(t.state)
      };
    });
    var payload = { tabs: serializable, activeTabId: activeId, savedAt: Date.now() };
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
    } catch (e) { /* localStorage 不可用或超限,静默 */ }
  }

  function restore() {
    if (!window.LqdTabs) return false;
    var payload;
    try {
      var raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return false;
      payload = JSON.parse(raw);
    } catch (e) {
      return false;
    }
    if (!payload || !payload.tabs || !payload.tabs.length) return false;

    var tabs = payload.tabs;
    var activeId = payload.activeTabId;
    var firstOpenedId = null;

    for (var i = 0; i < tabs.length; i++) {
      var t = tabs[i];
      // 跳过当前已存在的 id(避免重复)
      var existing = window.LqdTabs.list().some(function (x) { return x.id === t.id; });
      if (existing) continue;
      var id = window.LqdTabs.open({
        id: t.id,
        type: t.type,
        title: t.title,
        state: t.state || {}
      });
      if (i === 0) firstOpenedId = id;
    }

    // 恢复激活的标签
    if (activeId) {
      window.LqdTabs.activate(activeId);
    } else if (firstOpenedId) {
      window.LqdTabs.activate(firstOpenedId);
    }
    return true;
  }

  function clear() {
    try { localStorage.removeItem(STORAGE_KEY); } catch (e) { /* 忽略 */ }
  }

  function init() {
    // 先尝试恢复;若无可恢复,让 tabs.js init 走默认开 chat 标签逻辑
    var restored = restore();
    // 监听 store 变化持久化(单一监听,过滤 tabs/activeTabId)
    if (window.LqdStore && typeof window.LqdStore.subscribe === 'function') {
      window.LqdStore.subscribe('tabs', save);
      window.LqdStore.subscribe('activeTabId', save);
    }
    return restored;
  }

  window.LqdTabPersistence = {
    init: init,
    save: save,
    restore: restore,
    clear: clear
  };
})();
