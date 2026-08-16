/**
 * 轻量个人知识库 — Store
 * 最小全局 UI 状态,只放框架级状态,不放业务数据。
 */
(function () {
  'use strict';

  var state = {
    theme: 'auto',
    activity: 'chat',
    tabs: [],
    activeTabId: null,
    status: {
      indexReady: false,
      indexRunning: false,
      ingestRunning: false,
      provider: '',
      model: '',
      version: '0.1.0'
    }
  };

  var listeners = {};

  function notify(key) {
    if (!listeners[key]) return;
    listeners[key].slice().forEach(function (cb) {
      try { cb(state[key]); } catch (e) { /* ignore */ }
    });
  }

  function get(key) {
    if (key === undefined) return JSON.parse(JSON.stringify(state));
    return state[key];
  }

  function set(key, value) {
    if (typeof key === 'object' && value === undefined) {
      Object.keys(key).forEach(function (k) { set(k, key[k]); });
      return;
    }
    state[key] = value;
    notify(key);
    if (window.LqdEvents && typeof window.LqdEvents.emit === 'function') {
      window.LqdEvents.emit('store:changed', { key: key, value: value });
    }
  }

  function subscribe(key, cb) {
    if (!listeners[key]) listeners[key] = [];
    listeners[key].push(cb);
    return function () {
      var idx = listeners[key].indexOf(cb);
      if (idx !== -1) listeners[key].splice(idx, 1);
    };
  }

  function snapshot() {
    return JSON.parse(JSON.stringify(state));
  }

  window.LqdStore = {
    get: get,
    set: set,
    subscribe: subscribe,
    snapshot: snapshot
  };
})();
