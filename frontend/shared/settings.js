/**
 * LQ-D — Shared Settings
 *
 * 从 chat.js 迁出的 LLM 设置对象，后端驱动（/api/settings）。
 * 被 chat/llm.js、chat/agent.js 以及 config.js 共享。
 */
(function () {
  'use strict';

  var BASE = (window.LQD_CHAT_BASE || '').replace(/\/+$/, '');

  var Settings = {
    _cache: null,

    _defaults: function () {
      return { provider: 'anthropic', model: '', base_url: '', api_key: '', remember_key: false };
    },

    _providerDefaults: function () {
      return {
        anthropic: { model: 'claude-sonnet-4-6', base_url: 'https://api.anthropic.com' },
        deepseek: { model: 'deepseek-v4-flash', base_url: 'https://api.deepseek.com' },
        openai: { model: 'gpt-4o', base_url: 'https://api.openai.com' },
        siliconflow: { model: 'deepseek-ai/DeepSeek-V3', base_url: 'https://api.siliconflow.cn' },
        openrouter: { model: 'anthropic/claude-sonnet-4', base_url: 'https://openrouter.ai/api' },
        zhipu: { model: 'glm-4', base_url: 'https://open.bigmodel.cn/api/paas/v4' },
        dashscope: { model: 'qwen-plus', base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1' },
        ollama: { model: 'llama3', base_url: 'http://localhost:11434' },
        gemini: { model: 'gemini-2.5-flash', base_url: 'https://generativelanguage.googleapis.com/v1beta/openai' },
        custom: { model: '', base_url: '' }
      };
    },

    load: async function () {
      try {
        var r = await fetch(BASE + '/api/settings');
        if (!r.ok) throw new Error('HTTP ' + r.status);
        var data = await r.json();
        var p = data.active_provider || 'anthropic';
        var pcfg = (data.providers || {})[p] || {};
        var defaults = this._providerDefaults()[p] || { model: '', base_url: '' };
        this._cache = {
          provider: p,
          model: pcfg.model || defaults.model,
          base_url: pcfg.base_url || defaults.base_url,
          api_key: '',
          remember_key: data.remember_key || false,
          use_llm_proxy: data.use_llm_proxy || false,
          _providers: data.providers || {},
          _has_key: !!pcfg.has_key
        };
      } catch (_) {
        this._cache = this._defaults();
      }
      return this._cache;
    },

    get: function (key) {
      if (!this._cache) return this._defaults()[key] || '';
      if (key === 'api_key') return this._cache.api_key || '';
      if (key === 'remember_key') return String(this._cache.remember_key || false);
      if (key === 'provider') return this._cache.provider || '';
      if (key === 'model') return this._cache.model || '';
      if (key === 'base_url') return this._cache.base_url || '';
      return this._cache[key] || '';
    },

    set: async function (key, val) {
      var provider = this._cache ? this._cache.provider : 'anthropic';
      try {
        await fetch(BASE + '/api/settings', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ key: key, value: val, provider: provider })
        });
      } catch (_) { /* ignore */ }
      await this.load();
    },

    setApiKey: async function (key) {
      var provider = this._cache ? this._cache.provider : 'anthropic';
      try {
        await fetch(BASE + '/api/settings', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ key: 'api_key', value: key, provider: provider })
        });
        if (this._cache) this._cache._has_key = !!key;
      } catch (_) { /* ignore */ }
    },

    fetchApiKey: async function () {
      var provider = this._cache ? this._cache.provider : 'anthropic';
      try {
        var r = await fetch(BASE + '/api/settings/key?provider=' + encodeURIComponent(provider));
        if (!r.ok) return '';
        var data = await r.json();
        return data.api_key || '';
      } catch (_) {
        return '';
      }
    },

    resolve: function () {
      var p = this.get('provider');
      var model = this.get('model');
      var baseUrl = this.get('base_url');
      var apiKey = this.get('api_key');
      return { provider: p, model: model, baseUrl: baseUrl, apiKey: apiKey };
    }
  };

  window.LqdSettings = Settings;
})();
