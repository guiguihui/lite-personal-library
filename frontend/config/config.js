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
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
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
    var proto = pcfg.protocol || 'auto';
    var pm = pcfg.path_mode || 'auto';

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
          '<div class="lqd-input-with-action">' +
            '<input id="lqd-cfg-model" type="text" class="lqd-form-input" value="' + escapeHtml(pcfg.model || defaults.model) + '" placeholder="' + escapeHtml(defaults.model) + '" />' +
            '<button id="lqd-cfg-fetch-models" type="button" class="lqd-btn lqd-btn--icon" title="从当前 Base URL 拉取上游模型列表" aria-label="获取模型列表">' +
              (window.LqdIcons ? window.LqdIcons.icon('list') : '获取模型列表') +
            '</button>' +
          '</div>' +
          '<div class="lqd-form-hint" id="lqd-cfg-model-hint">点击右侧图标从上游 /models 端点拉取可选项</div>' +
        '</div>' +
        '<div class="lqd-form-group">' +
          '<label class="lqd-form-label" for="lqd-cfg-protocol">协议</label>' +
          '<select id="lqd-cfg-protocol" class="lqd-form-select" aria-describedby="lqd-cfg-protocol-hint">' +
            '<option value="auto"' + (proto === 'auto' ? ' selected' : '') + '>自动(按 provider 推断)</option>' +
            '<option value="anthropic"' + (proto === 'anthropic' ? ' selected' : '') + '>Anthropic (x-api-key)</option>' +
            '<option value="openai"' + (proto === 'openai' ? ' selected' : '') + '>OpenAI 兼容 (Bearer)</option>' +
          '</select>' +
          '<div class="lqd-form-hint" id="lqd-cfg-protocol-hint">custom 端点必须显式选协议;内置 provider 用"自动"即可</div>' +
        '</div>' +
        '<div class="lqd-form-group">' +
          '<label class="lqd-form-label" for="lqd-cfg-path-mode">路径模式</label>' +
          '<select id="lqd-cfg-path-mode" class="lqd-form-select" aria-describedby="lqd-cfg-path-mode-hint">' +
            '<option value="auto"' + (pm === 'auto' ? ' selected' : '') + '>自动(检测已知后缀)</option>' +
            '<option value="full"' + (pm === 'full' ? ' selected' : '') + '>完整路径(不拼接后缀)</option>' +
            '<option value="suffix"' + (pm === 'suffix' ? ' selected' : '') + '>强制拼接后缀</option>' +
          '</select>' +
          '<div class="lqd-form-hint" id="lqd-cfg-path-mode-hint">若 Base URL 已含完整请求路径(如 /v3/anthropic/model),选"完整路径"</div>' +
        '</div>' +
        '<div class="lqd-form-group">' +
          '<label class="lqd-form-label" for="lqd-cfg-api-key">API Key ' + hasKey + '</label>' +
          '<input id="lqd-cfg-api-key" type="password" class="lqd-form-input" placeholder="留空则保持现有 key" aria-describedby="lqd-cfg-api-key-hint" />' +
          '<div class="lqd-form-hint" id="lqd-cfg-api-key-hint">key 优先存系统凭证管理器(keyring),降级明文存本地 llm.yaml</div>' +
          '<div id="lqd-cfg-masked-key" class="lqd-form-masked-key"' + (pcfg.has_key ? '' : ' hidden') + '></div>' +
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
    $('lqd-cfg-fetch-models').addEventListener('click', fetchModels);

    // 异步加载当前 provider 的掩码 key
    loadMaskedKey(active);
  }

  async function loadMaskedKey(provider) {
    var el = document.getElementById('lqd-cfg-masked-key');
    if (!el) return;
    try {
      var r = await fetch('/api/settings/key/masked?provider=' + encodeURIComponent(provider));
      if (!r.ok) { el.hidden = true; return; }
      var data = await r.json();
      if (!data.masked_key) { el.hidden = true; return; }
      var storageLabel = data.storage === 'keyring' ? '系统凭证管理器(备用)' : '本地文件(llm.yaml)';
      var masked = escapeHtml(data.masked_key);
      el.innerHTML = '<span class="lqd-masked-key-value">' + masked + '</span>' +
        '<span class="lqd-masked-key-storage">存储于: ' + escapeHtml(storageLabel) + '</span>';
      el.hidden = false;
    } catch (_) {
      el.hidden = true;
    }
  }

  async function onProviderChange() {
    var provider = $('lqd-cfg-provider').value;
    var defaults = window.YuuProviders.getDefaults(provider);
    var pcfg = (state.settings && state.settings.providers && state.settings.providers[provider]) || {};
    $('lqd-cfg-base-url').value = pcfg.base_url || defaults.base_url;
    $('lqd-cfg-model').value = pcfg.model || defaults.model;
    $('lqd-cfg-protocol').value = pcfg.protocol || 'auto';
    $('lqd-cfg-path-mode').value = pcfg.path_mode || 'auto';
    $('lqd-cfg-api-key').value = '';
    // 更新 has_key 徽章(用 label for 定位,不依赖 DOM 位置)
    var hasKey = pcfg.has_key;
    var badge = document.querySelector('label[for="lqd-cfg-api-key"] .lqd-status-badge');
    if (badge) {
      badge.className = 'lqd-status-badge lqd-status-badge--' + (hasKey ? 'done' : 'failed');
      badge.textContent = hasKey ? '已配置' : '未配置';
    }
    loadMaskedKey(provider);
  }

  async function saveLLM() {
    var provider = $('lqd-cfg-provider').value;
    var baseUrl = $('lqd-cfg-base-url').value.trim();
    var model = $('lqd-cfg-model').value.trim();
    var apiKey = $('lqd-cfg-api-key').value;
    var protocol = $('lqd-cfg-protocol').value;
    var pathMode = $('lqd-cfg-path-mode').value;
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
      // 更新 model/base_url/protocol/path_mode
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
      await fetch('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key: 'protocol', value: protocol, provider: provider })
      });
      await fetch('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key: 'path_mode', value: pathMode, provider: provider })
      });
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
      // api_key 最后写(非空才写)。
      // 必须放在 remember_key/use_llm_proxy 之后:后两者的 PUT 走 save_llm_config
      // 重写 llm.yaml,若在它们之前写 api_key,_plain_keys 明文 key 会被冲掉。
      // 放最后确保 key 是落盘的最后一笔。
      if (apiKey) {
        await fetch('/api/settings', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ key: 'api_key', value: apiKey, provider: provider })
        });
      }

      var note = '';
      if (provider === 'custom' && protocol === 'anthropic') {
        note = '(注:入库翻译暂仅支持 OpenAI 兼容端点,聊天主路径可用)';
      }
      setTestStatus('success', 'LLM 配置已保存' + note);
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
    var model = ($('lqd-cfg-model').value || window.YuuProviders.getDefaults(provider).model).trim();
    var apiKey = $('lqd-cfg-api-key').value.trim() || await fetchApiKey(provider);
    var protocol = $('lqd-cfg-protocol').value;
    var pathMode = $('lqd-cfg-path-mode').value;

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
      var L = window.LqdChatLLM || {};
      var proto = L.resolveProtocol ? L.resolveProtocol(provider, protocol) : (protocol === 'anthropic' || (protocol === 'auto' && provider === 'anthropic') ? 'anthropic' : 'openai');
      var useProxy = window.LqdSettings && window.LqdSettings.get('use_llm_proxy');

      // use_llm_proxy=true 时走后端代理测试。浏览器 fetch 不能设 User-Agent,
      // 某些 Anthropic 协议代理(如澜智 lanz.hikvision.com)靠 UA 识别 Claude
      // 客户端,浏览器直连必 403。走后端代理(httpx 能设 UA)才能真实反映连通性。
      if (useProxy) {
        var BASE = (window.LQD_CHAT_BASE || '').replace(/\/+$/, '');
        var proxyResp = await fetch(BASE + '/api/llm/proxy', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            provider: provider, model: model, base_url: baseUrl,
            system: '', messages: [{ role: 'user', content: 'ping' }],
            max_tokens: 8, tools: null, thinking: false, has_key: !!apiKey,
            protocol: protocol, path_mode: pathMode
          })
        });
        if (proxyResp.ok) {
          // 代理返回 SSE 流,读第一个事件判断成功/失败
          var reader = proxyResp.body.getReader();
          var dec = new TextDecoder();
          var firstChunk = await reader.read();
          var firstText = firstChunk.value ? dec.decode(firstChunk.value) : '';
          // 代理把上游错误包成 {"error":true,...}; 正常是 message_start 事件
          if (firstText.indexOf('"error":true') !== -1 || firstText.indexOf('"error": true') !== -1) {
            var em = '上游返回错误';
            try { em = JSON.parse(firstText.replace(/^data:\s*/, '').trim()).message || em; } catch (_) {}
            // 截断过长的上游错误信息
            setTestStatus('error', '连接失败: ' + em.slice(0, 120));
          } else {
            setTestStatus('success', '连接成功(经后端代理)');
          }
          try { reader.cancel(); } catch (_) {}
        } else {
          setTestStatus('error', '代理请求失败: HTTP ' + proxyResp.status);
        }
        btn.disabled = false;
        return;
      }

      // 直连模式(无代理)
      var url = L.resolveEndpoint ? L.resolveEndpoint(baseUrl, proto, pathMode) : (baseUrl.replace(/\/+$/, '') + (proto === 'anthropic' ? '/v1/messages' : '/v1/chat/completions'));
      var headers, body;
      if (proto === 'anthropic') {
        headers = {
          'Content-Type': 'application/json',
          'x-api-key': apiKey,
          'anthropic-version': '2023-06-01',
          'anthropic-dangerous-direct-browser-access': 'true',
          'User-Agent': 'Claude/1.0'
        };
        body = JSON.stringify({ model: model, max_tokens: 8, messages: [{ role: 'user', content: 'ping' }] });
      } else {
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

  // ── 获取模型列表 ──────────────────────────────────────────────────────
  // 对齐 CC-switch「获取模型列表」:从当前 Base URL 推导 /models 端点,
  // GET 拉上游,弹窗展示 id + owned_by + created,点击回填 Model 字段。
  async function fetchModels() {
    var btn = $('lqd-cfg-fetch-models');
    if (!btn || btn.dataset.busy === '1') return;
    var provider = $('lqd-cfg-provider').value;
    var baseUrl = $('lqd-cfg-base-url').value.trim()
      || (window.YuuProviders.getDefaults(provider).base_url || '');
    var protocol = $('lqd-cfg-protocol').value;
    var pathMode = $('lqd-cfg-path-mode').value;

    if (!baseUrl) {
      setTestStatus('error', '请先填写 Base URL');
      return;
    }

    // 入口态:图标旋转 + 禁用
    btn.dataset.busy = '1';
    btn.disabled = true;
    if (window.LqdSpinner) window.LqdSpinner.inline(btn);
    setTestStatus('loading', '正在拉取模型列表...');

    try {
      var r = await fetch('/api/llm/models', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          provider: provider,
          base_url: baseUrl,
          has_key: true,
          protocol: protocol,
          path_mode: pathMode,
          timeout_sec: 30
        })
      });
      if (!r.ok) {
        var detail = '';
        try { detail = (await r.json()).detail || ''; } catch (_) {}
        throw new Error(detail || ('HTTP ' + r.status));
      }
      var data = await r.json();
      if (!data.ok) {
        setTestStatus('error', '拉取失败: ' + (data.error || '未知错误'));
        if (window.LqdToast) {
          window.LqdToast.show({
            type: 'error',
            message: '获取模型列表失败:' + (data.error || '未知错误'),
            duration: 5000
          });
        }
        return;
      }
      setTestStatus(
        'success',
        '已获取 ' + data.count + ' 个模型(' + data.elapsed_ms + ' ms)'
      );
      openModelsPicker(data, function (id) {
        $('lqd-cfg-model').value = id;
        // 触发 input 事件,让外部可能的 input 监听器也能感知
        $('lqd-cfg-model').dispatchEvent(new Event('input', { bubbles: true }));
      });
    } catch (e) {
      var m = e.message || '';
      if (m.indexOf('Failed to fetch') !== -1) {
        setTestStatus('error', '拉取失败: 网络异常或服务不可达');
      } else {
        setTestStatus('error', '拉取失败: ' + m.slice(0, 120));
      }
    } finally {
      btn.dataset.busy = '';
      btn.disabled = false;
      if (window.LqdSpinner) window.LqdSpinner.stop(btn);
    }
  }

  // 弹窗:对齐 CC-switch 「模型映射」表格 — 菜单显示名 / 实际请求模型 / 上下文窗口
  // 上下文窗口(owned_by)来自 OpenAI 模型元数据;无值时显示 — 。
  function openModelsPicker(data, onPick) {
    var url = data.url || '';
    var elapsed = data.elapsed_ms || 0;
    var proto = data.protocol || '';
    var count = data.count || 0;

    var rows = data.models.map(function (m) {
      var owned = m.owned_by || '—';
      return '' +
        '<tr data-id="' + escapeHtml(m.id) + '" tabindex="0" role="button" aria-label="选择 ' + escapeHtml(m.id) + '">' +
        '<td class="lqd-mp-cell-id"><span class="lqd-mp-id">' + escapeHtml(m.id) + '</span></td>' +
        '<td class="lqd-mp-cell-name"><span class="lqd-mp-name">' + escapeHtml(owned) + '</span></td>' +
        '<td class="lqd-mp-cell-action"><button type="button" class="lqd-btn lqd-btn--xs" data-pick="' + escapeHtml(m.id) + '">使用</button></td>' +
        '</tr>';
    }).join('');

    var message =
      '<div class="lqd-mp">' +
        '<div class="lqd-mp-meta">' +
          '<div class="lqd-mp-meta-row"><span class="lqd-mp-meta-label">端点</span><code class="lqd-mp-meta-val">' + escapeHtml(url) + '</code></div>' +
          '<div class="lqd-mp-meta-row"><span class="lqd-mp-meta-label">协议</span><span class="lqd-mp-meta-val">' + escapeHtml(proto) + '</span> · ' +
            '<span class="lqd-mp-meta-label">耗时</span><span class="lqd-mp-meta-val">' + elapsed + ' ms</span> · ' +
            '<span class="lqd-mp-meta-label">数量</span><span class="lqd-mp-meta-val">' + count + '</span>' +
          '</div>' +
        '</div>' +
        (count > 0
          ? '<div class="lqd-mp-table-wrap"><table class="lqd-mp-table">' +
              '<thead><tr><th>实际请求模型</th><th>所属方</th><th></th></tr></thead>' +
              '<tbody>' + rows + '</tbody>' +
            '</table></div>' +
            '<div class="lqd-mp-hint">点击行或「使用」按钮回填到 Model 字段;Esc 关闭</div>'
          : '<div class="lqd-mp-empty">无可用模型</div>') +
      '</div>';

    if (window.LqdModal) {
      window.LqdModal.alert({
        title: '选择模型(共 ' + count + ' 个)',
        message: message,
        confirmLabel: '关闭',
        cancelLabel: null,
        extraActions: count > 0 ? [{
          label: '复制全部 ID',
          className: 'lqd-btn--ghost',
          onClick: function () {
            var ids = data.models.map(function (m) { return m.id; }).join('\n');
            try {
              if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(ids).then(function () {
                  if (window.LqdToast) window.LqdToast.show({
                    type: 'success', message: '已复制 ' + data.models.length + ' 个模型 ID', duration: 2500
                  });
                });
              } else {
                // 降级:临时 textarea
                var ta = document.createElement('textarea');
                ta.value = ids;
                document.body.appendChild(ta);
                ta.select();
                document.execCommand('copy');
                document.body.removeChild(ta);
                if (window.LqdToast) window.LqdToast.show({
                  type: 'success', message: '已复制 ' + data.models.length + ' 个模型 ID', duration: 2500
                });
              }
            } catch (_) { /* ignore */ }
          }
        }] : []
      });
      // modal 渲染后绑事件(LqdModal 用 requestAnimationFrame 渲染)
      requestAnimationFrame(function () {
        var root = document.getElementById('lqd-modal-root');
        if (!root) return;
        root.querySelectorAll('tr[data-id]').forEach(function (tr) {
          function pick() {
            var id = tr.getAttribute('data-id');
            if (onPick) onPick(id);
            window.LqdModal.close();
          }
          tr.addEventListener('click', pick);
          tr.addEventListener('keydown', function (e) {
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(); }
          });
        });
        root.querySelectorAll('[data-pick]').forEach(function (b) {
          b.addEventListener('click', function (e) {
            e.stopPropagation();
            var id = b.getAttribute('data-pick');
            if (onPick) onPick(id);
            window.LqdModal.close();
          });
        });
      });
    } else {
      // 无 LqdModal(单元测试场景):只输出
      console.log('[fetchModels]', { url: url, count: count, models: data.models });
    }
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
