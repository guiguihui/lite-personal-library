/**
 * LQ-D — Chat Composer
 *
 * 输入区、发送按钮、Enter 快捷键、动态建议问题。
 */
(function () {
  'use strict';

  function create(container, options) {
    options = options || {};
    var existing = container.querySelector('.lqd-chat-composer');
    if (existing) return existing;

    var composer = document.createElement('div');
    composer.className = 'lqd-chat-composer';
    composer.innerHTML =
      '<textarea rows="1" placeholder="输入问题，从图书馆中检索答案……"></textarea>' +
      '<button class="lqd-chat-send" aria-label="发送">' +
        (window.LqdIcons ? window.LqdIcons.icon('send') : '→') +
      '</button>';
    container.appendChild(composer);

    var input = composer.querySelector('textarea');
    var sendBtn = composer.querySelector('.lqd-chat-send');

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
      input.style.height = Math.min(input.scrollHeight, 150) + 'px';
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
      return;
    }
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
