/**
 * 轻量个人知识库 — Events
 * 轻量级发布订阅事件总线,用于跨模块解耦通信。
 */
(function () {
  'use strict';

  var handlers = {};

  function on(event, handler) {
    if (!handlers[event]) handlers[event] = [];
    handlers[event].push(handler);
    return function () { off(event, handler); };
  }

  function off(event, handler) {
    if (!handlers[event]) return;
    var idx = handlers[event].indexOf(handler);
    if (idx !== -1) handlers[event].splice(idx, 1);
  }

  function emit(event, payload) {
    if (!handlers[event]) return;
    handlers[event].slice().forEach(function (handler) {
      try {
        handler(payload);
      } catch (e) {
        // 事件处理错误不应阻断其他监听者
        if (window.console && window.console.error) {
          window.console.error('[LqdEvents] handler error for', event, e);
        }
      }
    });
  }

  function once(event, handler) {
    function wrapper(payload) {
      off(event, wrapper);
      handler(payload);
    }
    on(event, wrapper);
  }

  window.LqdEvents = {
    on: on,
    off: off,
    emit: emit,
    once: once
  };
})();
