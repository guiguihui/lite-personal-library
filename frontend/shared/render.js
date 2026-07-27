/**
 * YuuRender — 共享 markdown 渲染器(Library 阅读器 + 未来 chat.js 复用)。
 *
 * 依赖(在 index.html 中先加载):
 *   - marked.js  (CDN: https://cdn.jsdelivr.net/npm/marked/marked.min.js)
 *   - KaTeX      (本地 /frontend/katex/,含 auto-render.min.js)
 *
 * 能力:
 *   - marked.parse 处理列表/表格/引用块/图片/链接/删除线/标题/代码块
 *   - 数学公式占位符保护(行内 $...$ / 行间 $$...$$ / \[...\] / \(...\)),避免 marked 破坏 KaTeX 语法
 *   - 渲染后调 renderMathInElement 让 KaTeX 渲染占位符还原的公式
 *   - Obsidian callout(> [!note] / [!warning] / [!tip] / [!info])预处理为 <div class="callout">
 *
 * 用法:
 *   const html = YuuRender.md(markdownText);
 *   el.innerHTML = html;
 *   YuuRender.renderKatex(el);  // 渲染 el 内所有数学公式
 *
 * XSS 说明:marked 默认不转义 HTML,但本地知识库内容可信(用户自己的笔记/书籍)。
 * 若未来要渲染不可信内容,加 DOMPurify:el.innerHTML = DOMPurify.sanitize(html)。
 */
(function () {
  'use strict';

  // ---- 数学公式占位符保护 ----
  // marked.parse 会把 $...$ 里的 _ * 等当成 markdown 语法,破坏 KaTeX 解析。
  // 先把所有数学公式替换成占位符,marked 处理完再恢复,最后交给 KaTeX。
  var _mathPlaceholders = [];
  var _MATH_TOKEN_PREFIX = 'MATH'; // 使用控制字符避免与正文冲突

  // callout 类型 → CSS class
  var _CALLOUT_TYPES = {
    'note': 'callout-note',
    'info': 'callout-info',
    'tip': 'callout-tip',
    'warning': 'callout-warning',
    'danger': 'callout-danger',
    'success': 'callout-success',
    'question': 'callout-question',
    'example': 'callout-example',
    'quote': 'callout-quote',
    'abstract': 'callout-abstract',
    'bug': 'callout-bug',
    'failure': 'callout-failure',
  };

  /**
   * 预处理:把数学公式替换成占位符,同时转换 Obsidian callout。
   * @param {string} md
   * @returns {string}
   */
  function _preprocess(md) {
    _mathPlaceholders = [];
    var out = md;

    // 1. 行间公式 $$...$$ (非贪婪,可跨行)
    out = out.replace(/\$\$([\s\S]+?)\$\$/g, function (_, body) {
      var idx = _mathPlaceholders.length;
      _mathPlaceholders.push({ display: true, body: '$$' + body + '$$' });
      return _MATH_TOKEN_PREFIX + idx + '';
    });

    // 2. 行间公式 \[...\]
    out = out.replace(/\\\[([\s\S]+?)\\\]/g, function (_, body) {
      var idx = _mathPlaceholders.length;
      _mathPlaceholders.push({ display: true, body: '\\[' + body + '\\]' });
      return _MATH_TOKEN_PREFIX + idx + '';
    });

    // 3. 行内公式 \(...\)
    out = out.replace(/\\\(([\s\S]+?)\\\)/g, function (_, body) {
      var idx = _mathPlaceholders.length;
      _mathPlaceholders.push({ display: false, body: '\\(' + body + '\\)' });
      return _MATH_TOKEN_PREFIX + idx + '';
    });

    // 4. 行内公式 $...$ (要求 $ 两侧非空白且非数字,避免 $5 苹果 误伤)
    //    正则:前一个字符非空白/数字,后一个字符非空白/数字
    out = out.replace(/(^|[^\s$])\$([^\n$]+?)\$(?=[^\s$]|$)/g, function (_, pre, body) {
      var idx = _mathPlaceholders.length;
      _mathPlaceholders.push({ display: false, body: '$' + body + '$' });
      return pre + _MATH_TOKEN_PREFIX + idx + '';
    });

    // 5. Obsidian callout 预处理
    //    > [!note] 标题
    //    > 正文
    out = out.replace(
      /^> *\[!(\w+)\][^\n]*\n((?:>.*(?:\n|$))+)/gm,
      function (_, type, block) {
        var cls = _CALLOUT_TYPES[type.toLowerCase()] || 'callout-note';
        // 提取标题(第一行 [!type] 后的文字)
        var titleMatch = _.match(/^> *\[!\w+\]\s*(.*)/);
        var title = titleMatch ? titleMatch[1].trim() : type;
        // 剥离每行开头的 >
        var content = block.replace(/^> ?/gm, '').trim();
        return '<div class="callout ' + cls + '"><div class="callout-title">' +
          _escHtml(title) + '</div><div class="callout-body">\n' + content + '\n</div></div>\n';
      }
    );

    return out;
  }

  /**
   * 后处理:恢复数学公式占位符。
   * @param {string} html
   * @returns {string}
   */
  function _postprocess(html) {
    return html.replace(/MATH(\d+)/g, function (_, idx) {
      var ph = _mathPlaceholders[parseInt(idx, 10)];
      return ph ? ph.body : '';
    });
  }

  function _escHtml(s) {
    return s.replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  /**
   * 渲染 markdown 为 HTML。
   * @param {string} text markdown 原文
   * @returns {string} HTML
   */
  function md(text) {
    if (!text) return '';
    if (typeof marked === 'undefined') {
      // 降级:marked 未加载,做最简单的转义 + <p> 包裹
      return '<p>' + _escHtml(text).replace(/\n\n/g, '</p><p>').replace(/\n/g, '<br>') + '</p>';
    }
    var preprocessed = _preprocess(text);
    var html = marked.parse(preprocessed);
    return _postprocess(html);
  }

  /**
   * 在指定元素内渲染所有数学公式(KaTeX auto-render)。
   * @param {HTMLElement} el
   */
  function renderKatex(el) {
    if (typeof renderMathInElement === 'undefined' || !el) return;
    renderMathInElement(el, {
      delimiters: [
        { left: '$$', right: '$$', display: true },
        { left: '\\[', right: '\\]', display: true },
        { left: '\\(', right: '\\)', display: false },
        { left: '$', right: '$', display: false }
      ],
      ignoredTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code'],
      ignoredClasses: ['pseudocode'],
      throwOnError: false
    });
  }

  // 导出
  window.YuuRender = { md: md, renderKatex: renderKatex };
})();
