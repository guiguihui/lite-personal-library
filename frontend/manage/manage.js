/* Manage 面板 — 索引构建区 + 入库流水线区。
   调 POST /api/index/build 触发构建,轮询 GET /api/index/build/{job_id}。
   调 POST /api/ingest/full 触发入库,轮询 GET /api/ingest/{job_id}。
   不用 console.log,状态写 DOM。 */

(function () {
  'use strict';

  var Manage = {
    initialized: false,
    pollTimer: null,
    currentJobId: null,
    ingestPollTimer: null,
    ingestJobId: null,
    els: {}
  };

  function $(id) { return document.getElementById(id); }

  function escapeHtml(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  // ── 索引构建区 ───────────────────────────────────────────────────────

  function setStatus(text, badge) {
    var el = Manage.els.status;
    if (!el) return;
    var html = '';
    if (text) html += '<span class="status-text">' + escapeHtml(text) + '</span>';
    if (badge) {
      html += '<span class="lqd-status-badge lqd-status-badge--' + badge + '">' + badge + '</span>';
    }
    el.innerHTML = html;
  }

  function setMeta(obj) {
    var el = Manage.els.meta;
    if (!el) return;
    if (!obj) { el.innerHTML = ''; return; }
    var parts = [];
    if (obj.docs_built != null) parts.push('<span>docs: ' + obj.docs_built + '</span>');
    if (obj.duration_sec != null) parts.push('<span>耗时: ' + obj.duration_sec + 's</span>');
    if (obj.mode) parts.push('<span>模式: ' + escapeHtml(obj.mode) + '</span>');
    el.innerHTML = parts.join('');
  }

  function appendLog(line, isError) {
    var el = Manage.els.log;
    if (!el) return;
    var node = document.createElement('span');
    node.className = 'manage-log-line' + (isError ? ' manage-log-line--error' : '');
    node.textContent = line;
    el.appendChild(node);
    el.scrollTop = el.scrollHeight;
  }

  function setLog(lines) {
    var el = Manage.els.log;
    if (!el) return;
    el.innerHTML = '';
    if (!lines || !lines.length) return;
    for (var i = 0; i < lines.length; i++) {
      var node = document.createElement('span');
      node.className = 'manage-log-line';
      var ln = lines[i];
      if (ln.indexOf('[build error]') !== -1 || ln.indexOf('[status error]') !== -1 || ln.indexOf('[error]') !== -1 || ln.indexOf('[pipeline error]') !== -1) {
        node.className += ' manage-log-line--error';
      }
      node.textContent = ln;
      el.appendChild(node);
    }
    el.scrollTop = el.scrollHeight;
  }

  function setButtonsDisabled(disabled) {
    var full = Manage.els.btnFull;
    var incr = Manage.els.btnIncr;
    if (full) full.disabled = disabled;
    if (incr) incr.disabled = disabled;
  }

  function buildIndex(mode) {
    if (Manage.pollTimer) {
      clearInterval(Manage.pollTimer);
      Manage.pollTimer = null;
    }
    setLog([]);
    setMeta(null);
    setStatus('正在启动 ' + (mode === 'incremental' ? '增量' : '全量') + ' 构建...', 'running');
    setButtonsDisabled(true);

    fetch('/api/index/build', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: mode, llm_model: '' })
    })
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      })
      .then(function (data) {
        Manage.currentJobId = data.job_id;
        setStatus('任务已创建,轮询中...', 'running');
        pollJob(data.job_id);
      })
      .catch(function (err) {
        setStatus('启动失败: ' + err.message, 'failed');
        appendLog('[error] ' + err.message, true);
        setButtonsDisabled(false);
      });
  }

  function pollJob(jobId) {
    var ticks = 0;
    Manage.pollTimer = setInterval(function () {
      ticks += 1;
      fetch('/api/index/build/' + encodeURIComponent(jobId))
        .then(function (res) {
          if (res.status === 404) {
            stopPoll();
            setStatus('任务不存在: ' + jobId, 'failed');
            setButtonsDisabled(false);
            return null;
          }
          if (!res.ok) throw new Error('HTTP ' + res.status);
          return res.json();
        })
        .then(function (data) {
          if (!data) return;
          handleJobStatus(data);
        })
        .catch(function (err) {
          appendLog('[poll error] ' + err.message, true);
        });
      if (ticks > 1800) {
        stopPoll();
        setStatus('轮询超时(30min),任务可能仍在后台运行', 'failed');
        setButtonsDisabled(false);
      }
    }, 1000);
  }

  function handleJobStatus(data) {
    setLog(data.log || []);
    var stage = data.current_stage || '';
    if (data.status === 'running') {
      setStatus('构建中... [' + stage + ']', 'running');
      return;
    }
    stopPoll();
    if (data.status === 'done') {
      var r = data.result || {};
      setStatus('构建完成', 'done');
      setMeta({
        docs_built: r.docs_built,
        duration_sec: r.duration_sec,
        mode: r.mode
      });
    } else if (data.status === 'failed') {
      setStatus('构建失败', 'failed');
      var r2 = data.result || {};
      setMeta({
        docs_built: r2.docs_built,
        duration_sec: r2.duration_sec,
        mode: r2.mode
      });
      if (r2.error) appendLog('[error] ' + r2.error, true);
    }
    setButtonsDisabled(false);
  }

  function stopPoll() {
    if (Manage.pollTimer) {
      clearInterval(Manage.pollTimer);
      Manage.pollTimer = null;
    }
  }

  // ── 入库流水线区 ─────────────────────────────────────────────────────

  function setIngestStatus(text, badge) {
    var el = Manage.els.ingestStatus;
    if (!el) return;
    var html = '';
    if (text) html += '<span class="status-text">' + escapeHtml(text) + '</span>';
    if (badge) {
      html += '<span class="lqd-status-badge lqd-status-badge--' + badge + '">' + badge + '</span>';
    }
    el.innerHTML = html;
  }

  function setIngestLog(lines) {
    var el = Manage.els.ingestLog;
    if (!el) return;
    el.innerHTML = '';
    if (!lines || !lines.length) return;
    for (var i = 0; i < lines.length; i++) {
      var node = document.createElement('span');
      node.className = 'manage-log-line';
      var ln = lines[i];
      if (ln.indexOf('[error]') !== -1 || ln.indexOf('[pipeline error]') !== -1 || ln.indexOf('[pipeline traceback]') !== -1) {
        node.className += ' manage-log-line--error';
      }
      node.textContent = ln;
      el.appendChild(node);
    }
    el.scrollTop = el.scrollHeight;
  }

  function appendIngestLog(line) {
    var el = Manage.els.ingestLog;
    if (!el) return;
    var node = document.createElement('span');
    node.className = 'manage-log-line';
    if (line.indexOf('[error]') !== -1 || line.indexOf('[pipeline error]') !== -1) {
      node.className += ' manage-log-line--error';
    }
    node.textContent = line;
    el.appendChild(node);
    el.scrollTop = el.scrollHeight;
  }

  function setIngestButtonsDisabled(disabled) {
    var btn = Manage.els.btnIngest;
    if (btn) btn.disabled = disabled;
  }

  function startIngest() {
    var inputEl = Manage.els.ingestInput;
    var docTypeEl = Manage.els.ingestDocType;
    var slugEl = Manage.els.ingestSlug;
    var strategyEl = Manage.els.ingestStrategy;
    var pagesEl = Manage.els.ingestPages;

    if (!inputEl || !inputEl.files || !inputEl.files.length) {
      setIngestStatus('请选择 PDF/EPUB 文件', 'failed');
      return;
    }
    var file = inputEl.files[0];
    var slug = slugEl ? slugEl.value.trim() : '';
    if (!slug) {
      setIngestStatus('请填写 slug', 'failed');
      return;
    }
    var docType = docTypeEl ? docTypeEl.value : 'book';
    var strategy = strategyEl ? strategyEl.value : 'local';
    var pages = pagesEl ? pagesEl.value.trim() : '';

    // 收集选中的 stages
    var stages = [];
    var checkboxes = document.querySelectorAll('.ingest-stage-cb:checked');
    for (var i = 0; i < checkboxes.length; i++) {
      stages.push(checkboxes[i].value);
    }
    if (!stages.length) {
      setIngestStatus('请至少选择一个阶段', 'failed');
      return;
    }

    setIngestLog([]);
    setIngestStatus('正在提交入库任务...', 'running');
    setIngestButtonsDisabled(true);

    // 用文件路径作为 input_pdf(桌面端文件选择器返回真实路径)
    // 注意:浏览器 input file 只给文件名,桌面端需 pywebview 的文件对话框
    // 此处用 file.name 作为占位,实际路径由后端从 pdfs_dir/<slug>/ 解析
    var body = {
      input_pdf: file.name,
      doc_type: docType,
      slug: slug,
      pages: pages || null,
      strategy: strategy,
      extract_strategy: strategy,
      network_policy: 'allow_ai',
      stages: stages
    };
    var form = new FormData();
    delete body.input_pdf;
    form.append('request', JSON.stringify(body));
    form.append('file', file, file.name);
    fetch('/api/ingest/upload', {
      method: 'POST',
      body: form
    })
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      })
      .then(function (data) {
        Manage.ingestJobId = data.job_id;
        setIngestStatus('任务已创建: ' + data.job_id + ',轮询中...', 'running');
        pollIngestJob(data.job_id);
      })
      .catch(function (err) {
        setIngestStatus('提交失败: ' + err.message, 'failed');
        appendIngestLog('[error] ' + err.message);
        setIngestButtonsDisabled(false);
      });
  }

  function pollIngestJob(jobId) {
    var ticks = 0;
    Manage.ingestPollTimer = setInterval(function () {
      ticks += 1;
      fetch('/api/ingest/' + encodeURIComponent(jobId))
        .then(function (res) {
          if (res.status === 404) {
            stopIngestPoll();
            setIngestStatus('任务不存在: ' + jobId, 'failed');
            setIngestButtonsDisabled(false);
            return null;
          }
          if (!res.ok) throw new Error('HTTP ' + res.status);
          return res.json();
        })
        .then(function (data) {
          if (!data) return;
          handleIngestStatus(data);
        })
        .catch(function (err) {
          appendIngestLog('[poll error] ' + err.message);
        });
      // 超时保护:60 分钟(入库流水线长)
      if (ticks > 3600) {
        stopIngestPoll();
        setIngestStatus('轮询超时(60min),任务可能仍在后台运行', 'failed');
        setIngestButtonsDisabled(false);
      }
    }, 1000);
  }

  function handleIngestStatus(data) {
    setIngestLog(data.log || []);
    var stage = data.current_stage || '';
    if (data.status === 'running') {
      setIngestStatus('入库中... [' + stage + ']', 'running');
      return;
    }
    stopIngestPoll();
    if (data.status === 'done') {
      setIngestStatus('入库完成,可触发索引构建', 'done');
      var r = data.result || {};
      var stages = r.stages || {};
      var extractInfo = stages.extract || {};
      if (extractInfo.page_count != null) {
        appendIngestLog('[info] 提取页数: ' + extractInfo.page_count);
      }
      if (extractInfo.title) {
        appendIngestLog('[info] 标题: ' + extractInfo.title);
      }
    } else if (data.status === 'failed') {
      setIngestStatus('入库失败', 'failed');
      var r2 = data.result || {};
      if (r2.error) appendIngestLog('[error] ' + r2.error);
    }
    setIngestButtonsDisabled(false);
  }

  function stopIngestPoll() {
    if (Manage.ingestPollTimer) {
      clearInterval(Manage.ingestPollTimer);
      Manage.ingestPollTimer = null;
    }
  }

  // ── 渲染 ──────────────────────────────────────────────────────────────

  function renderSkeleton(container) {
    container.innerHTML =
      '<div class="manage-section">' +
        '<h2>索引构建</h2>' +
        '<div class="manage-hint">全量:重写所有索引(慢,首次/大改用)。增量:仅重建变更文档(快,日常用)。</div>' +
        '<div class="manage-actions">' +
          '<button class="manage-btn manage-btn--primary" id="manage-btn-full">全量构建</button>' +
          '<button class="manage-btn" id="manage-btn-incremental">增量构建</button>' +
        '</div>' +
        '<div class="manage-status" id="manage-status"></div>' +
        '<div class="manage-meta" id="manage-meta"></div>' +
        '<pre class="manage-log" id="manage-log"></pre>' +
      '</div>' +
      '<div class="manage-section">' +
        '<h2>入库流水线</h2>' +
        '<div class="manage-hint">选择 PDF/EPUB 文件,填写 slug,选择阶段,提交后后台跑 extract→clean→translate→validate→note。</div>' +
        '<div class="ingest-form">' +
          '<div class="ingest-form-row">' +
            '<label class="ingest-label">文件</label>' +
            '<input type="file" id="ingest-input" accept=".pdf,.epub" class="ingest-input-file" />' +
          '</div>' +
          '<div class="ingest-form-row">' +
            '<label class="ingest-label">类型</label>' +
            '<select id="ingest-doc-type" class="ingest-select">' +
              '<option value="book">book(书籍)</option>' +
              '<option value="paper">paper(论文)</option>' +
              '<option value="note">note(笔记)</option>' +
            '</select>' +
          '</div>' +
          '<div class="ingest-form-row">' +
            '<label class="ingest-label">slug</label>' +
            '<input type="text" id="ingest-slug" class="ingest-input" placeholder="如:probability-theory" />' +
          '</div>' +
          '<div class="ingest-form-row">' +
            '<label class="ingest-label">策略</label>' +
            '<select id="ingest-strategy" class="ingest-select">' +
              '<option value="local">local(本地提取)</option>' +
              '<option value="mineru">mineru(高质量,需 API key)</option>' +
            '</select>' +
          '</div>' +
          '<div class="ingest-form-row">' +
            '<label class="ingest-label">页码</label>' +
            '<input type="text" id="ingest-pages" class="ingest-input" placeholder="如:1-50(可选)" />' +
          '</div>' +
          '<div class="ingest-form-row">' +
            '<label class="ingest-label">阶段</label>' +
            '<div class="ingest-stages">' +
              '<label class="ingest-stage-label"><input type="checkbox" class="ingest-stage-cb" value="extract" checked /> extract</label>' +
              '<label class="ingest-stage-label"><input type="checkbox" class="ingest-stage-cb" value="clean" checked /> clean</label>' +
              '<label class="ingest-stage-label"><input type="checkbox" class="ingest-stage-cb" value="translate" checked /> translate</label>' +
              '<label class="ingest-stage-label"><input type="checkbox" class="ingest-stage-cb" value="validate" checked /> validate</label>' +
              '<label class="ingest-stage-label"><input type="checkbox" class="ingest-stage-cb" value="note" /> note(paper)</label>' +
            '</div>' +
          '</div>' +
          '<div class="manage-actions">' +
            '<button class="manage-btn manage-btn--primary" id="ingest-btn-start">开始入库</button>' +
          '</div>' +
        '</div>' +
        '<div class="manage-status" id="ingest-status"></div>' +
        '<pre class="manage-log" id="ingest-log"></pre>' +
      '</div>';
  }

  function initManage() {
    var panel = document.getElementById('manage-panel');
    if (!panel) return;
    renderSkeleton(panel);
    // 索引构建
    Manage.els.btnFull = $('manage-btn-full');
    Manage.els.btnIncr = $('manage-btn-incremental');
    Manage.els.status = $('manage-status');
    Manage.els.meta = $('manage-meta');
    Manage.els.log = $('manage-log');
    if (Manage.els.btnFull) {
      Manage.els.btnFull.addEventListener('click', function () { buildIndex('full'); });
    }
    if (Manage.els.btnIncr) {
      Manage.els.btnIncr.addEventListener('click', function () { buildIndex('incremental'); });
    }
    // 入库流水线
    Manage.els.ingestInput = $('ingest-input');
    Manage.els.ingestDocType = $('ingest-doc-type');
    Manage.els.ingestSlug = $('ingest-slug');
    Manage.els.ingestStrategy = $('ingest-strategy');
    Manage.els.ingestPages = $('ingest-pages');
    Manage.els.btnIngest = $('ingest-btn-start');
    Manage.els.ingestStatus = $('ingest-status');
    Manage.els.ingestLog = $('ingest-log');
    if (Manage.els.btnIngest) {
      Manage.els.btnIngest.addEventListener('click', startIngest);
    }
    Manage.initialized = true;
  }

  // 暴露到全局供 index.html 调用
  window.initManage = initManage;

  // 暴露 YuuManage 接口供 shell.js / home.js 调用
  window.YuuManage = {
    init: initManage,
    buildIndex: buildIndex,
    renderStatusBadge: function (text, badge) {
      return '<span class="lqd-status-badge lqd-status-badge--' + badge + '">' + escapeHtml(text) + '</span>';
    }
  };

  // DOMContentLoaded 自动初始化(也供 index.html 手动调)
  document.addEventListener('DOMContentLoaded', initManage);
})();
