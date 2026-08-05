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
    // 本机文件、文档分两组渲染:
    // - 本机文件卡片带 data-localfile-* 属性,点击弹出文件路径+内容片段
    // - 文档卡片保持原有跳转逻辑(openDoc)
    var localFileItems = [];
    var docItems = [];
    contexts.forEach(function (c, i) {
      if (c.isLocalFile) {
        localFileItems.push(renderLocalFileCitation(c, i));
      } else {
        docItems.push(renderDocCitation(c, i));
      }
    });

    var html = '';
    if (docItems.length) {
      html += '<div class="lqd-chat-citations">' +
        '<div class="lqd-chat-citations-title">参考来源</div>' +
        '<div class="lqd-chat-citation-list">' + docItems.join('') + '</div>' +
        '</div>';
    }
    if (localFileItems.length) {
      html += '<div class="lqd-chat-citations lqd-chat-citations--localfile">' +
        '<div class="lqd-chat-citations-title">本机文件</div>' +
        '<div class="lqd-chat-citation-list">' + localFileItems.join('') + '</div>' +
        '</div>';
    }
    return html;
  }

  // 本机文件引用卡片:带 📁 图标 + data-localfile-* 属性,
  // 点击弹出浮窗显示文件路径、页码、行号、内容片段(不跳转文档库)。
  function renderLocalFileCitation(c, i) {
    var num = c.displayNum || i + 1;
    var fileName = c.docTitle || '本机文件';
    var pageLabel = c.pageLabel || '';
    var lineLabel = c.lineStart ? 'L' + c.lineStart + (c.lineEnd && c.lineEnd !== c.lineStart ? '-' + c.lineEnd : '') : '';
    var text = fileName + (pageLabel ? ' · ' + pageLabel : '') + (lineLabel ? ' · ' + lineLabel : '');
    var preview = (c.text || '').slice(0, 300);
    return '<a class="lqd-chat-citation lqd-chat-citation--localfile" role="button" tabindex="0"' +
      ' data-localfile-path="' + escHtml(c.filePath || '') + '"' +
      ' data-localfile-page="' + escHtml(pageLabel) + '"' +
      ' data-localfile-line="' + escHtml(lineLabel) + '"' +
      ' data-preview="' + escHtml(preview) + '"' +
      ' aria-label="本机文件: ' + escHtml(text) + '">' +
      '<span class="lqd-chat-citation-num">' + num + '</span>' +
      '<span class="lqd-chat-citation-icon">' + (window.LqdIcons ? window.LqdIcons.icon('folder') : '📁') + '</span>' +
      '<span class="lqd-chat-citation-text">' + escHtml(text) + '</span>' +
      '</a>';
  }

  function renderDocCitation(c, i) {
    var num = c.displayNum || i + 1;
    var crumb = (c.breadcrumb || []).join(' > ');
    var text = (crumb ? crumb + ' · ' : '') + (c.docTitle || '未知文档');
    var preview = (c.text || '').slice(0, 300);
    // 跳转数据:优先从 sourceId(格式 type:doc_id:node_id)解析,
    // 兼容显式字段。openDoc(type, slug, nodeId) 在应用内打开文档。
    var parsed = parseSourceId(c.sourceId);
    // docType 显式字段也可能是单数,统一归一化为复数
    var docType = normalizeTypePlural(c.docType || parsed.type || '');
    var docId = c.docId || parsed.docId || '';
    var nodeId = c.nodeId || parsed.nodeId || '';
    return '<a class="lqd-chat-citation" role="button" tabindex="0"' +
      ' data-doc-type="' + escHtml(docType) + '"' +
      ' data-doc-id="' + escHtml(docId) + '"' +
      ' data-node-id="' + escHtml(nodeId) + '"' +
      ' data-preview="' + escHtml(preview) + '"' +
      ' aria-label="打开来源: ' + escHtml(text) + '">' +
      '<span class="lqd-chat-citation-num">' + num + '</span>' +
      '<span class="lqd-chat-citation-text">' + escHtml(text) + '</span>' +
      '</a>';
  }

  // 解析 sourceId(type:doc_id:node_id)。doc_id/node_id 可能含冒号,
  // 故按首、尾两段切:第一段为 type,最后一段为 nodeId,中间为 docId。
  // sourceId 里的 type 是单数(book/paper/note,来自 global-index),
  // 但 library 前端 / openDoc 用复数(books/papers/notes,与 /api/search 的
  // doc_type 一致),此处统一归一化为复数,否则 selectDoc 用单数 type 会
  // 导致书架分类匹配错乱、文档加载状态异常(索引点击加载失败的根因)。
  var TYPE_TO_PLURAL = { book: 'books', paper: 'papers', note: 'notes' };
  function normalizeTypePlural(t) {
    if (!t) return '';
    if (TYPE_TO_PLURAL[t]) return TYPE_TO_PLURAL[t];
    return t; // 已是复数或未知值原样返回
  }
  function parseSourceId(sourceId) {
    var out = { type: '', docId: '', nodeId: '' };
    if (!sourceId || typeof sourceId !== 'string') return out;
    var first = sourceId.indexOf(':');
    var last = sourceId.lastIndexOf(':');
    if (first <= 0) return out;
    out.type = normalizeTypePlural(sourceId.slice(0, first));
    if (last > first) {
      out.docId = sourceId.slice(first + 1, last);
      out.nodeId = sourceId.slice(last + 1);
    } else {
      out.docId = sourceId.slice(first + 1);
    }
    return out;
  }

  window.LqdChatCitations = {
    escHtml: escHtml,
    injectReferenceLinks: injectReferenceLinks,
    renderDebugCard: renderDebugCard,
    renderCitations: renderCitations
  };
})();
