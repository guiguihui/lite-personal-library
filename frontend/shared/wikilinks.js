(function () {
  'use strict';

  function esc(value) {
    return String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function split(value) {
    var aliasParts = value.split('|');
    var targetParts = aliasParts[0].split('#');
    return { target: targetParts[0].trim(), anchor: targetParts.slice(1).join('#').trim(), alias: aliasParts.slice(1).join('|').trim() };
  }

  function preprocess(markdown) {
    var fenced = false;
    return String(markdown || '').split('\n').map(function (line) {
      if (/^\s*(```|~~~)/.test(line)) { fenced = !fenced; return line; }
      if (fenced) return line;
      var code = [];
      line = line.replace(/(`+)(.*?)\1/g, function (raw) { code.push(raw); return '\u0001CODE' + (code.length - 1) + '\u0001'; });
      line = line.replace(/(?<!!)\[\[([^\[\]\n]+)\]\]/g, function (raw, body) {
        var item = split(body);
        if (!item.target) return raw;
        return '<a class="lqd-wikilink pending" href="#" data-target="' + esc(item.target) + '" data-anchor="' + esc(item.anchor) + '">' + esc(item.alias || item.target) + '</a>';
      });
      return line.replace(/\u0001CODE(\d+)\u0001/g, function (_, index) { return code[Number(index)]; });
    }).join('\n');
  }

  function hydrate(container, currentId) {
    if (!container || !currentId) return Promise.resolve([]);
    var anchors = Array.prototype.slice.call(container.querySelectorAll('.lqd-wikilink.pending'));
    if (!anchors.length) return Promise.resolve([]);
    var targets = anchors.map(function (node) { return { target: node.dataset.target, anchor: node.dataset.anchor || null }; });
    return fetch('/api/links/resolve', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ current_id: currentId, targets: targets }) })
      .then(function (response) { if (!response.ok) throw new Error('HTTP ' + response.status); return response.json(); })
      .then(function (data) {
        (data.results || []).forEach(function (result, index) {
          var node = anchors[index];
          node.classList.remove('pending');
          node.classList.add(result.status);
          if (result.status !== 'resolved') { node.setAttribute('aria-disabled', 'true'); node.title = result.status === 'ambiguous' ? '链接目标不唯一' : '链接目标不存在'; return; }
          node.dataset.docId = result.id; node.dataset.type = result.type; node.dataset.slug = result.slug; node.dataset.nodeId = result.node_id || '';
          node.title = result.title || result.id;
        });
        return data.results || [];
      }).catch(function (error) { anchors.forEach(function (node) { node.classList.replace('pending', 'broken'); node.title = error.message; }); return []; });
  }

  document.addEventListener('click', function (event) {
    var link = event.target.closest && event.target.closest('.lqd-wikilink.resolved');
    if (!link) return;
    event.preventDefault();
    if (window.matchMedia && window.matchMedia('(pointer: coarse)').matches && link.dataset.touchPreview !== 'open') {
      link.dataset.touchPreview = 'open';
      if (window.LqdLinkPopover) window.LqdLinkPopover.show(link);
      return;
    }
    if (window.LqdLibrary && window.LqdLibrary.openDoc) window.LqdLibrary.openDoc(link.dataset.type, link.dataset.slug, link.dataset.nodeId || null);
  });

  window.LqdWikilinks = { preprocess: preprocess, hydrate: hydrate, split: split };
  if (typeof module !== 'undefined') module.exports = window.LqdWikilinks;
})();
