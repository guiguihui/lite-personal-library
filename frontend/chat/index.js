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
        '<div class="lqd-chat-empty-icon">' + (window.LqdIcons ? window.LqdIcons.icon('sparkles') : '') + '</div>' +
        '<h2>向图书馆提问</h2>' +
        '<p>我会从书籍、论文和笔记中检索相关内容，生成带依据的回答。</p>' +
        '<div class="lqd-chat-prompts"></div>' +
        '<div class="lqd-chat-shortcuts">' +
          '<button class="lqd-chat-shortcut" data-action="new-chat">' + (window.LqdIcons ? window.LqdIcons.icon('new') : '') + ' 新建对话</button>' +
          '<button class="lqd-chat-shortcut" data-action="upload">' + (window.LqdIcons ? window.LqdIcons.icon('upload') : '') + ' 上传文档</button>' +
          '<button class="lqd-chat-shortcut" data-action="rebuild-index">' + (window.LqdIcons ? window.LqdIcons.icon('refresh') : '') + ' 重建索引</button>' +
        '</div>' +
      '</div>' +
      '<div class="lqd-chat-messages" role="log" aria-live="polite" hidden></div>';
    container.appendChild(root);

    var emptyEl = root.querySelector('.lqd-chat-empty');
    var messagesEl = root.querySelector('.lqd-chat-messages');
    var suggestionsEl = root.querySelector('.lqd-chat-prompts');

    // 空状态快捷入口
    var shortcutsEl = root.querySelector('.lqd-chat-shortcuts');
    if (shortcutsEl) {
      shortcutsEl.querySelectorAll('[data-action]').forEach(function (btn) {
        btn.addEventListener('click', function () {
          var action = btn.getAttribute('data-action');
          if (action === 'new-chat') {
            openNewChat();
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
      window.LqdChatMessages.appendMessageBubble(m.role, m.content, refs.messagesEl);
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

  function mount(container, tab) {
    var mainBody = container.closest && container.closest('.lqd-main-body');
    if (mainBody) mainBody.scrollTop = 0;

    var refs = createChatDOM(container, tab);
    tabRefs.set(tab.id, refs);

    var messages = window.LqdChatSession.loadCurrent();
    renderMessages(refs, messages);

    var suggestions = window.LqdChatComposer.buildDynamicSuggestions();
    window.LqdChatComposer.renderSuggestions(refs.suggestionsEl, suggestions, function (q) {
      onSend(q, tab.id);
    });

    window.LqdChatComposer.focus(refs.composerInput);
  }

  function unmount(container, tab) {
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

  function renderSidebar(container) {
    container.innerHTML = '';
    var header = document.createElement('div');
    header.className = 'lqd-sidebar-section-title';
    header.textContent = '历史会话';
    container.appendChild(header);

    var sessions = window.LqdChatSession.listArchived();
    if (!sessions.length) {
      var empty = document.createElement('div');
      empty.className = 'lqd-empty';
      empty.textContent = '暂无历史会话';
      container.appendChild(empty);
      return;
    }

    var list = document.createElement('div');
    list.className = 'lqd-chat-history-list';
    for (var i = 0; i < sessions.length; i++) {
      var s = sessions[i];
      var d = new Date(s.date);
      var ds = d.toLocaleString('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
      var item = document.createElement('div');
      item.className = 'lqd-chat-history-item';
      item.setAttribute('data-id', s.id);
      item.setAttribute('role', 'button');
      item.setAttribute('tabindex', '0');
      item.setAttribute('aria-label', s.title || '历史对话');
      item.innerHTML =
        '<span class="lqd-chat-history-title">' + window.LqdChatCitations.escHtml(s.title) + '</span>' +
        '<span class="lqd-chat-history-meta">' + ds + ' · ' + (s.messages ? s.messages.length : 0) + ' 条</span>' +
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
        item.querySelector('.lqd-chat-history-del').addEventListener('click', function (e) {
          e.stopPropagation();
          window.LqdChatSession.removeArchived(id);
          if (window.LqdEvents) {
            window.LqdEvents.emit('chat:history:changed', {});
          }
          renderSidebar(container);
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
    if (window.LqdTabs) {
      var tab = window.LqdTabs.open({ type: 'chat', title: '新对话' });
      if (window.LqdShell) window.LqdShell.setActivity('chat');
      return tab;
    }
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
