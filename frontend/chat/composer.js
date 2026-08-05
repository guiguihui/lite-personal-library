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
          '<button class="lqd-chat-scope" aria-label="选择检索范围" title="选择检索范围">' +
            (window.LqdIcons ? window.LqdIcons.icon('filter') : '⊚') +
          '</button>' +
          '<span class="lqd-chat-composer-spacer"></span>' +
          '<span class="lqd-chat-model"></span>' +
          '<button class="lqd-chat-stop" aria-label="停止生成" title="停止生成" hidden>' +
            (window.LqdIcons ? window.LqdIcons.icon('close') : '✕') +
          '</button>' +
          '<button class="lqd-chat-send" aria-label="发送">' +
            (window.LqdIcons ? window.LqdIcons.icon('arrow-up') : '↑') +
          '</button>' +
        '</div>' +
        '<div class="lqd-chat-scope-panel" hidden>' +
          '<div class="lqd-chat-scope-title">检索范围</div>' +
          '<label class="lqd-chat-scope-item"><input type="checkbox" data-scope="books"><span>书籍</span></label>' +
          '<label class="lqd-chat-scope-item"><input type="checkbox" data-scope="papers"><span>论文</span></label>' +
          '<label class="lqd-chat-scope-item"><input type="checkbox" data-scope="notes"><span>笔记</span></label>' +
          '<label class="lqd-chat-scope-item"><input type="checkbox" data-scope="local"><span>本机文件</span></label>' +
        '</div>' +
      '</div>';
    container.appendChild(composer);

    var input = composer.querySelector('textarea');
    var sendBtn = composer.querySelector('.lqd-chat-send');
    var attachBtn = composer.querySelector('.lqd-chat-attach');
    var stopBtn = composer.querySelector('.lqd-chat-stop');
    var scopeBtn = composer.querySelector('.lqd-chat-scope');
    var scopePanel = composer.querySelector('.lqd-chat-scope-panel');

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

    // ── 检索范围切换 ──
    function syncScopePanel() {
      if (!scopePanel || !window.LqdChatAgent) return;
      var scope = window.LqdChatAgent.getSearchScope();
      scopePanel.querySelectorAll('input[data-scope]').forEach(function (cb) {
        var key = cb.getAttribute('data-scope');
        cb.checked = !!scope[key];
      });
    }
    function closeScopePanel() {
      if (scopePanel) scopePanel.hidden = true;
    }
    if (scopeBtn && scopePanel) {
      scopeBtn.addEventListener('click', function (e) {
        e.stopPropagation();
        syncScopePanel();
        scopePanel.hidden = !scopePanel.hidden;
      });
      scopePanel.addEventListener('click', function (e) {
        e.stopPropagation();
        var cb = e.target && e.target.closest ? e.target.closest('input[data-scope]') : null;
        if (!cb) return;
        if (!window.LqdChatAgent) return;
        var scope = window.LqdChatAgent.getSearchScope();
        var key = cb.getAttribute('data-scope');
        scope[key] = cb.checked;
        // 至少保留一个来源,避免全关
        var any = scope.books || scope.papers || scope.notes || scope.local;
        if (!any) {
          cb.checked = true;
          scope[key] = true;
          if (window.LqdToast) window.LqdToast.show({ type: 'warning', message: '至少保留一个检索来源', duration: 2000 });
          return;
        }
        window.LqdChatAgent.setSearchScope(scope);
      });
      // 点击外部关闭
      document.addEventListener('click', function (e) {
        if (scopePanel && !scopePanel.hidden && !scopePanel.contains(e.target) && !scopeBtn.contains(e.target)) {
          scopePanel.hidden = true;
        }
      });
      if (window.LqdTooltip) {
        window.LqdTooltip.attach(scopeBtn, { text: '选择检索范围', position: 'top' });
      }
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
      updateSlashMenu();
    });

    // ── 斜杠命令(P3-15) ──
    var slashMenu = null;
    var SLASH_COMMANDS = [
      { cmd: '/new', desc: '新开对话', run: function () { if (window.LqdChat) window.LqdChat.openNewChat(); } },
      { cmd: '/clear', desc: '清空当前对话', run: function () { if (window.LqdChat) window.LqdChat.openNewChat(); } },
      { cmd: '/export', desc: '导出当前对话为 Markdown', run: function () { exportCurrent(); } },
      { cmd: '/scope', desc: '选择检索范围', run: function () { if (scopeBtn) scopeBtn.click(); } },
      { cmd: '/status', desc: '一键诊断(API Key / 索引 / 服务)', run: function () { runDiagnostics(); } },
      { cmd: '/help', desc: '显示帮助', run: function () { showHelp(); } }
    ];
    function closeSlashMenu() {
      if (slashMenu) { slashMenu.remove(); slashMenu = null; }
    }
    function showSlashMenu() {
      closeSlashMenu();
      var wrap = document.createElement('div');
      wrap.className = 'lqd-chat-slash';
      var active = input.value.slice(0, input.selectionStart || input.value.length);
      var token = active.match(/\/([a-z]*)$/i);
      var filter = token ? token[1].toLowerCase() : '';
      var cmds = SLASH_COMMANDS.filter(function (c) { return c.cmd.indexOf('/' + filter) === 0; });
      cmds.forEach(function (c) {
        var item = document.createElement('div');
        item.className = 'lqd-chat-slash-item';
        item.innerHTML = '<span class="lqd-chat-slash-cmd">' + c.cmd + '</span><span class="lqd-chat-slash-desc">' + c.desc + '</span>';
        item.addEventListener('click', function (e) {
          e.stopPropagation();
          runSlash(c);
        });
        wrap.appendChild(item);
      });
      if (!cmds.length) {
        var none = document.createElement('div');
        none.className = 'lqd-chat-slash-none';
        none.textContent = '没有匹配的命令';
        wrap.appendChild(none);
      }
      composer.appendChild(wrap);
      slashMenu = wrap;
    }
    function runSlash(c) {
      closeSlashMenu();
      input.value = '';
      input.style.height = 'auto';
      c.run();
    }
    function updateSlashMenu() {
      var text = input.value.slice(0, input.selectionStart || input.value.length);
      // 仅当输入的是"独占的斜杠词"(行首 /xxx,后面是空格或行尾)才显示
      var lineStart = text.lastIndexOf('\n') + 1;
      var currentLine = text.slice(lineStart);
      var m = currentLine.match(/^\/([a-z]*)$/i);
      if (m) {
        showSlashMenu();
      } else {
        closeSlashMenu();
      }
    }
    function exportCurrent() {
      if (!window.LqdChatSession) return;
      var messages = window.LqdChatSession.loadCurrent();
      if (!messages || !messages.length) {
        if (window.LqdToast) window.LqdToast.show({ type: 'info', message: '当前没有可导出的对话', duration: 2000 });
        return;
      }
      var title = (window.LqdChat && window.LqdChat.getTitle ? window.LqdChat.getTitle({}) : '对话导出');
      var md = (window.LqdChat && window.LqdChat.exportSessionMarkdown)
        ? window.LqdChat.exportSessionMarkdown({ title: title, messages: messages })
        : ('# ' + title + '\n\n' + messages.map(function (m) { return (m.role === 'user' ? '## 用户\n\n' : '## LQ-D\n\n') + (m.content || ''); }).join('\n\n---\n\n'));
      var fname = title.replace(/[\\/:*?"<>|]/g, '_') + '.md';
      if (window.LqdChat && window.LqdChat.downloadTextFile) {
        window.LqdChat.downloadTextFile(fname, md);
      } else {
        var blob = new Blob([md], { type: 'text/markdown;charset=utf-8' });
        var url = URL.createObjectURL(blob);
        var a = document.createElement('a');
        a.href = url; a.download = fname;
        document.body.appendChild(a); a.click();
        setTimeout(function () { document.body.removeChild(a); URL.revokeObjectURL(url); }, 100);
      }
    }
    function showHelp() {
      if (window.LqdToast) {
        window.LqdToast.show({ message: '命令: /new 新对话 · /clear 清空 · /export 导出 · /scope 检索范围 · /status 诊断 · /help 帮助', duration: 4000 });
      }
    }

    // P3-20: 一键诊断 — 检查 API Key / 索引 / 服务状态
    function runDiagnostics() {
      var BASE = (window.LQD_CHAT_BASE || '').replace(/\/+$/, '');
      var lines = [];
      lines.push('🔍 诊断中…');
      if (window.LqdToast) {
        window.LqdToast.show({ type: 'info', message: '正在诊断环境…', duration: 2000 });
      }
      var done = false;
      function finish() {
        if (done) return;
        done = true;
        if (window.LqdToast) {
          window.LqdToast.show({ message: lines.join(' '), duration: 6000 });
        }
      }
      // 1) API Key
      window.LqdSettings.fetchApiKey().then(function (key) {
        var cfg = window.LqdSettings.resolve();
        if (key) {
          lines.push('✅ API Key 已配置(' + (cfg.provider || '') + ')');
        } else {
          lines.push('❌ API Key 未配置 → 配置页设置');
        }
        // 2) 服务状态
        fetch(BASE + '/api/status').then(function (res) {
          if (!res.ok) { lines.push('❌ 后端服务异常(HTTP ' + res.status + ')'); finish(); return; }
          return res.json();
        }).then(function (st) {
          if (!st) return;
          if (st.index_ready) {
            lines.push('✅ 索引就绪(' + (st.index_version || '') + ')');
          } else {
            lines.push('❌ 索引未构建 → 索引管理重建');
          }
          if (st.index_running) lines.push('⏳ 索引构建中');
          if (st.ingest_running) lines.push('⏳ 入库任务运行中');
          lines.push('ℹ️ 版本 ' + (st.version || '?'));
          finish();
        }).catch(function () {
          lines.push('❌ 无法连接后端服务');
          finish();
        });
      }).catch(function () {
        lines.push('❌ API Key 读取失败');
        finish();
      });
    }
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') closeSlashMenu();
    });
    // 点击外部关闭斜杠菜单
    composer.addEventListener('click', function (e) {
      if (slashMenu && !slashMenu.contains(e.target) && e.target !== input) closeSlashMenu();
    });

    return { composer: composer, input: input, sendBtn: sendBtn, stopBtn: stopBtn };
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
