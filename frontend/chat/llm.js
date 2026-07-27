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
        if (!line.startsWith('data: ')) continue;
        var raw = line.slice(6);
        if (raw === '[DONE]') return;
        try {
          var json = JSON.parse(raw);
          // OpenAI 协议（DeepSeek / OpenAI / 兼容端点）
          var choice = json.choices && json.choices[0];
          if (choice) {
            var delta = choice.delta || {};
            if (delta.reasoning_content) {
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
              return;
            }
            continue;
          }
          // Anthropic 协议（保留兼容）
          if (json.type === 'content_block_delta') {
            if (json.delta && json.delta.type === 'thinking_delta' && json.delta.thinking) {
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
            return;
          }
        } catch (_) { /* ignore */ }
      }
    }
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

    if (provider === 'anthropic') {
      return {
        url: baseUrl + '/v1/messages',
        headers: {
          'Content-Type': 'application/json',
          'x-api-key': apiKey,
          'anthropic-version': '2023-06-01',
          'anthropic-dangerous-direct-browser-access': 'true'
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
    // OpenAI 兼容协议（含 DeepSeek / OpenAI / SiliconFlow / GLM / DashScope / Gemini / Ollama）
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
      url: baseUrl + '/v1/chat/completions',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + apiKey },
      body: JSON.stringify(body)
    };
  }

  async function* streamText(opts) {
    var req = buildRequest(opts);
    var resp = await fetch(req.url, { method: 'POST', headers: req.headers, body: req.body });
    yield* readSSE(resp);
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
      system: systemPrompt,
      messages: [{ role: 'user', content: userPrompt }],
      maxTokens: 1024
    };
    var req = buildRequest(opts);
    var ctrl = new AbortController();
    var timer = setTimeout(function () { ctrl.abort(); }, 30000);
    try {
      var resp = await fetch(req.url, {
        method: 'POST',
        headers: Object.assign({}, req.headers, { Accept: 'application/json' }),
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
    streamText: streamText,
    callLLMSync: callLLMSync
  };
})();
