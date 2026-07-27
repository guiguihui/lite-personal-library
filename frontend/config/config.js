/**
 * LQ-D — 配置页
 *
 * 三组配置:LLM(provider/model/base_url/api_key) + 存储(路径) + 应用(端口/策略)。
 * LLM 走 /api/settings(单 key-value PUT),存储/应用走 /api/app/config(整体 PUT)。
 * api_key 不缓存前端,通过 /api/settings/key 按需取。
 *
 * 从 chat.js 抽屉抽出 LLM 配置 UI(chat.js:1152-1213,1423-1505),新增存储/应用配置。
 */
(function () {
  'use strict';

  var state = {
    initialized: false,
    currentGroup: 'llm',
    els: {},
    settings: null  // 从后端加载的 LLM 配置
  };

  function $(id) { return document.getElementById(id); }

  function escapeHtml(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  // ── 加载 LLM 配置 ─────────────────────────────────────────────────────
  async function loadSettings() {
    try {
      var r = await fetch('/api/settings');
      if (!r.ok) throw new Error('HTTP ' + r.status);
      state.settings = await r.json();
    } catch (e) {
      state.settings = null;
    }
    return state.settings;
  }

  async function fetchApiKey(provider) {
    try {
      var r = await fetch('/api/settings/key?provider=' + encodeURIComponent(provider));
      if (!r.ok) return '';
      var data = await r.json();
      return data.api_key || '';
    } catch (_) {
      return '';
    }
  }

  // ── LLM 表单 ──────────────────────────────────────────────────────────
  async function renderLLMForm(container) {
    var settings = state.settings || {};
    var active = settings.active_provider || 'anthropic';
    var providers = settings.providers || {};
    var pcfg = providers[active] || {};
    var defaults = window.YuuProviders.getDefaults(active);

    var names = window.YuuProviders.getNames();
    var options = names.map(function (n) {
      return '<option value="' + n + '"' + (n === active ? ' selected' : '') + '>' + n + '</option>';
    }).join('');

    var hasKey = pcfg.has_key ? '<span class="lqd-status-badge lqd-status-badge--done">已配置</span>' : '<span class="lqd-status-badge lqd-status-badge--failed">未配置</span>';

    container.innerHTML =
      '<div class="lqd-config-section">' +
        '<h3>LLM 模型配置</h3>' +
        '<div class="lqd-form-group">' +
          '<label class="lqd-form-label" for="lqd-cfg-provider">Provider</label>' +
          '<select id="lqd-cfg-provider" class="lqd-form-select" aria-describedby="lqd-cfg-provider-hint">' + options + '</select>' +
          '<div class="lqd-form-hint" id="lqd-cfg-provider-hint">选择 LLM 服务商(BYOK,前端直连)</div>' +
        '</div>' +
        '<div class="lqd-form-group">' +
          '<label class="lqd-form-label" for="lqd-cfg-base-url">Base URL</label>' +
          '<input id="lqd-cfg-base-url" type="text" class="lqd-form-input" value="' + escapeHtml(pcfg.base_url || defaults.base_url) + '" placeholder="' + escapeHtml(defaults.base_url) + '" />' +
        '</div>' +
        '<div class="lqd-form-group">' +
          '<label class="lqd-form-label" for="lqd-cfg-model">Model</label>' +
          '<input id="lqd-cfg-model" type="text" class="lqd-form-input" value="' + escapeHtml(pcfg.model || defaults.model) + '" placeholder="' + escapeHtml(defaults.model) + '" />' +
        '</div>' +
        '<div class="lqd-form-group">' +
          '<label class="lqd-form-label" for="lqd-cfg-api-key">API Key ' + hasKey + '</label>' +
          '<input id="lqd-cfg-api-key" type="password" class="lqd-form-input" placeholder="留空则保持现有 key" aria-describedby="lqd-cfg-api-key-hint" />' +
          '<div class="lqd-form-hint" id="lqd-cfg-api-key-hint">key 优先存系统凭证管理器(keyring),降级明文存本地 llm.yaml</div>' +
        '</div>' +
        '<div class="lqd-form-group">' +
          '<div class="lqd-checkbox-row">' +
            '<label><input id="lqd-cfg-remember" type="checkbox" ' + (settings.remember_key ? 'checked' : '') + ' /> 记住 key</label>' +
          '</div>' +
          '<div class="lqd-checkbox-row">' +
            '<label><input id="lqd-cfg-proxy" type="checkbox" ' + (settings.use_llm_proxy ? 'checked' : '') + ' /> 走后端代理(解决 CORS)</label>' +
          '</div>' +
        '</div>' +
        '<div class="lqd-form-row lqd-form-row--actions">' +
          '<button id="lqd-cfg-save-llm" class="lqd-btn lqd-btn--primary">保存</button>' +
          '<button id="lqd-cfg-test" class="lqd-btn">测试连接</button>' +
          '<button id="lqd-cfg-clear-key" class="lqd-btn lqd-btn--danger">清除 key</button>' +
        '</div>' +
        '<div id="lqd-cfg-test-status" class="lqd-cfg-test-status"></div>' +
      '</div>';

    // 绑定事件
    $('lqd-cfg-provider').addEventListener('change', onProviderChange);
    $('lqd-cfg-save-llm').addEventListener('click', saveLLM);
    $('lqd-cfg-test').addEventListener('click', testConnection);
    $('lqd-cfg-clear-key').addEventListener('click', clearApiKey);
  }

  async function onProviderChange() {
    var provider = $('lqd-cfg-provider').value;
    var defaults = window.YuuProviders.getDefaults(provider);
    var pcfg = (state.settings && state.settings.providers && state.settings.providers[provider]) || {};
    $('lqd-cfg-base-url').value = pcfg.base_url || defaults.base_url;
    $('lqd-cfg-model').value = pcfg.model || defaults.model;
    $('lqd-cfg-api-key').value = '';
    // 更新 has_key 徽章
    var hasKey = pcfg.has_key;
    var badge = state.els.body.querySelector('.lqd-form-group:nth-child(4) .lqd-status-badge');
    if (badge) {
      badge.className = 'lqd-status-badge lqd-status-badge--' + (hasKey ? 'done' : 'failed');
      badge.textContent = hasKey ? '已配置' : '未配置';
    }
  }

  async function saveLLM() {
    var provider = $('lqd-cfg-provider').value;
    var baseUrl = $('lqd-cfg-base-url').value.trim();
    var model = $('lqd-cfg-model').value.trim();
    var apiKey = $('lqd-cfg-api-key').value;
    var remember = $('lqd-cfg-remember').checked;
    var proxy = $('lqd-cfg-proxy').checked;

    var btn = $('lqd-cfg-save-llm');
    btn.disabled = true;
    btn.textContent = '保存中...';

    try {
      // 切换 active provider
      await fetch('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key: 'active_provider', value: provider })
      });
      // 更新 model/base_url
      await fetch('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key: 'model', value: model, provider: provider })
      });
      await fetch('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key: 'base_url', value: baseUrl, provider: provider })
      });
      // api_key(非空才写)
      if (apiKey) {
        await fetch('/api/settings', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ key: 'api_key', value: apiKey, provider: provider })
        });
      }
      // remember_key
      await fetch('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key: 'remember_key', value: remember })
      });
      // use_llm_proxy
      await fetch('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key: 'use_llm_proxy', value: proxy })
      });

      setTestStatus('success', 'LLM 配置已保存');
      // 重新加载
      await loadSettings();
      await renderLLMForm(state.els.body);
    } catch (e) {
      setTestStatus('error', '保存失败: ' + e.message);
    }
    btn.disabled = false;
    btn.textContent = '保存';
  }

  async function clearApiKey() {
    var ok = window.LqdModal
      ? await window.LqdModal.confirm({
          title: '清除 API Key',
          message: '确定清除当前 provider 的 API Key?此操作不可撤销。',
          confirmLabel: '清除',
          cancelLabel: '取消',
          danger: true
        })
      : confirm('确定清除当前 provider 的 API Key?');
    if (!ok) return;
    var provider = $('lqd-cfg-provider').value;
    try {
      await fetch('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key: 'api_key', value: '', provider: provider })
      });
      $('lqd-cfg-api-key').value = '';
      setTestStatus('success', '已清除 key');
      await loadSettings();
      await renderLLMForm(state.els.body);
    } catch (e) {
      setTestStatus('error', '清除失败: ' + e.message);
    }
  }

  async function testConnection() {
    var provider = $('lqd-cfg-provider').value;
    var baseUrl = ($('lqd-cfg-base-url').value || window.YuuProviders.getDefaults(provider).base_url).trim();
    baseUrl = baseUrl.replace(/\/+$/, '');
    var model = ($('lqd-cfg-model').value || window.YuuProviders.getDefaults(provider).model).trim();
    var apiKey = $('lqd-cfg-api-key').value.trim() || await fetchApiKey(provider);

    if (!apiKey) {
      setTestStatus('error', '请先填写 API Key');
      return;
    }
    if (!baseUrl) {
      setTestStatus('error', '请先填写 Base URL');
      return;
    }

    setTestStatus('loading', '正在测试...');
    var btn = $('lqd-cfg-test');
    btn.disabled = true;

    try {
      var isAnthropic = provider === 'anthropic';
      var url, headers, body;
      if (isAnthropic) {
        url = baseUrl + '/v1/messages';
        headers = {
          'Content-Type': 'application/json',
          'x-api-key': apiKey,
          'anthropic-version': '2023-06-01',
          'anthropic-dangerous-direct-browser-access': 'true'
        };
        body = JSON.stringify({ model: model, max_tokens: 8, messages: [{ role: 'user', content: 'ping' }] });
      } else {
        url = baseUrl + '/v1/chat/completions';
        headers = { 'Content-Type': 'application/json', Authorization: 'Bearer ' + apiKey };
        body = JSON.stringify({ model: model, messages: [{ role: 'user', content: 'ping' }], max_tokens: 8, stream: false });
      }
      var resp = await fetch(url, { method: 'POST', headers: headers, body: body });
      if (resp.ok) {
        setTestStatus('success', '连接成功');
      } else {
        var status = resp.status;
        var text = await resp.text().catch(function () { return ''; });
        var msg = '';
        try { msg = JSON.parse(text).error.message || ''; } catch (_) {}
        if (status === 401) msg = 'API Key 无效';
        else if (status === 403) msg = '无权限或浏览器跨域受限';
        else if (status === 404) msg = 'Base URL 或模型不存在';
        else if (status === 429) msg = '请求过多或余额不足';
        else if (!msg) msg = 'HTTP ' + status;
        setTestStatus('error', '连接失败: ' + msg);
      }
    } catch (e) {
      var m = e.message || '';
      if (m.indexOf('Failed to fetch') !== -1 || m.indexOf('NetworkError') !== -1) {
        setTestStatus('error', '连接失败: 可能是 CORS、网络或 Base URL 错误');
      } else {
        setTestStatus('error', '连接失败: ' + m.slice(0, 80));
      }
    }
    btn.disabled = false;
  }

  function setTestStatus(status, text) {
    var el = $('lqd-cfg-test-status');
    if (!el) return;
    el.dataset.status = status;
    el.textContent = text || '';
  }

  // ── 存储表单 ──────────────────────────────────────────────────────────
  async function renderStorageForm(container) {
    var cfg = await loadAppConfig();
    container.innerHTML =
      '<div class="lqd-config-section">' +
        '<h3>文件存储位置</h3>' +
        '<div class="lqd-form-hint" style="margin-bottom:var(--space-4)">修改路径后需手动迁移现有文档。系统敏感目录(如 C:\\Windows)被禁止。</div>' +
        '<div class="lqd-form-group">' +
          '<label class="lqd-form-label">文档库目录(content)</label>' +
          '<input id="lqd-cfg-content-dir" type="text" class="lqd-form-input" value="' + escapeHtml(cfg.content_dir) + '" />' +
        '</div>' +
        '<div class="lqd-form-group">' +
          '<label class="lqd-form-label">索引目录(pageindex)</label>' +
          '<input id="lqd-cfg-pageindex-dir" type="text" class="lqd-form-input" value="' + escapeHtml(cfg.pageindex_dir) + '" />' +
        '</div>' +
        '<div class="lqd-form-group">' +
          '<label class="lqd-form-label">PDF 原档目录(pdfs)</label>' +
          '<input id="lqd-cfg-pdfs-dir" type="text" class="lqd-form-input" value="' + escapeHtml(cfg.pdfs_dir) + '" />' +
        '</div>' +
        '<div class="lqd-form-group">' +
          '<label class="lqd-form-label">PDF 提取策略</label>' +
          '<select id="lqd-cfg-pdf-strategy" class="lqd-form-select">' +
            '<option value="local"' + (cfg.pdf_strategy === 'local' ? ' selected' : '') + '>local(本地,离线)</option>' +
            '<option value="mineru"' + (cfg.pdf_strategy === 'mineru' ? ' selected' : '') + '>mineru(高质量,需 API key)</option>' +
          '</select>' +
        '</div>' +
        '<div class="lqd-form-row lqd-form-row--actions">' +
          '<button id="lqd-cfg-save-storage" class="lqd-btn lqd-btn--primary">保存</button>' +
        '</div>' +
        '<div id="lqd-cfg-storage-status" class="lqd-cfg-test-status"></div>' +
      '</div>';

    $('lqd-cfg-save-storage').addEventListener('click', saveStorage);
  }

  async function saveStorage() {
    var body = {
      content_dir: $('lqd-cfg-content-dir').value.trim(),
      pageindex_dir: $('lqd-cfg-pageindex-dir').value.trim(),
      pdfs_dir: $('lqd-cfg-pdfs-dir').value.trim(),
      pdf_strategy: $('lqd-cfg-pdf-strategy').value
    };
    var btn = $('lqd-cfg-save-storage');
    btn.disabled = true;
    btn.textContent = '保存中...';
    try {
      var r = await fetch('/api/app/config', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      if (!r.ok) {
        var data = await r.json().catch(function () { return {}; });
        throw new Error(data.detail || 'HTTP ' + r.status);
      }
      var result = await r.json();
      var msg = '存储配置已保存';
      if (result.requires_restart) msg += '(端口/主机变更需重启应用)';
      setStorageStatus('success', msg);
    } catch (e) {
      setStorageStatus('error', '保存失败: ' + e.message);
    }
    btn.disabled = false;
    btn.textContent = '保存';
  }

  function setStorageStatus(status, text) {
    var el = $('lqd-cfg-storage-status');
    if (!el) return;
    el.dataset.status = status;
    el.textContent = text || '';
  }

  // ── 应用表单 ──────────────────────────────────────────────────────────
  async function renderAppForm(container) {
    var cfg = await loadAppConfig();
    container.innerHTML =
      '<div class="lqd-config-section">' +
        '<h3>应用设置</h3>' +
        '<div class="lqd-form-hint" style="margin-bottom:var(--space-4)">HTTP 服务监听设置,变更后需重启应用生效。</div>' +
        '<div class="lqd-form-group">' +
          '<label class="lqd-form-label">HTTP Host</label>' +
          '<input id="lqd-cfg-http-host" type="text" class="lqd-form-input" value="' + escapeHtml(cfg.http_host) + '" />' +
          '<div class="lqd-form-hint">默认 127.0.0.1(仅本地),改 0.0.0.0 允许局域网访问</div>' +
        '</div>' +
        '<div class="lqd-form-group">' +
          '<label class="lqd-form-label">HTTP Port</label>' +
          '<input id="lqd-cfg-http-port" type="number" class="lqd-form-input" value="' + cfg.http_port + '" />' +
        '</div>' +
        '<div class="lqd-form-row lqd-form-row--actions">' +
          '<button id="lqd-cfg-save-app" class="lqd-btn lqd-btn--primary">保存</button>' +
          '<button id="lqd-cfg-restart" class="lqd-btn">重启应用</button>' +
        '</div>' +
        '<div id="lqd-cfg-app-status" class="lqd-cfg-test-status"></div>' +
      '</div>';

    $('lqd-cfg-save-app').addEventListener('click', saveApp);
    $('lqd-cfg-restart').addEventListener('click', restartApp);
  }

  async function saveApp() {
    var body = {
      http_host: $('lqd-cfg-http-host').value.trim(),
      http_port: parseInt($('lqd-cfg-http-port').value, 10)
    };
    var btn = $('lqd-cfg-save-app');
    btn.disabled = true;
    btn.textContent = '保存中...';
    try {
      var r = await fetch('/api/app/config', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      var result = await r.json();
      setAppStatus('success', result.requires_restart ? '已保存,需重启应用生效' : '应用设置已保存');
    } catch (e) {
      setAppStatus('error', '保存失败: ' + e.message);
    }
    btn.disabled = false;
    btn.textContent = '保存';
  }

  async function restartApp() {
    var ok = window.LqdModal
      ? await window.LqdModal.confirm({
          title: '重启应用',
          message: '确定重启应用?未保存的更改将丢失。',
          confirmLabel: '重启',
          cancelLabel: '取消',
          danger: true
        })
      : confirm('确定重启应用?未保存的更改将丢失。');
    if (!ok) return;
    // pywebview 提供 restart API(若可用)
    if (window.pywebview && window.pywebview.api && window.pywebview.api.restart) {
      window.pywebview.api.restart();
    } else {
      setAppStatus('error', '重启功能仅在桌面应用内可用(开发模式请手动重启)');
    }
  }

  function setAppStatus(status, text) {
    var el = $('lqd-cfg-app-status');
    if (!el) return;
    el.dataset.status = status;
    el.textContent = text || '';
  }

  // ── 应用配置加载 ──────────────────────────────────────────────────────
  async function loadAppConfig() {
    try {
      var r = await fetch('/api/app/config');
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return await r.json();
    } catch (e) {
      return {
        content_dir: '', pageindex_dir: '', pdfs_dir: '',
        pdf_strategy: 'local', http_host: '127.0.0.1', http_port: 8765, use_llm_proxy: false
      };
    }
  }

  // ── 分组切换 ──────────────────────────────────────────────────────────
  function showGroup(group) {
    state.currentGroup = group;
    var body = state.els.body;
    if (!body) return;
    if (group === 'llm') renderLLMForm(body);
    else if (group === 'storage') renderStorageForm(body);
    else if (group === 'app') renderAppForm(body);
  }

  // ── 初始化 ────────────────────────────────────────────────────────────
  async function init(container) {
    // 先加载 providers(单一来源)
    await window.YuuProviders.load();
    await loadSettings();

    container.innerHTML =
      '<div class="lqd-config-page">' +
        '<aside class="lqd-config-nav">' +
          '<div class="lqd-config-nav-item active" data-group="llm">LLM 模型</div>' +
          '<div class="lqd-config-nav-item" data-group="storage">存储位置</div>' +
          '<div class="lqd-config-nav-item" data-group="app">应用设置</div>' +
        '</aside>' +
        '<div class="lqd-config-body" id="lqd-config-body"></div>' +
      '</div>';

    state.els.body = $('lqd-config-body');

    // 导航切换
    container.querySelectorAll('.lqd-config-nav-item').forEach(function (item) {
      item.addEventListener('click', function () {
        var group = item.getAttribute('data-group');
        container.querySelectorAll('.lqd-config-nav-item').forEach(function (n) { n.classList.remove('active'); });
        item.classList.add('active');
        showGroup(group);
      });
    });

    showGroup(state.currentGroup);
    state.initialized = true;
  }

  window.YuuConfig = {
    init: init,
    showGroup: showGroup
  };
})();
