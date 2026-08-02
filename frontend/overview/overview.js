/**
 * LQ-D — Overview Content
 * 右侧上下文面板内容实现，适配 core/ 框架。
 * 监听 chat:context、tab:activated、activity:changed 事件。
 */
(function () {
  'use strict';

  var state = {
    activity: 'chat',
    activeTab: null,
    chatContexts: [],
    lastStatus: null
  };

  function $(id) { return document.getElementById(id); }

  function getBody() { return $('lqd-overview-body'); }
  function getHeader() { return $('lqd-overview-header'); }

  function setHeader(title) {
    var h = getHeader();
    if (h) h.textContent = title || '上下文';
  }

  function escapeHtml(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function icon(name) {
    return window.LqdIcons && typeof window.LqdIcons.icon === 'function' ? window.LqdIcons.icon(name) : '';
  }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function renderEmpty(container, message) {
    container.appendChild(el('div', 'lqd-empty', message || '暂无内容'));
  }

  function createActionButton(label, iconName, onClick) {
    var btn = document.createElement('button');
    btn.className = 'lqd-btn lqd-btn--block lqd-overview-action';
    btn.setAttribute('aria-label', label);
    btn.innerHTML = icon(iconName) + ' <span>' + escapeHtml(label) + '</span>';
    btn.addEventListener('click', onClick);
    return btn;
  }

  function renderActions(container) {
    var section = el('div', 'lqd-overview-section');
    section.appendChild(el('div', 'lqd-overview-section-title', '快捷操作'));

    var actions = el('div', 'lqd-overview-actions');
    actions.appendChild(createActionButton('新建对话', 'new', function () {
      if (window.LqdChat && typeof window.LqdChat.openNewChat === 'function') {
        window.LqdChat.openNewChat();
      } else if (window.LqdTabs) {
        window.LqdTabs.open({ type: 'chat', title: '新对话' });
      }
    }));
    actions.appendChild(createActionButton('上传文档', 'upload', function () {
      if (window.LqdShell && typeof window.LqdShell.setActivity === 'function') {
        window.LqdShell.setActivity('upload');
      } else if (window.LqdEvents) {
        window.LqdEvents.emit('activity:changed', { activity: 'upload' });
      }
    }));
    actions.appendChild(createActionButton('重建索引', 'refresh', function () {
      triggerBuild('incremental');
    }));

    section.appendChild(actions);
    container.appendChild(section);
  }

  function triggerBuild(mode) {
    if (window.LqdManage && typeof window.LqdManage.buildIndex === 'function') {
      window.LqdManage.buildIndex(mode);
    } else if (window.YuuManage && typeof window.YuuManage.buildIndex === 'function') {
      window.YuuManage.buildIndex(mode);
    } else if (window.LqdEvents) {
      window.LqdEvents.emit('index:build', { mode: mode });
    }
  }

  // ── Chat 场景 ───────────────────────────────────────────────────────────
  function renderChatOverview(container) {
    setHeader('上下文');
    renderActions(container);

    var section = el('div', 'lqd-overview-section');
    section.appendChild(el('div', 'lqd-overview-section-title', '检索引用'));

    if (!state.chatContexts || !state.chatContexts.length) {
      section.appendChild(el('div', 'lqd-overview-hint', '发送消息后将显示检索到的引用片段'));
      container.appendChild(section);
      return;
    }

    var list = el('div', 'lqd-citation-list');
    var seenDocs = {};

    for (var i = 0; i < state.chatContexts.length; i++) {
      var c = state.chatContexts[i];
      var title = c.docTitle || '未知文档';
      var breadcrumb = (c.breadcrumb || []).join(' > ');
      var snippet = (c.text || '').slice(0, 140);
      var item = el('div', 'lqd-citation-item');
      item.innerHTML =
        '<div class="lqd-citation-title">' + escapeHtml(title) + '</div>' +
        '<div class="lqd-citation-meta">' + escapeHtml(breadcrumb) + (c.docType ? ' · ' + escapeHtml(c.docType) : '') + '</div>' +
        '<div class="lqd-citation-snippet">' + escapeHtml(snippet) + (c.text && c.text.length > 140 ? '…' : '') + '</div>';
      item.addEventListener('click', (function (ctx) {
        return function () {
          if (window.LqdEvents) {
            window.LqdEvents.emit('search:result:selected', {
              type: ctx.docType || '',
              slug: ctx.docId || '',
              node_id: ctx.nodeId || '',
              title: ctx.docTitle || '',
              breadcrumb: ctx.breadcrumb || []
            });
          }
        };
      })(c));
      list.appendChild(item);

      var docKey = c.docTitle || c.sourceId || String(i);
      seenDocs[docKey] = true;
    }
    section.appendChild(list);
    container.appendChild(section);

    var docSection = el('div', 'lqd-overview-section');
    docSection.appendChild(el('div', 'lqd-overview-section-title', '引用文档'));
    var docList = el('div', 'lqd-citation-doc-list');
    Object.keys(seenDocs).forEach(function (docKey) {
      docList.appendChild(el('div', 'lqd-citation-doc', escapeHtml(docKey)));
    });
    docSection.appendChild(docList);
    container.appendChild(docSection);
  }

  // ── Library 场景 ─────────────────────────────────────────────────────────
  function renderMetaItem(container, label, value) {
    if (value === null || value === undefined || value === '') return;
    var row = el('div', 'lqd-meta-item');
    row.innerHTML =
      '<span class="lqd-meta-label">' + escapeHtml(label) + '</span>' +
      '<span class="lqd-meta-value">' + escapeHtml(value) + '</span>';
    container.appendChild(row);
  }

  function renderOutlineNode(container, node, depth) {
    if (!node) return;
    var hasChildren = node.nodes && node.nodes.length > 0;
    var row = el('div', 'lqd-outline-node-row');
    row.style.marginLeft = 'calc(' + depth + ' * var(--space-3))';

    var toggle = el('span', 'lqd-outline-toggle' + (hasChildren ? '' : ' leaf'));
    if (hasChildren) {
      toggle.innerHTML = icon('chevron-right');
    }
    row.appendChild(toggle);
    row.appendChild(el('span', 'lqd-outline-node-title', node.title || '(无标题)'));

    row.addEventListener('click', function (e) {
      e.stopPropagation();
      if (window.LqdEvents) {
        window.LqdEvents.emit('library:node:select', {
          node_id: node.node_id || '',
          title: node.title || ''
        });
      }
    });
    container.appendChild(row);

    if (hasChildren) {
      var children = el('div', 'lqd-outline-children');
      for (var j = 0; j < node.nodes.length; j++) {
        renderOutlineNode(children, node.nodes[j], depth + 1);
      }
      container.appendChild(children);
    }
  }

  function renderLibraryOverview(container, tab) {
    setHeader('文档概览');
    var doc = tab && tab.state && tab.state.doc ? tab.state.doc : null;
    if (!doc) {
      renderEmpty(container, '选择文档以查看元信息和目录');
      return;
    }

    var metaSection = el('div', 'lqd-overview-section');
    metaSection.appendChild(el('div', 'lqd-overview-section-title', '文档信息'));
    var metaList = el('div', 'lqd-meta-list');
    renderMetaItem(metaList, '标题', doc.title || doc.doc_name);
    renderMetaItem(metaList, '作者', doc.author);
    renderMetaItem(metaList, '年份', doc.year);
    renderMetaItem(metaList, '日期', doc.date);
    renderMetaItem(metaList, '类型', doc.type);
    renderMetaItem(metaList, '描述', doc.description);
    metaSection.appendChild(metaList);

    if (doc.tags && doc.tags.length) {
      var tags = el('div', 'lqd-doc-tags');
      for (var i = 0; i < doc.tags.length; i++) {
        tags.appendChild(el('span', 'lqd-doc-tag', doc.tags[i]));
      }
      metaSection.appendChild(tags);
    }
    container.appendChild(metaSection);

    var outlineSection = el('div', 'lqd-overview-section');
    outlineSection.appendChild(el('div', 'lqd-overview-section-title', '目录大纲'));
    var tree = el('div', 'lqd-outline-tree');
    var structure = doc.structure || [];
    if (!structure.length) {
      tree.appendChild(el('div', 'lqd-overview-hint', '该文档暂无目录'));
    } else {
      for (var k = 0; k < structure.length; k++) {
        renderOutlineNode(tree, structure[k], 0);
      }
    }
    outlineSection.appendChild(tree);
    container.appendChild(outlineSection);

    var tabState = tab && tab.state ? tab.state : {};
    var kind = String(tabState.type || doc.type || '').replace(/s$/, '');
    var slug = tabState.slug || doc.doc_name || doc.id;
    if (window.LqdKnowledge && kind && slug) {
      window.LqdKnowledge.renderOverview(container, kind + ':' + slug);
    }
  }

  // ── Manage 场景 ──────────────────────────────────────────────────────────
  function getStatus() {
    if (state.lastStatus) return state.lastStatus;
    if (window.LqdStore && typeof window.LqdStore.get === 'function') {
      return window.LqdStore.get('status') || {};
    }
    return {};
  }

  function renderStatusBadge(container, text, type) {
    container.appendChild(el('span', 'lqd-status-badge lqd-status-badge--' + type, text));
  }

  function renderManageOverview(container) {
    setHeader('任务进度');
    var status = getStatus();

    var summary = el('div', 'lqd-status-summary');

    var indexRow = el('div', 'lqd-status-row');
    indexRow.appendChild(el('span', 'lqd-status-label', '索引'));
    if (status.indexRunning) {
      renderStatusBadge(indexRow, '构建中', 'running');
    } else if (status.indexReady) {
      renderStatusBadge(indexRow, '就绪', 'done');
    } else {
      renderStatusBadge(indexRow, '未构建', 'failed');
    }
    summary.appendChild(indexRow);

    var ingestRow = el('div', 'lqd-status-row');
    ingestRow.appendChild(el('span', 'lqd-status-label', '入库'));
    if (status.ingestRunning) {
      renderStatusBadge(ingestRow, '运行中', 'running');
    } else {
      renderStatusBadge(ingestRow, '空闲', 'done');
    }
    summary.appendChild(ingestRow);

    if (status.provider || status.model) {
      var modelRow = el('div', 'lqd-status-row');
      modelRow.appendChild(el('span', 'lqd-status-label', '模型'));
      modelRow.appendChild(el('span', 'lqd-status-value', escapeHtml((status.provider || '-') + ' / ' + (status.model || '-'))));
      summary.appendChild(modelRow);
    }

    container.appendChild(summary);

    var actionSection = el('div', 'lqd-overview-section');
    actionSection.appendChild(el('div', 'lqd-overview-section-title', '快捷操作'));
    var actions = el('div', 'lqd-overview-actions');
    actions.appendChild(createActionButton('全量构建', 'refresh', function () { triggerBuild('full'); }));
    actions.appendChild(createActionButton('增量构建', 'refresh', function () { triggerBuild('incremental'); }));
    actionSection.appendChild(actions);
    container.appendChild(actionSection);
  }

  // ── Upload 场景 ──────────────────────────────────────────────────────────
  function renderUploadOverview(container) {
    setHeader('上传队列');
    var queue = (window.YuuUploadQueue && typeof window.YuuUploadQueue.all === 'function')
      ? window.YuuUploadQueue.all()
      : [];

    var summary = el('div', 'lqd-status-summary');
    var counts = { pending: 0, running: 0, done: 0, failed: 0 };
    queue.forEach(function (item) {
      if (counts[item.status] !== undefined) counts[item.status]++;
    });

    var row = el('div', 'lqd-status-row');
    row.appendChild(el('span', 'lqd-status-label', '总计'));
    row.appendChild(el('span', 'lqd-status-value', String(queue.length)));
    summary.appendChild(row);

    var detailRow = el('div', 'lqd-status-row');
    detailRow.appendChild(el('span', 'lqd-status-label', '状态'));
    detailRow.appendChild(el('span', 'lqd-status-value',
      '待处理 ' + counts.pending + ' / 进行中 ' + counts.running + ' / 完成 ' + counts.done + ' / 失败 ' + counts.failed));
    summary.appendChild(detailRow);
    container.appendChild(summary);

    var actionSection = el('div', 'lqd-overview-section');
    actionSection.appendChild(el('div', 'lqd-overview-section-title', '快捷操作'));
    var actions = el('div', 'lqd-overview-actions');
    actions.appendChild(createActionButton('新建对话', 'new', function () {
      if (window.LqdShell) window.LqdShell.setActivity('chat');
    }));
    actions.appendChild(createActionButton('重建索引', 'refresh', function () { triggerBuild('incremental'); }));
    actionSection.appendChild(actions);
    container.appendChild(actionSection);
  }

  // ── Config 场景 ──────────────────────────────────────────────────────────
  function renderConfigOverview(container) {
    setHeader('配置');
    var section = el('div', 'lqd-overview-section');
    section.appendChild(el('div', 'lqd-overview-section-title', '快捷键'));
    var shortcuts = el('div', 'lqd-shortcut-list');
    var items = [
      { keys: 'Ctrl/Cmd + K', desc: '打开命令面板' },
      { keys: 'Ctrl/Cmd + W', desc: '关闭当前标签' },
      { keys: 'Esc', desc: '关闭面板/对话框' }
    ];
    items.forEach(function (it) {
      var row = el('div', 'lqd-shortcut-row');
      row.innerHTML = '<kbd class="lqd-kbd">' + escapeHtml(it.keys) + '</kbd><span class="lqd-shortcut-desc">' + escapeHtml(it.desc) + '</span>';
      shortcuts.appendChild(row);
    });
    section.appendChild(shortcuts);
    container.appendChild(section);

    var actionSection = el('div', 'lqd-overview-section');
    actionSection.appendChild(el('div', 'lqd-overview-section-title', '快捷操作'));
    var actions = el('div', 'lqd-overview-actions');
    actions.appendChild(createActionButton('新建对话', 'new', function () {
      if (window.LqdShell) window.LqdShell.setActivity('chat');
    }));
    actionSection.appendChild(actions);
    container.appendChild(actionSection);
  }

  // ── 默认场景 ─────────────────────────────────────────────────────────────
  function renderDefaultOverview(container, activity) {
    setHeader('上下文');
    renderEmpty(container, '当前视图暂无上下文');
  }

  // ── 渲染调度 ───────────────────────────────────────────────────────────────
  function renderTo(container) {
    if (!container) return;
    container.innerHTML = '';
    switch (state.activity) {
      case 'chat':
        renderChatOverview(container);
        break;
      case 'library':
        renderLibraryOverview(container, state.activeTab);
        break;
      case 'manage':
        renderManageOverview(container);
        break;
      case 'upload':
        renderUploadOverview(container);
        break;
      case 'config':
        renderConfigOverview(container);
        break;
      default:
        renderDefaultOverview(container, state.activity);
    }
  }

  function renderOverview(container, tab) {
    state.activeTab = tab || (window.LqdTabs ? window.LqdTabs.active() : null);
    state.activity = window.LqdStore ? window.LqdStore.get('activity') : 'chat';
    renderTo(container);
  }

  // ── 事件处理 ───────────────────────────────────────────────────────────────
  function hasCoreOverview() {
    return !!(window.LqdOverview && typeof window.LqdOverview.register === 'function');
  }

  function onChatContext(payload) {
    state.chatContexts = payload && payload.contexts ? payload.contexts.slice() : [];
    if (hasCoreOverview()) {
      window.LqdOverview.refresh();
    } else {
      renderTo(getBody());
    }
  }

  function onTabActivated(payload) {
    state.activeTab = payload && payload.tab ? payload.tab : null;
    if (!hasCoreOverview()) renderTo(getBody());
  }

  function onActivityChanged(payload) {
    state.activity = payload && payload.activity ? payload.activity : 'chat';
    if (!hasCoreOverview()) renderTo(getBody());
  }

  function onIndexStatus(payload) {
    state.lastStatus = payload || {};
    if (state.activity === 'manage') {
      if (hasCoreOverview()) {
        window.LqdOverview.refresh();
      } else {
        renderTo(getBody());
      }
    }
  }

  function init() {
    if (window.LqdEvents) {
      window.LqdEvents.on('chat:context', onChatContext);
      window.LqdEvents.on('tab:activated', onTabActivated);
      window.LqdEvents.on('activity:changed', onActivityChanged);
      window.LqdEvents.on('index:status', onIndexStatus);
      // 文档加载后重渲染 library overview(tab:activated 只在切换时触发,不够)
      window.LqdEvents.on('library:doc:loaded', function (payload) {
        if (payload && payload.doc) {
          state.activeTab = window.LqdTabs ? window.LqdTabs.active() : state.activeTab;
          if (state.activeTab) {
            state.activeTab.state = state.activeTab.state || {};
            state.activeTab.state.doc = payload.doc;
          }
        }
        if (hasCoreOverview()) window.LqdOverview.refresh();
        else renderTo(getBody());
      });
    }

    if (hasCoreOverview()) {
      var adapter = { renderOverview: renderOverview };
      ['chat', 'library', 'manage', 'upload', 'config'].forEach(function (activity) {
        window.LqdOverview.register(activity, adapter);
      });
    } else {
      renderTo(getBody());
    }
  }

  window.LqdOverviewContent = {
    init: init,
    renderOverview: renderOverview,
    refresh: function () { renderTo(getBody()); }
  };

  init();
})();
