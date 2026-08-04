(function () {
  'use strict';

  var DOC_TYPES = [
    { key: 'books', label: '书籍' },
    { key: 'papers', label: '论文' },
    { key: 'notes', label: '笔记' }
  ];
  var activeSession = null;
  var eventsBound = false;

  function sessions() { return window.LqdLibrarySessions; }
  function el(tag, cls, text) { var node = document.createElement(tag); if (cls) node.className = cls; if (text != null) node.textContent = text; return node; }
  function empty(node) { while (node && node.firstChild) node.removeChild(node.firstChild); }
  function isAlive(session) { return !!session && sessions().get(session.tabId) === session; }
  function pinQuery(session) {
    if (!session.generation || !session.viewId) return '';
    return '&generation=' + encodeURIComponent(session.generation) +
      '&view_id=' + encodeURIComponent(session.viewId);
  }


  function showLoading(node, message, skeletonType) {
    empty(node);
    if (window.LqdSkeleton && skeletonType) {
      var html = skeletonType === 'list' ? window.LqdSkeleton.list({ count: 5, itemHeight: 32 }) : window.LqdSkeleton.block({ width: '100%', height: 400 });
      if (html) { node.innerHTML = html; return; }
    }
    node.appendChild(el('div', 'library-loading', message || '加载中…'));
  }

  function showError(node, message) { empty(node); node.appendChild(el('div', 'library-error', message)); }
  function renderWelcome(node, title, hint) {
    empty(node);
    var wrap = el('div', 'library-welcome');
    var icon = el('div', 'library-welcome-icon');
    icon.innerHTML = window.LqdIcons ? window.LqdIcons.icon('library') : '';
    wrap.appendChild(icon);
    wrap.appendChild(el('h3', 'library-welcome-title', title || '浏览个人图书馆'));
    wrap.appendChild(el('p', 'library-welcome-hint', hint || '从左侧书架选择文档，然后在目录树中选择章节阅读。'));
    node.appendChild(wrap);
  }

  function typeLabel(type) {
    var found = DOC_TYPES.find(function (item) { return item.key === type; });
    return found ? found.label : type;
  }

  function persist(session, patch) {
    if (!window.LqdTabs || !session) return;
    window.LqdTabs.updateTabState(session.tabId, patch);
  }

  function renderShelf(session) {
    var shelf = session.shelfEl;
    empty(shelf);
    var subtabs = el('div', 'library-subtabs');
    DOC_TYPES.forEach(function (item) {
      var tab = el('div', 'library-subtab' + (item.key === session.type ? ' active' : ''), item.label);
      tab.setAttribute('role', 'tab');
      tab.setAttribute('tabindex', '0');
      tab.setAttribute('aria-selected', String(item.key === session.type));
      function activate() {
        if (item.key === session.type) return;
        session.type = item.key;
        session.slug = null;
        session.nodeId = null;
        session.currentDoc = null;
        session.docs = [];
        persist(session, { type: item.key, slug: null, nodeId: null, doc: null });
        loadDocs(session, false);
      }
      tab.addEventListener('click', activate);
      tab.addEventListener('keydown', function (event) { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); activate(); } });
      subtabs.appendChild(tab);
    });
    shelf.appendChild(subtabs);
    if (!session.docs.length) {
      shelf.appendChild(el('div', 'library-empty', '暂无' + typeLabel(session.type)));
      return;
    }
    session.docs.forEach(function (doc) {
      var card = el('div', 'shelf-card' + (session.slug === doc.id ? ' active' : ''));
      card.appendChild(el('div', 'shelf-card-title', doc.title));
      if (doc.author) card.appendChild(el('div', 'shelf-card-author', doc.author));
      if (doc.description) card.appendChild(el('div', 'shelf-card-desc', doc.description));
      card.addEventListener('click', function () { selectDoc(session, session.type, doc.id, null); });
      shelf.appendChild(card);
    });
  }

  function loadDocs(session, preserveContent) {
    if (!isAlive(session)) return;
    session.docsVersion = (session.docsVersion || 0) + 1;
    var version = session.docsVersion;
    if (!preserveContent) {
      showLoading(session.shelfEl, '加载文档列表…', 'list');
      session.docs = [];
      renderShelf(session);
    }
    fetch('/api/content/docs?type=' + encodeURIComponent(session.type))
      .then(function (response) { if (!response.ok) throw new Error('HTTP ' + response.status); return response.json(); })
      .then(function (data) {
        if (!isAlive(session) || version !== session.docsVersion) return;
        session.generation = data.generation || null;
        session.viewId = data.view_id || null;
        session.docs = data.docs || [];
        renderShelf(session);
        if (!preserveContent) {
          renderWelcome(session.treeEl, '选择文档', '从书架中选择一本书、论文或笔记。');
          renderWelcome(session.readerEl);
        }
      })
      .catch(function (error) { if (isAlive(session) && version === session.docsVersion) showError(session.shelfEl, '加载失败：' + error.message); });
  }

  function renderTree(session, doc) {
    empty(session.treeEl);
    session.treeEl.appendChild(el('div', 'tree-header', doc.title || doc.doc_name));
    var structure = doc.structure || [];
    if (!structure.length) { session.treeEl.appendChild(el('div', 'library-empty', '无目录结构')); return; }
    structure.forEach(function (node) { session.treeEl.appendChild(renderTreeNode(session, node, 0)); });
  }

  function renderTreeNode(session, node, depth) {
    var wrapper = el('div', 'tree-node');
    wrapper.style.marginLeft = 'calc(' + depth + ' * var(--space-2-5))';
    var row = el('div', 'tree-node-row' + (session.nodeId === node.node_id ? ' active' : ''));
    row.setAttribute('role', 'button'); row.setAttribute('tabindex', '0'); row.setAttribute('aria-label', node.title || '(无标题)');
    var hasChildren = node.nodes && node.nodes.length;
    var toggle = el('span', 'tree-toggle' + (hasChildren ? '' : ' leaf'));
    if (hasChildren) toggle.innerHTML = window.LqdIcons ? window.LqdIcons.icon('chevron-right') : '›';
    row.appendChild(toggle); row.appendChild(el('span', 'tree-node-title', node.title));
    function activate(event) {
      event.stopPropagation();
      session.nodeId = node.node_id;
      persist(session, { nodeId: session.nodeId });
      renderTree(session, session.currentDoc);
      selectSection(session, node);
    }
    row.addEventListener('click', activate);
    row.addEventListener('keydown', function (event) { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); activate(event); } });
    wrapper.appendChild(row);
    if (hasChildren) {
      var children = el('div', 'tree-children');
      node.nodes.forEach(function (child) { children.appendChild(renderTreeNode(session, child, depth + 1)); });
      wrapper.appendChild(children);
    }
    return wrapper;
  }

  function renderReaderHeader(session, doc) {
    empty(session.readerEl);
    var header = el('div', 'reader-header');
    header.appendChild(el('div', 'reader-title', doc.title || doc.doc_name));
    var meta = [doc.author, doc.year, doc.date].filter(Boolean);
    if (meta.length) header.appendChild(el('div', 'reader-meta', meta.join(' · ')));
    if (doc.description) header.appendChild(el('div', 'reader-desc', doc.description));
    session.readerEl.appendChild(header);
    var content = el('div', 'reader-content');
    renderWelcome(content, '选择章节', '在左侧目录树中点击章节，开始阅读正文。');
    session.readerEl.appendChild(content);
  }

  function findNode(nodes, nodeId) {
    for (var index = 0; index < (nodes || []).length; index++) {
      var node = nodes[index];
      if (node.node_id === nodeId) return node;
      var nested = findNode(node.nodes || [], nodeId);
      if (nested) return nested;
    }
    return null;
  }

  function selectDoc(session, type, slug, nodeId) {
    if (!isAlive(session)) return;
    session.type = sessions().normalizeType(type);
    session.slug = slug;
    session.nodeId = nodeId || null;
    persist(session, { type: session.type, slug: slug, nodeId: session.nodeId });
    renderShelf(session);
    showLoading(session.treeEl, '加载目录…', 'list');
    showLoading(session.readerEl, '加载文档…', 'block');
    var request = sessions().begin(session);
    var options = request.signal ? { signal: request.signal } : {};
    fetch('/api/content/read?type=' + encodeURIComponent(session.type) + '&slug=' + encodeURIComponent(slug) + pinQuery(session), options)
      .then(function (response) { if (!response.ok) throw new Error('HTTP ' + response.status); return response.json(); })
      .then(function (doc) {
        if (!sessions().isCurrent(session, request.version)) return;
        session.currentDoc = doc;
        persist(session, { type: session.type, slug: slug, nodeId: session.nodeId, doc: doc });
        renderTree(session, doc);
        renderReaderHeader(session, doc);
        if (session.nodeId) {
          var target = findNode(doc.structure || [], session.nodeId);
          if (target) selectSection(session, target);
        }
        if (window.LqdEvents) window.LqdEvents.emit('library:doc:loaded', { tabId: session.tabId, doc: doc, type: session.type, slug: slug });
        loadDocs(session, true);
      })
      .catch(function (error) {
        if (!isAlive(session) || error.name === 'AbortError') return;
        showError(session.treeEl, '加载失败：' + error.message);
        showError(session.readerEl, '加载失败：' + error.message);
      });
  }

  function renderSection(session, text) {
    var content = session.readerEl.querySelector('.reader-content');
    if (!content) { content = el('div', 'reader-content'); empty(session.readerEl); session.readerEl.appendChild(content); }
    empty(content);
    if (!text || !text.trim()) { content.appendChild(el('div', 'library-empty', '该章节无正文内容')); return; }
    if (!window.YuuRender) { content.appendChild(el('pre', null, text)); return; }
    content.innerHTML = window.YuuRender.md(text);
    window.YuuRender.renderKatex(content);
    var id = session.type.replace(/s$/, '') + ':' + session.slug;
    if (window.LqdWikilinks) window.LqdWikilinks.hydrate(content, id);
    if (window.LqdLinkPopover) window.LqdLinkPopover.attach(content);
    if (window.LqdKnowledge) window.LqdKnowledge.renderReader(content, id);
  }

  function selectSection(session, node) {
    if (!isAlive(session)) return;
    if (!node.source_md) { renderSection(session, node.text || ''); return; }
    var content = session.readerEl.querySelector('.reader-content');
    if (content) showLoading(content, '加载正文…');
    var request = sessions().begin(session);
    var options = request.signal ? { signal: request.signal } : {};
    var url = '/api/content/section?source_md=' + encodeURIComponent(node.source_md) + '&line_num=' + (node.line_num || 0) + '&line_end=' + (node.line_end || '') + '&type=' + encodeURIComponent(session.type) + '&slug=' + encodeURIComponent(session.slug) + pinQuery(session);
    fetch(url, options)
      .then(function (response) { if (!response.ok) throw new Error('HTTP ' + response.status); return response.text(); })
      .then(function (text) { if (sessions().isCurrent(session, request.version)) renderSection(session, text); })
      .catch(function (error) { if (isAlive(session) && error.name !== 'AbortError') showError(session.readerEl, '加载失败：' + error.message); });
  }

  function initLibrary(container, tab) {
    if (!container || !tab || !sessions()) throw new Error('library session runtime unavailable');
    sessions().dispose(tab.id);
    var session = sessions().create(tab.id, tab.state || {});
    sessions().bind(session, {
      shelfEl: container.querySelector('.library-shelf'),
      treeEl: container.querySelector('.library-tree'),
      readerEl: container.querySelector('.library-reader')
    });
    activeSession = session;
    if (session.slug) selectDoc(session, session.type, session.slug, session.nodeId);
    else loadDocs(session, false);
    bindEvents();
    return session;
  }

  function unmountLibrary(tab) {
    if (!tab) return;
    if (activeSession && activeSession.tabId === tab.id) activeSession = null;
    sessions().dispose(tab.id);
  }

  function bindEvents() {
    if (eventsBound || !window.LqdEvents) return;
    eventsBound = true;
    window.LqdEvents.on('library:node:select', function (payload) {
      var session = activeSession;
      if (!session || !payload) return;
      var type = payload.type ? sessions().normalizeType(payload.type) : session.type;
      var slug = payload.slug || session.slug;
      var nodeId = payload.nodeId || payload.node_id || null;
      if (type !== session.type || slug !== session.slug) { selectDoc(session, type, slug, nodeId); return; }
      var node = findNode(session.currentDoc && session.currentDoc.structure, nodeId);
      if (node) { session.nodeId = nodeId; persist(session, { nodeId: nodeId }); renderTree(session, session.currentDoc); selectSection(session, node); }
    });
  }

  window.initLibrary = initLibrary;
  window.unmountLibrary = unmountLibrary;
  window.selectDoc = function (type, slug) { if (activeSession) selectDoc(activeSession, type, slug, null); };
})();
