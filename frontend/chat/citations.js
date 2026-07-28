/**
 * LQ-D — Chat Citations
 *
 * 引用链接注入、检索调试卡片渲染。
 */
(function () {
  'use strict';

  var BASE = (window.LQD_CHAT_BASE || '').replace(/\/+$/, '');

  function escHtml(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function injectReferenceLinks(markdown, refMap) {
    if (!refMap || Object.keys(refMap).length === 0) return markdown || '';
    // 防御:markdown 可能是 undefined/null/非字符串(上游流式异常中断等),
    // 直接 .replace 会抛 "Cannot read properties of undefined (reading 'replace')",
    // 与此前 'match' 同类(undefined 接收者),一并堵死。
    if (markdown == null) return '';
    if (typeof markdown !== 'string') markdown = String(markdown);

    var stash = [];
    var PH = function (i) { return 'CODE' + i + ''; };
    var work = markdown.replace(/```[\s\S]*?```/g, function (m) {
      stash.push(m);
      return PH(stash.length - 1);
    });
    work = work.replace(/`[^`\n]*`/g, function (m) {
      stash.push(m);
      return PH(stash.length - 1);
    });

    function joinUrl(u) {
      return BASE + '/' + String(u).replace(/^\/+/, '');
    }

    var lines = work.split('\n');
    var result = lines.map(function (line) {
      var refMatch = line.match(/^\[(\d+)\]\s+(.+)$/);
      if (refMatch) {
        var ref = refMap[parseInt(refMatch[1], 10)];
        if (ref && ref.url) {
          return '[' + refMatch[1] + '] [' + refMatch[2] + '](' + joinUrl(ref.url) + ')';
        }
        return line;
      }
      return line.replace(/\[(\d+)\](?:\([^)]*\))?/g, function (m, num) {
        var ref = refMap[parseInt(num, 10)];
        if (ref && ref.url) return '[' + num + '](' + joinUrl(ref.url) + ')';
        return m;
      });
    });
    work = result.join('\n');
    work = work.replace(/CODE(\d+)/g, function (_, i) {
      return stash[parseInt(i, 10)];
    });
    return work;
  }

  function renderDebugCard(hits, contexts, systemPrompt, confidence) {
    var hitRows = (hits || []).map(function (h, i) {
      var used = false;
      for (var k = 0; k < contexts.length; k++) {
        var c = contexts[k];
        if (c.nodeId === h.node.node_id && c.docTitle === h.node.doc_id) {
          used = true;
          break;
        }
      }
      return '<tr class="' + (used ? 'lqd-debug-used' : '') + '">' +
        '<td>' + (i + 1) + '</td>' +
        '<td>' + (h.score || '?') + '</td>' +
        '<td>' + escHtml(h.node.doc_id) + '</td>' +
        '<td>' + escHtml((h.node.breadcrumb || []).join(' > ')) + '</td>' +
        '<td>' + (used ? '✓' : '') + '</td>' +
        '</tr>';
    }).join('');

    var ctxBlocks = (contexts || []).map(function (c, i) {
      var textPreview = escHtml((c.text || '').slice(0, 120));
      return '<div class="lqd-debug-ctx">' +
        '<strong>[' + (i + 1) + '] ' + escHtml(c.docTitle) + ' &gt; ' + escHtml((c.breadcrumb || []).join(' > ')) + '</strong>' +
        '<span class="lqd-debug-ctx-meta">' + (c.text || '').length + ' chars | ' + escHtml(c.sourceId || '') + ' | ' + escHtml(c.url || '') + '</span>' +
        '<pre>' + textPreview + '…</pre>' +
        '</div>';
    }).join('');

    var promptPreview = escHtml((systemPrompt || '').slice(0, 800));
    var totalChars = 0;
    for (var i = 0; i < contexts.length; i++) totalChars += (contexts[i].text || '').length;

    return '<details class="lqd-debug-card">' +
      '<summary>检索调试: ' + (hits || []).length + ' hits → ' + contexts.length + ' contexts (' + totalChars + ' chars) | 置信度: ' + (confidence || '?') + '</summary>' +
      '<div class="lqd-debug-section">' +
        '<h4>Search Hits</h4>' +
        '<table class="lqd-debug-table"><thead><tr><th>#</th><th>分数</th><th>文档</th><th>路径</th><th>命中</th></tr></thead>' +
        '<tbody>' + hitRows + '</tbody></table>' +
      '</div>' +
      '<div class="lqd-debug-section">' +
        '<h4>Context Chunks</h4>' + ctxBlocks +
      '</div>' +
      '<div class="lqd-debug-section">' +
        '<h4>System Prompt <span class="lqd-debug-ctx-meta">(' + (systemPrompt || '').length + ' chars)</span></h4>' +
        '<pre class="lqd-debug-prompt">' + promptPreview + '…</pre>' +
      '</div>' +
      '</details>';
  }

  function renderCitations(contexts) {
    if (!contexts || !contexts.length) return '';
    var items = contexts.map(function (c, i) {
      var num = c.displayNum || i + 1;
      var crumb = (c.breadcrumb || []).join(' > ');
      var text = (crumb ? crumb + ' · ' : '') + (c.docTitle || '未知文档');
      var url = c.url || '';
      var href = url ? ' href="' + escHtml(BASE + '/' + String(url).replace(/^\/+/, '')) + '"' : '';
      var tag = url ? 'a' : 'span';
      return '<' + tag + ' class="lqd-chat-citation"' + href + ' target="_blank" aria-label="' + escHtml(text) + '">' +
        '<span class="lqd-chat-citation-num">' + num + '</span>' +
        '<span class="lqd-chat-citation-text">' + escHtml(text) + '</span>' +
      '</' + tag + '>';
    }).join('');
    return '<div class="lqd-chat-citations">' +
      '<div class="lqd-chat-citations-title">参考来源</div>' +
      '<div class="lqd-chat-citation-list">' + items + '</div>' +
      '</div>';
  }

  window.LqdChatCitations = {
    escHtml: escHtml,
    injectReferenceLinks: injectReferenceLinks,
    renderDebugCard: renderDebugCard,
    renderCitations: renderCitations
  };
})();
