/**
 * LQ-D — Chat Agent
 *
 * ReAct 工具循环、检索上下文、发送消息（原 handleSend 改名为 sendMessage）。
 * 依赖: LqdSettings / LqdChatLLM / LqdChatSession / LqdChatMessages / LqdChatCitations / LqdEvents / YuuRetrieval。
 */
(function () {
  'use strict';

  var BASE = (window.LQD_CHAT_BASE || '').replace(/\/+$/, '');
  var PAGEINDEX = BASE + '/pageindex';

  var R = window.YuuRetrieval;
  var tokenize = R.tokenize;
  var bm25ScorePure = R.bm25Score;
  var lexicalRerank = R.lexicalRerank;
  var rm3Expand = R.rm3Expand;
  var mmrSelect = R.mmrSelect;
  var estimateTokens = R.estimateTokens;

  var globalIndex = null;
  var nodeIndex = null;
  var indexReady = false;
  var invertedIndex = null;
  var chunkStats = null;
  var invertedReady = false;
  var docCache = {};
  var mdCache = {};
  var bm25Stats = null;

  var MAX_SECTION_CHARS = 2500;

  // ── Index loading ───────────────────────────────────────────────────────
  async function loadIndexes() {
    if (indexReady) return;
    var [gi, ni] = await Promise.all([
      fetch(PAGEINDEX + '/global-index.json').then(function (r) { return r.json(); }),
      fetch(PAGEINDEX + '/node-index.json').then(function (r) { return r.json(); })
    ]);
    globalIndex = gi;
    nodeIndex = ni;
    indexReady = true;
    loadInvertedIndex().catch(function () {});
  }

  async function loadInvertedIndex() {
    if (invertedReady) return;
    try {
      var [inv, chunks] = await Promise.all([
        fetch(PAGEINDEX + '/inverted-index.json').then(function (r) { return r.json(); }),
        fetch(PAGEINDEX + '/chunks.json').then(function (r) { return r.json(); })
      ]);
      invertedIndex = inv.postings || {};
      chunkStats = R.buildChunkStats(chunks);
      invertedReady = true;
    } catch (_) {
      invertedReady = true;
    }
  }

  function buildBM25Stats() {
    if (bm25Stats || !nodeIndex) return;
    bm25Stats = R.buildBM25Stats(nodeIndex);
  }

  function bm25Score(queryTokens, node) {
    if (!bm25Stats) return 0;  // stats 未构建(nodeIndex 未加载等),返回 0 不参与打分
    return bm25ScorePure(queryTokens, node, bm25Stats);
  }

  function search(query, topK) {
    topK = topK || 50;
    if (invertedReady && invertedIndex && chunkStats) {
      return R.searchMultiPath(query, invertedIndex, chunkStats, globalIndex, topK);
    }
    if (!nodeIndex) return [];
    buildBM25Stats();
    return R.search(query, nodeIndex, bm25Stats, topK);
  }

  // ── Token budget packing ────────────────────────────────────────────────
  function packWithContextBudget(contexts, historyTokens, systemTokens) {
    var MODEL_WINDOW = 64000;
    var OUTPUT_RESERVE = 4096;
    var retrievalBudget = Math.floor((MODEL_WINDOW - OUTPUT_RESERVE - historyTokens - systemTokens) * 0.5);
    if (retrievalBudget <= 0) return contexts.slice(0, 2);
    var used = 0;
    var packed = [];
    for (var i = 0; i < contexts.length; i++) {
      var c = contexts[i];
      var text = c.text.length > MAX_SECTION_CHARS * 2 ? c.text.slice(0, MAX_SECTION_CHARS * 2) : c.text;
      var n = estimateTokens(text);
      if (used + n > retrievalBudget) continue;
      packed.push(Object.assign({}, c, { text: text }));
      used += n;
    }
    return packed.length ? packed : contexts.slice(0, 2);
  }

  // ── MD fetch / doc tree ───────────────────────────────────────────────
  async function fetchMdLines(sourceMd) {
    if (!sourceMd) return null;
    var fullUrl = (window.LQD_CHAT_RAW_BASE || '') + sourceMd;
    if (mdCache[fullUrl]) return mdCache[fullUrl];
    try {
      var resp = await fetch(fullUrl);
      if (!resp.ok) return null;
      var text = await resp.text();
      var body = text.replace(/^---\n[\s\S]*?\n---\n/, '');
      var lines = body.split('\n');
      mdCache[fullUrl] = lines;
      return lines;
    } catch (_) {
      return null;
    }
  }

  async function fetchMdSection(sourceMd, lineNum, lineEnd) {
    var lines = await fetchMdLines(sourceMd);
    if (!lines) return '';
    var start = lineNum || 0;
    var end = lineEnd || lines.length;
    return lines.slice(start, end).join('\n').trim();
  }

  async function loadDocTree(docId) {
    if (docCache[docId] !== undefined) return;
    var doc = globalIndex && globalIndex.docs ? globalIndex.docs.find(function (d) { return d.id === docId; }) : null;
    var typeRaw = doc ? doc.type : 'papers';
    var type = typeRaw.endsWith('s') ? typeRaw : typeRaw + 's';
    try {
      var resp = await fetch(PAGEINDEX + '/' + type + '/' + docId + '.json');
      var data = await resp.json();
      var flat = [];
      function walk(nodes, crumb) {
        for (var i = 0; i < nodes.length; i++) {
          var n = nodes[i];
          var c = crumb.concat([n.title]);
          flat.push(Object.assign({}, n, { _crumb: c }));
          if (n.nodes) walk(n.nodes, c);
        }
      }
      walk(data.structure, []);
      docCache[docId] = { tree: data, flat: flat };
    } catch (_) {
      docCache[docId] = null;
    }
  }

  async function buildContextChunk(doc, nodeId, docMeta) {
    var flat = doc.flat;
    var idx = -1;
    for (var i = 0; i < flat.length; i++) {
      if (flat[i].node_id === nodeId) { idx = i; break; }
    }
    if (idx < 0) return null;
    var node = flat[idx];
    var crumb = node._crumb || [node.title];
    var text = await fetchMdSection(node.source_md, node.line_num, node.line_end);
    if (!text) text = node.summary || node.text || '';

    var parent = null;
    if (crumb.length > 1) {
      for (var j = 0; j < flat.length; j++) {
        var n = flat[j];
        if (!n._crumb || n._crumb.length !== crumb.length - 1) continue;
        var match = true;
        for (var k = 0; k < crumb.length - 1; k++) {
          if (n._crumb[k] !== crumb[k]) { match = false; break; }
        }
        if (match) { parent = n; break; }
      }
    }
    function siblingsFilter(n) {
      if (!n._crumb || n._crumb.length !== crumb.length) return false;
      if (n.node_id === node.node_id) return false;
      for (var k = 0; k < crumb.length - 1; k++) {
        if (n._crumb[k] !== crumb[k]) return false;
      }
      return true;
    }
    function childrenFilter(n) {
      if (!n._crumb || n._crumb.length !== crumb.length + 1) return false;
      for (var k = 0; k < crumb.length; k++) {
        if (n._crumb[k] !== crumb[k]) return false;
      }
      return true;
    }
    var siblings = flat.filter(siblingsFilter).slice(0, 4);
    var children = flat.filter(childrenFilter).slice(0, 4);

    return {
      sourceId: (docMeta.type || 'doc') + ':' + (docMeta.doc_id || docMeta.title || 'unknown') + ':' + nodeId,
      docType: docMeta.type || '',
      docTitle: docMeta.title || docMeta.doc_name || '',
      docAuthor: docMeta.author || '',
      nodeId: nodeId,
      title: node.title,
      breadcrumb: crumb,
      text: text,
      parentTitle: parent ? parent.title : '',
      siblingTitles: siblings.map(function (n) { return n.title; }),
      childTitles: children.map(function (n) { return n.title; })
    };
  }

  async function retrieveContext(query) {
    var hits = search(query);
    if (!hits.length) return { contexts: [], docCount: 0, thin: true };

    var origTokens = tokenize(query);
    var expandedTokens = rm3Expand(origTokens, hits);
    if (expandedTokens.length > origTokens.length) {
      // 确保 bm25Stats 已构建。search() 走倒排索引路径(searchMultiPath)时
      // 不会构建 bm25Stats,但下面 RM3 重打分要用 node 级 bm25Score,
      // stats 为 null 会报 "Cannot read properties of null (fieldAvgLen)"。
      buildBM25Stats();
      for (var i = 0; i < hits.length; i++) {
        hits[i].score = Math.round(bm25Score(expandedTokens, hits[i].node) * 100) / 100;
      }
      hits = hits.filter(function (h) { return h.score > 0; }).sort(function (a, b) { return b.score - a.score; });
    }

    hits = lexicalRerank(origTokens, query, hits);

    var uniqueDocs = [];
    var seenDocs = {};
    for (var i = 0; i < hits.length; i++) {
      var did = hits[i].node.doc_id;
      if (!seenDocs[did]) { seenDocs[did] = true; uniqueDocs.push(did); }
    }
    uniqueDocs = uniqueDocs.slice(0, 6);
    await Promise.all(uniqueDocs.map(loadDocTree));

    var candidates = [];
    var seenNodes = {};
    for (var i = 0; i < Math.min(hits.length, 12); i++) {
      var hit = hits[i];
      var doc = docCache[hit.node.doc_id];
      if (!doc) continue;
      var key = hit.node.doc_id + ':' + hit.node.node_id;
      if (seenNodes[key]) continue;
      seenNodes[key] = true;
      var ctx = await buildContextChunk(doc, hit.node.node_id, doc.tree);
      if (ctx && ctx.text) {
        ctx.url = hit.node.url || '';
        ctx.rerankScore = hit.rerankScore || 0;
        candidates.push(ctx);
      }
    }

    var thin = candidates.length < 2;
    if (thin && query.length > 4) {
      var queryTokens = tokenize(query);
      for (var t = 0; t < Math.min(queryTokens.length, 3); t++) {
        var termHits = search(queryTokens[t], 4);
        for (var h = 0; h < termHits.length; h++) {
          var termHit = termHits[h];
          if (!docCache[termHit.node.doc_id]) await loadDocTree(termHit.node.doc_id);
          var d = docCache[termHit.node.doc_id];
          if (!d) continue;
          var key2 = termHit.node.doc_id + ':' + termHit.node.node_id;
          if (seenNodes[key2]) continue;
          seenNodes[key2] = true;
          var ctx2 = await buildContextChunk(d, termHit.node.node_id, d.tree);
          if (ctx2 && ctx2.text) {
            ctx2.url = termHit.node.url || '';
            ctx2.rerankScore = termHit.rerankScore || 0.1;
            candidates.push(ctx2);
          }
          if (candidates.length >= 8) break;
        }
        if (candidates.length >= 8) break;
      }
      thin = candidates.length < 2;
    }

    var contexts = mmrSelect(candidates, 0.6, 8);
    var sourceCount = uniqueDocs.length;
    var signals = R.computeConfidenceSignals(query, hits);
    var confidence = R.classifyConfidenceMulti(signals);

    var finalContexts = contexts;
    if (confidence === 'low' && contexts.length > 2) {
      finalContexts = await llmRerank(query, contexts);
    }

    return {
      contexts: finalContexts,
      docCount: sourceCount,
      thin: thin,
      confidence: confidence,
      hits: hits.slice(0, 12)
    };
  }

  async function llmRerank(query, contexts) {
    var cfg = window.LqdSettings.resolve();
    if (!cfg.apiKey) return contexts;
    var top = contexts.slice(0, 8);
    var qToks = tokenize(query);
    var docs = top.map(function (c, i) {
      var crumb = (c.breadcrumb || []).join(' > ');
      var fullText = c.text || '';
      var lower = fullText.toLowerCase();
      var hitTok = null;
      for (var t = 0; t < qToks.length; t++) {
        if (lower.includes(qToks[t])) { hitTok = qToks[t]; break; }
      }
      var snippet = '';
      if (hitTok) {
        var idx = lower.indexOf(hitTok);
        var start = Math.max(0, idx - 150);
        snippet = (start > 0 ? '…' : '') + fullText.slice(start, start + 400);
      } else {
        snippet = fullText.slice(0, 400);
      }
      return '[' + (i + 1) + '] ' + crumb + '\n片段：' + snippet;
    }).join('\n---\n');
    var userPrompt = '查询：' + query + '\n\n候选文档：\n' + docs + '\n\n为每个文档打 0-10 分（10 最相关），格式"[编号] 分数"，每行一个。只返回评分。';
    try {
      var resp = await window.LqdChatLLM.callLLMSync(
        '你是文档相关性评估专家。根据查询评估文档相关性。',
        userPrompt
      );
      if (!resp) return contexts;
      var scores = new Map();
      var lines = resp.split('\n');
      for (var i = 0; i < lines.length; i++) {
        var m = lines[i].match(/\[(\d+)\]\s*[:：]?\s*(\d+(?:\.\d+)?)/);
        if (m) scores.set(parseInt(m[1], 10) - 1, parseFloat(m[2]));
      }
      if (!scores.size) return contexts;
      var scored = top.map(function (c, i) { return { c: c, score: scores.has(i) ? scores.get(i) : 0 }; });
      scored.sort(function (a, b) { return b.score - a.score; });
      return scored.map(function (s) { return s.c; }).concat(contexts.slice(8));
    } catch (_) {
      return contexts;
    }
  }

  // ── System prompts ──────────────────────────────────────────────────────
  function truncateAtBoundary(text, maxChars) {
    if (!text || text.length <= maxChars) return text;
    var paragraphs = text.split(/\n+/).filter(function (p) { return p.trim(); });
    var out = '';
    for (var i = 0; i < paragraphs.length; i++) {
      var p = paragraphs[i];
      if ((out + '\n' + p).length > maxChars) break;
      out += (out ? '\n' : '') + p;
    }
    if (!out) {
      var sentences = text.split(/(?<=[。！？；!?])/g).filter(function (s) { return s.trim(); });
      for (var i = 0; i < sentences.length; i++) {
        if ((out + sentences[i]).length > maxChars) break;
        out += sentences[i];
      }
    }
    if (!out) out = text.slice(0, maxChars);
    return out + '\n\n…[已按语义边界截断，可追问获取完整内容]…';
  }

  function buildSystemPrompt(contexts, thin, confidence) {
    var docNames = [];
    var seen = {};
    for (var i = 0; i < contexts.length; i++) {
      var n = contexts[i].docTitle;
      if (!seen[n]) { seen[n] = true; docNames.push(n); }
    }
    var docToc = docNames.map(function (name) {
      var dc = contexts.filter(function (c) { return c.docTitle === name; });
      var meta = [dc[0].docAuthor, dc[0].docType].filter(Boolean).join(' · ');
      return '- **' + name + '**' + (meta ? ' (' + meta + ')' : '') + ' — ' + dc.length + ' 个相关段落';
    });
    var blocks = [];
    var seenHash = {};
    var n = 0;
    for (var i = 0; i < contexts.length; i++) {
      var c = contexts[i];
      var hash = c.text.slice(0, 80);
      if (seenHash[hash]) continue;
      seenHash[hash] = true;
      n++;
      var crumb = c.breadcrumb.join(' > ');
      var text = truncateAtBoundary(c.text, MAX_SECTION_CHARS);
      var block = '### [' + n + '] ' + crumb + '\n*来源: ' + c.docTitle + ' | source_id: ' + c.sourceId + '*\n';
      var nearby = [];
      if (c.parentTitle && c.breadcrumb.length > 1) nearby.push('上级: ' + c.parentTitle);
      if (c.siblingTitles.length) nearby.push('同级: ' + c.siblingTitles.join(' / '));
      if (c.childTitles.length) nearby.push('子节: ' + c.childTitles.join(' / '));
      if (nearby.length) block += '*' + nearby.join('  |  ') + '*\n';
      block += '\n' + text;
      blocks.push(block);
    }
    var thinNotice = '';
    if (confidence === 'low' || thin) {
      thinNotice = '\n> **检索置信度较低**：当前检索相关性不足。请优先说明依据不足，只基于最相关来源简短回答，**不要扩展、不要猜测、不要编造**。\n';
    } else if (confidence === 'medium') {
      thinNotice = '\n> **检索置信度中等**：依据基本充足，但请只基于 Context 回答，对证据不足的部分明确标注。\n';
    } else {
      thinNotice = '\n> **检索置信度高**：可基于 Context 充分回答。\n';
    }
    return '你是 **LQ-D** 的知识助手，基于个人数字图书馆内容的 RAG 问答系统。\n\n' +
      '## 检索概览\n' + docToc.join('\n') + '\n' + thinNotice + '\n' +
      '## 推理步骤\n' +
      '1. **扫描结构**：先浏览下方各段落标题和层级关系，判断哪些 [N] 与问题最相关\n' +
      '2. **精读内容**：重点阅读匹配度高的段落。被截断的段落可追问\n' +
      '3. **交叉验证**：如果多个来源有不同观点，指出差异\n' +
      '4. **组织回答**：先给直接答案，再展开解释。用 [N] 标注每个论断的来源\n' +
      '5. **诚实评估**：Context 不足时明确说"当前图书馆中没有足够依据"\n\n' +
      '## Context（按相关度排序）\n\n' + blocks.join('\n\n---\n\n') + '\n\n' +
      '## 回答规则\n' +
      '- 只能根据 Context 回答，不要使用外部知识\n' +
      '- 每个关键论断标注来源编号\n' +
      '- 回答末尾列出参考来源：参考来源：\n[1] 《文档名》 > 章节 > 节名\n' +
      '- 回答使用中文，专业术语保留原文。公式用 KaTeX：行内 $...$（仅短符号如 $\\alpha$、$\\hbar$），复杂/多行公式用行间 $$...$$。**禁止把长公式塞进行内 $...$**';
  }

  // ── Agent tools ─────────────────────────────────────────────────────────
  var LIBRARY_TOOLS = [
    {
      type: 'function',
      function: {
        name: 'search_library',
        description: '在个人数字图书馆中检索书籍、论文、笔记内容。当需要查找事实、概念、章节内容、文献时使用。可用不同的关键词多次检索以覆盖不同角度。返回结果含 source_id（格式 doc_type:doc_id:node_id），供 get_section 深挖。',
        parameters: {
          type: 'object',
          properties: {
            query: { type: 'string', description: '检索关键词或问题，用文档中可能出现的原词效果最好' }
          },
          required: ['query']
        }
      }
    },
    {
      type: 'function',
      function: {
        name: 'get_section',
        description: '取指定文档某章节的完整内容（不截断）。先用 search_library 找到 source_id，解析出 doc_id 和 node_id，再用本工具取全文。用于检索结果被截断、需要完整推导/证明/上下文时。',
        parameters: {
          type: 'object',
          properties: {
            doc_id: { type: 'string', description: '文档 ID（source_id 第二段，如 linear-response-theory-foundations）' },
            node_id: { type: 'string', description: '节点 ID（source_id 第三段，如 0003）' }
          },
          required: ['doc_id', 'node_id']
        }
      }
    },
    {
      type: 'function',
      function: {
        name: 'rewrite_query',
        description: '分析用户问题并生成更好的检索查询建议。当直接搜索结果不佳、查询模糊/口语化/含代词时使用。返回改写后的查询词，你选择合适的去调 search_library。',
        parameters: {
          type: 'object',
          properties: {
            query: { type: 'string', description: '用户原始问题' },
            strategy: {
              type: 'string',
              enum: ['rewrite', 'decompose', 'step_back'],
              description: 'rewrite=改写更具体；decompose=拆成子问题；step_back=生成更宽泛的背景查询'
            }
          },
          required: ['query']
        }
      }
    }
  ];

  async function retrieveContextAsText(query, budgetCtx, sourceCtx) {
    var result = await retrieveContext(query);
    var confidence = result.confidence;
    var contexts = result.contexts;
    if (!contexts.length) return { text: '未找到相关内容。', contexts: [], confidence: confidence };
    if (budgetCtx) {
      contexts = packWithContextBudget(contexts, budgetCtx.historyTokens || 0, budgetCtx.systemTokens || 0);
    }
    var blocks = contexts.map(function (c) {
      var num;
      if (sourceCtx && sourceCtx.registry) {
        if (sourceCtx.registry.has(c.sourceId)) {
          num = sourceCtx.registry.get(c.sourceId);
        } else {
          sourceCtx.counter[0] += 1;
          num = sourceCtx.counter[0];
          sourceCtx.registry.set(c.sourceId, num);
        }
        c.displayNum = num;
      } else {
        num = contexts.indexOf(c) + 1;
      }
      return '### [' + num + '] ' + c.breadcrumb.join(' > ') + '\n*来源: ' + c.docTitle + ' | source_id: ' + c.sourceId + '*\n\n' + truncateAtBoundary(c.text, MAX_SECTION_CHARS);
    });
    var text = blocks.join('\n\n---\n\n') + '\n\n检索置信度: ' + confidence;
    return { text: text, contexts: contexts, confidence: confidence };
  }

  async function executeTool(name, args, budgetCtx, sourceCtx) {
    if (name === 'search_library') {
      // 防御:模型返回的 tool arguments 可能缺 query 字段(空 {} 或格式异常),
      // 此时 args.query 为 undefined。若放任传给 retrieveContext → lexicalRerank,
      // 会抛 "Cannot read properties of undefined (reading 'match')" 导致整条 ReAct 崩溃。
      // 上游兜底:参数缺失时返回提示,让模型在下一轮换关键词重试。
      var searchQuery = args && args.query;
      if (!searchQuery || typeof searchQuery !== 'string') {
        return { text: '检索参数 query 缺失或非字符串,请换关键词重新调用 search_library。', __contexts: [] };
      }
      var r = await retrieveContextAsText(searchQuery, budgetCtx, sourceCtx);
      r.__contexts = r.contexts;
      return r;
    }
    if (name === 'get_section') {
      var docId = args.doc_id || '';
      var nodeId = args.node_id || '';
      await loadDocTree(docId);
      var doc = docCache[docId];
      if (!doc) return { text: '文档 ' + docId + ' 未找到或加载失败' };
      var node = null;
      for (var i = 0; i < doc.flat.length; i++) {
        if (doc.flat[i].node_id === nodeId) { node = doc.flat[i]; break; }
      }
      if (!node) {
        var available = doc.flat.slice(0, 5).map(function (n) {
          return n.node_id + ' ' + n.title.slice(0, 20);
        }).join('; ');
        return { text: '节点 ' + nodeId + ' 未找到。可用节点：' + available + '...' };
      }
      var text = await fetchMdSection(node.source_md, node.line_num, node.line_end);
      if (!text) text = node.summary || '(正文获取失败，仅显示摘要：' + (node.summary || '无') + ')';
      var breadcrumb = (node._crumb || [node.title]).join(' > ');
      var docTitle = doc.tree.title || docId;
      return {
        text: '### ' + breadcrumb + '\n*来源: ' + docTitle + '*\n\n' + text,
        __contexts: [{
          sourceId: docId + ':' + nodeId,
          text: text,
          docTitle: docTitle,
          breadcrumb: node._crumb || [node.title],
          url: ''
        }]
      };
    }
    if (name === 'rewrite_query') {
      var strategy = args.strategy || 'rewrite';
      var promptTemplates = {
        rewrite: '把以下查询改写得更具体、更适合文档检索。包含文档中可能出现的专业术语和同义词。只返回改写后的查询，不要解释。\n\n原始查询：',
        decompose: '把以下复合问题分解成 2-3 个可独立检索的子问题，每行一个，不要编号。只返回子问题，不要解释。\n\n原始查询：',
        step_back: '为以下具体查询生成一个更宽泛的背景查询，用于检索基础概念和上下文。只返回背景查询，不要解释。\n\n原始查询：'
      };
      var userPrompt = (promptTemplates[strategy] || promptTemplates.rewrite) + args.query;
      var rewritten = await window.LqdChatLLM.callLLMSync(
        '你是检索查询优化专家。根据策略改写用户查询，使其更适合在专业文档库中检索。',
        userPrompt
      );
      if (!rewritten) {
        return { text: '改写失败（可能未配置 API Key），请直接换关键词调 search_library。' };
      }
      return { text: '改写建议（' + strategy + '）：\n' + rewritten + '\n\n请用以上查询词调 search_library 检索。' };
    }
    return { text: '未知工具: ' + name };
  }

  function buildLibraryTOC() {
    if (!globalIndex || !globalIndex.docs || !globalIndex.docs.length) {
      return { text: '(索引未加载)', docCount: 0 };
    }
    var groups = { book: '书籍', paper: '论文', note: '笔记' };
    var counts = { book: 0, paper: 0, note: 0 };
    var lines = { book: [], paper: [], note: [] };
    for (var i = 0; i < globalIndex.docs.length; i++) {
      var doc = globalIndex.docs[i];
      var t = doc.type || 'note';
      if (!(t in counts)) continue;
      counts[t]++;
      var author = doc.author ? ' ' + doc.author.split(/[,，]/)[0] : '';
      var tags = doc.tags && doc.tags.length ? ' [' + doc.tags.slice(0, 3).join(', ') + ']' : '';
      var desc = (doc.description || '').slice(0, 50);
      lines[t].push('- 《' + doc.title + '》' + author + ' — ' + desc + tags);
    }
    var text = '';
    for (var key in groups) {
      if (counts[key]) text += '### ' + groups[key] + '（' + counts[key] + ' 篇）\n' + lines[key].join('\n') + '\n\n';
    }
    return { text: text.trim(), docCount: globalIndex.docs.length };
  }

  function buildAgentSystemPrompt() {
    var toc = buildLibraryTOC();
    return '你是 **LQ-D** 的知识助手，基于个人数字图书馆的 RAG 问答系统。\n\n' +
      '## 图书馆目录（' + toc.docCount + ' 篇文档，检索前先浏览相关领域）\n' + toc.text + '\n\n' +
      '## 工作方式\n' +
      '- 你有三个工具：search_library（检索）、get_section（取完整章节）、rewrite_query（改写查询）\n' +
      '- 回答用户问题前，**必须先调用 search_library 检索**，不要凭记忆回答\n' +
      '- 只能基于检索到的内容回答，不要使用外部知识编造\n\n' +
      '## 检索策略（重要）\n' +
      '第一次用用户原话检索。如果结果不足，换策略重试：\n' +
      '- **查询重写**：换成文档里可能出现的专业术语重搜（如"那个相变"→"量子相变 Rabi 模型"）\n' +
      '- **子查询分解**：复合问题拆成子问题分别检索（如"Berry phase 和线性响应的关系"→分两搜）\n' +
      '- **步退查询**：太具体搜不到时，先用更宽泛的概念搜背景知识\n' +
      '- 可调 rewrite_query 工具生成改写建议，也可直接换关键词调 search_library\n' +
      '- 找到相关章节但内容被截断时，用 get_section 取完整内容（需 doc_id 和 node_id）\n\n' +
      '## 回答规则\n' +
      '- 每个关键论断标注来源编号 [N]，对应检索结果中的 [N]\n' +
      '- 回答末尾列出参考来源，格式：\n[1] 文档名 > 章节 > 节名\n' +
      '- 检索结果不足时明确说明"当前图书馆中没有足够依据"，不要硬答\n' +
      '- 回答使用中文，专业术语保留原文。公式用 LaTeX：行内 \\(...\\)，行间 \\[...\\]';
  }

  // ── Send / ReAct loop ──────────────────────────────────────────────────
  function debugEnabled() {
    return localStorage.getItem('lqd_chat_debug') === '1';
  }

  function thinkingEnabled() {
    return localStorage.getItem('lqd_chat_thinking') !== '0';
  }

  async function sendMessage(query, refs) {
    var messagesEl = refs.messagesEl;
    var composerInput = refs.composerInput;
    var sendBtn = refs.sendBtn;

    await window.LqdSettings.load();
    var apiKey = await window.LqdSettings.fetchApiKey();
    if (window.LqdSettings._cache) window.LqdSettings._cache.api_key = apiKey;
    var cfg = window.LqdSettings.resolve();

    if (!cfg.apiKey) {
      window.LqdChatMessages.appendMessageBubble('assistant', '请先前往「配置」页设置 LLM API Key。', messagesEl);
      return;
    }

    window.LqdChatMessages.hideEmpty(refs.emptyEl, messagesEl);
    window.LqdChatMessages.appendMessageBubble('user', query, messagesEl);
    window.LqdChatSession.appendToCurrent('user', query);

    try {
      await loadIndexes();
    } catch (e) {
      window.LqdChatMessages.appendMessageBubble('assistant', '索引加载失败：' + e.message, messagesEl);
      return;
    }

    var contentEl = window.LqdChatMessages.appendMessageBubble('assistant', '<em>准备中……</em>', messagesEl);
    window.LqdChatMessages.setBusy(true, sendBtn, composerInput);

    var thinkingOn = thinkingEnabled();
    var debugOn = debugEnabled();
    var systemPrompt = buildAgentSystemPrompt();
    var messages = window.LqdChatSession.loadCurrent().slice(-6);
    var MAX_LOOPS = 4;

    var budgetCtx = {
      historyTokens: messages.reduce(function (s, m) { return s + estimateTokens(m.content || ''); }, 0),
      systemTokens: estimateTokens(systemPrompt)
    };

    var finalText = '';
    var finalThinking = '';
    var didSearch = false;
    var allContexts = [];
    var seenSourceIds = new Set();
    var toolTrail = [];
    var sourceRegistry = new Map();
    var nextSourceNum = [0];

    var emitContext = function (contexts) {
      if (window.LqdEvents) {
        window.LqdEvents.emit('chat:context', { query: query, contexts: contexts });
      }
    };

    try {
      var agentStart = Date.now();
      for (var loop = 0; loop < MAX_LOOPS; loop++) {
        if (Date.now() - agentStart > 120000) {
          contentEl.innerHTML = window.LqdChatMessages.renderThinkingAndText(
            finalThinking, '<em>检索超时，正在用已获取的内容生成回答……</em>', toolTrail
          );
          break;
        }
        var roundText = '';
        var roundThinking = '';
        var toolCalls = null;
        var stopReason = null;

        var stream = window.LqdChatLLM.streamText({
          provider: cfg.provider,
          model: cfg.model,
          baseUrl: cfg.baseUrl,
          apiKey: cfg.apiKey,
          protocol: cfg.protocol,
          pathMode: cfg.pathMode,
          system: systemPrompt,
          messages: messages,
          tools: LIBRARY_TOOLS,
          thinking: thinkingOn,
          maxTokens: thinkingOn ? 8192 : 4096
        });

        var _dbg = window.LQD_DEBUG_SSE;
        var _recv = _dbg ? { thinking: 0, text: 0, tool_calls: 0, stop: 0 } : null;
        for await (var chunk of stream) {
          if (_dbg) {
            if (chunk.type === 'thinking') _recv.thinking++;
            else if (chunk.type === 'text') _recv.text++;
            else if (chunk.type === 'tool_calls') _recv.tool_calls++;
            else if (chunk.type === 'stop') _recv.stop++;
          }
          if (chunk.type === 'thinking') {
            roundThinking += chunk.text;
            finalThinking += chunk.text;
            contentEl.innerHTML = window.LqdChatMessages.renderThinkingAndText(finalThinking, roundText, toolTrail);
          } else if (chunk.type === 'text') {
            roundText += chunk.text;
            finalText = roundText;
            contentEl.innerHTML = window.LqdChatMessages.renderThinkingAndText(finalThinking, finalText, toolTrail);
            window.LqdChatMessages.reRenderKatex(contentEl);
          } else if (chunk.type === 'tool_calls') {
            toolCalls = chunk.calls;
          } else if (chunk.type === 'stop') {
            stopReason = chunk.reason;
          }
          messagesEl.scrollTop = messagesEl.scrollHeight;
        }
        if (_dbg) console.log('[AGENT] loop', loop, 'recv', _recv, 'roundThinking.len', roundThinking.length, 'finalThinking.len', finalThinking.length, 'toolCalls', toolCalls);

        // 没有工具调用 → 本轮是最终回答，退出循环
        // 注意:不能依赖 stopReason 值——Anthropic 协议 stop_reason 是 "tool_use"
        // (且 tool_use 时 readSSE 只 yield tool_calls 不 yield stop,stopReason 保持 null),
        // OpenAI 协议 finish_reason 是 "tool_calls"。两者值不同,统一用 toolCalls 是否存在判断。
        if (!toolCalls || !toolCalls.length) break;

        messages.push({
          role: 'assistant',
          content: roundText || null,
          tool_calls: toolCalls.map(function (tc) {
            return { id: tc.id, type: 'function', function: { name: tc.name, arguments: tc.arguments } };
          })
        });

        for (var i = 0; i < toolCalls.length; i++) {
          var tc = toolCalls[i];
          var args = {};
          try { args = JSON.parse(tc.arguments || '{}'); } catch (_) { /* ignore */ }
          toolTrail.push(
            '<div class="lqd-tool-step">🔍 检索: <code>' + window.LqdChatCitations.escHtml(args.query || tc.arguments) + '</code></div>'
          );
          contentEl.innerHTML = window.LqdChatMessages.renderThinkingAndText(finalThinking, finalText, toolTrail);
          if (tc.name === 'search_library') didSearch = true;

          var toolResult = await executeTool(tc.name, args, budgetCtx, { registry: sourceRegistry, counter: nextSourceNum });
          if (toolResult.__contexts) {
            for (var j = 0; j < toolResult.__contexts.length; j++) {
              var c = toolResult.__contexts[j];
              if (!seenSourceIds.has(c.sourceId)) {
                allContexts.push(c);
                seenSourceIds.add(c.sourceId);
              }
            }
          }
          messages.push({
            role: 'tool',
            tool_call_id: tc.id,
            content: toolResult.text || JSON.stringify(toolResult)
          });
        }
        contentEl.innerHTML = window.LqdChatMessages.renderThinkingAndText(
          finalThinking, '<em>已检索，正在综合回答……</em>', toolTrail
        );
        finalText = '';
      }

      if (!didSearch) {
        var forced = await retrieveContextAsText(query, budgetCtx, { registry: sourceRegistry, counter: nextSourceNum });
        if (forced.__contexts) {
          forced.__contexts = forced.contexts;
          for (var j = 0; j < forced.__contexts.length; j++) {
            var c2 = forced.__contexts[j];
            if (!seenSourceIds.has(c2.sourceId)) {
              allContexts.push(c2);
              seenSourceIds.add(c2.sourceId);
            }
          }
        }
        toolTrail.push('<div class="lqd-tool-step">🔍 检索(强制): <code>' + window.LqdChatCitations.escHtml(query) + '</code></div>');
      }

      if (!finalText.trim() && allContexts.length) {
        contentEl.innerHTML = window.LqdChatMessages.renderThinkingAndText(
          finalThinking, '<em>检索完成，正在综合回答……</em>', toolTrail
        );
        var ctxBlocks = allContexts.map(function (c) {
          var num = c.displayNum || allContexts.indexOf(c) + 1;
          return '### [' + num + '] ' + c.breadcrumb.join(' > ') + '\n*来源: ' + c.docTitle + '*\n\n' + truncateAtBoundary(c.text, MAX_SECTION_CHARS);
        }).join('\n\n---\n\n');
        var summaryMessages = [
          { role: 'user', content: query },
          { role: 'user', content: '基于以下检索到的图书馆内容，回答上面的问题。\n\n## 回答规则\n- 每个关键论断用 [N] 标注来源编号（对应下方的 [N]，**只写编号，不要自己写 url 或链接**）\n- 回答末尾列出参考来源，格式：[N] 文档名 > 章节\n- 只基于 Context 回答，不要编造\n- 回答使用中文，公式用 KaTeX：行内 $...$（仅短符号），复杂/多行公式用行间 $$...$$。禁止长公式塞进行内\n\n## Context（按相关度排序）\n\n' + ctxBlocks }
        ];
        var summaryText = '';
        var summaryTimeout = setTimeout(function () {
          if (!finalText.trim()) {
            finalText = '已检索到相关内容，但生成回答超时。请尝试换个问法，或直接在书架中浏览相关章节。';
            contentEl.innerHTML = window.LqdChatMessages.renderThinkingAndText(finalThinking, finalText, toolTrail);
            window.LqdChatMessages.reRenderKatex(contentEl);
          }
        }, 30000);
        try {
          var summaryStream = window.LqdChatLLM.streamText({
            provider: cfg.provider,
            model: cfg.model,
            baseUrl: cfg.baseUrl,
            apiKey: cfg.apiKey,
            protocol: cfg.protocol,
            pathMode: cfg.pathMode,
            system: systemPrompt,
            messages: summaryMessages,
            tools: undefined,
            thinking: false,
            maxTokens: 4096
          });
          for await (var chunk2 of summaryStream) {
            if (chunk2.type === 'text') {
              summaryText += chunk2.text;
              finalText = summaryText;
              contentEl.innerHTML = window.LqdChatMessages.renderThinkingAndText(finalThinking, finalText, toolTrail);
              window.LqdChatMessages.reRenderKatex(contentEl);
            }
          }
        } catch (_) { /* ignore */ }
        clearTimeout(summaryTimeout);
      }

      if (!finalText.trim()) {
        if (allContexts.length) {
          finalText = '已检索到相关内容，但未能生成回答。请尝试换个问法重试。';
        } else {
          finalText = '未找到相关内容。可以在书架中浏览，或换关键词重试。';
        }
        contentEl.innerHTML = window.LqdChatMessages.renderThinkingAndText(finalThinking, finalText, toolTrail);
        window.LqdChatMessages.reRenderKatex(contentEl);
      }

      var citationsHtml = '';
      if (allContexts.length) {
        var refMap = {};
        for (var i = 0; i < allContexts.length; i++) {
          var c3 = allContexts[i];
          var num = c3.displayNum || i + 1;
          if (c3.url) refMap[num] = { title: c3.docTitle, breadcrumb: c3.breadcrumb, url: c3.url };
        }
        if (Object.keys(refMap).length > 0) {
          finalText = window.LqdChatCitations.injectReferenceLinks(finalText, refMap);
        }
        citationsHtml = window.LqdChatCitations.renderCitations(allContexts);
      }

      if (debugOn && allContexts.length) {
        var debugHits = allContexts.slice(0, 12).map(function (c, i) {
          return {
            node: { doc_id: c.docTitle, node_id: c.nodeId, breadcrumb: c.breadcrumb },
            score: '?'
          };
        });
        contentEl.innerHTML =
          window.LqdChatMessages.renderThinkingAndText(finalThinking, finalText, toolTrail) +
          citationsHtml +
          window.LqdChatCitations.renderDebugCard(debugHits, allContexts.slice(0, 8), systemPrompt, 'agent');
      } else {
        contentEl.innerHTML = window.LqdChatMessages.renderThinkingAndText(finalThinking, finalText, toolTrail) + citationsHtml;
      }
      window.LqdChatMessages.reRenderKatex(contentEl);

      window.LqdChatSession.appendToCurrent('assistant', finalText);
      emitContext(allContexts);
      if (window.LqdEvents) {
        window.LqdEvents.emit('chat:message', { role: 'assistant', content: finalText });
      }
    } catch (e) {
      // 打印完整堆栈到 console(F12 可见)——此前只显示 e.message,
      // "Cannot read properties of undefined" 类错误无法定位源头,每次修都靠猜。
      if (window.LqdErrors) window.LqdErrors.report(e, 'sendMessage');
      var stackHtml = '';
      if (e && e.stack) {
        stackHtml = '<details class="lqd-error-stack"><summary>错误堆栈(排查用)</summary><pre>' +
          window.LqdChatCitations.escHtml(e.stack) + '</pre></details>';
      }
      contentEl.innerHTML += '<br><span style="color:#dc2626">错误: ' +
        window.LqdChatCitations.escHtml(e && e.message ? e.message : String(e)) + '</span>' + stackHtml;
    }
    // 流式结束,移除 busy 标记(工作流 G)
    var bubble = contentEl.parentNode;
    if (bubble) bubble.removeAttribute('aria-busy');
    window.LqdChatMessages.setBusy(false, sendBtn, composerInput);
  }

  window.LqdChatAgent = {
    loadIndexes: loadIndexes,
    retrieveContext: retrieveContext,
    retrieveContextAsText: retrieveContextAsText,
    executeTool: executeTool,
    LIBRARY_TOOLS: LIBRARY_TOOLS,
    buildSystemPrompt: buildSystemPrompt,
    buildAgentSystemPrompt: buildAgentSystemPrompt,
    sendMessage: sendMessage,
    debugEnabled: debugEnabled,
    thinkingEnabled: thinkingEnabled
  };
})();
