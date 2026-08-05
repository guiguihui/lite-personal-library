/**
 * LQ-D — Config Tab Component
 *
 * 包装原有 config.js 的配置表单。
 */
(function () {
  'use strict';

  function mount(container, tab) {
    container.innerHTML = '<div class="lqd-config" id="lqd-config-root"></div>';
    if (window.YuuConfig && typeof window.YuuConfig.init === 'function') {
      window.YuuConfig.init(container.querySelector('#lqd-config-root'));
    }
  }

  function unmount(container, tab) {}

  function getTitle(tab) {
    return tab.title || '配置';
  }

  function getIcon() {
    return 'config';
  }

  function renderSidebar(container) {
    container.innerHTML =
      '<div class="lqd-sidebar-section-title">配置</div>' +
      '<div class="lqd-config-nav" role="tablist">' +
        '<div class="lqd-list-item active" data-group="llm" role="tab" tabindex="0" aria-selected="true">LLM</div>' +
        '<div class="lqd-list-item" data-group="storage" role="tab" tabindex="0" aria-selected="false">存储</div>' +
        '<div class="lqd-list-item" data-group="app" role="tab" tabindex="0" aria-selected="false">应用</div>' +
      '</div>';

    container.querySelectorAll('[data-group]').forEach(function (item) {
      function activate() {
        container.querySelectorAll('[data-group]').forEach(function (i) {
          i.classList.remove('active');
          i.setAttribute('aria-selected', 'false');
        });
        item.classList.add('active');
        item.setAttribute('aria-selected', 'true');
        if (window.YuuConfig && typeof window.YuuConfig.showGroup === 'function') {
          window.YuuConfig.showGroup(item.getAttribute('data-group'));
        }
      }
      item.addEventListener('click', activate);
      item.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          activate();
        }
      });
    });
  }

  function renderOverview(container, tab) {
    container.innerHTML =
      '<div class="lqd-overview-section-title">配置</div>' +
      '<div class="lqd-empty">修改 LLM、存储与应用设置</div>';
  }

  var LqdConfig = {
    type: 'config',
    getTitle: getTitle,
    getIcon: getIcon,
    mount: mount,
    unmount: unmount,
    renderSidebar: renderSidebar,
    renderOverview: renderOverview
  };

  window.LqdConfig = LqdConfig;

  function tryRegister() {
    if (window.LqdTabs) window.LqdTabs.register('config', LqdConfig);
    if (window.LqdSidebar) window.LqdSidebar.register('config', LqdConfig);
    if (window.LqdOverview) window.LqdOverview.register('config', LqdConfig);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', tryRegister);
  } else {
    tryRegister();
  }
})();
