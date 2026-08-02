(function () {
  'use strict';
  var timer = null, controller = null, popover = null, cache = new Map();

  function close() { clearTimeout(timer); if (controller) controller.abort(); controller = null; if (popover) popover.remove(); popover = null; }
  function show(link) { close();
    if (window.LQD_FEATURES && !window.LQD_FEATURES.link_preview_enabled) return;
    timer = setTimeout(function () {
      var id = link.dataset.docId || link.dataset.previewId;
      if (!id) return;
      var request = cache.has(id) ? Promise.resolve(cache.get(id)) : (function () {
        controller = new AbortController();
        return fetch('/api/links/preview?id=' + encodeURIComponent(id) + '&node_id=' + encodeURIComponent(link.dataset.nodeId || ''), { signal: controller.signal })
          .then(function (response) { if (!response.ok) throw new Error('HTTP ' + response.status); return response.json(); })
          .then(function (data) { cache.set(id, data); if (cache.size > 50) cache.delete(cache.keys().next().value); return data; });
      })();
      request.then(function (data) {
        popover = document.createElement('aside'); popover.className = 'lqd-link-popover'; popover.setAttribute('role', 'tooltip');
        var title = document.createElement('strong'); title.textContent = data.title; popover.appendChild(title);
        var meta = document.createElement('div'); meta.className = 'lqd-link-popover-meta'; meta.textContent = data.type + ' · ' + ((data.governance || {}).status || 'draft'); popover.appendChild(meta);
        var body = document.createElement('div'); body.innerHTML = window.YuuRender ? window.YuuRender.md(data.excerpt_markdown || '') : ''; popover.appendChild(body);
        document.body.appendChild(popover); var rect = link.getBoundingClientRect(); popover.style.left = Math.min(rect.left, window.innerWidth - 340) + 'px'; popover.style.top = Math.min(rect.bottom + 8, window.innerHeight - popover.offsetHeight - 8) + 'px';
      }).catch(function () {});
    }, 250);
  }

  function attach(container) {
    if (!container) return function () {};
    function over(event) { var link = event.target.closest && event.target.closest('.lqd-wikilink.resolved,[data-preview-id]'); if (link) show(link); }
    container.addEventListener('mouseover', over); container.addEventListener('focusin', over); container.addEventListener('mouseout', close); container.addEventListener('focusout', close);
    return function () { close(); container.removeEventListener('mouseover', over); container.removeEventListener('focusin', over); container.removeEventListener('mouseout', close); container.removeEventListener('focusout', close); };
  }
  window.LqdLinkPopover = { attach: attach, close: close, show: show };
})();
