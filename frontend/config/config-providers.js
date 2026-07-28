/**
 * LQ-D — Provider 默认值(前端单一来源)
 *
 * 从后端 GET /api/settings/providers 读取,消除 chat.js/defaults.py/providers.py
 * 三处手动同步。后端 defaults.py 是唯一真实来源。
 *
 * 用法:await YuuProviders.load(); YuuProviders.getDefaults('anthropic')
 */
(function () {
  'use strict';

  var cache = null;

  // 降级默认值(后端不可达时用,与后端 defaults.py 保持一致)
  var FALLBACK = {
    anthropic: { model: 'claude-sonnet-4-6', base_url: 'https://api.anthropic.com', protocol: 'auto', path_mode: 'auto' },
    deepseek: { model: 'deepseek-v4-flash', base_url: 'https://api.deepseek.com', protocol: 'auto', path_mode: 'auto' },
    openai: { model: 'gpt-4o', base_url: 'https://api.openai.com', protocol: 'auto', path_mode: 'auto' },
    siliconflow: { model: 'deepseek-ai/DeepSeek-V3', base_url: 'https://api.siliconflow.cn', protocol: 'auto', path_mode: 'auto' },
    openrouter: { model: 'anthropic/claude-sonnet-4', base_url: 'https://openrouter.ai/api', protocol: 'auto', path_mode: 'auto' },
    zhipu: { model: 'glm-4', base_url: 'https://open.bigmodel.cn/api/paas/v4', protocol: 'auto', path_mode: 'auto' },
    dashscope: { model: 'qwen-plus', base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1', protocol: 'auto', path_mode: 'auto' },
    ollama: { model: 'llama3', base_url: 'http://localhost:11434', protocol: 'auto', path_mode: 'auto' },
    gemini: { model: 'gemini-2.5-flash', base_url: 'https://generativelanguage.googleapis.com/v1beta/openai', protocol: 'auto', path_mode: 'auto' },
    custom: { model: '', base_url: '', protocol: 'auto', path_mode: 'auto' }
  };

  var FALLBACK_NAMES = [
    'anthropic', 'deepseek', 'openai', 'siliconflow', 'openrouter',
    'zhipu', 'dashscope', 'ollama', 'gemini', 'custom'
  ];

  function load() {
    if (cache) return Promise.resolve(cache);
    return fetch('/api/settings/providers')
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (data && data.names && data.defaults) {
          cache = { names: data.names, defaults: data.defaults };
        } else {
          cache = { names: FALLBACK_NAMES, defaults: FALLBACK };
        }
        return cache;
      })
      .catch(function () {
        cache = { names: FALLBACK_NAMES, defaults: FALLBACK };
        return cache;
      });
  }

  function getNames() {
    return cache ? cache.names : FALLBACK_NAMES;
  }

  function getDefaults(provider) {
    if (cache && cache.defaults[provider]) return cache.defaults[provider];
    return FALLBACK[provider] || { model: '', base_url: '' };
  }

  window.YuuProviders = {
    load: load,
    getNames: getNames,
    getDefaults: getDefaults
  };
})();
