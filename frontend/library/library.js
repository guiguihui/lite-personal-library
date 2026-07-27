/**
 * Library 文档浏览面板。
 *
 * 三栏布局:书架(shelf) → 目录树(tree) → 阅读区(reader)。
 *
 * 端点(后端 app/http/routes_content.py):
 *   GET /api/content/docs?type=books        列该 type 的文档
 *   GET /api/content/read?type=books&slug=x 读文档树(structure)
 *   GET /api/content/section?source_md=...&line_num=0&line_end=10  读正文片段
 *
 * 渲染:window.YuuRender.md(text) + renderKatex(el)(frontend/shared/render.js)。
 */
(function () {
  'use strict';

  var DOC_TYPES = [
    { key: 'books', label: '书籍' },
    { key: 'papers', label: '论文' },
    { key: 'notes', label: '笔记' }
  ];

  var state = {
    currentType: 'books',
    currentSlug: null,
    currentNodeId: null,
    docs: []          // 当前 type 的文档列表
  };

  // ---- DOM 引用 ----
  var shelfEl, treeEl, readerEl;

  // ---- 工具 ----
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function empty(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function showLoading(node, msg, skeletonType) {
    empty(node);
    // 工作流 G:优先用骨架占位(taste-skill: skeleton loaders, never circular spinners)
    if (window.LqdSkeleton && skeletonType) {
      var html = '';
      if (skeletonType === 'list') html = window.LqdSkeleton.list({ count: 5, itemHeight: 32 });
      else if (skeletonType === 'text') html = window.LqdSkeleton.text({ lines: 3 });
      else if (skeletonType === 'block') html = window.LqdSkeleton.block({ width: '100%', height: 400 });
      if (html) { node.innerHTML = html; return; }
    }
    node.appendChild(el('div', 'library-loading', msg || '加载中...'));
  }

  function showEmpty(node, msg) {
    empty(node);
    node.appendChild(el('div', 'library-empty', msg || '暂无内容'));
  }

  function showError(node, msg) {
    empty(node);
    node.appendChild(el('div', 'library-error', msg || '加载失败'));
  }

  function escapeHtml(s) {
    if (!s) return '';
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function renderWelcome(container, title, hint) {
    empty(container);
    var wrap = el('div', 'library-welcome');
    var iconEl = el('div', 'library-welcome-icon');
    iconEl.innerHTML = window.LqdIcons ? window.LqdIcons.icon('library') : '';
    wrap.appendChild(iconEl);
    wrap.appendChild(el('h3', 'library-welcome-title', title || '浏览个人图书馆'));
    wrap.appendChild(el('p', 'library-welcome-hint', hint || '从左侧书架选择文档，然后在目录树中选择章节阅读。'));
    container.appendChild(wrap);
  }

  // ---- 书架栏 ----
  function renderShelf() {
    empty(shelfEl);

    // 子标签(books/papers/notes)
    var subtabs = el('div', 'library-subtabs');
    DOC_TYPES.forEach(function (t) {
      var tab = el('div', 'library-subtab' + (t.key === state.currentType ? ' active' : ''), t.label);
      tab.setAttribute('role', 'tab');
      tab.setAttribute('tabindex', '0');
      tab.setAttribute('aria-selected', String(t.key === state.currentType));
      tab.addEventListener('click', function () {
        if (t.key !== state.currentType) {
          state.currentType = t.key;
          state.currentSlug = null;
          state.currentNodeId = null;
          loadDocs();
        }
      });
      tab.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          tab.click();
        }
      });
      subtabs.appendChild(tab);
    });
    shelfEl.appendChild(subtabs);

    // 文档卡片
    if (!state.docs.length) {
      shelfEl.appendChild(el('div', 'library-empty', '暂无' +
        DOC_TYPES.find(function (t) { return t.key === state.currentType; }).label));
      return;
    }
    state.docs.forEach(function (doc) {
      var card = el('div', 'shelf-card' +
        (state.currentSlug === doc.id ? ' active' : ''));

      card.appendChild(el('div', 'shelf-card-title', doc.title));
      if (doc.author) card.appendChild(el('div', 'shelf-card-author', doc.author));
      if (doc.description) card.appendChild(el('div', 'shelf-card-desc', doc.description));

      if (doc.tags && doc.tags.length) {
        var tags = el('div', 'shelf-card-tags');
        doc.tags.forEach(function (tag) {
          tags.appendChild(el('span', 'shelf-tag', tag));
        });
        card.appendChild(tags);
      }

      card.addEventListener('click', function () {
        selectDoc(state.currentType, doc.id);
      });
      shelfEl.appendChild(card);
    });
  }

  // ---- 目录树栏 ----
  function renderTree(doc) {
    empty(treeEl);

    var header = el('div', 'tree-header', doc.title || doc.doc_name);
    treeEl.appendChild(header);

    var structure = doc.structure || [];
    if (!structure.length) {
      treeEl.appendChild(el('div', 'library-empty', '无目录结构'));
      return;
    }

    structure.forEach(function (node) {
      treeEl.appendChild(renderTreeNode(node, 0));
    });
  }

  function renderTreeNode(node, depth) {
    var wrapper = el('div', 'tree-node');
    wrapper.style.marginLeft = 'calc(' + depth + ' * var(--space-2-5))';

    var hasChildren = node.nodes && node.nodes.length > 0;
    var row = el('div', 'tree-node-row' +
      (state.currentNodeId === node.node_id ? ' active' : ''));
    row.setAttribute('role', 'button');
    row.setAttribute('tabindex', '0');
    row.setAttribute('aria-label', node.title || '(无标题)');

    var toggle = el('span', 'tree-toggle' + (hasChildren ? '' : ' leaf'));
    if (hasChildren) {
      toggle.innerHTML = window.LqdIcons ? window.LqdIcons.icon('chevron-right') : '▸';
    }
    row.appendChild(toggle);

    var title = el('span', 'tree-node-title', node.title);
    row.appendChild(title);

    function onActivate(e) {
      e.stopPropagation();
      if (e.target === toggle && hasChildren) {
        // 点 toggle 只折叠/展开
        toggleCollapse(wrapper, toggle);
        return;
      }
      // 选中并加载正文
      state.currentNodeId = node.node_id;
      // 重新渲染树以更新 active 状态
      var doc = state._currentDoc;
      renderTree(doc);
      // 但要恢复展开状态:简化处理,直接加载正文
      selectSection(node);
    }
    // 点击行:加载正文 + 切换折叠
    row.addEventListener('click', onActivate);
    // 键盘:Enter/Space 激活(工作流 H a11y)
    row.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        onActivate(e);
      }
    });

    wrapper.appendChild(row);

    if (hasChildren) {
      var children = el('div', 'tree-children');
      node.nodes.forEach(function (child) {
        children.appendChild(renderTreeNode(child, depth + 1));
      });
      wrapper.appendChild(children);
    }

    return wrapper;
  }

  function toggleCollapse(wrapper, toggle) {
    var children = wrapper.querySelector('.tree-children');
    if (!children) return;
    var collapsed = children.classList.toggle('collapsed');
    toggle.innerHTML = collapsed
      ? (window.LqdIcons ? window.LqdIcons.icon('chevron-right') : '▸')
      : (window.LqdIcons ? window.LqdIcons.icon('chevron-down') : '▾');
  }

  // ---- 阅读区 ----
  function renderReaderHeader(doc) {
    empty(readerEl);

    var header = el('div', 'reader-header');
    header.appendChild(el('div', 'reader-title', doc.title || doc.doc_name));
    var metaParts = [];
    if (doc.author) metaParts.push(doc.author);
    if (doc.year) metaParts.push(String(doc.year));
    if (doc.date) metaParts.push(doc.date);
    if (metaParts.length) header.appendChild(el('div', 'reader-meta', metaParts.join(' · ')));
    if (doc.description) header.appendChild(el('div', 'reader-desc', doc.description));
    if (doc.tags && doc.tags.length) {
      var tags = el('div', 'shelf-card-tags');
      doc.tags.forEach(function (tag) {
        tags.appendChild(el('span', 'shelf-tag', tag));
      });
      header.appendChild(tags);
    }
    readerEl.appendChild(header);

    var content = el('div', 'reader-content');
    renderWelcome(content, '选择章节', '在左侧目录树中点击章节，开始阅读正文。');
    readerEl.appendChild(content);
  }

  function renderSection(text) {
    var content = readerEl.querySelector('.reader-content');
    if (!content) {
      empty(readerEl);
      content = el('div', 'reader-content');
      readerEl.appendChild(content);
    }
    empty(content);

    if (!text || !text.trim()) {
      content.appendChild(el('div', 'library-empty', '该章节无正文内容'));
      return;
    }

    // 用 YuuRender 渲染 markdown
    if (window.YuuRender) {
      content.innerHTML = window.YuuRender.md(text);
      // KaTeX 渲染数学公式
      window.YuuRender.renderKatex(content);
    } else {
      // 降级:纯文本
      var pre = el('pre', null, text);
      content.appendChild(pre);
    }
  }

  // ---- 数据加载 ----
  function loadDocs() {
    showLoading(shelfEl, '加载文档列表...');
    state.docs = [];
    renderShelf();

    fetch('/api/content/docs?type=' + encodeURIComponent(state.currentType))
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      })
      .then(function (data) {
        state.docs = data.docs || [];
        renderShelf();
        renderWelcome(treeEl, '选择文档', '从书架中选择一本书、论文或笔记。');
        renderWelcome(readerEl);
      })
      .catch(function (err) {
        showError(shelfEl, '加载失败:' + err.message);
      });
  }

  function selectDoc(type, slug) {
    state.currentType = type;
    state.currentSlug = slug;
    state.currentNodeId = null;
    renderShelf(); // 更新 active 状态

    showLoading(treeEl, '加载目录...', 'list');
    showLoading(readerEl, '加载文档...', 'block');

    fetch('/api/content/read?type=' + encodeURIComponent(type) +
      '&slug=' + encodeURIComponent(slug))
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      })
      .then(function (doc) {
        state._currentDoc = doc;
        renderTree(doc);
        renderReaderHeader(doc);
        // 把已加载文档推入 tab.state(公共 API),供 Overview 渲染元信息+大纲
        if (window.LqdTabs) {
          var active = window.LqdTabs.active();
          if (active && active.type === 'library') {
            window.LqdTabs.updateTabState(active.id, function (t) { t.doc = doc; });
          }
        }
        // 发事件让 Overview 重渲染(tab:activated 只在切换时触发,文档加载后需主动 refresh)
        if (window.LqdEvents) window.LqdEvents.emit('library:doc:loaded', { doc: doc, type: type, slug: slug });
      })
      .catch(function (err) {
        showError(treeEl, '加载失败:' + err.message);
        showError(readerEl, '加载失败:' + err.message);
      });
  }

  function selectSection(node) {
    if (!node.source_md) {
      // 无 source_md 的节点(如纯容器节点),尝试用 text 字段
      if (node.text) {
        renderSection(node.text);
      } else {
        var content = readerEl.querySelector('.reader-content');
        if (content) {
          empty(content);
          content.appendChild(el('div', 'library-empty', '该节点无正文内容'));
        }
      }
      return;
    }

    var content = readerEl.querySelector('.reader-content');
    if (content) {
      empty(content);
      content.appendChild(el('div', 'library-loading', '加载正文...'));
    }

    var lineNum = node.line_num || 0;
    var lineEnd = node.line_end || '';
    var url = '/api/content/section?source_md=' + encodeURIComponent(node.source_md) +
      '&line_num=' + lineNum + '&line_end=' + lineEnd;

    fetch(url)
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.text();
      })
      .then(function (text) {
        renderSection(text);
      })
      .catch(function (err) {
        var c = readerEl.querySelector('.reader-content');
        if (c) {
          empty(c);
          c.appendChild(el('div', 'library-error', '加载失败:' + err.message));
        }
      });
  }

  // 按 node_id 递归查找 node(供 library:node:select 事件跳转)
  function findNodeById(nodes, nodeId) {
    if (!nodes) return null;
    for (var i = 0; i < nodes.length; i++) {
      var n = nodes[i];
      if (n.node_id === nodeId) return n;
      if (n.nodes && n.nodes.length) {
        var found = findNodeById(n.nodes, nodeId);
        if (found) return found;
      }
    }
    return null;
  }

  // ---- 初始化 ----
  function initLibrary() {
    shelfEl = document.getElementById('library-shelf');
    treeEl = document.getElementById('library-tree');
    readerEl = document.getElementById('library-reader');
    if (!shelfEl || !treeEl || !readerEl) {
      console.error('[Library] missing layout elements');
      return;
    }
    loadDocs();

    // 监听 library:node:select 事件(open-doc.js 和 overview 大纲点击均会发)
    // 跨模块通信:只走事件总线,不直接调他模块内部函数
    if (window.LqdEvents) {
      window.LqdEvents.on('library:node:select', function (payload) {
        if (!payload) return;
        var nodeId = payload.nodeId || payload.node_id;
        if (!nodeId) return;
        // 若指定了 type/slug 且与当前不同,先切换文档
        if (payload.type && payload.slug && (payload.type !== state.currentType || payload.slug !== state.currentSlug)) {
          selectDoc(payload.type, payload.slug);
          // 文档加载是异步的,节点跳转在 library:doc:loaded 后再触发(简化:延迟重试一次)
          setTimeout(function () {
            var node = findNodeById(state._currentDoc && state._currentDoc.structure, nodeId);
            if (node) selectSection(node);
          }, 400);
          return;
        }
        var node = findNodeById(state._currentDoc && state._currentDoc.structure, nodeId);
        if (node) selectSection(node);
      });
    }
  }

  // 导出(供 index.html DOMContentLoaded 调用,以及 open-doc.js / index.js mount 恢复文档)
  window.initLibrary = initLibrary;
  window.selectDoc = selectDoc;
})();
