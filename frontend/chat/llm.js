/**
 * LQ-D — Chat LLM
 *
 * SSE 读取、buildRequest、streamText、callLLMSync。
 * 依赖 window.LqdSettings 解析配置。
 */
(function () {
  'use strict';

  // 结构化 SSE 事件：{type:"thinking"|"text"|"tool_calls"|"stop", ...}
  async function* readSSE(response) {
    var _dbg = window.LQD_DEBUG_SSE;
    var _counts = _dbg ? { thinking: 0, text: 0, tool_calls: 0, stop: 0, lines: 0, dataLines: 0, anthropicDelta: 0, thinkingDelta: 0 } : null;
    if (_dbg) console.log('[SSE] readSSE start', { ok: response.ok, status: response.status });
    if (!response.ok) {
      var text = await response.text().catch(function () { return ''; });
      var msg = 'HTTP ' + response.status;
      try {
        var err = JSON.parse(text);
        if (err.error && err.error.message) msg = err.error.message;
      } catch (_) { /* ignore */ }
      throw new Error(msg);
    }
    var reader = response.body.getReader();
    var decoder = new TextDecoder();
    var buffer = '';
    var toolCallBuffers = {};
    while (true) {
      var read = await reader.read();
      if (read.done) break;
      buffer += decoder.decode(read.value, { stream: true });
      var lines = buffer.split('\n');
      buffer = lines.pop() || '';
      for (var i = 0; i < lines.length; i++) {
        var line = lines[i];
        if (_dbg) _counts.lines++;
        if (!line.startsWith('data: ')) continue;
        if (_dbg) _counts.dataLines++;
        var raw = line.slice(6);
        if (raw === '[DONE]') { if (_dbg) console.log('[SSE] [DONE]', _counts); return; }
        try {
          var json = JSON.parse(raw);
          if (_dbg && json.type) _counts['evt_' + json.type] = (_counts['evt_' + json.type] || 0) + 1;
          // OpenAI 协议（DeepSeek / OpenAI / 兼容端点）
          var choice = json.choices && json.choices[0];
          if (choice) {
            var delta = choice.delta || {};
            if (delta.reasoning_content) {
              if (_dbg) _counts.thinkingDelta++;
              yield { type: 'thinking', text: delta.reasoning_content };
            }
            if (delta.content) {
              yield { type: 'text', text: delta.content };
            }
            if (delta.tool_calls) {
              for (var j = 0; j < delta.tool_calls.length; j++) {
                var tc = delta.tool_calls[j];
                var idx = tc.index != null ? tc.index : 0;
                if (!toolCallBuffers[idx]) {
                  toolCallBuffers[idx] = { id: '', name: '', arguments: '' };
                }
                if (tc.id) toolCallBuffers[idx].id = tc.id;
                if (tc.function && tc.function.name) toolCallBuffers[idx].name = tc.function.name;
                if (tc.function && tc.function.arguments) {
                  toolCallBuffers[idx].arguments += tc.function.arguments;
                }
              }
            }
            if (choice.finish_reason) {
              if (choice.finish_reason === 'tool_calls') {
                yield { type: 'tool_calls', calls: Object.values(toolCallBuffers) };
              }
              yield { type: 'stop', reason: choice.finish_reason, usage: json.usage };
              if (_dbg) console.log('[SSE] openai finish', _counts);
              return;
            }
            continue;
          }
          // Anthropic 协议（保留兼容）
          if (_dbg && json.type === 'content_block_delta') _counts.anthropicDelta++;
          // content_block_start:澜智/Anthropic 把 tool_use 的 id+name 放在这里
          // (content_block.id / content_block.name),不在 delta 里。
          // 不处理的话 toolCallBuffers[idx] 的 id/name 始终为空 →
          // agent.js executeTool 拿不到 name → 工具调用失效。
          if (json.type === 'content_block_start' && json.content_block && json.content_block.type === 'tool_use') {
            var idxStart = json.index != null ? json.index : 0;
            if (!toolCallBuffers[idxStart]) toolCallBuffers[idxStart] = { id: '', name: '', arguments: '' };
            if (json.content_block.id) toolCallBuffers[idxStart].id = json.content_block.id;
            if (json.content_block.name) toolCallBuffers[idxStart].name = json.content_block.name;
          } else if (json.type === 'content_block_delta') {
            if (json.delta && json.delta.type === 'thinking_delta' && json.delta.thinking) {
              if (_dbg) _counts.thinkingDelta++;
              yield { type: 'thinking', text: json.delta.thinking };
            } else if (json.delta && json.delta.text) {
              yield { type: 'text', text: json.delta.text };
            }
          } else if (json.delta && json.delta.type === 'input_json_delta' && json.delta.partial_json) {
            var idx2 = json.index != null ? json.index : 0;
            if (!toolCallBuffers[idx2]) toolCallBuffers[idx2] = { id: '', name: '', arguments: '' };
            toolCallBuffers[idx2].arguments += json.delta.partial_json;
          } else if (json.type === 'message_delta' && json.delta && json.delta.stop_reason === 'tool_use') {
            yield { type: 'tool_calls', calls: Object.values(toolCallBuffers) };
          } else if (json.type === 'message_delta' && json.delta && json.delta.stop_reason) {
            yield { type: 'stop', reason: json.delta.stop_reason };
          } else if (json.type === 'message_stop') {
            if (_dbg) console.log('[SSE] message_stop', _counts);
            return;
          }
        } catch (_) { /* ignore */ }
      }
    }
    if (_dbg) console.log('[SSE] readSSE end (stream done)', _counts);
  }

  // ── 协议 + 路径判定(与后端 app.llm.providers.resolve_* 严格同步) ─────
  var ANTHROPIC_SUFFIX = '/v1/messages';
  var OPENAI_SUFFIX = '/v1/chat/completions';

  // 部分 Anthropic 协议代理(如澜智大模型 lanz.hikvision.com)靠 User-Agent
  // 识别 "Claude 客户端",非 Claude UA 会被 403。官方 api.anthropic.com 不校验
  // UA,所以默认带上是安全的。与后端 providers.py _CLAUDE_UA 严格同步。
  var CLAUDE_UA = 'Claude/1.0';

  function resolveProtocol(provider, protocol) {
    protocol = protocol || 'auto';
    if (protocol === 'anthropic') return 'anthropic';
    if (protocol === 'openai') return 'openai';
    return provider === 'anthropic' ? 'anthropic' : 'openai';
  }

  function resolveEndpoint(baseUrl, protocol, pathMode) {
    pathMode = pathMode || 'auto';
    var url = (baseUrl || '').replace(/\/+$/, '');
    if (pathMode === 'full') return url;
    var known = [ANTHROPIC_SUFFIX, '/messages', OPENAI_SUFFIX, '/chat/completions'];
    for (var i = 0; i < known.length; i++) {
      if (url.length >= known[i].length && url.endsWith(known[i])) return url;
    }
    return url + (protocol === 'anthropic' ? ANTHROPIC_SUFFIX : OPENAI_SUFFIX);
  }

  function buildRequest(opts) {
    var provider = opts.provider;
    var model = opts.model;
    var baseUrl = opts.baseUrl;
    var apiKey = opts.apiKey;
    var system = opts.system;
    var messages = opts.messages;
    var maxTokens = opts.maxTokens;
    var tools = opts.tools;
    var thinking = opts.thinking;

    var proto = resolveProtocol(provider, opts.protocol);
    var url = resolveEndpoint(baseUrl, proto, opts.pathMode);

    if (proto === 'anthropic') {
      return {
        url: url,
        headers: {
          'Content-Type': 'application/json',
          'x-api-key': apiKey,
          'anthropic-version': '2023-06-01',
          'anthropic-dangerous-direct-browser-access': 'true',
          'User-Agent': CLAUDE_UA
        },
        body: JSON.stringify({
          model: model,
          max_tokens: maxTokens || 4096,
          system: system,
          messages: messages,
          stream: true,
          ...(tools && tools.length ? {
            tools: tools.map(function (t) {
              return {
                name: t.function.name,
                description: t.function.description,
                input_schema: t.function.parameters
              };
            }),
            tool_choice: { type: 'auto' }
          } : {}),
          ...(thinking ? {
            thinking: { type: 'enabled', budget_tokens: Math.min(maxTokens || 4096, 8000) }
          } : {})
        })
      };
    }
    // OpenAI 兼容协议（含 DeepSeek / OpenAI / SiliconFlow / GLM / DashScope / Gemini / Ollama / custom+openai）
    var body = {
      model: model,
      max_tokens: maxTokens || 4096,
      messages: [{ role: 'system', content: system }].concat(messages),
      stream: true
    };
    if (tools && tools.length) {
      body.tools = tools;
      body.tool_choice = 'auto';
    }
    if (provider === 'deepseek') {
      body.thinking = { type: thinking ? 'enabled' : 'disabled' };
    }
    return {
      url: url,
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + apiKey },
      body: JSON.stringify(body)
    };
  }

  // 走后端代理时,前端不直连上游(解决 CORS + 浏览器禁设 User-Agent 的问题)。
  // 后端 /api/llm/proxy 用 httpx 转发,能自由设 x-api-key/UA 等头。
  // 请求体对齐 app.http.routes_llm_proxy.LlmProxyRequest(不传 api_key,后端从 keyring 读)。
  function buildProxyBody(opts) {
    return JSON.stringify({
      provider: opts.provider,
      model: opts.model,
      base_url: opts.baseUrl,
      system: opts.system || '',
      messages: opts.messages || [],
      max_tokens: opts.maxTokens || 4096,
      tools: opts.tools || null,
      thinking: !!opts.thinking,
      has_key: !!opts.apiKey,  // 后端凭此决定是否从 keyring 取 key
      protocol: opts.protocol || 'auto',
      path_mode: opts.pathMode || 'auto'
    });
  }

  async function* streamText(opts) {
    var useProxy = window.LqdSettings && window.LqdSettings.get('use_llm_proxy');
    if (window.LQD_DEBUG_SSE) console.log('[SSE] streamText start', { useProxy: useProxy, thinking: !!opts.thinking, protocol: opts.protocol, provider: opts.provider, hasTools: !!(opts.tools && opts.tools.length) });
    if (useProxy) {
      var BASE = (window.LQD_CHAT_BASE || '').replace(/\/+$/, '');
      var resp = await fetch(BASE + '/api/llm/proxy', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: buildProxyBody(opts)
      });
      if (window.LQD_DEBUG_SSE) console.log('[SSE] proxy resp', { ok: resp.ok, status: resp.status });
      yield* readSSE(resp);
      return;
    }
    var req = buildRequest(opts);
    var resp2 = await fetch(req.url, { method: 'POST', headers: req.headers, body: req.body });
    yield* readSSE(resp2);
  }

  // 轻量非流式 LLM 调用（给 rewrite_query / llmRerank 用）
  async function callLLMSync(systemPrompt, userPrompt) {
    var cfg = window.LqdSettings ? window.LqdSettings.resolve() : {};
    if (!cfg.apiKey) return '';
    var opts = {
      provider: cfg.provider,
      model: cfg.model,
      baseUrl: cfg.baseUrl,
      apiKey: cfg.apiKey,
      protocol: cfg.protocol,
      pathMode: cfg.pathMode,
      system: systemPrompt,
      messages: [{ role: 'user', content: userPrompt }],
      maxTokens: 1024
    };
    var useProxy = window.LqdSettings && window.LqdSettings.get('use_llm_proxy');

    // 走后端代理:上游返回 SSE 流,用 readSSE 聚合出文本
    if (useProxy) {
      try {
        var BASE = (window.LQD_CHAT_BASE || '').replace(/\/+$/, '');
        var text = '';
        for await (var chunk of streamText(opts)) {
          if (chunk.type === 'text') text += chunk.text;
          if (chunk.type === 'stop') break;
        }
        return text;
      } catch (_) {
        return '';
      }
    }

    // 直连:非流式一次性请求
    var req = buildRequest(opts);
    var ctrl = new AbortController();
    var timer = setTimeout(function () { ctrl.abort(); }, 30000);
    try {
      var resp = await fetch(req.url, {
        method: 'POST',
        headers: Object.assign({}, req.headers, { Accept: 'application/json' }),
        body: req.body,
        signal: ctrl.signal
      });
      if (!resp.ok) return '';
      var data = await resp.json().catch(function () { return null; });
      return (data && data.choices && data.choices[0] && data.choices[0].message && data.choices[0].message.content) ||
        (data && data.content && data.content[0] && data.content[0].text) || '';
    } catch (_) {
      return '';
    } finally {
      clearTimeout(timer);
    }
  }

  window.LqdChatLLM = {
    readSSE: readSSE,
    buildRequest: buildRequest,
    resolveProtocol: resolveProtocol,
    resolveEndpoint: resolveEndpoint,
    streamText: streamText,
    callLLMSync: callLLMSync
  };
})();
