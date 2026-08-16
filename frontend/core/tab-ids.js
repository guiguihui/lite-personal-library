/**
 * Stable tab ID allocation across restored and newly opened tabs.
 */
(function (root) {
  'use strict';

  var nextId = 1;

  function reserve(id) {
    var match = /^tab-(\d+)$/.exec(String(id || ''));
    if (!match) return;
    var numericId = Number(match[1]);
    if (numericId >= nextId) nextId = numericId + 1;
  }

  function next(exists) {
    var isTaken = typeof exists === 'function' ? exists : function () { return false; };
    var candidate;
    do {
      candidate = 'tab-' + (nextId++);
    } while (isTaken(candidate));
    return candidate;
  }

  function reset() {
    nextId = 1;
  }

  var api = { reserve: reserve, next: next, reset: reset };
  root.LqdTabIds = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
