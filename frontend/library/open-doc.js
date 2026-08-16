/**
 * 轻量个人知识库 — Library Open-Doc Bridge
 * 规范的文档打开桥,augment 现有 window.LqdLibrary 对象(不替换、不改 library/index.js)。
 *
 * 修复 bug:command-palette.js:360 和 search/index.js:138 调用 LqdLibrary.openDoc/searchDocs,
 * 但 library/index.js 只暴露 getTitle/getIcon/mount/unmount/renderSidebar/renderOverview,
 * 导致两处调用静默失败(typeof === 'function' 守卫跳过)。
 *
 * 此文件在 library/index.js 之后加载,augment 已存在的 window.LqdLibrary。
 * 跨模块通信:调 LqdTabs.open(公共 API);发 library:doc:opened 事件(供 overview 监听)。
 */
(function () {
  'use strict';

  var TYPE_ALIASES = { book: 'books', paper: 'papers', note: 'notes' };
  function normalizeType(type) { return TYPE_ALIASES[type] || type; }

  function openDoc(type, slug, nodeId) {
    if (!type || !slug || !window.LqdTabs) return null;
    type = normalizeType(type);
    var tabs = window.LqdTabs.list();
    var existing = null;
    for (var i = 0; i < tabs.length; i++) {
      var candidate = tabs[i];
      if (candidate.type === 'library' && candidate.state && normalizeType(candidate.state.type) === type && candidate.state.slug === slug) {
        existing = candidate;
        break;
      }
    }

    if (existing) {
      var active = window.LqdTabs.active();
      var alreadyActive = !!active && active.id === existing.id;
      window.LqdTabs.updateTabState(existing.id, { type: type, slug: slug, nodeId: nodeId || existing.state.nodeId || null });
      window.LqdTabs.activate(existing.id);
      if (alreadyActive && nodeId && window.LqdEvents) {
        window.LqdEvents.emit('library:node:select', { nodeId: nodeId, type: type, slug: slug });
      }
      if (window.LqdEvents) window.LqdEvents.emit('library:doc:opened', { type: type, slug: slug, nodeId: nodeId, reused: true });
      return existing.id;
    }

    var id = window.LqdTabs.open({
      type: 'library',
      title: slug,
      state: { type: type, slug: slug, nodeId: nodeId || null }
    });
    if (window.LqdEvents) window.LqdEvents.emit('library:doc:opened', { type: type, slug: slug, nodeId: nodeId, reused: false });
    return id;
  }
  function searchDocs(query) {
    if (!query) return Promise.resolve([]);
    var url = '/api/search?q=' + encodeURIComponent(query) + '&limit=10';
    return fetch(url)
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      })
      .then(function (data) {
        var results = (data && data.results) || [];
        return results.map(function (r) {
          return {
            type: r.doc_type || r.type,
            slug: r.slug || r.doc_id,
            title: r.title || r.slug || r.doc_id
          };
        });
      })
      .catch(function () {
        return [];
      });
  }

  // augment 现有 window.LqdLibrary(若不存在则创建空对象,但正常情况下 index.js 已创建)
  if (!window.LqdLibrary) window.LqdLibrary = {};
  window.LqdLibrary.openDoc = openDoc;
  window.LqdLibrary.searchDocs = searchDocs;
})();
