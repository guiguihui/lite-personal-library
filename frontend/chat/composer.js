/**
 * LQ-D — Chat Composer (Codex 风格)
 *
 * 悬浮式圆角卡片:textarea + 底部操作条(更多操作 / 模型标识 / 圆形发送键)。
 * 保留 .lqd-chat-composer / textarea / .lqd-chat-send 契约,agent.js 无需改动。
 */
(function () {
  'use strict';

  function getModelLabel() {
    var status = (window.LqdStore && window.LqdStore.get('status')) || {};
    if (!status.provider) return '';
    return status.provider + (status.model ? ' / ' + status.model : '');
  }

  function create(container, options) {
    options = options || {};
    var existing = container.querySelector('.lqd-chat-composer');
    if (existing) return existing;

    var composer = document.createElement('div');
    composer.className = 'lqd-chat-composer';
    composer.innerHTML =
      '<div class="lqd-chat-composer-inner">' +
        '<textarea rows="1" placeholder="向图书馆提问，检索书籍、论文与笔记" aria-label="问题输入框"></textarea>' +
        '<div class="lqd-chat-composer-bar">' +
          '<button class="lqd-chat-attach" aria-label="上传文档到图书馆">' +
            (window.LqdIcons ? window.LqdIcons.icon('plus') : '+') +
          '</button>' +
          '<span class="lqd-chat-composer-spacer"></span>' +
          '<span class="lqd-chat-model"></span>' +
          '<button class="lqd-chat-send" aria-label="发送">' +
            (window.LqdIcons ? window.LqdIcons.icon('arrow-up') : '↑') +
          '</button>' +
        '</div>' +
      '</div>';
    container.appendChild(composer);

    var input = composer.querySelector('textarea');
    var sendBtn = composer.querySelector('.lqd-chat-send');
    var attachBtn = composer.querySelector('.lqd-chat-attach');

    // 模型选择器:替换静态 .lqd-chat-model
    if (window.LqdModelPicker && typeof window.LqdModelPicker.create === 'function') {
      window.LqdModelPicker.create(composer, {
        onChange: function (newProvider) {
          window.LqdSettings.set('active_provider', newProvider).then(function () {
            if (window.LqdEvents) window.LqdEvents.emit('settings:loaded', {});
          });
        }
      });
    } else {
      // 降级:保留原有模型文本展示
      var modelEl = composer.querySelector('.lqd-chat-model');
      function updateModel() {
        var label = getModelLabel();
        modelEl.textContent = label;
        modelEl.hidden = !label;
      }
      updateModel();
      if (window.LqdStore && typeof window.LqdStore.subscribe === 'function') {
        window.LqdStore.subscribe('status', updateModel);
      }
      if (window.LqdEvents) {
        window.LqdEvents.on('settings:loaded', updateModel);
      }
    }

    attachBtn.addEventListener('click', function () {
      if (window.LqdShell) window.LqdShell.setActivity('upload');
    });
    if (window.LqdTooltip) {
      window.LqdTooltip.attach(attachBtn, { text: '上传文档到图书馆', position: 'top' });
    }

    function doSend() {
      var query = input.value.trim();
      if (!query) return;
      if (typeof options.onSend === 'function') {
        options.onSend(query);
      }
    }

    sendBtn.addEventListener('click', doSend);
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        doSend();
      }
    });
    input.addEventListener('input', function () {
      input.style.height = 'auto';
      input.style.height = Math.min(input.scrollHeight, 200) + 'px';
    });

    return { composer: composer, input: input, sendBtn: sendBtn };
  }

  function focus(input) {
    if (input && typeof input.focus === 'function') input.focus();
  }

  function getPageTitle() {
    var h1 = document.querySelector('article h1, main h1, h1');
    if (h1 && h1.textContent.trim()) return h1.textContent.trim();
    var t = document.title || '';
    return t.replace(/\s*[•·-]\s*LQ-D.*$/i, '').trim();
  }

  function buildDynamicSuggestions() {
    var path = location.pathname;
    var title = getPageTitle();
    if (!title || title.length < 2) return null;
    var isBook = /\/books?\//.test(path);
    var isPaper = /\/papers?\//.test(path);
    var short = title.length > 20 ? title.slice(0, 20) + '…' : title;
    if (isBook) {
      return [
        '总结「' + short + '」的核心内容',
        '「' + short + '」需要哪些前置知识？',
        '「' + short + '」有什么实际应用？'
      ];
    }
    if (isPaper) {
      return [
        '解释「' + short + '」的核心贡献',
        '「' + short + '」用了哪些关键方法？',
        '「' + short + '」和哪些理论相关？'
      ];
    }
    if (title.length <= 16 && !/^(首页|home|index|关于|about)$/i.test(title)) {
      return [
        '介绍一下「' + title + '」',
        '「' + title + '」的核心概念是什么？',
        '关于「' + title + '」有哪些参考资料？'
      ];
    }
    return null;
  }

  function renderSuggestions(container, suggestions, onClick) {
    if (!container) return;
    if (!suggestions || !suggestions.length) {
      container.innerHTML = '';
      container.hidden = true;
      return;
    }
    container.hidden = false;
    container.innerHTML = suggestions.map(function (s) {
      return '<button data-prompt="' + window.LqdChatMessages.escHtml(s) + '">' + window.LqdChatMessages.escHtml(s) + '</button>';
    }).join('');
    container.querySelectorAll('[data-prompt]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var prompt = btn.getAttribute('data-prompt');
        if (onClick) onClick(prompt);
      });
    });
  }

  window.LqdChatComposer = {
    create: create,
    focus: focus,
    buildDynamicSuggestions: buildDynamicSuggestions,
    renderSuggestions: renderSuggestions
  };
})();
