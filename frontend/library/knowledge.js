(function () {
  'use strict';
  var graphCleanup = null;

  function open(node) { if (window.LqdLibrary && window.LqdLibrary.openDoc) window.LqdLibrary.openDoc(node.type, node.slug); }
  function section(title) { var root = document.createElement('section'); root.className = 'lqd-knowledge-section'; var heading = document.createElement('h3'); heading.textContent = title; root.appendChild(heading); return root; }
  function loadJson(url) { return fetch(url).then(function (response) { if (!response.ok) throw new Error('HTTP ' + response.status); return response.json(); }); }

  function renderBacklinks(container, docId) {
    if (window.LQD_FEATURES && !window.LQD_FEATURES.backlinks_enabled) return Promise.resolve();
    var root = section('反向链接'); container.appendChild(root);
    return loadJson('/api/links/backlinks?id=' + encodeURIComponent(docId)).then(function (data) {
      var items = data.backlinks || [];
      if (!items.length) { var empty = document.createElement('p'); empty.className = 'lqd-knowledge-empty'; empty.textContent = '暂无反向链接'; root.appendChild(empty); return; }
      var list = document.createElement('ul'); list.className = 'lqd-backlinks';
      items.forEach(function (item) { var li = document.createElement('li'); var button = document.createElement('button'); button.type = 'button'; button.textContent = item.source.title + ' (' + item.count + ')'; button.dataset.previewId = item.source.id; button.addEventListener('click', function () { open(item.source); }); li.appendChild(button); var excerpt = item.occurrences[0] && item.occurrences[0].excerpt; if (excerpt) { var p = document.createElement('p'); p.textContent = excerpt; li.appendChild(p); } list.appendChild(li); });
      root.appendChild(list); if (window.LqdLinkPopover) window.LqdLinkPopover.attach(root);
    }).catch(function (error) { var p = document.createElement('p'); p.className = 'lqd-knowledge-empty'; p.textContent = error.message.indexOf('503') >= 0 ? '尚未构建链接索引' : '反向链接加载失败'; root.appendChild(p); });
  }

  function renderGraph(container, docId) {
    if (window.LQD_FEATURES && !window.LQD_FEATURES.local_graph_enabled) return Promise.resolve();
    var root = section('局部图谱'); container.appendChild(root); var canvas = document.createElement('div'); canvas.className = 'lqd-local-graph'; root.appendChild(canvas);
    return loadJson('/api/links/neighborhood?id=' + encodeURIComponent(docId) + '&limit=40').then(function (data) {
      if (graphCleanup) graphCleanup();
      graphCleanup = window.LqdLocalGraph.mount(canvas, data, open);
      if (data.truncated) { var more = document.createElement('p'); more.className = 'lqd-knowledge-empty'; more.textContent = '另有 ' + (data.total_neighbors - data.nodes.length + 1) + ' 个节点未展开'; root.appendChild(more); }
      var list = document.createElement('ul'); list.className = 'lqd-graph-fallback';
      (data.nodes || []).filter(function (node) { return node.id !== docId; }).forEach(function (node) { var li = document.createElement('li'); var button = document.createElement('button'); button.type = 'button'; button.textContent = node.title; button.addEventListener('click', function () { open(node); }); li.appendChild(button); list.appendChild(li); }); root.appendChild(list);
    }).catch(function () { var p = document.createElement('p'); p.className = 'lqd-knowledge-empty'; p.textContent = '局部图谱暂不可用'; canvas.appendChild(p); });
  }

  function renderOverview(container, docId) { return Promise.all([renderGraph(container, docId)]); }
  // 阅读器底部:局部图谱 + 反向链接(总览面板在 Codex 布局中被隐藏,图谱移到此处展示)
  function renderReader(container, docId) {
    return Promise.all([renderGraph(container, docId), renderBacklinks(container, docId)]);
  }
  window.LqdKnowledge = { renderBacklinks: renderBacklinks, renderGraph: renderGraph, renderOverview: renderOverview, renderReader: renderReader };
})();
