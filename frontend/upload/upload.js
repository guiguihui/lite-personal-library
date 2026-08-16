/**
 * 轻量个人知识库 — 上传页
 *
 * 拖拽区 + 批量队列 + 元数据表单 + 阶段勾选。
 * 从 manage.js 抽出入库部分(manage.js:234-431),扩展批量+元数据。
 * 文件路径用 pywebview 原生对话框(浏览器 input 不给真实路径)。
 *
 * 批量策略:前端循环调 POST /api/ingest/full(方案 A,不改后端)。
 */
(function () {
  'use strict';

  var state = {
    initialized: false,
    els: {},
    batchPromise: null,
    pollTimers: {}  // itemId → setInterval id
  };

  function $(id) { return document.getElementById(id); }

  function escapeHtml(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  function icons() { return window.LqdIcons || { icon: function () { return ''; } }; }

  function slugify(name) {
    return name
      .replace(/\.[^.]+$/, '')  // 去扩展名
      .toLowerCase()
      .replace(/[^a-z0-9一-龥]+/g, '-')
      .replace(/^-+|-+$/g, '')
      .slice(0, 60);
  }

  // ── 渲染骨架 ──────────────────────────────────────────────────────────
  function renderSkeleton(container) {
    container.innerHTML =
      '<div class="lqd-upload-page">' +
        // 拖拽区
        '<div class="lqd-upload-dropzone" id="lqd-upload-dropzone">' +
          '<div class="lqd-upload-dropzone-inner">' +
            '<div class="lqd-upload-dropzone-icon">' + icons().icon('upload') + '</div>' +
            '<div class="lqd-upload-dropzone-text">拖拽 PDF/EPUB 文件到此处</div>' +
            '<div class="lqd-upload-dropzone-sub">或</div>' +
            '<button class="lqd-btn lqd-btn--primary" id="lqd-upload-choose">选择文件</button>' +
            '<div class="lqd-form-hint">支持批量上传,文件路径通过原生对话框获取</div>' +
          '</div>' +
          '<input type="file" id="lqd-upload-input" accept=".pdf,.epub" multiple style="display:none" />' +
        '</div>' +
        // 元数据表单(批量默认)
        '<div class="lqd-upload-meta">' +
          '<h3>元数据(批量默认,可在队列中逐项编辑)</h3>' +
          '<div class="lqd-form-row">' +
            '<div class="lqd-form-group"><label class="lqd-form-label">类型</label>' +
              '<select id="lqd-upload-doc-type" class="lqd-form-select">' +
                '<option value="book">book(书籍)</option>' +
                '<option value="paper">paper(论文)</option>' +
                '<option value="note">note(笔记)</option>' +
              '</select>' +
            '</div>' +
            '<div class="lqd-form-group"><label class="lqd-form-label">策略</label>' +
              '<select id="lqd-upload-strategy" class="lqd-form-select">' +
                '<option value="local">local(本地提取)</option>' +
                '<option value="mineru">mineru(高质量,需 API key)</option>' +
              '</select>' +
            '</div>' +
            '<div class="lqd-form-group"><label class="lqd-form-label">页码</label>' +
              '<input id="lqd-upload-pages" type="text" class="lqd-form-input" placeholder="如:1-50(可选)" />' +
            '</div>' +
          '</div>' +
          '<div class="lqd-form-group"><label class="lqd-form-label">处理阶段</label>' +
            '<div class="lqd-upload-stages">' +
              '<label class="lqd-checkbox-row"><input type="checkbox" class="lqd-upload-stage-cb" value="extract" checked /> extract</label>' +
              '<label class="lqd-checkbox-row"><input type="checkbox" class="lqd-upload-stage-cb" value="clean" checked /> clean</label>' +
              '<label class="lqd-checkbox-row"><input type="checkbox" class="lqd-upload-stage-cb" value="translate" checked /> translate</label>' +
              '<label class="lqd-checkbox-row"><input type="checkbox" class="lqd-upload-stage-cb" value="validate" checked /> validate</label>' +
              '<label class="lqd-checkbox-row"><input type="checkbox" class="lqd-upload-stage-cb" value="note" /> note(paper)</label>' +
            '</div>' +
          '</div>' +
          '<div class="lqd-form-row lqd-form-row--actions">' +
            '<button class="lqd-btn lqd-btn--primary" id="lqd-upload-start">全部开始</button>' +
            '<button class="lqd-btn" id="lqd-upload-clear-done">清空已完成</button>' +
          '</div>' +
        '</div>' +
        // 队列列表(Main 区)
        '<div class="lqd-upload-queue" id="lqd-upload-queue-list"></div>' +
      '</div>';
  }

  // ── 文件入队 ──────────────────────────────────────────────────────────
  function handleFiles(files, paths) {
    // files: File[] 或 null;paths: string[](pywebview 返回真实路径)
    var list = [];
    if (files) {
      for (var i = 0; i < files.length; i++) {
        list.push({ file: files[i], path: paths ? paths[i] : null });
      }
    } else if (paths) {
      for (var j = 0; j < paths.length; j++) {
        list.push({ file: null, path: paths[j] });
      }
    }

    var meta = collectMeta();
    list.forEach(function (item) {
      var name = item.file ? item.file.name : (item.path ? item.path.split(/[\\/]/).pop() : 'unknown');
      var perMeta = Object.assign({}, meta, { slug: slugify(name) });
      window.YuuUploadQueue.add(item.file, item.path, perMeta);
    });
    renderQueueList();
  }

  function collectMeta() {
    var stages = [];
    var cbs = document.querySelectorAll('.lqd-upload-stage-cb:checked');
    for (var i = 0; i < cbs.length; i++) stages.push(cbs[i].value);
    return {
      docType: $('lqd-upload-doc-type').value,
      strategy: $('lqd-upload-strategy').value,
      pages: $('lqd-upload-pages').value.trim(),
      stages: stages
    };
  }

  // ── 队列渲染 ──────────────────────────────────────────────────────────
  function renderQueueList() {
    var list = $('lqd-upload-queue-list');
    if (!list) return;
    var items = window.YuuUploadQueue.all();
    if (!items.length) {
      list.innerHTML = '<div class="lqd-empty">队列为空,拖拽或选择文件加入队列</div>';
      return;
    }
    list.innerHTML = items.map(function (item, idx) {
      var badge = '';
      if (item.status === 'running') {
        badge = '<span class="lqd-status-badge lqd-status-badge--running">运行中' + (item.stage ? ': ' + escapeHtml(item.stage) : '') + '</span>';
      } else if (item.status === 'done') {
        badge = '<span class="lqd-status-badge lqd-status-badge--done">完成</span>';
      } else if (item.status === 'failed') {
        badge = '<span class="lqd-status-badge lqd-status-badge--failed">失败</span>';
      } else {
        badge = '<span class="lqd-status-badge">待处理</span>';
      }
      var logBtn = item.log.length
        ? '<button class="lqd-btn lqd-btn--sm lqd-btn--ghost" data-action="log" data-id="' + item.id + '">查看日志</button>'
        : '';
      var retryBtn = item.status === 'failed'
        ? '<button class="lqd-btn lqd-btn--sm" data-action="retry" data-id="' + item.id + '">Retry</button>'
        : '';
      var removeBtn = item.status === 'pending' || item.status === 'done' || item.status === 'failed'
        ? '<button class="lqd-btn lqd-btn--sm lqd-btn--ghost lqd-btn--danger" data-action="remove" data-id="' + item.id + '">' + icons().icon('trash') + '</button>'
        : '';
      return '<div class="lqd-upload-queue-item" data-id="' + item.id + '">' +
        '<div class="lqd-upload-queue-item-info">' +
          '<div class="lqd-upload-queue-item-name">#' + (idx + 1) + ' ' + escapeHtml(item.name) + '</div>' +
          '<div class="lqd-upload-queue-item-meta">slug: ' + escapeHtml(item.meta.slug || '') + ' · 类型: ' + escapeHtml(item.meta.docType || '') + '</div>' +
        '</div>' +
        '<div class="lqd-upload-queue-item-actions">' + badge + retryBtn + logBtn + removeBtn + '</div>' +
      '</div>';
    }).join('');

    // 绑定动作
    list.querySelectorAll('[data-action]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var action = btn.getAttribute('data-action');
        var id = btn.getAttribute('data-id');
        if (action === 'remove') window.YuuUploadQueue.remove(id);
        else if (action === 'log') showLog(id);
        else if (action === 'retry') window.YuuUploadQueue.retry(id);
      });
    });
  }

  // ── 剪贴板复制(WebView2 navigator.clipboard + execCommand 兜底) ───────
  // 用户要复制报错给 AI 助手,需兼容非安全上下文(本地 origin 可能无
  // navigator.clipboard)。execCommand 兜底在用户点击的同步调用栈内(onClick)。
  function copyToClipboardFallback(text) {
    try {
      var ta = document.createElement('textarea');
      ta.setAttribute('readonly', '');
      ta.style.position = 'fixed';
      ta.style.top = '-9999px';
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      var ok = document.execCommand('copy');
      document.body.removeChild(ta);
      return ok;
    } catch (e) {
      return false;
    }
  }

  function copyLogToClipboard(text) {
    var done = function () {
      if (window.LqdToast) window.LqdToast.show({ message: '日志已复制到剪贴板', type: 'success', duration: 3000 });
    };
    var fail = function (err) {
      if (window.LqdToast) {
        window.LqdToast.show({ message: '复制失败:' + (err && err.message ? err.message : '未知错误'), type: 'error', duration: 5000 });
      }
    };
    if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
      navigator.clipboard.writeText(text).then(done, function (e) {
        if (copyToClipboardFallback(text)) done();
        else fail(e);
      });
    } else if (copyToClipboardFallback(text)) {
      done();
    } else {
      fail(new Error('剪贴板不可用'));
    }
  }

  function showLog(id) {
    var item = window.YuuUploadQueue.get(id);
    if (!item) return;
    var rawText = item.log.join('\n');  // 未转义纯文本,供复制(显示用 escapeHtml 后的)
    var lines = item.log.map(function (l) { return escapeHtml(l); }).join('\n');
    if (window.LqdModal) {
      window.LqdModal.alert({
        title: '上传日志',
        message: '<pre class="lqd-log-viewer">' + lines + '</pre>',
        confirmLabel: '关闭',
        extraActions: [{ label: '复制日志', onClick: function () { copyLogToClipboard(rawText); } }]
      });
    } else {
      alert('日志:\n' + rawText);
    }
  }

  // ── 批量开始 ──────────────────────────────────────────────────────────
  async function startBatch() {
    if (state.batchPromise) return state.batchPromise;
    state.batchPromise = (async function () {
      var item = window.YuuUploadQueue.next();
      while (item) {
        await processItem(item);
        item = window.YuuUploadQueue.next();
      }
    })();
    try {
      await state.batchPromise;
    } finally {
      state.batchPromise = null;
    }
  }

  async function processItem(item) {
    window.YuuUploadQueue.update(item.id, { status: 'running', stage: '提交中', log: [] });
    renderQueueList();

    var body = {
      input_pdf: item.path || item.name,  // 真实路径(桌面)或文件名占位
      doc_type: item.meta.docType,
      slug: item.meta.slug,
      pages: item.meta.pages || null,
      strategy: item.meta.strategy,
      extract_strategy: item.meta.strategy,
      network_policy: item.meta.networkPolicy || 'allow_ai',
      stages: item.meta.stages,
      title: item.meta.title || null,
      author: item.meta.author || null,
      tags: item.meta.tags || null
    };

    try {
      var endpoint = '/api/ingest/full';
      var options;
      if (item.file) {
        var form = new FormData();
        var uploadMeta = Object.assign({}, body);
        delete uploadMeta.input_pdf;
        form.append('request', JSON.stringify(uploadMeta));
        form.append('file', item.file, item.name);
        endpoint = '/api/ingest/upload';
        options = { method: 'POST', body: form };
      } else {
        options = {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body)
        };
      }
      var r = await fetch(endpoint, options);
      if (!r.ok) {
        var payload = await r.json().catch(function () { return {}; });
        var detail = payload.detail || {};
        throw new Error(detail.message || ('HTTP ' + r.status));
      }
      var data = await r.json();
      window.YuuUploadQueue.update(item.id, { jobId: data.job_id, stage: '轮询中' });
      renderQueueList();
      await pollJob(item.id, data.job_id);
    } catch (e) {
      window.YuuUploadQueue.update(item.id, {
        status: 'failed',
        log: ['[error] ' + e.message]
      });
      renderQueueList();
    }
  }

  function pollJob(itemId, jobId) {
    return new Promise(function (resolve) {
      var ticks = 0;
      state.pollTimers[itemId] = setInterval(function () {
        ticks += 1;
        fetch('/api/ingest/' + encodeURIComponent(jobId))
          .then(function (r) {
            if (r.status === 404) {
              stopPoll(itemId);
              window.YuuUploadQueue.update(itemId, { status: 'failed', log: ['[error] 任务不存在: ' + jobId] });
              renderQueueList();
              resolve();
              return null;
            }
            if (!r.ok) throw new Error('HTTP ' + r.status);
            return r.json();
          })
          .then(function (data) {
            if (!data) return;
            handleStatus(itemId, data);
            var buildJobId = data.result && data.result.build_job_id;
            if (data.status === 'done' && buildJobId) {
              stopPoll(itemId);
              pollBuild(itemId, buildJobId).then(resolve);
            } else if (data.status === 'done' || data.status === 'failed') {
              stopPoll(itemId);
              resolve();
            }
          })
          .catch(function (e) {
            window.YuuUploadQueue.update(itemId, function (it) {
              it.log.push('[poll error] ' + e.message);
            });
            renderQueueList();
          });
        if (ticks > 3600) {
          stopPoll(itemId);
          window.YuuUploadQueue.update(itemId, { status: 'failed', log: ['[error] 轮询超时(60min)'] });
          renderQueueList();
          resolve();
        }
      }, 1000);
    });
  }


  function pollBuild(itemId, buildJobId) {
    window.YuuUploadQueue.update(itemId, { status: 'running', stage: 'index' });
    return new Promise(function (resolve) {
      var ticks = 0;
      var timer = setInterval(function () {
        ticks += 1;
        fetch('/api/index/build/' + encodeURIComponent(buildJobId))
          .then(function (response) {
            if (!response.ok) throw new Error('HTTP ' + response.status);
            return response.json();
          })
          .then(function (data) {
            if (data.status === 'running') return;
            clearInterval(timer);
            if (data.status === 'done') {
              window.YuuUploadQueue.update(itemId, { status: 'done', stage: 'published' });
              if (window.LqdEvents) window.LqdEvents.emit('index:published', data);
            } else {
              window.YuuUploadQueue.update(itemId, { status: 'failed', stage: 'index', log: ['[index error] ' + ((data.result && data.result.error) || 'build failed')] });
            }
            renderQueueList();
            resolve();
          })
          .catch(function (error) {
            window.YuuUploadQueue.update(itemId, function (current) {
              current.log.push('[index poll error] ' + error.message);
            });
          });
        if (ticks > 3600) {
          clearInterval(timer);
          window.YuuUploadQueue.update(itemId, { status: 'failed', stage: 'index', log: ['[index error] publish timeout'] });
          renderQueueList();
          resolve();
        }
      }, 1000);
    });
  }
  function handleStatus(itemId, data) {
    var patch = { log: data.log || [], stage: data.current_stage || '' };
    if (data.status === 'running') {
      patch.status = 'running';
    } else if (data.status === 'done') {
      patch.status = 'done';
      patch.stage = '完成';
    } else if (data.status === 'failed') {
      patch.status = 'failed';
    }
    window.YuuUploadQueue.update(itemId, patch);
    renderQueueList();
  }

  function stopPoll(itemId) {
    if (state.pollTimers[itemId]) {
      clearInterval(state.pollTimers[itemId]);
      delete state.pollTimers[itemId];
    }
  }

  function clearDone() {
    window.YuuUploadQueue.clearDone();
    renderQueueList();
  }

  // ── 文件选择(pywebview 原生对话框优先) ────────────────────────────────
  // 防重入:pywebview 的 create_file_dialog 是阻塞调用,await 期间若用户
  // 再点按钮(或事件累积),会弹多个对话框"关一个又弹一个"。用锁串行化。
  var _chooseFilesInFlight = false;
  async function chooseFiles() {
    if (_chooseFilesInFlight) return;
    // 优先用 pywebview 原生对话框(返回真实路径)
    if (window.pywebview && window.pywebview.api && window.pywebview.api.choose_files) {
      _chooseFilesInFlight = true;
      try {
        var paths = await window.pywebview.api.choose_files();
        if (paths && paths.length) {
          handleFiles(null, paths);
          return;
        }
      } catch (_) { /* 降级到 input */ } finally {
        _chooseFilesInFlight = false;
      }
    }
    // 降级:浏览器 input(只给文件名,路径占位)
    var input = $('lqd-upload-input');
    if (input) input.click();
  }

  // ── 初始化 ────────────────────────────────────────────────────────────
  function init(container) {
    renderSkeleton(container);
    state.els.container = container;
    // 幂等:切 tab 回来时 container 是新 DOM,但若同一容器已绑过事件则跳过
    // (防止 addEventListener 累积导致点一次弹多个对话框)
    if (state.initialized && state.els._boundContainer === container) {
      renderQueueList();
      return;
    }
    state.els._boundContainer = container;

    // 拖拽
    var dz = $('lqd-upload-dropzone');
    if (dz) {
      dz.addEventListener('dragover', function (e) {
        e.preventDefault();
        dz.classList.add('dragover');
      });
      dz.addEventListener('dragleave', function () { dz.classList.remove('dragover'); });
      dz.addEventListener('drop', function (e) {
        e.preventDefault();
        dz.classList.remove('dragover');
        var files = e.dataTransfer && e.dataTransfer.files;
        if (files && files.length) handleFiles(files, null);
      });
      dz.addEventListener('click', function (e) {
        // 点击 dropzone 但不点按钮时,也触发选择
        if (e.target.tagName !== 'BUTTON') chooseFiles();
      });
    }

    var chooseBtn = $('lqd-upload-choose');
    if (chooseBtn) chooseBtn.addEventListener('click', function (e) { e.stopPropagation(); chooseFiles(); });

    var input = $('lqd-upload-input');
    if (input) {
      input.addEventListener('change', function () {
        if (input.files && input.files.length) handleFiles(input.files, null);
        input.value = '';
      });
    }

    var startBtn = $('lqd-upload-start');
    if (startBtn) startBtn.addEventListener('click', startBatch);

    var clearBtn = $('lqd-upload-clear-done');
    if (clearBtn) clearBtn.addEventListener('click', clearDone);

    // 订阅队列变化
    window.YuuUploadQueue.onStatusChange(function () {
      renderQueueList();
      // 同步刷新 sidebar 队列
      if (window.LqdShell && typeof window.LqdShell.refreshSidebar === 'function') {
        window.LqdShell.refreshSidebar();
      }
    });

    renderQueueList();
    state.initialized = true;
  }

  // ── Sidebar 队列渲染(供 shell.js 调用) ────────────────────────────────
  function renderQueue(sidebarContainer) {
    if (!sidebarContainer) return;
    var items = window.YuuUploadQueue.all();
    if (!items.length) {
      sidebarContainer.innerHTML = '<div class="lqd-empty">队列为空</div>';
      return;
    }
    sidebarContainer.innerHTML = items.map(function (item) {
      var badge = '';
      if (item.status === 'running') badge = '<span class="lqd-status-badge lqd-status-badge--running">运行</span>';
      else if (item.status === 'done') badge = '<span class="lqd-status-badge lqd-status-badge--done">完成</span>';
      else if (item.status === 'failed') badge = '<span class="lqd-status-badge lqd-status-badge--failed">失败</span>';
      else badge = '<span class="lqd-status-badge">待处理</span>';
      return '<div class="lqd-sidebar-queue-item">' +
        '<div class="lqd-sidebar-queue-item-name">' + escapeHtml(item.name) + '</div>' +
        '<div class="lqd-sidebar-queue-item-status">' + badge + '</div>' +
      '</div>';
    }).join('');
  }

  window.YuuUpload = {
    init: init,
    renderQueue: renderQueue,
    clearDone: clearDone
  };
})();
