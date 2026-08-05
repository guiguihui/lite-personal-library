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

  // 代码复制:base64 编码原始代码(unicode 安全),挂在 .lqd-codebox[data-code] 上,
  // 点击 .lqd-codebox-copy 时解码写剪贴板。
  function _b64encode(s) {
    try {
      return btoa(unescape(encodeURIComponent(String(s))));
    } catch (_) {
      return btoa(String(s));
    }
  }
  function _b64decode(s) {
    try {
      return decodeURIComponent(escape(atob(String(s))));
    } catch (_) {
      return atob(String(s));
    }
  }

  // 全局委托:点击代码框复制按钮 → 复制该块代码
  document.addEventListener('click', function (e) {
    var btn = e.target && e.target.closest ? e.target.closest('.lqd-codebox-copy') : null;
    if (!btn) return;
    var box = btn.closest('.lqd-codebox');
    if (!box) return;
    e.preventDefault();
    e.stopPropagation();
    var code = box.getAttribute('data-code') ? _b64decode(box.getAttribute('data-code')) : (box.querySelector('code') || {}).textContent || '';
    function done(ok) {
      var orig = btn.innerHTML;
      btn.innerHTML = ok ? '✓' : '✕';
      btn.classList.add(ok ? 'is-copied' : 'is-error');
      setTimeout(function () { btn.innerHTML = orig; btn.classList.remove('is-copied', 'is-error'); }, 1400);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(code).then(function () { done(true); }, function () { done(false); });
    } else {
      // 降级:textarea 选中复制
      var ta = document.createElement('textarea');
      ta.value = code;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      try { done(document.execCommand('copy')); } catch (_) { done(false); }
      document.body.removeChild(ta);
    }
  }, true);

  /**
   * 渲染 markdown 为 HTML。
   * @param {string} text markdown 原文
   * @returns {string} HTML
   */
  function md(text) {
    if (!text) return '';
    if (typeof text !== 'string') {
      // 防御:非字符串输入(理论上不应发生,但流式累加时若混入 undefined 会崩)
      text = String(text);
    }
    if (typeof marked === 'undefined') {
      // 降级:marked 未加载,做最简单的转义 + <p> 包裹
      return '<p>' + _escHtml(text).replace(/\n\n/g, '</p><p>').replace(/\n/g, '<br>') + '</p>';
    }
    // 配置 marked renderer:代码块加 hljs 高亮
    _configureMarked();
    // 整个渲染流程(_preprocess + marked.parse + _postprocess)包 try/catch。
    // 流式渲染时每个 chunk 都调一次,任何环节对畸形/不完整片段抛错都会导致整条消息崩溃
    // (典型症状:Cannot read properties of undefined (reading 'match') ——
    //  marked 内部 tokenizer 或 _preprocess 的 callout 正则对边界片段的处理)。
    // 兜底降级到转义文本,保证消息可见;打印堆栈 + 输入片段便于定位上游。
    try {
      // P2 知识链接:marked 渲染前先做 wikilink [[target|alias#anchor]] 预处理。
      // 仅当 wikilinks 模块已加载且特性开关开启时生效;渲染失败仍走兜底降级。
      var linked = (window.LqdWikilinks && window.LQD_FEATURES && window.LQD_FEATURES.wikilinks_enabled)
        ? window.LqdWikilinks.preprocess(text)
        : text;
      var preprocessed = _preprocess(linked);
      var html = marked.parse(preprocessed);
      if (html == null) {
        console.warn('[YuuRender] marked.parse returned', typeof html, 'for input(len=' + text.length + '):', text.slice(0, 200));
        return '<p>' + _escHtml(text).replace(/\n\n/g, '</p><p>').replace(/\n/g, '<br>') + '</p>';
      }
      return _postprocess(html);
    } catch (e) {
      console.error('[YuuRender] md() render failed, falling back to escaped text', e, 'input(len=' + text.length + '):', text.slice(0, 200));
      return '<p>' + _escHtml(text).replace(/\n\n/g, '</p><p>').replace(/\n/g, '<br>') + '</p>';
    }
  }

  // marked renderer 配置(只配一次)
  var _markedConfigured = false;
  function _configureMarked() {
    if (_markedConfigured) return;
    _markedConfigured = true;
    try {
      var renderer = new marked.Renderer();
      var origCode = renderer.code;
      renderer.code = function (code, infostring) {
        // marked v4: code 是字符串,infoString 是语言
        // marked v12+: 参数可能是 token 对象
        var lang = '';
        var codeText = '';
        if (typeof code === 'string') {
          codeText = code;
          lang = infoString || '';
        } else if (code && typeof code === 'object') {
          codeText = code.text || '';
          lang = code.lang || '';
        }
        var langLabel = (lang || '').split(/\s+/)[0] || 'code';
        // VS Code 风格代码框:窗口圆点 + 语言标签 + 深色画布
        var header = '<div class="lqd-codebox-bar">' +
          '<span class="lqd-codebox-dots"><i></i><i></i><i></i></span>' +
          '<span class="lqd-codebox-lang">' + _escHtml(langLabel) + '</span>' +
          '<button class="lqd-codebox-copy" type="button" title="复制代码">' +
          '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">' +
          '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>' +
          '</button>' +
          '</div>';
        var codeHtml;
        if (typeof hljs !== 'undefined' && lang && hljs.getLanguage(lang)) {
          try {
            codeHtml = hljs.highlight(codeText, { language: lang }).value;
          } catch (_) {
            codeHtml = _escHtml(codeText);
          }
        } else {
          // 无语言或高亮失败:仍加 hljs 类以便 CSS 统一背景
          codeHtml = _escHtml(codeText);
        }
        // 编码原始代码供复制按钮读取(文本节点,避免 HTML 转义问题)
        var encoded = _b64encode(codeText);
        return '<div class="lqd-codebox" data-code="' + encoded + '">' + header +
          '<pre class="hljs"><code class="language-' + lang + '">' + codeHtml + '</code></pre>' +
          '</div>';
      };
      marked.setOptions({ renderer: renderer, breaks: false, gfm: true });
    } catch (e) {
      console.warn('[YuuRender] marked renderer config failed', e);
    }
  }

  /**
   * 在指定元素内渲染所有数学公式(KaTeX auto-render)。
   * @param {HTMLElement} el
   */
  function renderKatex(el) {
    if (typeof renderMathInElement === 'undefined' || !el) return;
    // 注意:katex 对非 ParseError(非法输入的 TypeError 等)即使 throwOnError:false
    // 也会从 renderError 重抛(实测 "KaTeX can only parse string typed expression")。
    // 公式渲染失败不应拖垮整页内容,兜住并记录堆栈。
    try {
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
    } catch (e) {
      if (window.LqdErrors) window.LqdErrors.report(e, 'renderKatex');
    }
  }

  // 导出
  window.YuuRender = { md: md, renderKatex: renderKatex };
})();
