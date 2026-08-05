/**
 * LQ-D — Chat Messages
 *
 * 消息气泡、思考过程、工具调用轨迹、空状态、KaTeX 重渲染。
 */
(function () {
  'use strict';

  function escHtml(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function renderMarkdown(text) {
    if (!text) return '';
    if (window.YuuRender && typeof window.YuuRender.md === 'function') {
      return window.YuuRender.md(text);
    }
    // 降级：简单 markdown 渲染
    var html = escHtml(text);
    html = html.replace(/```([\s\S]*?)```/g, function (_, code) {
      return '<pre><code>' + escHtml(code.trim()) + '</code></pre>';
    });
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
    html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/\*([^*]+)\*/g, '<em>$1</em>');
    html = html.replace(/^### (.+)$/gm, '<h4>$1</h4>');
    html = html.replace(/^## (.+)$/gm, '<h3>$1</h3>');
    html = html.replace(/^# (.+)$/gm, '<h2>$1</h2>');
    html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    html = html.replace(/\n\n/g, '</p><p>').replace(/\n/g, '<br>');
    return '<p>' + html + '</p>';
  }

  function renderToolTrail(toolTrail) {
    if (!toolTrail || !toolTrail.length) return '';
    return '<div class="lqd-tool-trail">' + toolTrail.join('') + '</div>';
  }

  function renderThinkingAndText(thinking, text, toolTrail) {
    var parts = [];
    if (thinking) {
      parts.push(
        '<details class="lqd-thinking"><summary>思考过程</summary>' +
        '<div class="lqd-thinking-body">' + renderMarkdown(thinking) + '</div></details>'
      );
    }
    if (toolTrail && toolTrail.length) {
      parts.push(renderToolTrail(toolTrail));
    }
    if (text) {
      parts.push(renderMarkdown(text));
    } else if (!parts.length) {
      parts.push('<em>……</em>');
    }
    return parts.join('');
  }

  function appendMessageBubble(role, text, container) {
    var el = document.createElement('div');
    el.className = 'lqd-chat-message ' + role;
    el.setAttribute('data-role', role);
    if (role === 'assistant') {
      // 流式期间标记 busy,供屏幕阅读器播报(工作流 G)
      el.setAttribute('aria-busy', 'true');
      el.innerHTML = '<div class="lqd-chat-msg-content">' + text + '</div>' +
        '<div class="lqd-chat-msg-actions" hidden>' +
          '<button class="lqd-msg-action lqd-msg-copy" type="button" title="复制回答">' +
            (window.LqdIcons ? window.LqdIcons.icon('copy') : '复制') +
          '</button>' +
          '<button class="lqd-msg-action lqd-msg-regen" type="button" title="重新生成">' +
            (window.LqdIcons ? window.LqdIcons.icon('refresh') : '重试') +
          '</button>' +
        '</div>';
    } else {
      el.textContent = text;
      el.innerHTML = '<span class="lqd-chat-msg-text">' + escHtml(text) + '</span>' +
        '<div class="lqd-chat-msg-actions" hidden>' +
          '<button class="lqd-msg-action lqd-msg-copy" type="button" title="复制">' +
            (window.LqdIcons ? window.LqdIcons.icon('copy') : '复制') +
          '</button>' +
        '</div>';
    }
    container.appendChild(el);
    container.scrollTop = container.scrollHeight;
    // 悬浮显示操作条
    if (window.LqdChatMessages && typeof window.LqdChatMessages.attachHoverActions === 'function') {
      window.LqdChatMessages.attachHoverActions(el);
    }
    return role === 'assistant' ? el.querySelector('.lqd-chat-msg-content') : el;
  }

  // 消息悬浮操作条:鼠标移入消息显示复制/重新生成
  function attachHoverActions(el) {
    if (!el || !el.classList || !el.classList.contains('lqd-chat-message')) return;
    var actionsEl = el.querySelector('.lqd-chat-msg-actions');
    if (!actionsEl) return;
    el.addEventListener('mouseenter', function () { actionsEl.hidden = false; });
    el.addEventListener('mouseleave', function () { actionsEl.hidden = true; });
    el.addEventListener('focusin', function () { actionsEl.hidden = false; });
    el.addEventListener('focusout', function (e) {
      if (!el.contains(e.relatedTarget)) actionsEl.hidden = true;
    });
  }

  function setBusy(busy, sendBtn, composerInput) {
    if (sendBtn) sendBtn.disabled = busy;
    if (composerInput) composerInput.disabled = busy;
  }

  function reRenderKatex(el) {
    if (typeof renderMathInElement !== 'function') return;
    try {
      renderMathInElement(el, {
        delimiters: [
          { left: '$$', right: '$$', display: true },
          { left: '\\[', right: '\\]', display: true },
          { left: '\\(', right: '\\)', display: false },
          { left: '$', right: '$', display: false }
        ],
        throwOnError: false
      });
    } catch (_) { /* ignore */ }
  }

  function hideEmpty(emptyEl, messagesEl) {
    if (emptyEl) emptyEl.hidden = true;
    if (messagesEl) messagesEl.hidden = false;
  }

  function showEmpty(emptyEl, messagesEl) {
    if (emptyEl) emptyEl.hidden = false;
    if (messagesEl) {
      messagesEl.hidden = true;
      messagesEl.innerHTML = '';
    }
  }

  function emptyStateHTML(suggestionsHTML) {
    return '<div class="lqd-chat-empty-icon">' + (window.LqdIcons ? window.LqdIcons.icon('book') : '') + '</div>' +
      '<h2>向图书馆提问</h2>' +
      '<p>我会从书籍、论文和笔记中检索相关内容，生成带依据的回答。</p>' +
      '<div class="lqd-chat-prompts">' + (suggestionsHTML || '') + '</div>';
  }

  window.LqdChatMessages = {
    escHtml: escHtml,
    renderMarkdown: renderMarkdown,
    renderToolTrail: renderToolTrail,
    renderThinkingAndText: renderThinkingAndText,
    appendMessageBubble: appendMessageBubble,
    attachHoverActions: attachHoverActions,
    setBusy: setBusy,
    reRenderKatex: reRenderKatex,
    hideEmpty: hideEmpty,
    showEmpty: showEmpty,
    emptyStateHTML: emptyStateHTML
  };
})();
