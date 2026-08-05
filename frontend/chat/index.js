/**
 * LQ-D — Chat Tab Component
 *
 * 注册 window.LqdChat 标签组件，组合 LLM / Session / Citations / Messages / Composer / Agent。
 * 无浮动弹窗、无抽屉、无旧 Settings 表单；完全适配 LQ-D core/ 框架。
 */
(function () {
  'use strict';

  var tabRefs = new Map(); // tabId -> { root, emptyEl, messagesEl, composerInput, sendBtn, suggestionsEl }

  function createChatDOM(container, tab) {
    var root = document.createElement('div');
    root.className = 'lqd-chat';
    root.setAttribute('data-tab-id', tab.id);
    root.innerHTML =
      '<div class="lqd-chat-empty">' +
        '<div class="lqd-chat-welcome">' +
          '<div class="lqd-chat-welcome-logo">' + (window.LqdIcons ? window.LqdIcons.icon('book') : '') + '</div>' +
          '<h2>有什么想从图书馆了解的？</h2>' +
          '<div class="lqd-chat-actions">' +
            '<button class="lqd-chat-action" data-action="ask">' +
              '<span class="lqd-chat-action-icon">' + (window.LqdIcons ? window.LqdIcons.icon('search') : '') + '</span>' +
              '<span class="lqd-chat-action-text">检索提问</span>' +
            '</button>' +
            '<button class="lqd-chat-action" data-action="summarize">' +
              '<span class="lqd-chat-action-icon">' + (window.LqdIcons ? window.LqdIcons.icon('summarize') : '') + '</span>' +
              '<span class="lqd-chat-action-text">总结一篇文档</span>' +
            '</button>' +
            '<button class="lqd-chat-action" data-action="upload">' +
              '<span class="lqd-chat-action-icon">' + (window.LqdIcons ? window.LqdIcons.icon('upload') : '') + '</span>' +
              '<span class="lqd-chat-action-text">上传新文档</span>' +
            '</button>' +
            '<button class="lqd-chat-action" data-action="rebuild-index">' +
              '<span class="lqd-chat-action-icon">' + (window.LqdIcons ? window.LqdIcons.icon('refresh') : '') + '</span>' +
              '<span class="lqd-chat-action-text">重建检索索引</span>' +
            '</button>' +
          '</div>' +
          '<div class="lqd-chat-prompts" hidden></div>' +
        '</div>' +
      '</div>' +
      '<div class="lqd-chat-messages" role="log" aria-live="polite" hidden></div>';
    container.appendChild(root);

    var emptyEl = root.querySelector('.lqd-chat-empty');
    var messagesEl = root.querySelector('.lqd-chat-messages');
    var suggestionsEl = root.querySelector('.lqd-chat-prompts');

    // 欢迎页动作卡片
    var actionsEl = root.querySelector('.lqd-chat-actions');
    if (actionsEl) {
      actionsEl.querySelectorAll('[data-action]').forEach(function (btn) {
        btn.addEventListener('click', function () {
          var action = btn.getAttribute('data-action');
          var refs = tabRefs.get(tab.id);
          if (action === 'ask') {
            if (refs) window.LqdChatComposer.focus(refs.composerInput);
          } else if (action === 'summarize') {
            if (refs) {
              refs.composerInput.value = '总结这篇文档的核心内容：';
              refs.composerInput.dispatchEvent(new Event('input'));
              window.LqdChatComposer.focus(refs.composerInput);
              refs.composerInput.setSelectionRange(refs.composerInput.value.length, refs.composerInput.value.length);
            }
          } else if (action === 'upload') {
            if (window.LqdShell) window.LqdShell.setActivity('upload');
          } else if (action === 'rebuild-index') {
            triggerBuild('incremental');
          }
        });
      });
    }

    // 输入区放在 root 底部
    var composer = window.LqdChatComposer.create(root, {
      onSend: function (query) { onSend(query, tab.id); }
    });

    return {
      root: root,
      emptyEl: emptyEl,
      messagesEl: messagesEl,
      composerInput: composer.input,
      sendBtn: composer.sendBtn,
      suggestionsEl: suggestionsEl
    };
  }

  function renderMessages(refs, messages) {
    refs.messagesEl.innerHTML = '';
    if (!messages || !messages.length) {
      window.LqdChatMessages.showEmpty(refs.emptyEl, refs.messagesEl);
      return;
    }
    window.LqdChatMessages.hideEmpty(refs.emptyEl, refs.messagesEl);
    for (var i = 0; i < messages.length; i++) {
      var m = messages[i];
      // 历史消息重渲染:assistant 存的是原始 markdown(流式时的 finalText),
      // 需先经 renderMarkdown 渲染成 HTML 再插入,否则切回标签后
      // 会看到裸 markdown(##、**、表格、$..$ 未渲染)。user 仍是纯文本。
      var content = m.content;
      if (m.role === 'assistant') {
        content = window.LqdChatMessages.renderMarkdown(m.content);
        // 拼接流式时存下的引用卡片 HTML,恢复"正文 + 参考来源"完整结构
        if (m.citations) content += m.citations;
      }
      var bubble = window.LqdChatMessages.appendMessageBubble(m.role, content, refs.messagesEl);
      if (m.role === 'assistant' && bubble) {
        window.LqdChatMessages.reRenderKatex(bubble);
        bubble.removeAttribute('aria-busy');
      }
    }
  }

  function onSend(query, tabId) {
    var refs = tabRefs.get(tabId);
    if (!refs) return;
    refs.composerInput.value = '';
    refs.composerInput.style.height = 'auto';
    window.LqdChatAgent.sendMessage(query, refs).then(function () {
      if (window.LqdEvents) {
        window.LqdEvents.emit('chat:session:changed', {});
      }
    }).catch(function (e) {
      if (window.console && window.console.error) {
        window.console.error('[LqdChat] sendMessage failed', e);
      }
    });
  }

  // ── 引用浮窗(Popover) ──
  // 点击引用编号后先弹出浮窗显示缩略信息,点击"查看详情"再跳转。
  var popoverEl = null;

  function closeCitationPopover() {
    if (popoverEl) {
      popoverEl.classList.remove('lqd-citation-popover--show');
      var el = popoverEl;
      setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 180);
      popoverEl = null;
    }
    document.removeEventListener('click', onPopoverOutside, true);
    document.removeEventListener('keydown', onPopoverEsc, true);
  }

  function onPopoverOutside(e) {
    if (popoverEl && !popoverEl.contains(e.target)) {
      // 点击浮窗内部不关闭,由内部按钮处理
      closeCitationPopover();
    }
  }

  function onPopoverEsc(e) {
    if (e.key === 'Escape') {
      e.preventDefault();
      closeCitationPopover();
    }
  }

  // 微型 Markdown 渲染器:支持标题、粗体、斜体、行内代码、链接、无序列表、有序列表、引用块、分隔线
  // 轻量级实现,不依赖 marked.js,适合浮窗等小空间场景
  function miniMarkdown(src) {
    if (!src) return '';
    var esc = window.LqdChatCitations ? window.LqdChatCitations.escHtml : function (s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); };

    // 先按行分割,逐行处理
    var lines = String(src).split('\n');
    var html = [];
    var inUl = false, inOl = false, inQuote = false;

    function closeLists() {
      if (inUl) { html.push('</ul>'); inUl = false; }
      if (inOl) { html.push('</ol>'); inOl = false; }
    }
    function closeQuote() {
      if (inQuote) { html.push('</blockquote>'); inQuote = false; }
    }

    // 行内格式:粗体、斜体、行内代码、链接
    function inline(text) {
      var s = esc(text);
      // 行内代码 `code`
      s = s.replace(/`([^`]+)`/g, '<code class="lqd-mini-code">$1</code>');
      // 链接 [text](url)
      s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
      // 粗体 **text**
      s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
      // 斜体 *text* (避免与粗体冲突,要求*后非空格)
      s = s.replace(/(^|[^*])\*([^*]+)\*/g, '$1<em>$2</em>');
      return s;
    }

    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];
      var trimmed = line.trim();

      // 空行
      if (!trimmed) { closeLists(); closeQuote(); continue; }

      // 分隔线
      if (/^(-{3,}|\*{3,}|_{3,})$/.test(trimmed)) {
        closeLists(); closeQuote();
        html.push('<hr class="lqd-mini-hr">');
        continue;
      }

      // 标题 # ~ ######
      var hMatch = trimmed.match(/^(#{1,6})\s+(.+)$/);
      if (hMatch) {
        closeLists(); closeQuote();
        var level = hMatch[1].length;
        html.push('<h' + level + ' class="lqd-mini-h lqd-mini-h' + level + '">' + inline(hMatch[2]) + '</h' + level + '>');
        continue;
      }

      // 引用块 >
      if (trimmed.charAt(0) === '>') {
        closeLists();
        if (!inQuote) { html.push('<blockquote class="lqd-mini-quote">'); inQuote = true; }
        html.push('<p>' + inline(trimmed.replace(/^>\s*/, '')) + '</p>');
        continue;
      }
      closeQuote();

      // 无序列表 - / * / +
      var ulMatch = trimmed.match(/^[-*+]\s+(.+)$/);
      if (ulMatch) {
        if (inOl) { html.push('</ol>'); inOl = false; }
        if (!inUl) { html.push('<ul class="lqd-mini-ul">'); inUl = true; }
        html.push('<li>' + inline(ulMatch[1]) + '</li>');
        continue;
      }

      // 有序列表 1.
      var olMatch = trimmed.match(/^\d+\.\s+(.+)$/);
      if (olMatch) {
        if (inUl) { html.push('</ul>'); inUl = false; }
        if (!inOl) { html.push('<ol class="lqd-mini-ol">'); inOl = true; }
        html.push('<li>' + inline(olMatch[1]) + '</li>');
        continue;
      }

      // 普通段落
      closeLists();
      html.push('<p class="lqd-mini-p">' + inline(trimmed) + '</p>');
    }
    closeLists(); closeQuote();
    return html.join('\n');
  }

  function showCitationPopover(anchorEl, title, preview, onOpenDetail) {
    closeCitationPopover();

    var esc = window.LqdChatCitations ? window.LqdChatCitations.escHtml : function (s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); };

    var pop = document.createElement('div');
    pop.className = 'lqd-citation-popover';
    pop.setAttribute('role', 'dialog');
    pop.setAttribute('aria-label', title || '引用预览');
    pop.innerHTML =
      '<div class="lqd-citation-popover-header">' +
        '<span class="lqd-citation-popover-icon">' + (window.LqdIcons ? window.LqdIcons.icon('file-text') : '') + '</span>' +
        '<span class="lqd-citation-popover-title">' + esc(title || '来源') + '</span>' +
        '<button class="lqd-citation-popover-close" aria-label="关闭">' +
          (window.LqdIcons ? window.LqdIcons.icon('close') : '×') +
        '</button>' +
      '</div>' +
      '<div class="lqd-citation-popover-body">' + miniMarkdown(preview || '暂无预览内容') + '</div>' +
      '<div class="lqd-citation-popover-footer">' +
        '<button class="lqd-citation-popover-detail">' +
          (window.LqdIcons ? window.LqdIcons.icon('arrow-right') : '') +
          '<span>查看详情</span>' +
        '</button>' +
      '</div>';

    document.body.appendChild(pop);
    popoverEl = pop;

    // 定位:基于 anchorEl 的位置,智能避免超出视口
    if (anchorEl && anchorEl.getBoundingClientRect) {
      var rect = anchorEl.getBoundingClientRect();
      var popRect = pop.getBoundingClientRect();
      var margin = 8;
      var left = rect.left;
      var top = rect.bottom + margin;

      // 水平:超出右侧则向左偏移
      if (left + popRect.width > window.innerWidth - margin) {
        left = window.innerWidth - popRect.width - margin;
      }
      // 水平:不能小于左侧
      if (left < margin) left = margin;

      // 垂直:下方放不下则放上方
      if (top + popRect.height > window.innerHeight - margin) {
        var altTop = rect.top - popRect.height - margin;
        if (altTop > margin) {
          top = altTop;
        } else {
          // 上下都放不下,取较小溢出
          top = Math.max(margin, window.innerHeight - popRect.height - margin);
        }
      }

      pop.style.left = left + 'px';
      pop.style.top = top + 'px';
    } else {
      // 无锚点时居中
      pop.style.left = '50%';
      pop.style.top = '50%';
      pop.style.transform = 'translate(-50%, -50%)';
    }

    // 触发显示动画
    requestAnimationFrame(function () {
      pop.classList.add('lqd-citation-popover--show');
    });

    // 事件绑定
    pop.querySelector('.lqd-citation-popover-close').addEventListener('click', function (e) {
      e.stopPropagation();
      closeCitationPopover();
    });

    pop.querySelector('.lqd-citation-popover-detail').addEventListener('click', function (e) {
      e.stopPropagation();
      closeCitationPopover();
      if (typeof onOpenDetail === 'function') onOpenDetail();
    });

    // 点击浮窗内部阻止冒泡到 document(避免 onPopoverOutside 误关)
    pop.addEventListener('click', function (e) {
      e.stopPropagation();
    });

    // 延迟绑定外部点击关闭,避免当前 click 事件立即触发
    setTimeout(function () {
      document.addEventListener('click', onPopoverOutside, true);
      document.addEventListener('keydown', onPopoverEsc, true);
    }, 0);
  }

  function mount(container, tab) {
    var refs = createChatDOM(container, tab);
    tabRefs.set(tab.id, refs);

    // 参考来源点击:先弹出浮窗显示缩略信息,点击"查看详情"再跳转。
    // citations.js 渲染的引用项带 data-doc-type/data-doc-id/data-node-id/data-preview。
    refs.messagesEl.addEventListener('click', function (e) {
      var item = e.target && e.target.closest ? e.target.closest('.lqd-chat-citation') : null;
      if (!item) return;
      e.preventDefault();
      e.stopPropagation();

      var preview = item.getAttribute('data-preview') || '';
      var citationText = item.querySelector('.lqd-chat-citation-text');
      var title = citationText ? citationText.textContent : '来源';

      // 本机文件:浮窗内容增加文件路径、页码、行号信息
      if (item.classList.contains('lqd-chat-citation--localfile')) {
        var lfPath = item.getAttribute('data-localfile-path') || '';
        var lfPage = item.getAttribute('data-localfile-page') || '';
        var lfLine = item.getAttribute('data-localfile-line') || '';
        var lfMeta = '**文件路径:** `' + lfPath + '`';
        if (lfPage) lfMeta += '\n\n**页码:** ' + lfPage;
        if (lfLine) lfMeta += '  |  **行号:** ' + lfLine;
        lfMeta += '\n\n---\n\n' + preview;
        preview = lfMeta;
      }

      // 弹出浮窗
      showCitationPopover(item, title, preview, function () {
        // 点击"查看详情"回调

        // 本机文件引用:不跳转文档库,只显示文件路径+内容片段(已在浮窗中展示)
        // 用户可通过浮窗内容获取文件路径和定位信息
        if (item.classList.contains('lqd-chat-citation--localfile')) {
          return;
        }

        // 文档引用:打开文档节点
        var docType = item.getAttribute('data-doc-type') || '';
        var docId = item.getAttribute('data-doc-id') || '';
        var nodeId = item.getAttribute('data-node-id') || '';
        if (!docType || !docId) return;
        if (window.LqdEvents) {
          window.LqdEvents.emit('search:result:selected', {
            doc_type: docType, slug: docId, node_id: nodeId
          });
        }
        if (window.LqdLibrary && typeof window.LqdLibrary.openDoc === 'function') {
          window.LqdLibrary.openDoc(docType, docId, nodeId);
        }
      });
    });

    var messages = window.LqdChatSession.loadCurrent();
    renderMessages(refs, messages);

    var suggestions = window.LqdChatComposer.buildDynamicSuggestions();
    window.LqdChatComposer.renderSuggestions(refs.suggestionsEl, suggestions, function (q) {
      onSend(q, tab.id);
    });

    window.LqdChatComposer.focus(refs.composerInput);
  }

  function unmount(container, tab) {
    // M3: 流式输出中切标签时,把已收到的部分回答存入 session,
    // 避免用户切回后看到空白或丢失流式内容。
    // agent.js 的 sendMessage 在流式过程中通过 contentEl 实时更新 DOM,
    // unmount 时 DOM 被清除,但 session 里可能只有 user 消息没有 assistant 回答。
    // 这里检查:如果 session 里最后一条是 user 消息(说明 assistant 还没存),
    // 尝试从 DOM 读取当前流式内容并保存。
    var messages = window.LqdChatSession.loadCurrent(tab.id);
    if (messages && messages.length) {
      var last = messages[messages.length - 1];
      if (last.role === 'user') {
        // 流式未完成,尝试从 DOM 读取部分回答
        var refs = tabRefs.get(tab.id);
        if (refs && refs.messagesEl) {
          var bubbles = refs.messagesEl.querySelectorAll('.lqd-chat-message');
          if (bubbles.length > 0) {
            var lastBubble = bubbles[bubbles.length - 1];
            if (lastBubble.getAttribute('data-role') === 'assistant' || lastBubble.classList.contains('lqd-chat-message--assistant')) {
              var partialText = lastBubble.textContent || '';
              if (partialText && partialText.trim()) {
                window.LqdChatSession.appendToCurrent('assistant', partialText.trim(), {}, tab.id);
              }
            }
          }
        }
      }
    }
    tabRefs.delete(tab.id);
  }

  function getTitle(tab) {
    var messages = window.LqdChatSession.loadCurrent();
    var first = '';
    for (var i = 0; i < messages.length; i++) {
      if (messages[i].role === 'user' && messages[i].content) {
        first = messages[i].content;
        break;
      }
    }
    return tab.title || first.slice(0, 20) || '新对话';
  }

  function getIcon() {
    return 'chat';
  }

  function getSidebarActions() {
    return [{
      icon: 'new',
      title: '新建对话',
      onClick: function () { openNewChat(); }
    }];
  }

  // Codex 侧栏时间格式:今天显示时分,否则相对天数/周数
  function relativeTime(iso) {
    var d = new Date(iso);
    var now = new Date();
    var diffMs = now - d;
    var diffDays = Math.floor(diffMs / 86400000);
    if (diffDays <= 0 && d.getDate() === now.getDate()) {
      return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
    }
    if (diffDays < 1) return '1d';
    if (diffDays < 7) return diffDays + 'd';
    if (diffDays < 30) return Math.floor(diffDays / 7) + 'w';
    return d.toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' });
  }

  // ── 上下文菜单(删除/置顶) ──
  var ctxMenuEl = null;
  var ctxTargetId = null;

  function hideCtxMenu() {
    if (ctxMenuEl) { ctxMenuEl.remove(); ctxMenuEl = null; ctxTargetId = null; }
  }

  function showCtxMenu(e, sid) {
    hideCtxMenu();
    ctxTargetId = sid;
    var sessions = window.LqdChatSession.listArchived();
    var isPinned = false;
    for (var i = 0; i < sessions.length; i++) {
      if (sessions[i].id === sid) { isPinned = !!sessions[i].pinned; break; }
    }
    var menu = document.createElement('div');
    menu.className = 'lqd-ctx-menu';
    menu.style.left = e.pageX + 'px';
    menu.style.top = e.pageY + 'px';
    menu.setAttribute('role', 'menu');
    // 置顶/取消置顶
    var pinItem = document.createElement('div');
    pinItem.className = 'lqd-ctx-menu-item';
    pinItem.setAttribute('role', 'menuitem');
    pinItem.innerHTML = '<span class="lqd-icon">' + (window.LqdIcons ? window.LqdIcons.icon('pin') : '') + '</span><span>' + (isPinned ? '取消置顶' : '置顶') + '</span>';
    pinItem.addEventListener('click', function () {
      hideCtxMenu();
      if (isPinned) {
        window.LqdChatSession.unpinArchived(sid);
      } else {
        window.LqdChatSession.pinArchived(sid);
      }
      if (window.LqdEvents) window.LqdEvents.emit('chat:history:changed', {});
      // 重新渲染侧栏
      var sidebarBody = document.querySelector('.lqd-sidebar-body');
      if (sidebarBody) renderSidebar(sidebarBody);
    });
    menu.appendChild(pinItem);
    // 分隔线
    var sep = document.createElement('div');
    sep.className = 'lqd-ctx-menu-sep';
    menu.appendChild(sep);
    // 删除
    var delItem = document.createElement('div');
    delItem.className = 'lqd-ctx-menu-item lqd-ctx-menu-item--danger';
    delItem.setAttribute('role', 'menuitem');
    delItem.innerHTML = '<span class="lqd-icon">' + (window.LqdIcons ? window.LqdIcons.icon('trash') : '') + '</span><span>删除</span>';
    delItem.addEventListener('click', function () {
      hideCtxMenu();
      if (!window.LqdModal) {
        // 降级:直接删除
        window.LqdChatSession.removeArchived(sid);
        if (window.LqdEvents) window.LqdEvents.emit('chat:history:changed', {});
        var sidebarBody = document.querySelector('.lqd-sidebar-body');
        if (sidebarBody) renderSidebar(sidebarBody);
        return;
      }
      window.LqdModal.confirm({
        title: '删除对话',
        message: '确定要删除这个历史对话吗？此操作不可恢复。',
        danger: true,
        confirmLabel: '删除'
      }).then(function (ok) {
        if (!ok) return;
        window.LqdChatSession.removeArchived(sid);
        if (window.LqdEvents) window.LqdEvents.emit('chat:history:changed', {});
        var sidebarBody = document.querySelector('.lqd-sidebar-body');
        if (sidebarBody) renderSidebar(sidebarBody);
      });
    });
    menu.appendChild(delItem);
    document.body.appendChild(menu);
    ctxMenuEl = menu;
    // 点击其他地方关闭
    setTimeout(function () {
      document.addEventListener('click', hideCtxMenu, { once: true });
      document.addEventListener('contextmenu', hideCtxMenu, { once: true });
    }, 0);
  }

  function renderSidebar(container) {
    container.innerHTML = '';

    var sessions = window.LqdChatSession.listArchived();
    if (!sessions.length) {
      var empty = document.createElement('div');
      empty.className = 'lqd-empty';
      empty.textContent = '暂无历史对话';
      container.appendChild(empty);
      return;
    }

    var list = document.createElement('div');
    list.className = 'lqd-chat-history-list';
    for (var i = 0; i < sessions.length; i++) {
      var s = sessions[i];
      var ds = relativeTime(s.date);
      var item = document.createElement('div');
      item.className = 'lqd-chat-history-item';
      item.setAttribute('data-id', s.id);
      item.setAttribute('role', 'button');
      item.setAttribute('tabindex', '0');
      item.setAttribute('aria-label', s.title || '历史对话');
      // 置顶标记
      var pinHtml = s.pinned ? '<span class="lqd-chat-history-pin">' + (window.LqdIcons ? window.LqdIcons.icon('pin') : '📌') + '</span>' : '';
      item.innerHTML =
        pinHtml +
        '<span class="lqd-chat-history-title">' + window.LqdChatCitations.escHtml(s.title) + '</span>' +
        '<span class="lqd-chat-history-meta">' + ds + '</span>' +
        '<button class="lqd-chat-history-del" aria-label="删除对话">' + (window.LqdIcons ? window.LqdIcons.icon('trash') : '×') + '</button>';

      (function (id) {
        function activate(e) {
          if (e.target.closest('.lqd-chat-history-del')) return;
          window.LqdChatSession.restoreArchived(id);
          if (window.LqdEvents) {
            window.LqdEvents.emit('chat:session:restored', { sessionId: id });
          }
          // 刷新当前活动标签
          var activeTab = window.LqdTabs ? window.LqdTabs.active() : null;
          if (activeTab && activeTab.type === 'chat') {
            var refs = tabRefs.get(activeTab.id);
            if (refs) renderMessages(refs, window.LqdChatSession.loadCurrent());
            window.LqdTabs.updateTabState(activeTab.id, function (t) {
              t.title = window.LqdChat.getTitle(t);
            });
          }
        }
        item.addEventListener('click', activate);
        item.addEventListener('keydown', function (e) {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            activate(e);
          }
        });
        item.addEventListener('contextmenu', function (e) {
          e.preventDefault();
          showCtxMenu(e, id);
        });
        item.querySelector('.lqd-chat-history-del').addEventListener('click', function (e) {
          e.stopPropagation();
          // 带确认的删除
          if (!window.LqdModal) {
            window.LqdChatSession.removeArchived(id);
            if (window.LqdEvents) window.LqdEvents.emit('chat:history:changed', {});
            renderSidebar(container);
            return;
          }
          window.LqdModal.confirm({
            title: '删除对话',
            message: '确定要删除这个历史对话吗？此操作不可恢复。',
            danger: true,
            confirmLabel: '删除'
          }).then(function (ok) {
            if (!ok) return;
            window.LqdChatSession.removeArchived(id);
            if (window.LqdEvents) window.LqdEvents.emit('chat:history:changed', {});
            renderSidebar(container);
          });
        });
      })(s.id);

      list.appendChild(item);
    }
    container.appendChild(list);
  }

  function renderOverview(container, tab) {
    container.innerHTML = '';
    var section = document.createElement('div');
    section.className = 'lqd-chat-overview';
    section.innerHTML =
      '<div class="lqd-overview-section-title">AI 问答</div>' +
      '<p class="lqd-overview-hint">基于个人数字图书馆内容的 RAG 问答。问题会先检索相关段落，再生成带引用标注的回答。</p>';
    container.appendChild(section);
  }

  function openNewChat() {
    window.LqdChatSession.archiveCurrent();
    window.LqdChatSession.clearCurrent();
    if (!window.LqdTabs) return;

    // Codex 单会话布局:优先复用任何 chat 标签(不限于 active),避免隐藏标签堆积。
    // 例外:流式输出进行中(sendBtn 禁用)时新开标签,让旧回答写回旧会话,
    // 避免完成后落入新会话造成污染。
    var active = window.LqdTabs.active();
    var busy = false;
    var targetTab = null;

    if (active && active.type === 'chat') {
      var activeRefs = tabRefs.get(active.id);
      busy = !!(activeRefs && activeRefs.sendBtn && activeRefs.sendBtn.disabled);
    }

    if (active && active.type === 'chat' && !busy) {
      targetTab = active;
    } else if (!busy) {
      // 当前活跃标签不是 chat(或 chat 在流式中不可复用):
      // 查找是否有其他已有的 chat 标签可以复用
      var allTabs = window.LqdTabs.list();
      for (var i = 0; i < allTabs.length; i++) {
        if (allTabs[i].type === 'chat') {
          // 找一个不在流式中的 chat 标签
          var checkRefs = tabRefs.get(allTabs[i].id);
          var isBusy = !!(checkRefs && checkRefs.sendBtn && checkRefs.sendBtn.disabled);
          if (!isBusy) {
            targetTab = allTabs[i];
            break;
          }
        }
      }
    }

    if (targetTab) {
      var refs = tabRefs.get(targetTab.id);
      if (refs) {
        refs.composerInput.value = '';
        refs.composerInput.style.height = 'auto';
        renderMessages(refs, []);
        var suggestions = window.LqdChatComposer.buildDynamicSuggestions();
        window.LqdChatComposer.renderSuggestions(refs.suggestionsEl, suggestions, function (q) {
          onSend(q, targetTab.id);
        });
        window.LqdChatComposer.focus(refs.composerInput);
      }
      window.LqdTabs.updateTabState(targetTab.id, function (t) { t.title = '新对话'; });
      window.LqdTabs.activate(targetTab.id);
      if (window.LqdEvents) window.LqdEvents.emit('chat:session:changed', {});
      return targetTab.id;
    }

    // 没有任何可复用的 chat 标签,新建一个
    var tab = window.LqdTabs.open({ type: 'chat', title: '新对话' });
    if (window.LqdShell) window.LqdShell.setActivity('chat');
    return tab;
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

  function openSession(id) {
    window.LqdChatSession.restoreArchived(id);
    if (window.LqdTabs) {
      var tab = window.LqdTabs.open({ type: 'chat', title: getTitle({}) });
      if (window.LqdShell) window.LqdShell.setActivity('chat');
      return tab;
    }
  }

  var LqdChat = {
    type: 'chat',
    getTitle: getTitle,
    getIcon: getIcon,
    getSidebarActions: getSidebarActions,
    mount: mount,
    unmount: unmount,
    renderSidebar: renderSidebar,
    renderOverview: renderOverview,
    openNewChat: openNewChat,
    openSession: openSession
  };

  window.LqdChat = LqdChat;

  // 注册到 core 框架
  function tryRegister() {
    if (window.LqdTabs) window.LqdTabs.register('chat', LqdChat);
    if (window.LqdSidebar) window.LqdSidebar.register('chat', LqdChat);
    if (window.LqdOverview) window.LqdOverview.register('chat', LqdChat);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', tryRegister);
  } else {
    tryRegister();
  }
})();
