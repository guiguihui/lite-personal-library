(function () {
  'use strict';
  function mount(container, graph, onOpen) {
    container.innerHTML = '';
    var width = Math.max(container.clientWidth || 280, 240), height = 240, cx = width / 2, cy = height / 2;
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 ' + width + ' ' + height);
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', '当前文档的一跳知识图谱');

    // ── defs:箭头 + 光晕渐变 + 边渐变 ──
    var defs = document.createElementNS(svg.namespaceURI, 'defs');
    var marker = document.createElementNS(svg.namespaceURI, 'marker');
    marker.setAttribute('id', 'lqd-graph-arrow');
    marker.setAttribute('viewBox', '0 0 10 10');
    marker.setAttribute('refX', '8');
    marker.setAttribute('refY', '5');
    marker.setAttribute('markerWidth', '4.5');
    marker.setAttribute('markerHeight', '4.5');
    marker.setAttribute('orient', 'auto-start-reverse');
    var arrow = document.createElementNS(svg.namespaceURI, 'path');
    arrow.setAttribute('d', 'M 0 0 L 10 5 L 0 10 z');
    arrow.setAttribute('class', 'lqd-graph-arrow');
    marker.appendChild(arrow);
    defs.appendChild(marker);

    // 节点光晕:径向渐变(中心色 → 透明)。stop-color 用 currentColor,
    // 每个节点按自己的 --node-color 解析,共用同一 def。
    var haloGrad = document.createElementNS(svg.namespaceURI, 'radialGradient');
    haloGrad.setAttribute('id', 'lqd-graph-halo-grad');
    var h0 = document.createElementNS(svg.namespaceURI, 'stop');
    h0.setAttribute('offset', '0%'); h0.setAttribute('stop-color', 'currentColor'); h0.setAttribute('stop-opacity', '0.50');
    var h1 = document.createElementNS(svg.namespaceURI, 'stop');
    h1.setAttribute('offset', '65%'); h1.setAttribute('stop-color', 'currentColor'); h1.setAttribute('stop-opacity', '0.16');
    var h2 = document.createElementNS(svg.namespaceURI, 'stop');
    h2.setAttribute('offset', '100%'); h2.setAttribute('stop-color', 'currentColor'); h2.setAttribute('stop-opacity', '0');
    haloGrad.appendChild(h0); haloGrad.appendChild(h1); haloGrad.appendChild(h2);
    defs.appendChild(haloGrad);

    // 边:柔和紫→蓝渐变
    var edgeGrad = document.createElementNS(svg.namespaceURI, 'linearGradient');
    edgeGrad.setAttribute('id', 'lqd-graph-edge-grad');
    edgeGrad.setAttribute('x1', '0'); edgeGrad.setAttribute('y1', '0'); edgeGrad.setAttribute('x2', '1'); edgeGrad.setAttribute('y2', '1');
    var e0 = document.createElementNS(svg.namespaceURI, 'stop');
    e0.setAttribute('offset', '0%'); e0.setAttribute('stop-color', 'var(--tint-purple, #8b5cf6)'); e0.setAttribute('stop-opacity', '0.5');
    var e1 = document.createElementNS(svg.namespaceURI, 'stop');
    e1.setAttribute('offset', '100%'); e1.setAttribute('stop-color', 'var(--tint-blue, #3b82f6)'); e1.setAttribute('stop-opacity', '0.5');
    edgeGrad.appendChild(e0); edgeGrad.appendChild(e1);
    defs.appendChild(edgeGrad);
    svg.appendChild(defs);

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

    // 边:底层柔和线 + 上层流动光点(能量沿连线流动)
    (graph.edges || []).forEach(function (edge, index) {
      var a = positions[edge.source_id], b = positions[edge.target_id];
      if (!a || !b) return;
      var x1 = a[0], y1 = a[1], x2 = b[0], y2 = b[1];
      var base = document.createElementNS(svg.namespaceURI, 'line');
      base.setAttribute('x1', x1); base.setAttribute('y1', y1); base.setAttribute('x2', x2); base.setAttribute('y2', y2);
      base.setAttribute('class', 'lqd-graph-edge lqd-graph-edge--base');
      base.setAttribute('marker-end', 'url(#lqd-graph-arrow)');
      svg.appendChild(base);
      var pulse = document.createElementNS(svg.namespaceURI, 'line');
      pulse.setAttribute('x1', x1); pulse.setAttribute('y1', y1); pulse.setAttribute('x2', x2); pulse.setAttribute('y2', y2);
      pulse.setAttribute('class', 'lqd-graph-edge lqd-graph-edge--pulse');
      pulse.setAttribute('stroke-dasharray', '6 90');
      pulse.style.animationDelay = (index * 0.7) + 's';
      svg.appendChild(pulse);
    });

    // 节点:光晕 + 主体 + (中心)呼吸环
    nodes.forEach(function (node, index) {
      var pos = positions[node.id]; if (!pos) return;
      var isCenter = node.id === center;
      var group = document.createElementNS(svg.namespaceURI, 'g');
      group.setAttribute('class', 'lqd-graph-node ' + node.type + (isCenter ? ' is-center' : ''));
      group.setAttribute('tabindex', '0');
      group.setAttribute('aria-label', node.title);

      var halo = document.createElementNS(svg.namespaceURI, 'circle');
      halo.setAttribute('cx', pos[0]); halo.setAttribute('cy', pos[1]);
      halo.setAttribute('r', isCenter ? 24 : 17);
      halo.setAttribute('class', 'lqd-graph-halo');
      halo.style.animationDelay = (index * 0.22) + 's';
      group.appendChild(halo);

      if (isCenter) {
        var ring = document.createElementNS(svg.namespaceURI, 'circle');
        ring.setAttribute('cx', pos[0]); ring.setAttribute('cy', pos[1]);
        ring.setAttribute('r', 14);
        ring.setAttribute('class', 'lqd-graph-ring');
        group.appendChild(ring);
      }

      var circle = document.createElementNS(svg.namespaceURI, 'circle');
      circle.setAttribute('cx', pos[0]); circle.setAttribute('cy', pos[1]);
      circle.setAttribute('r', isCenter ? 9 : 7);
      circle.setAttribute('class', 'lqd-graph-dot');
      group.appendChild(circle);

      var label = document.createElementNS(svg.namespaceURI, 'text');
      var labelRight = pos[0] > width / 2;
      label.setAttribute('x', pos[0] + (labelRight ? -10 : 10));
      label.setAttribute('y', pos[1] + 4);
      label.setAttribute('text-anchor', labelRight ? 'end' : 'start');
      label.textContent = node.title;
      group.appendChild(label);

      group.addEventListener('click', function () { onOpen(node); });
      group.addEventListener('keydown', function (event) { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onOpen(node); } });
      svg.appendChild(group);
    });

    container.appendChild(svg);
    return function () { if (simulation) simulation.stop(); container.innerHTML = ''; };
  }
  window.LqdLocalGraph = { mount: mount };
})();
