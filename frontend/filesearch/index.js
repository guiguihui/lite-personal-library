/**
 * LQ-D — 本机文件检索 Tab Component
 * 注册到 LqdTabs,提供本机文件内容检索界面。
 * 后端 API: /api/filesearch/build, /api/filesearch/search, /api/filesearch/info
 */
(function () {
  'use strict';

  function $(id) { return document.getElementById(id); }

  function escapeHtml(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function icon(name) {
    return window.LqdIcons && typeof window.LqdIcons.icon === 'function' ? window.LqdIcons.icon(name) : '';
  }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function getBase() {
    return (window.LQD_CHAT_BASE || '/').replace(/\/$/, '');
  }

  function highlightText(text, query) {
    var esc = escapeHtml(text);
    if (!query) return esc;
    var q = (query || '').trim();
    if (!q) return esc;
    var escQ = escapeHtml(q).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    try {
      return esc.replace(new RegExp('(' + escQ + ')', 'gi'), '<mark>$1</mark>');
    } catch (_) {
      return esc;
    }
  }

  function debounce(fn, ms) {
    var t = null;
    return function () {
      var args = arguments;
      var self = this;
      if (t) clearTimeout(t);
      t = setTimeout(function () { fn.apply(self, args); }, ms);
    };
  }

  // ── 状态 ──────────────────────────────────────────────────────────────
  var state = {
    loading: false,
    building: false,
    buildJobId: null,
    pollTimer: null,
    lastQuery: '',
    scanDir: ''  // 用户选择的扫描目录(空则用后端默认)
  };

  // ── 标签组件接口 ───────────────────────────────────────────────────────
  function mount(container, tab) {
    var root = el('div', 'lqd-filesearch');
    root.innerHTML =
      // ── 第1行: 操作引导区(选目录 → 构建索引) ──
      '<div class="lqd-filesearch-setup">' +
        '<div class="lqd-filesearch-setup-left">' +
          '<button class="lqd-btn lqd-btn--primary" id="lqd-filesearch-choosedir">' + icon('folder') + ' 选择目录</button>' +
          '<span class="lqd-filesearch-scandir" id="lqd-filesearch-scandir">未选择目录</span>' +
        '</div>' +
        '<div class="lqd-filesearch-setup-right">' +
          '<button class="lqd-btn" id="lqd-filesearch-build">' + icon('tools') + ' 增量构建</button>' +
          '<button class="lqd-btn" id="lqd-filesearch-rebuild">' + icon('refresh') + ' 全量重建</button>' +
        '</div>' +
      '</div>' +
      // ── 第2行: 索引概况 ──
      '<div class="lqd-filesearch-info" id="lqd-filesearch-info"></div>' +
      // ── 第3行: 构建日志+进度条(构建时显示) ──
      '<div class="lqd-filesearch-build-log" id="lqd-filesearch-build-log" style="display:none"></div>' +
      // ── 第4行: 搜索区(核心功能,居中突出) ──
      '<div class="lqd-filesearch-searchbar">' +
        '<input type="text" class="lqd-filesearch-input" id="lqd-filesearch-input" placeholder="输入关键词搜索文件内容..." />' +
        '<button class="lqd-btn lqd-btn--primary" id="lqd-filesearch-btn">' + icon('search') + ' 搜索</button>' +
      '</div>' +
      // ── 检索结果 ──
      '<div class="lqd-filesearch-results" id="lqd-filesearch-results">' +
        '<div class="lqd-filesearch-empty">输入关键词后点击搜索</div>' +
      '</div>';
    container.appendChild(root);

    var input = root.querySelector('#lqd-filesearch-input');
    var searchBtn = root.querySelector('#lqd-filesearch-btn');
    var buildBtn = root.querySelector('#lqd-filesearch-build');
    var rebuildBtn = root.querySelector('#lqd-filesearch-rebuild');
    var chooseDirBtn = root.querySelector('#lqd-filesearch-choosedir');
    var resultsEl = root.querySelector('#lqd-filesearch-results');
    var selectedIndex = -1;

    function clearActive() {
      var items = resultsEl.querySelectorAll('.lqd-filesearch-card');
      for (var i = 0; i < items.length; i++) items[i].classList.remove('active');
    }

    function setActive(idx) {
      var items = resultsEl.querySelectorAll('.lqd-filesearch-card');
      if (!items.length) { selectedIndex = -1; return; }
      if (idx < 0) idx = items.length - 1;
      if (idx >= items.length) idx = 0;
      clearActive();
      selectedIndex = idx;
      var el_ = items[idx];
      el_.classList.add('active');
      if (el_.scrollIntoView) el_.scrollIntoView({ block: 'nearest' });
    }

    function triggerActive() {
      var items = resultsEl.querySelectorAll('.lqd-filesearch-card');
      if (selectedIndex < 0 || selectedIndex >= items.length) return;
      items[selectedIndex].click();
    }

    var debouncedSearch = debounce(function () {
      selectedIndex = -1;
      doSearch(root);
    }, 250);

    searchBtn.addEventListener('click', function () { doSearch(root); });
    input.addEventListener('input', debouncedSearch);
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') {
        e.preventDefault();
        if (selectedIndex >= 0) triggerActive();
        else doSearch(root);
      } else if (e.key === 'ArrowDown') {
        e.preventDefault();
        setActive(selectedIndex + 1);
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        setActive(selectedIndex - 1);
      }
    });
    resultsEl.addEventListener('click', function () { selectedIndex = -1; clearActive(); });

    buildBtn.addEventListener('click', function () { startBuild(root, 'incremental'); });
    rebuildBtn.addEventListener('click', function () { startBuild(root, 'full'); });
    chooseDirBtn.addEventListener('click', function () { chooseDirectory(root); });

    // 加载索引概况
    loadInfo(root);
  }

  function unmount(container, tab) {
    if (state.pollTimer) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
    var root = container.querySelector('.lqd-filesearch');
    if (root) root.remove();
    state.lastQuery = '';
  }

  function getTitle(tab) {
    return tab.title || '本机检索';
  }

  function getIcon() { return 'search'; }

  // ── 索引概况 ──────────────────────────────────────────────────────────
  function loadInfo(root) {
    var infoEl = root.querySelector('#lqd-filesearch-info');
    var url = getBase() + '/api/filesearch/info';
    fetch(url)
      .then(function (res) {
        if (!res.ok) {
          return res.text().then(function (body) {
            throw new Error('HTTP ' + res.status + ' ' + res.statusText + ' | URL: ' + url + ' | Body: ' + body);
          });
        }
        return res.json();
      })
      .then(function (data) {
        infoEl.innerHTML =
          '<span class="lqd-filesearch-info-item">文件: <b>' + data.files_count + '</b></span>' +
          '<span class="lqd-filesearch-info-item">切片: <b>' + data.chunks_count + '</b></span>' +
          '<span class="lqd-filesearch-info-item">词表: <b>' + data.tokens_count + '</b></span>' +
          '<span class="lqd-filesearch-info-item">扫描目录: <code>' + escapeHtml(data.scan_dir) + '</code></span>';
      })
      .catch(function (e) {
        infoEl.innerHTML =
          '<div class="lqd-filesearch-error">索引信息加载失败: ' + escapeHtml(e.message) + '</div>' +
          '<details class="lqd-fs-error-detail"><summary>排查指引</summary>' +
          '<pre class="lqd-fs-error-trace">1. 确认后端服务已启动(127.0.0.1:8766)\n2. 确认 /api/filesearch/info 路由已注册\n3. 查看后端控制台日志</pre>' +
          '</details>';
      });
  }

  // ── 选择目录 ──────────────────────────────────────────────────────────
  function chooseDirectory(root) {
    var dirEl = root.querySelector('#lqd-filesearch-scandir');
    // 优先使用 pywebview 原生目录选择对话框
    if (window.pywebview && window.pywebview.api && window.pywebview.api.choose_directory) {
      window.pywebview.api.choose_directory().then(function (dir) {
        if (dir && typeof dir === 'string' && dir.length > 0) {
          state.scanDir = dir;
          if (dirEl) dirEl.textContent = '扫描目录: ' + dir;
          if (window.LqdToast) {
            window.LqdToast.show('已选择: ' + dir, 'success');
          }
        } else {
          // 用户取消或返回空值
          if (dirEl) dirEl.textContent = '扫描目录: 未选择(已取消)';
        }
      }).catch(function (e) {
        // pywebview 调用失败 — 展示具体错误
        var errMsg = e && e.message ? e.message : String(e);
        if (dirEl) dirEl.textContent = '扫描目录: 选择失败';
        if (window.LqdToast) {
          window.LqdToast.show('目录选择失败: ' + errMsg, 'error');
        } else {
          console.error('[FileSearch] choose_directory 调用失败:', e);
        }
      });
      return;
    }

    // 非 pywebview 环境(浏览器开发):用 prompt 输入路径
    var dir = window.prompt('请输入要扫描的文件夹路径:');
    if (dir && dir.trim()) {
      state.scanDir = dir.trim();
      if (dirEl) dirEl.textContent = '扫描目录: ' + state.scanDir;
    }
  }

  // ── 索引构建 ──────────────────────────────────────────────────────────
  function startBuild(root, mode) {
    if (state.building) return;
    state.building = true;

    var logEl = root.querySelector('#lqd-filesearch-build-log');
    logEl.style.display = 'block';
    logEl.innerHTML = '<div class="lqd-filesearch-build-status">构建中...</div>';

    var url = getBase() + '/api/filesearch/build';
    fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: mode, scan_dir: state.scanDir || '' })
    })
      .then(function (res) {
        if (!res.ok) {
          return res.text().then(function (body) {
            throw new Error('HTTP ' + res.status + ' ' + res.statusText + ' | URL: ' + url + ' | Body: ' + body);
          });
        }
        return res.json();
      })
      .then(function (data) {
        if (data.error) {
          logEl.innerHTML = '<div class="lqd-filesearch-error">构建启动失败(' + escapeHtml(data.error) + '): ' + escapeHtml(data.message || '') +
            '<details class="lqd-fs-error-detail"><summary>错误详情</summary>' +
            '<pre class="lqd-fs-error-trace">' + escapeHtml(data.traceback || '') + '</pre></details></div>';
          state.building = false;
          return;
        }
        state.buildJobId = data.job_id;
        pollBuildStatus(root);
      })
      .catch(function (e) {
        logEl.innerHTML = '<div class="lqd-filesearch-error">构建启动失败: ' + escapeHtml(e.message) +
          '<details class="lqd-fs-error-detail"><summary>排查指引</summary>' +
          '<pre class="lqd-fs-error-trace">1. 确认后端服务已启动(127.0.0.1:8766)\n2. 确认 POST /api/filesearch/build 路由可用\n3. 确认 scan_dir 路径存在且可读\n4. 查看后端控制台 traceback</pre></details></div>';
        state.building = false;
      });
  }

  function pollBuildStatus(root) {
    if (state.pollTimer) clearInterval(state.pollTimer);

    var logEl = root.querySelector('#lqd-filesearch-build-log');
    var jobId = state.buildJobId;
    var lastLogCount = 0;  // 上次已渲染的日志条数

    state.pollTimer = setInterval(function () {
      fetch(getBase() + '/api/filesearch/status/' + jobId)
        .then(function (res) { return res.json(); })
        .then(function (data) {
          var logLines = data.log || [];
          var statusText = data.status;
          var stageText = data.current_stage || '';
          var progress = data.progress || 0;
          var currentFile = data.current_file || '';
          var pct = Math.round(progress * 100);

          // ── 进度条 ──
          var barHtml = '';
          if (statusText === 'running' || statusText === 'queued') {
            barHtml = '<div class="lqd-fs-progress-wrap">' +
              '<div class="lqd-fs-progress-bar-track">' +
                '<div class="lqd-fs-progress-bar-fill" style="width:' + pct + '%"></div>' +
              '</div>' +
              '<div class="lqd-fs-progress-info">' +
                '<span class="lqd-fs-progress-pct">' + pct + '%</span>' +
                (currentFile ? '<span class="lqd-fs-progress-file" title="' + escapeHtml(currentFile) + '">📄 ' + escapeHtml(currentFile) + '</span>' : '') +
              '</div>' +
            '</div>';
          }

          // ── 状态行 ──
          var statusHtml = '<div class="lqd-filesearch-build-status">' +
            '<span class="lqd-filesearch-build-badge lqd-filesearch-build-badge--' +
            (statusText === 'done' ? 'ok' : statusText === 'failed' ? 'err' : 'run') +
            '">' + escapeHtml(statusText.toUpperCase()) + '</span>' +
            ' <span class="lqd-filesearch-build-stage">' + escapeHtml(stageText) + '</span>' +
            (statusText === 'running' ? '<span class="lqd-filesearch-build-spinner">●</span>' : '') +
          '</div>';

          // ── 日志行 ──
          var logHtml = '';
          if (logLines.length) {
            logHtml += '<pre class="lqd-filesearch-build-pre">';
            for (var i = 0; i < logLines.length; i++) {
              var line = logLines[i];
              var cls = 'lqd-build-log-line';
              // 关键日志行高亮
              if (/开始/.test(line)) cls += ' lqd-build-log-line--start';
              else if (/完成|成功/.test(line)) cls += ' lqd-build-log-line--ok';
              else if (/错误|失败|不存在|警告|FATAL/.test(line)) cls += ' lqd-build-log-line--err';
              else if (/扫描到|切片|页码|新文件|变更|删除|正在解析/.test(line)) cls += ' lqd-build-log-line--info';
              logHtml += '<span class="' + cls + '">' +
                '<span class="lqd-build-log-num">' + (i + 1) + '</span> ' +
                escapeHtml(line) + '</span>\n';
            }
            logHtml += '</pre>';
          }

          // ── 错误详情(如果有) ──
          var errorHtml = '';
          if (data.error && statusText === 'failed') {
            errorHtml = '<details class="lqd-fs-error-detail">' +
              '<summary>❌ 错误详情(点击展开)</summary>' +
              '<pre class="lqd-fs-error-trace">' + escapeHtml(data.error) + '</pre>' +
            '</details>';
          }

          // ── 结果摘要 ──
          var resultHtml = '';
          if (data.result) {
            var r = data.result;
            resultHtml = '<div class="lqd-filesearch-build-result">' +
              '<b>构建结果</b> — ' +
              '扫描: ' + r.files_scanned + ' | 索引: ' + r.files_indexed +
              ' | 跳过: ' + r.files_skipped + ' | 切片: ' + r.chunks_built +
              ' | 耗时: ' + r.duration_sec.toFixed(2) + 's' +
              (r.error ? ' | <span class="lqd-filesearch-error-text">' + escapeHtml(r.error) + '</span>' : '') +
              '</div>';
          }

          logEl.innerHTML = barHtml + statusHtml + logHtml + errorHtml + resultHtml;
          logEl.style.display = '';

          // 自动滚动到底部(只有新日志时才滚)
          if (logLines.length > lastLogCount) {
            lastLogCount = logLines.length;
            var pre = logEl.querySelector('.lqd-filesearch-build-pre');
            if (pre) pre.scrollTop = pre.scrollHeight;
          }

          if (data.status === 'done' || data.status === 'failed') {
            clearInterval(state.pollTimer);
            state.pollTimer = null;
            state.building = false;

            if (data.status === 'done') {
              loadInfo(root);
            }
          }
        })
        .catch(function (e) {
          // 轮询网络错误也要展示
          lastLogCount = -1;  // 强制下次刷新
          var errHtml = '<div class="lqd-filesearch-build-status">' +
            '<span class="lqd-filesearch-build-badge lqd-filesearch-build-badge--err">POLL_ERR</span>' +
            ' <span class="lqd-filesearch-build-stage">轮询失败: ' + escapeHtml(e.message || String(e)) + '</span>' +
          '</div>';
          logEl.innerHTML = errHtml;
        });
    }, 800);  // 800ms 轮询,更实时
  }

  // ── 检索逻辑 ──────────────────────────────────────────────────────────
  function doSearch(root) {
    if (state.loading) return;

    var input = root.querySelector('#lqd-filesearch-input');
    var q = input.value.trim();
    if (!q) return;

    state.lastQuery = q;
    state.loading = true;

    var resultsEl = root.querySelector('#lqd-filesearch-results');
    resultsEl.innerHTML = '<div class="lqd-filesearch-loading">检索中...</div>';

    var url = getBase() + '/api/filesearch/search?q=' + encodeURIComponent(q) + '&limit=20';

    fetch(url)
      .then(function (res) {
        if (!res.ok) {
          return res.text().then(function (body) {
            throw new Error('HTTP ' + res.status + ' ' + res.statusText + ' | URL: ' + url + ' | Body: ' + body);
          });
        }
        return res.json();
      })
      .then(function (data) {
        renderResults(root, data);
      })
      .catch(function (e) {
        resultsEl.innerHTML = '<div class="lqd-filesearch-error">检索失败: ' + escapeHtml(e.message) +
          '<details class="lqd-fs-error-detail"><summary>排查指引</summary>' +
          '<pre class="lqd-fs-error-trace">1. 确认后端服务已启动(127.0.0.1:8766)\n2. 确认索引已构建(先点击"构建索引")\n3. 确认 GET /api/filesearch/search?q=xxx 路由可用\n4. 查看后端控制台 traceback</pre></details></div>';
      })
      .finally(function () {
        state.loading = false;
      });
  }

  function renderResults(root, data) {
    var resultsEl = root.querySelector('#lqd-filesearch-results');

    var results = data.results || [];
    if (!results.length) {
      resultsEl.innerHTML = '<div class="lqd-filesearch-empty">未找到匹配结果</div>';
      return;
    }

    resultsEl.innerHTML = '<div class="lqd-filesearch-count">共 ' + data.total + ' 条结果</div>';

    for (var i = 0; i < results.length; i++) {
      resultsEl.appendChild(renderResultItem(results[i], i + 1));
    }
  }

  function renderResultItem(item, index) {
    var card = el('div', 'lqd-filesearch-card');
    card.innerHTML =
      '<div class="lqd-filesearch-card-header">' +
        '<span class="lqd-filesearch-card-rank">' + index + '</span>' +
        '<span class="lqd-filesearch-card-name" title="' + escapeHtml(item.file_path) + '">' + escapeHtml(item.file_name) + '</span>' +
        '<span class="lqd-filesearch-card-page">' + escapeHtml(item.page_label) + '</span>' +
        '<span class="lqd-filesearch-card-score">score: ' + item.score.toFixed(3) + '</span>' +
      '</div>' +
      '<div class="lqd-filesearch-card-location">' +
        '<span class="lqd-filesearch-card-path">' + escapeHtml(item.file_path) + '</span>' +
        '<span class="lqd-filesearch-card-line">行 ' + item.line_start + '-' + item.line_end + '</span>' +
      '</div>' +
      '<div class="lqd-filesearch-card-snippet">' + highlightText(item.snippet || item.text.slice(0, 200), state.lastQuery) + '</div>';

    // 点击复制文件路径
    card.addEventListener('click', function () {
      if (navigator.clipboard) {
        navigator.clipboard.writeText(item.file_path);
      }
    });

    return card;
  }

  // ── 打开标签 ──────────────────────────────────────────────────────────
  function open() {
    if (!window.LqdTabs) return;

    var tabs = window.LqdTabs.list();
    for (var i = 0; i < tabs.length; i++) {
      if (tabs[i].type === 'filesearch') {
        window.LqdTabs.activate(tabs[i].id);
        return;
      }
    }

    window.LqdTabs.open({
      type: 'filesearch',
      title: '本机检索'
    });
  }

  var LqdFileSearch = {
    type: 'filesearch',
    getTitle: getTitle,
    getIcon: getIcon,
    mount: mount,
    unmount: unmount,
    open: open
  };

  window.LqdFileSearch = LqdFileSearch;

  function tryRegister() {
    if (window.LqdTabs) window.LqdTabs.register('filesearch', LqdFileSearch);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', tryRegister);
  } else {
    tryRegister();
  }
})();
