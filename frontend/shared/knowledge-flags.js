(function () {
  'use strict';
  window.LQD_FEATURES = {
    knowledge_index_enabled: true,
    wikilinks_enabled: true,
    backlinks_enabled: true,
    link_preview_enabled: true,
    local_graph_enabled: true,
    provenance_edges_enabled: false
  };
  fetch('/api/links/features').then(function (response) { return response.ok ? response.json() : {}; })
    .then(function (flags) { Object.assign(window.LQD_FEATURES, flags); })
    .catch(function () {});
})();
