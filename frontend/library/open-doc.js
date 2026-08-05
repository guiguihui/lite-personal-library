/**
 * LQ-D — Library Open-Doc Bridge
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

  // P2 知识链接:link-index 的 doc_type 是单数(book/paper/note),library 组件用复数。
  var TYPE_ALIASES = { book: 'books', paper: 'papers', note: 'notes' };
  function normalizeType(type) { return TYPE_ALIASES[type] || type; }

  function openDoc(type, slug, nodeId) {
    if (!type || !slug) return null;
    if (!window.LqdTabs) return null;
    type = normalizeType(type);

    // 复用已打开的同文档 library 标签(若存在),否则新开
    var tabs = window.LqdTabs.list();
    var existing = null;
    for (var i = 0; i < tabs.length; i++) {
      var t = tabs[i];
      if (t.type === 'library' && t.state && normalizeType(t.state.type) === type && t.state.slug === slug) {
        existing = t;
        break;
      }
    }

    var state = { type: type, slug: slug };
    if (nodeId) state.nodeId = nodeId;

    if (existing) {
      // 更新 state 以反映可能的 nodeId 跳转
      window.LqdTabs.updateTabState(existing.id, { type: type, nodeId: nodeId || existing.state.nodeId });
      window.LqdTabs.activate(existing.id);
      // 若 library 已 mount,直接触发节点跳转
      if (nodeId && window.LqdEvents) {
        window.LqdEvents.emit('library:node:select', { nodeId: nodeId, type: type, slug: slug });
      }
      if (window.LqdEvents) window.LqdEvents.emit('library:doc:opened', { type: type, slug: slug, nodeId: nodeId, reused: true });
      return existing.id;
    }

    var title = slug;
    var id = window.LqdTabs.open({
      type: 'library',
      title: title,
      state: state
    });

    // mount 完成后,若携带 nodeId,发事件让 library.js 跳转到该节点
    if (nodeId && window.LqdEvents) {
      // 延迟到下一帧,确保 mount 已执行
      setTimeout(function () {
        window.LqdEvents.emit('library:node:select', { nodeId: nodeId, type: type, slug: slug });
      }, 0);
    }
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
