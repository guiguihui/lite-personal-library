/**
 * LQ-D — Thinking Orbs (原生 JS 封装)
 *
 * 源自 thinking-orbs@0.1.1 (React 组件库)，提取纯 canvas 渲染逻辑，
 * 移除 React 依赖，封装为原生 JS 类，供项目全局使用。
 *
 * 6 种动画状态：working / searching / solving / listening / composing / shaping
 * 每种状态对应不同的 canvas 绘制模式（orbits / globe / rubik / wave / ribbon / morph）。
 *
 * 用法：
 *   var orb = new ThinkingOrb(canvasEl, { state: 'working', size: 64 });
 *   orb.setState('searching');
 *   orb.destroy();
 *
 * 或通过便捷方法挂载到元素：
 *   ThinkingOrb.mount(containerEl, { state: 'working', size: 32 });
 */
(function () {
  'use strict';

  // ── 辅助函数 ──────────────────────────────────────────────────────

  // 伪随机数生成（用于确定性粒子分布）
  function T(s, n) {
    var e = Math.sin(s * 12.9898 + n * 78.233) * 43758.5453;
    return e - Math.floor(e);
  }

  // 斐波那契球面分布
  function et(s, n) {
    var e = Math.PI * (3 - Math.sqrt(5));
    var i = 1 - 2 * (s + 0.5) / n;
    var t = Math.sqrt(1 - i * i);
    var o = s * e;
    return [t * Math.cos(o), i, t * Math.sin(o)];
  }

  // 角度差归一化
  function ot(s, n) {
    return Math.atan2(Math.sin(s - n), Math.cos(s - n));
  }

  // 3D 旋转矩阵构造
  function $(s, n, e, i, t) {
    var o = Math.sin(n), a = Math.cos(n);
    var r = Math.sin(s), h = Math.cos(s);
    return function (b, d, c) {
      var f = b * h + c * r;
      var M = -b * r + c * h;
      var x = d * a - M * o;
      var v = d * o + M * a;
      return [e + f * t, i - x * t, v];
    };
  }

  // 深度排序绘制粒子
  function q(s, n, e, i) {
    i = i || 0.3;
    n.sort(function (t, o) { return t.z - o.z; });
    for (var t of n) {
      var o = t.a != null ? t.a : 1;
      if (o < 0.02) continue;
      var a = Math.min(1, Math.max(0, t.white));
      var r = Math.round((e ? 1 - a : a) * 255);
      s.fillStyle = 'rgba(' + r + ',' + r + ',' + r + ',' + o + ')';
      s.beginPath();
      s.arc(t.x, t.y, Math.max(i, t.r), 0, Math.PI * 2);
      s.fill();
    }
  }

  // 尺寸缩放函数
  function _(s, n) {
    return Math.pow(s / 300, n);
  }

  // Rubik 模式：层旋转动画状态机
  function st(s, n, e, i) {
    i = i || 1.2;
    var t = 2 * n * e + i;
    var o = s % t;
    var a = new Array(n).fill(0);
    var r = -1;
    if (o < 2 * n * e) {
      var h = Math.floor(o / e);
      var b = (o - h * e) / e;
      var d = 1 - Math.pow(1 - Math.min(1, b / 0.7), 3);
      if (h < n) {
        for (var c = 0; c < h; c++) a[c] = 1;
        a[h] = d; r = h;
      } else {
        var c2 = 2 * n - 1 - h;
        for (var f = 0; f < c2; f++) a[f] = 1;
        a[c2] = 1 - d; r = c2;
      }
    }
    return { amount: a, active: r };
  }

  // Rubik 模式：应用层旋转
  function at(s, n, e) {
    var i = s[0], t = s[1], o = s[2];
    var a = false;
    for (var r = 0; r < n.length; r++) {
      if (e.amount[r] <= 0) continue;
      var h = n[r];
      var b = h.axis === 0 ? i : h.axis === 1 ? t : o;
      if (b < h.lo || b >= h.hi) continue;
      if (r === e.active) a = true;
      var d = h.ang * e.amount[r];
      var c = Math.cos(d), f = Math.sin(d);
      if (h.axis === 0) {
        var M = t * c - o * f;
        o = t * f + o * c; t = M;
      } else if (h.axis === 1) {
        var M2 = i * c + o * f;
        o = -i * f + o * c; i = M2;
      } else {
        var M3 = i * c - t * f;
        t = i * f + t * c; i = M3;
      }
    }
    return [i, t, o, a];
  }

  // Rubik 模式：生成层旋转配置
  function rt(s) {
    var n = [];
    for (var e = 0; e < s; e++) {
      var i = Math.min(2, Math.floor(T(e, 2.3) * 3));
      var t = -1 + 0.5 * Math.min(3, Math.floor(T(e, 5.9) * 4));
      var o = T(e, 7.7) < 0.5 ? 1 : -1;
      n.push({ axis: i, lo: t, hi: t + 0.5, ang: o * Math.PI / 2 });
    }
    return n;
  }

  // ── 6 种绘制模式 ──────────────────────────────────────────────────

  // globe：旋转球体 + 扫描线
  var it = function (s, n, e, i, t) {
    var o = n / 2, a = n / 2, r = n / 2 * 0.82;
    var h = 0.4 + 0.06 * Math.sin(e * 0.35);
    var b = $(e * 0.5, h, o, a, r);
    var d = e * (0.5 + (1.7 - 0.5) * (t.scanMul || 1));
    var c = _(n, t.rsPow || 0.6);
    var f = t.dimBase != null ? t.dimBase : 1;
    var M = [];
    var x = t.latRings || 17;
    var v = t.lonDensity || 44;
    for (var D = 0; D <= x; D++) {
      var k = -Math.PI / 2 + D / x * Math.PI;
      var m = Math.cos(k), z = Math.sin(k);
      var y = Math.max(1, Math.round(Math.abs(m) * v));
      for (var P = 0; P < y; P++) {
        var w = P / y * 2 * Math.PI;
        var R = b(m * Math.cos(w), z, m * Math.sin(w));
        var p = (R[2] + 1) / 2;
        var g = ot(w + e * 0.5, d);
        var I = Math.exp(-(g * g) / 0.18) * Math.max(0, R[2]);
        M.push({
          x: R[0], y: R[1], z: R[2],
          r: ((t.rBase || 0.6) + (t.rDepth || 1.7) * p + (t.rBoost || 1) * I) * c,
          white: (t.inkFar || 0.62) - (t.inkSpan || 0.54) * p,
          a: f + (1 - f) * Math.min(1, I)
        });
      }
    }
    q(s, M, i, t.rMin);
  };

  // rubik：魔方层旋转
  var ht = function (s, n, e, i, t) {
    var o = n / 2, a = n / 2, r = n / 2 * 0.82;
    var h = $(e * 0.55, 0.35 + 0.1 * Math.sin(e * 0.9), o, a, r);
    var b = _(n, t.rsPow || 0.6);
    var d = t.moveCount || 14;
    var c = rt(d);
    var f = st(e, d, 0.42, 1.2);
    var M = [];
    var x = t.latRings || 15;
    var v = t.lonDensity || 40;
    for (var D = 0; D <= x; D++) {
      var k = -Math.PI / 2 + D / x * Math.PI;
      var m = Math.cos(k), z = Math.sin(k);
      var y = Math.max(1, Math.round(Math.abs(m) * v));
      for (var P = 0; P < y; P++) {
        var w = P / y * 2 * Math.PI;
        var atR = at([m * Math.cos(w), z, m * Math.sin(w)], c, f);
        var hR = h(atR[0], atR[1], atR[2]);
        var A = (hR[2] + 1) / 2;
        M.push({
          x: hR[0], y: hR[1], z: hR[2],
          r: ((t.rBase || 0.6) + (t.rDepth || 1.7) * A + (atR[3] ? t.rActive || 0.3 : 0)) * b,
          white: (t.inkFar || 0.62) - (t.inkSpan || 0.54) * A - (atR[3] ? 0.14 : 0)
        });
      }
    }
    q(s, M, i, t.rMin);
  };

  // wave：波动球体
  var ct = function (s, n, e, i, t) {
    var o = n / 2, a = n / 2, r = n / 2 * 0.874;
    var h = $(e * 0.18, 0.38, o, a, 1);
    var b = _(n, t.rsPow || 0.6);
    var d = [];
    var c = t.rings || 15;
    var f = t.lonDensity || 40;
    for (var M = 0; M <= c; M++) {
      var x = -Math.PI / 2 + M / c * Math.PI;
      var v = Math.cos(x), D = Math.sin(x);
      var k = 0.62 * Math.sin(e * 2.1 - M * 0.52) + 0.38 * Math.sin(e * 1.27 + M * 0.83);
      var m = r * (0.88 + 0.105 * k);
      var z = Math.max(1, Math.round(Math.abs(v) * f));
      for (var y = 0; y < z; y++) {
        var P = y / z * 2 * Math.PI;
        var hR = h(v * Math.cos(P) * m, D * m, v * Math.sin(P) * m);
        var p = (hR[2] / r + 1) / 2;
        var u = Math.max(0, k);
        d.push({
          x: hR[0], y: hR[1], z: hR[2],
          r: ((t.rBase || 0.6) + (t.rDepth || 1.7) * p) * (1 + 0.4 * u) * b,
          white: 0.66 - 0.56 * p - 0.1 * u
        });
      }
    }
    q(s, d, i, t.rMin);
  };

  // morph：形状变形（圆→三角→方形）
  function Mt(s) { return s * s * (3 - 2 * s); }

  function V(s) {
    var n = s.length;
    var e = [];
    var i = 0;
    for (var t = 0; t < n; t++) {
      var o = s[t];
      var a = s[(t + 1) % n];
      var r = Math.hypot(a[0] - o[0], a[1] - o[1]);
      e.push(r);
      i += r;
    }
    return function (t) {
      var o = t * i;
      var a = 0;
      while (o > e[a] && a < n - 1) { o -= e[a]; a++; }
      var r = s[a];
      var h = s[(a + 1) % n];
      var b = e[a] ? Math.min(1, o / e[a]) : 0;
      return [r[0] + (h[0] - r[0]) * b, r[1] + (h[1] - r[1]) * b];
    };
  }

  var ut = function (s) {
    var n = -Math.PI / 2 + s * 2 * Math.PI;
    return [Math.cos(n) * 0.24, Math.sin(n) * 0.24];
  };
  var lt = V([[0, -0.26], [0.24, 0.16], [-0.24, 0.16]]);
  var ft = V([[0, -0.2], [0.2, -0.2], [0.2, 0.2], [-0.2, 0.2], [-0.2, -0.2]]);
  var j = [ut, lt, ft];

  function pt(s) { return Math.max(6, Math.round(34 * s)); }

  var H = 1.4, X = 0.9, G = H + X;

  var dt = function (s, n, e, i, t) {
    var o = j.length;
    var a = e % (G * o);
    var r = Math.floor(a / G);
    var h = a - r * G;
    var b = h > H ? Mt((h - H) / X) : 0;
    var d = t.spread != null ? t.spread : 1;
    var c = j[r], f = j[(r + 1) % o];
    var M = 160;
    var x = [];
    for (var l = 0; l < M; l++) {
      var p = l / M;
      var u = c(p);
      var g = f(p);
      x.push([(u[0] + (g[0] - u[0]) * b) * d, (u[1] + (g[1] - u[1]) * b) * d]);
    }
    var v = [];
    var D = 0;
    for (var l2 = 0; l2 < M; l2++) {
      var p2 = x[l2];
      var u2 = x[(l2 + 1) % M];
      var g2 = Math.hypot(u2[0] - p2[0], u2[1] - p2[1]);
      v.push(g2);
      D += g2;
    }
    var k = pt(t.iconD || 1);
    var m = (t.rDot || 0.021) * 1.35 * d;
    var z = 1 + 0.02 * Math.sin(h * 3.1);
    var y = [];
    var P = n / 2;
    var w = 0, R = 0;
    for (var l3 = 0; l3 < k; l3++) {
      var p3 = l3 / k * D;
      while (R + v[w] < p3 && w < M - 1) { R += v[w]; w++; }
      var u3 = x[w];
      var g3 = x[(w + 1) % M];
      var I = v[w] ? Math.min(1, (p3 - R) / v[w]) : 0;
      var E = (u3[0] + (g3[0] - u3[0]) * I) * z;
      var A = (u3[1] + (g3[1] - u3[1]) * I) * z;
      y.push({ x: P + E * n, y: P + A * n, z: 0, r: Math.max(0.35, m * n), white: 0.1 });
    }
    q(s, y, i, t.rMin);
  };

  // orbits：轨道粒子
  var mt = function (s, n, e, i, t) {
    var o = n / 2, a = n / 2, r = n / 2 * 0.82;
    var h = $(e * 0.12, 0.3, o, a, 1);
    var b = _(n, t.rsPow || 0.6);
    var d = [];
    var c = t.orbitN || 12;
    var f = t.ghostN || 40;
    var M = t.particles || 3;
    for (var x = 0; x < c; x++) {
      var v = T(x, 1.7);
      var D = T(x, 5.2);
      var k = T(x, 8.9);
      var m = r * (0.45 + 0.52 * v);
      var z = v * 2 * Math.PI;
      var y = Math.acos(2 * D - 1);
      var P = Math.sin(y) * Math.cos(z);
      var w = Math.cos(y);
      var R = Math.sin(y) * Math.sin(z);
      var l = -w, p = P, u = 0;
      var g = Math.max(1e-6, Math.sqrt(l * l + p * p));
      l /= g; p /= g;
      var I = w * u - R * p;
      var E = R * l - P * u;
      var A = P * p - w * l;
      var B = (0.25 + 0.55 * k) * (k > 0.5 ? 1 : -1);
      for (var L = 0; L < f; L++) {
        var S = L / f * 2 * Math.PI;
        var hR = h(l * Math.cos(S) + I * Math.sin(S) * m, p * Math.cos(S) + E * Math.sin(S) * m, u * Math.cos(S) + A * Math.sin(S) * m);
        var O = (hR[2] / m + 1) / 2;
        d.push({ x: hR[0], y: hR[1], z: hR[2], r: (t.ghostR || 0.9) * b, white: 0.72, a: (t.ghostA || 0.5) * (0.4 + 0.6 * O) });
      }
      for (var L2 = 0; L2 < M; L2++) {
        var S2 = e * B + L2 / M * 2 * Math.PI + D * 6;
        var hR2 = h(l * Math.cos(S2) + I * Math.sin(S2) * m, p * Math.cos(S2) + E * Math.sin(S2) * m, u * Math.cos(S2) + A * Math.sin(S2) * m);
        var O2 = (hR2[2] / m + 1) / 2;
        d.push({ x: hR2[0], y: hR2[1], z: hR2[2], r: ((t.partR || 1.2) + (t.partRDepth || 1.6) * O2) * b, white: 0.3 - 0.22 * O2 });
      }
    }
    q(s, d, i, t.rMin);
  };

  // ribbon：缎带曲面
  var gt = function (s, n, e, i, t) {
    var o = n / 2, a = n / 2, r = n / 2 * 0.78;
    var h = t.spin != null ? t.spin : 1;
    var b = $(e * 0.1 * h, 0.3, o, a, 1);
    var d = _(n, t.rsPow || 0.6);
    var c = [];
    var f = t.ghostN || 150;
    for (var g = 0; g < f; g++) {
      var I = et(g, f);
      var hR = b(I[0] * r, I[1] * r, I[2] * r);
      var L = (hR[2] / r + 1) / 2;
      c.push({ x: hR[0], y: hR[1], z: hR[2], r: 0.8 * d, white: 0.78, a: 0.1 + 0.22 * L });
    }
    var M = e * 0.24 * h;
    var x = 0.55 + 0.3 * Math.sin(e * 0.18) * h;
    var v = Math.cos(M), D = 0, k = Math.sin(M);
    var m = -k * Math.sin(x), z = Math.cos(x), y = v * Math.sin(x);
    var P = D * y - k * z;
    var w = k * m - v * y;
    var R = v * z - D * m;
    var l = t.lanes || 5;
    var p = t.segs || 88;
    var u = Math.max(1, Math.round(l * (t.bandMul || 1)));
    for (var g2 = 0; g2 < u; g2++) {
      var I2 = (g2 - (u - 1) / 2) * 0.075;
      var E2 = Math.abs(g2 - (u - 1) / 2) / Math.max(1, (u - 1) / 2);
      for (var A = 0; A < p; A++) {
        var B = A / p * 2 * Math.PI;
        var L2 = (0.16 * Math.sin(B * 3 - e * 1.7 + g2 * 0.22) + 0.07 * Math.sin(B * 5 + e * 1.1)) * (t.wobMul || 1);
        var S = I2 + L2;
        var C = v * Math.cos(B) + m * Math.sin(B) + P * S;
        var F = D * Math.cos(B) + z * Math.sin(B) + w * S;
        var N = k * Math.cos(B) + y * Math.sin(B) + R * S;
        var O = Math.sqrt(C * C + F * F + N * N);
        var hR2 = b(C / O * r, F / O * r, N / O * r);
        var W = (hR2[2] / r + 1) / 2;
        c.push({
          x: hR2[0], y: hR2[1], z: hR2[2],
          r: ((t.rBase || 1.1) + (t.rDepth || 1.7) * W) * (1 - 0.25 * E2) * d,
          white: 0.52 - 0.44 * W + 0.18 * E2,
          a: 0.4 + 0.6 * W
        });
      }
    }
    q(s, c, i, t.rMin);
  };

  // ── 模式注册表 ────────────────────────────────────────────────────

  var MODE_DRAWS = {
    orbits: mt, globe: it, rubik: ht,
    wave: ct, ribbon: gt, morph: dt
  };

  var STATE_TO_MODE = {
    working: 'orbits', searching: 'globe', solving: 'rubik',
    listening: 'wave', composing: 'ribbon', shaping: 'morph'
  };

  // ── 预设缩放函数 ──────────────────────────────────────────────────

  var xt = [['latRings', 'lonDensity'], ['rings', 'lonDensity'], ['lanes', 'segs']];
  var wt = ['orbitN', 'ghostN'];
  var vt = ['iconD'];
  var yt = ['rBase', 'rDepth', 'rActive', 'rDot', 'ghostR', 'partR', 'partRDepth'];

  function Pt(s, n) {
    var e = Object.assign({}, s);
    var i = new Set();
    var t = Math.sqrt(n);
    for (var _i = 0; _i < xt.length; _i++) {
      var pair = xt[_i];
      var o = pair[0], a = pair[1];
      var r = e[o], h = e[a];
      if (r != null && h != null && !i.has(o) && !i.has(a)) {
        e[o] = Math.max(2, Math.round(r * t));
        e[a] = Math.max(2, Math.round(h * t));
        i.add(o); i.add(a);
      }
    }
    for (var _i2 = 0; _i2 < wt.length; _i2++) {
      var o2 = wt[_i2];
      var a2 = e[o2];
      if (a2 != null && !i.has(o2)) {
        e[o2] = Math.max(1, Math.round(a2 * n));
      }
    }
    for (var _i3 = 0; _i3 < vt.length; _i3++) {
      var o3 = vt[_i3];
      var a3 = e[o3];
      if (a3 != null) {
        e[o3] = Math.max(0.02, a3 * n);
      }
    }
    return e;
  }

  function Dt(s, n) {
    var e = Object.assign({}, s);
    for (var _i = 0; _i < yt.length; _i++) {
      var o = yt[_i];
      var t = e[o];
      if (t != null) {
        e[o] = t * n;
      }
    }
    e.rSizeMul = (e.rSizeMul || 1) * n;
    return e;
  }

  // ── 默认预设 ──────────────────────────────────────────────────────

  var kt = {
    globe: { latRings: 17, lonDensity: 44, rBase: 0.6, rDepth: 1.7, rBoost: 1, inkFar: 0.62, inkSpan: 0.54, rsPow: 0.6, rMin: 0.3 },
    orbits: { orbitN: 12, ghostN: 40, ghostR: 0.9, ghostA: 0.5, particles: 3, partR: 1.2, partRDepth: 1.6, rsPow: 0.6, rMin: 0.3 },
    rubik: { latRings: 15, lonDensity: 40, moveCount: 14, rBase: 0.6, rDepth: 1.7, rActive: 0.3, inkFar: 0.62, inkSpan: 0.54, rsPow: 0.6, rMin: 0.3 },
    wave: { rings: 15, lonDensity: 40, rBase: 0.6, rDepth: 1.7, rsPow: 0.6, rMin: 0.3 },
    ribbon: { lanes: 5, segs: 88, ghostN: 150, rBase: 1.1, rDepth: 1.7, rsPow: 0.6, rMin: 0.3 },
    morph: { rDot: 0.021, iconD: 1, rMin: 0.25 }
  };

  var It = {
    orbits: { 64: { speed: 1.885, count: 1, size: 1 }, 20: { speed: 3.9, count: 0.238, size: 2.4 } },
    globe: { 64: { speed: 2.015, count: 0.42, size: 1.15, extra: { scanMul: 4.08, dimBase: 0.45 } }, 20: { speed: 2.665, count: 0.105, size: 1.75, extra: { scanMul: 4.335, dimBase: 0.45 } } },
    rubik: { 64: { speed: 1.82, count: 0.35, size: 1.05 }, 20: { speed: 1.95, count: 0.088, size: 1.9 } },
    wave: { 64: { speed: 4.388, count: 0.341, size: 1 }, 20: { speed: 3.998, count: 0.105, size: 1.6 } },
    ribbon: { 64: { speed: 2.34, count: 0.25, size: 0.85, extra: { spin: 0, bandMul: 3.9, wobMul: 1 } }, 20: { speed: 3.12, count: 0.051, size: 1.073, extra: { spin: 0, bandMul: 4.94, wobMul: 1 } } },
    morph: { 64: { speed: 2.405, count: 0.54, size: 0.395, extra: { spread: 1.45 } }, 20: { speed: 2.08, count: 0.53, size: 1.011, extra: { spread: 1.45 } } }
  };

  // 预设缓存
  var Q = new Map();

  function resolvePreset(state, size) {
    var key = state + '-' + size;
    var cached = Q.get(key);
    if (cached) return cached;

    var mode = STATE_TO_MODE[state];
    var modePresets = It[mode];
    // 若传入的 size 不在预设表中，回退到最近的预设尺寸
    var preset = modePresets[size];
    if (!preset) {
      var sizes = Object.keys(modePresets).map(Number).sort(function (a, b) { return a - b; });
      var nearest = sizes[0];
      var minDiff = Math.abs(size - nearest);
      for (var i = 1; i < sizes.length; i++) {
        var diff = Math.abs(size - sizes[i]);
        if (diff < minDiff) { minDiff = diff; nearest = sizes[i]; }
      }
      preset = modePresets[nearest];
    }
    var opts = Object.assign({}, kt[mode]);

    if (preset.count !== 1) {
      opts = Pt(opts, preset.count);
    }
    if (preset.size !== 1) {
      opts = Dt(opts, preset.size);
    }
    if (preset.extra) {
      opts = Object.assign(opts, preset.extra);
    }

    var result = { mode: mode, speed: preset.speed, opts: opts };
    Q.set(key, result);
    return result;
  }

  // ── 主题检测（不依赖 React）──────────────────────────────────────

  function detectDarkTheme(el) {
    var node = el;
    while (node) {
      var attr = node.getAttribute ? node.getAttribute('data-theme') : null;
      if (attr === 'dark') return true;
      if (attr === 'light') return false;
      if (node.classList && node.classList.contains('dark')) return true;
      if (node.classList && node.classList.contains('light')) return false;
      node = node.parentElement;
    }
    // 回退到系统偏好
    if (typeof matchMedia !== 'undefined') {
      return matchMedia('(prefers-color-scheme: dark)').matches;
    }
    return true;
  }

  function prefersReducedMotion() {
    if (typeof matchMedia === 'undefined') return false;
    return matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  // ── ThinkingOrb 类 ─────────────────────────────────────────────────

  var VALID_STATES = ['working', 'searching', 'solving', 'listening', 'composing', 'shaping'];
  var DEFAULT_LABELS = {
    working: 'Working\u2026',
    searching: 'Searching\u2026',
    solving: 'Solving\u2026',
    listening: 'Listening\u2026',
    composing: 'Composing\u2026',
    shaping: 'Shaping\u2026'
  };

  function ThinkingOrb(canvas, options) {
    options = options || {};
    this.canvas = canvas;
    this.state = options.state || 'working';
    this.size = options.size || 64;
    this.theme = options.theme || 'auto'; // 'auto' | 'dark' | 'light'
    this.speed = options.speed != null ? options.speed : 1;
    this.paused = !!options.paused;
    this.label = options.label || DEFAULT_LABELS[this.state];

    this._rafId = 0;
    this._running = false;
    this._visible = true;
    this._observer = null;
    this._themeObserver = null;
    this._onVisibilityChange = this._onVisibilityChange.bind(this);

    this._init();
  }

  ThinkingOrb.prototype._init = function () {
    var self = this;
    var canvas = this.canvas;
    if (!canvas) return;

    try {
      var dpr = Math.min(2, (typeof devicePixelRatio !== 'undefined' && devicePixelRatio) || 1);
      canvas.width = Math.round(this.size * dpr);
      canvas.height = Math.round(this.size * dpr);
      canvas.style.width = this.size + 'px';
      canvas.style.height = this.size + 'px';
      canvas.setAttribute('role', 'img');
      canvas.setAttribute('aria-label', this.label);

      var ctx = canvas.getContext('2d');
      if (!ctx) return;
      this._ctx = ctx;
      this._dpr = dpr;

      // 检测暗色主题
      this._isDark = this._resolveTheme();

      // 检测 reduced-motion
      this._reducedMotion = prefersReducedMotion();

      // 获取预设
      var preset = resolvePreset(this.state, this.size);
      this._mode = preset.mode;
      this._baseSpeed = preset.speed;
      this._opts = preset.opts;
      this._drawFn = MODE_DRAWS[this._mode];

    // 渲染单帧（reduced-motion 时只画一帧）
    if (this._reducedMotion) {
      this._render(0.6);
      return;
    }

    // 初始渲染
    this._render(performance.now() / 1000 * this._baseSpeed * this.speed);

    // 启动动画循环
    this._startLoop();

    // IntersectionObserver：不可见时暂停
    if (typeof IntersectionObserver !== 'undefined') {
      this._observer = new IntersectionObserver(function (entries) {
        var entry = entries[0];
        self._visible = entry.isIntersecting;
        if (self._visible && document.visibilityState !== 'hidden') {
          self._startLoop();
        } else {
          self._stopLoop();
        }
      });
      this._observer.observe(canvas);
    }

    // 页面可见性
    document.addEventListener('visibilitychange', this._onVisibilityChange);

    // 主题变化监听(auto 模式)。
    // 只观察 html[data-theme] 属性(不扫 subtree),避免流式渲染时的
    // 大量 DOM 属性变更触发回调 → CPU 空转 → 动画卡顿。
    if (this.theme === 'auto' && typeof MutationObserver !== 'undefined') {
      this._themeObserver = new MutationObserver(function () {
        var newDark = self._resolveTheme();
        if (newDark !== self._isDark) {
          self._isDark = newDark;
        }
      });
      this._themeObserver.observe(document.documentElement, {
        attributes: true,
        attributeFilter: ['data-theme'],
        subtree: false
      });
    }
    } catch (e) {
      // 初始化失败时静默退出，不影响宿主页面
    }
  };

  ThinkingOrb.prototype._resolveTheme = function () {
    if (this.theme === 'dark') return true;
    if (this.theme === 'light') return false;
    return detectDarkTheme(this.canvas);
  };

  ThinkingOrb.prototype._render = function (time) {
    var ctx = this._ctx;
    if (!ctx) return;
    var dpr = this._dpr;
    var n = this.size;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, n, n);
    try {
      this._drawFn(ctx, n, time, this._isDark, this._opts);
    } catch (e) {
      // 静默忽略渲染错误
    }
  };

  ThinkingOrb.prototype._startLoop = function () {
    if (this._running || this.paused || this._reducedMotion) return;
    this._running = true;
    var self = this;
    var animSpeed = this._baseSpeed * this.speed;
    function tick() {
      if (!self._running) return;
      self._render(performance.now() / 1000 * animSpeed);
      self._rafId = requestAnimationFrame(tick);
    }
    this._rafId = requestAnimationFrame(tick);
  };

  ThinkingOrb.prototype._stopLoop = function () {
    this._running = false;
    if (this._rafId) {
      cancelAnimationFrame(this._rafId);
      this._rafId = 0;
    }
  };

  ThinkingOrb.prototype._onVisibilityChange = function () {
    if (document.visibilityState === 'hidden') {
      this._stopLoop();
    } else if (this._visible) {
      this._startLoop();
    }
  };

  // 公开 API

  ThinkingOrb.prototype.setState = function (newState) {
    if (VALID_STATES.indexOf(newState) === -1) return;
    if (newState === this.state) return;
    this.state = newState;
    this.label = DEFAULT_LABELS[newState];
    this.canvas.setAttribute('aria-label', this.label);

    var preset = resolvePreset(newState, this.size);
    this._mode = preset.mode;
    this._baseSpeed = preset.speed;
    this._opts = preset.opts;
    this._drawFn = MODE_DRAWS[this._mode];

    // 重启动画循环
    this._stopLoop();
    if (!this._reducedMotion) {
      this._render(performance.now() / 1000 * this._baseSpeed * this.speed);
      this._startLoop();
    } else {
      this._render(0.6);
    }
  };

  ThinkingOrb.prototype.setPaused = function (paused) {
    this.paused = !!paused;
    if (this.paused) {
      this._stopLoop();
    } else if (!this._reducedMotion) {
      this._startLoop();
    }
  };

  ThinkingOrb.prototype.destroy = function () {
    this._stopLoop();
    if (this._observer) {
      this._observer.disconnect();
      this._observer = null;
    }
    if (this._themeObserver) {
      this._themeObserver.disconnect();
      this._themeObserver = null;
    }
    document.removeEventListener('visibilitychange', this._onVisibilityChange);
  };

  // ── 便捷挂载方法 ──────────────────────────────────────────────────

  // 在容器内创建 canvas + ThinkingOrb
  ThinkingOrb.mount = function (container, options) {
    options = options || {};
    var size = options.size || 64;
    var canvas = document.createElement('canvas');
    canvas.className = 'lqd-thinking-orb' + (options.className ? ' ' + options.className : '');
    canvas.style.width = size + 'px';
    canvas.style.height = size + 'px';
    canvas.style.display = 'block';
    if (options.style) {
      for (var k in options.style) {
        if (Object.prototype.hasOwnProperty.call(options.style, k)) {
          canvas.style[k] = options.style[k];
        }
      }
    }
    container.appendChild(canvas);
    try {
      var orb = new ThinkingOrb(canvas, options);
      canvas._thinkingOrb = orb;
      return { canvas: canvas, orb: orb };
    } catch (e) {
      // 构造失败时移除 canvas 并返回 null orb，不影响宿主代码
      if (canvas.parentNode) canvas.parentNode.removeChild(canvas);
      return { canvas: null, orb: null };
    }
  };

  // 从元素销毁并移除
  ThinkingOrb.unmount = function (canvas) {
    if (!canvas) return;
    if (canvas._thinkingOrb) {
      canvas._thinkingOrb.destroy();
    }
    if (canvas.parentNode) {
      canvas.parentNode.removeChild(canvas);
    }
  };

  // 暴露到全局
  window.ThinkingOrb = ThinkingOrb;
  window.ThinkingOrbUtils = {
    MODE_DRAWS: MODE_DRAWS,
    STATE_TO_MODE: STATE_TO_MODE,
    resolvePreset: resolvePreset,
    detectDarkTheme: detectDarkTheme,
    VALID_STATES: VALID_STATES,
    DEFAULT_LABELS: DEFAULT_LABELS
  };
})();
