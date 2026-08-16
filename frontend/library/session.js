(function () {
  'use strict';

  var registry = new Map();
  var TYPE_ALIASES = { book: 'books', paper: 'papers', note: 'notes' };

  function normalizeType(type) {
    return TYPE_ALIASES[type] || type || 'books';
  }

  function create(tabId, initialState) {
    if (!tabId) throw new Error('library session requires tabId');
    var state = initialState || {};
    var session = {
      tabId: tabId,
      type: normalizeType(state.type),
      slug: state.slug || null,
      nodeId: state.nodeId || null,
      docs: [],
      currentDoc: null,
      generation: state.generation || null,
      viewId: state.viewId || null,
      shelfEl: null,
      treeEl: null,
      readerEl: null,
      requestVersion: 0,
      controller: null
    };
    registry.set(tabId, session);
    return session;
  }

  function get(tabId) {
    return registry.get(tabId) || null;
  }

  function bind(session, refs) {
    refs = refs || {};
    session.shelfEl = refs.shelfEl || null;
    session.treeEl = refs.treeEl || null;
    session.readerEl = refs.readerEl || null;
    return session;
  }

  function begin(session) {
    if (session.controller && typeof session.controller.abort === 'function') session.controller.abort();
    session.requestVersion += 1;
    session.controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
    return { version: session.requestVersion, signal: session.controller ? session.controller.signal : null };
  }

  function isCurrent(session, version) {
    return registry.get(session.tabId) === session && session.requestVersion === version;
  }

  function dispose(tabId) {
    var session = registry.get(tabId);
    if (!session) return;
    if (session.controller && typeof session.controller.abort === 'function') session.controller.abort();
    session.shelfEl = null;
    session.treeEl = null;
    session.readerEl = null;
    registry.delete(tabId);
  }

  var api = { begin: begin, bind: bind, create: create, dispose: dispose, get: get, isCurrent: isCurrent, normalizeType: normalizeType };
  window.LqdLibrarySessions = api;
  if (typeof module !== 'undefined') module.exports = api;
})();
