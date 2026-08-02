(function () {
  'use strict';
  function mount(container, graph, onOpen) {
    container.innerHTML = '';
    var width = Math.max(container.clientWidth || 280, 240), height = 240, cx = width / 2, cy = height / 2;
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); svg.setAttribute('viewBox', '0 0 ' + width + ' ' + height); svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', '当前文档的一跳知识图谱');
    var defs = document.createElementNS(svg.namespaceURI, 'defs'); var marker = document.createElementNS(svg.namespaceURI, 'marker'); marker.setAttribute('id', 'lqd-graph-arrow'); marker.setAttribute('viewBox', '0 0 10 10'); marker.setAttribute('refX', '9'); marker.setAttribute('refY', '5'); marker.setAttribute('markerWidth', '5'); marker.setAttribute('markerHeight', '5'); marker.setAttribute('orient', 'auto-start-reverse'); var arrow = document.createElementNS(svg.namespaceURI, 'path'); arrow.setAttribute('d', 'M 0 0 L 10 5 L 0 10 z'); marker.appendChild(arrow); defs.appendChild(marker); svg.appendChild(defs);
    var nodes = graph.nodes || [], center = graph.center && graph.center.id;
    var positions = {}, simulation = null;
    if (window.d3 && nodes.length) {
      nodes.forEach(function (node) { if (node.id === center) { node.fx = cx; node.fy = cy; } });
      simulation = window.d3.forceSimulation(nodes)
        .force('charge', window.d3.forceManyBody().strength(-140))
        .force('center', window.d3.forceCenter(cx, cy))
        .force('collision', window.d3.forceCollide(20)).stop();
      for (var tick = 0; tick < 100; tick++) simulation.tick();
      nodes.forEach(function (node) { positions[node.id] = [node.x, node.y]; });
    } else {
      nodes.forEach(function (node, index) { var angle = ((index - 1) / Math.max(1, nodes.length - 1)) * Math.PI * 2; positions[node.id] = node.id === center ? [cx, cy] : [cx + Math.cos(angle) * 88, cy + Math.sin(angle) * 88]; });
    }
    nodes.forEach(function (node) { var pos = positions[node.id]; if (!pos) return; pos[0] = Math.max(12, Math.min(width - 12, pos[0])); pos[1] = Math.max(14, Math.min(height - 14, pos[1])); });
    (graph.edges || []).forEach(function (edge) { var a = positions[edge.source_id], b = positions[edge.target_id]; if (!a || !b) return; var line = document.createElementNS(svg.namespaceURI, 'line'); line.setAttribute('x1', a[0]); line.setAttribute('y1', a[1]); line.setAttribute('x2', b[0]); line.setAttribute('y2', b[1]); line.setAttribute('class', 'lqd-graph-edge'); line.setAttribute('marker-end', 'url(#lqd-graph-arrow)'); svg.appendChild(line); });
    nodes.forEach(function (node) { var pos = positions[node.id], group = document.createElementNS(svg.namespaceURI, 'g'); group.setAttribute('class', 'lqd-graph-node ' + node.type); group.setAttribute('tabindex', '0'); group.setAttribute('aria-label', node.title); var circle = document.createElementNS(svg.namespaceURI, 'circle'); circle.setAttribute('cx', pos[0]); circle.setAttribute('cy', pos[1]); circle.setAttribute('r', node.id === center ? 9 : 7); group.appendChild(circle); var label = document.createElementNS(svg.namespaceURI, 'text'); var labelRight = pos[0] > width / 2; label.setAttribute('x', pos[0] + (labelRight ? -10 : 10)); label.setAttribute('y', pos[1] + 4); label.setAttribute('text-anchor', labelRight ? 'end' : 'start'); label.textContent = node.title; group.appendChild(label); group.addEventListener('click', function () { onOpen(node); }); group.addEventListener('keydown', function (event) { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onOpen(node); } }); svg.appendChild(group); });
    container.appendChild(svg);
    return function () { if (simulation) simulation.stop(); container.innerHTML = ''; };
  }
  window.LqdLocalGraph = { mount: mount };
})();
