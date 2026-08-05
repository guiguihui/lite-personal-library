/**
 * LQ-D — Chat Agent
 *
 * ReAct 工具循环、检索上下文、发送消息（原 handleSend 改名为 sendMessage）。
 * 依赖: LqdSettings / LqdChatLLM / LqdChatSession / LqdChatMessages / LqdChatCitations / LqdEvents / YuuRetrieval。
 *
 * P3 检索收敛:search_library 走后端 GET /api/search(V3 优先,legacy 回退),
 * 浏览器不再下载 inverted-index.json / chunks.json(那 ~26MB)。
 * global-index.json 仍加载(小)仅供 buildLibraryTOC 用;node-index 亦不再需要。
 */
(function () {
  'use strict';

  var BASE = (window.LQD_CHAT_BASE || '').replace(/\/+$/, '');
  var PAGEINDEX = BASE + '/pageindex';

  var R = window.YuuRetrieval || {};
  var tokenize = R.tokenize || function (s) { return String(s || '').match(/[a-z0-9]+/g) || []; };
  var mmrSelect = R.mmrSelect || function (c) { return c; };
  var estimateTokens = R.estimateTokens || function (t) {
    t = String(t || '');
    var cjk = (t.match(/[一-鿿]/g) || []).length;
    return Math.max(1, Math.ceil(cjk * 0.7 + (t.length - cjk) / 4));
  };

  var globalIndex = null;
  var indexReady = false;
  var docCache = {};
  var mdCache = {};
  // get_section 的 V3 快路径缓存:sourceId(和 docId:nodeId 键)→ context(含 sourceMd/lineNum/lineEnd)
  var sectionCache = {};
  // P1-7: 会话级"上次检索上下文",用于追问感知(代词/范围延续)。
  // 评审修复:改为按 tabId 隔离,避免多会话标签间互相污染。
  var lastContextsByTab = {};

  var MAX_SECTION_CHARS = 2500;

  // ── Index loading(轻量:仅 global-index 供 buildLibraryTOC)───────────────
  async function loadIndexes() {
    if (indexReady) return;
    // 不再下载 inverted-index.json + chunks.json(26MB)。global-index.json 体积小,
    // 仅用于 buildLibraryTOC 展示文档目录;加载失败不影响聊天(检索走后端 API)。
    try {
      var resp = await fetch(PAGEINDEX + '/global-index.json');
      if (resp.ok) globalIndex = await resp.json();
    } catch (e) {
      if (window.console && window.console.warn) {
        window.console.warn('[Agent] global-index load failed, TOC degraded (search unaffected)', e);
      }
      globalIndex = null;
    }
    indexReady = true;
  }

  // ── 检索来源范围(Codex 式上下文范围) ──
  // 会话级设置:books/papers/notes/local 任一组合;空=全部。
  // 存 localStorage(按 active tab 共享),改变时发 chat:scope:changed 事件。
  var SCOPE_KEY = 'lqd_chat_scope';
  function getSearchScope() {
    try {
      var raw = localStorage.getItem(SCOPE_KEY);
      if (!raw) return { books: true, papers: true, notes: true, local: true };
      var obj = JSON.parse(raw);
      return Object.assign({ books: true, papers: true, notes: true, local: true }, obj);
    } catch (_) {
      return { books: true, papers: true, notes: true, local: true };
    }
  }
  function setSearchScope(scope) {
    try { localStorage.setItem(SCOPE_KEY, JSON.stringify(scope)); } catch (_) {}
    if (window.LqdEvents) window.LqdEvents.emit('chat:scope:changed', { scope: scope });
    return scope;
  }
  function scopeDocTypes(scope) {
    var out = [];
    if (scope.books) out.push('books');
    if (scope.papers) out.push('papers');
    if (scope.notes) out.push('notes');
    return out;
  }

  // ── 后端检索(V3 优先 / legacy 回退,双轨都由 /api/search 封装)─────────────
  async function searchLibrary(query, topK) {
    var scope = getSearchScope();
    var types = scopeDocTypes(scope);
    // 评审修复:三个库来源全部关闭时,直接返回空(不搜索整个库)
    if (!types.length) {
      return [];
    }
    var url = BASE + '/api/search?q=' + encodeURIComponent(query) + '&limit=' + (topK || 12);
    // 来源范围过滤:books/papers/notes(本机文件检索独立走 /api/filesearch)
    url += '&doc_types=' + encodeURIComponent(types.join(','));
    var response = await fetch(url);
    if (!response.ok) throw new Error('HTTP ' + response.status);
    var payload = await response.json();
    return payload && Array.isArray(payload.results) ? payload.results : [];
  }

  // 兼容接口:search_library 不再在浏览器内跑 BM25/RM3/MMR,直接走后端
  async function search(query, topK) {
    return searchLibrary(query, topK || 12);
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
      // CRLF 兼容:Windows 下 md 可能以 \r\n 保存,硬编码 \n 会导致 front matter
      // 剥离失败,行号偏移。用 \r?\n 兼容两种换行。
      var body = text.replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n/, '');
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
    // V3 的 /api/search 用复数 doc_type(books/papers/notes);legacy doc-tree 目录亦为复数
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

  // 把 /api/search 的单条 result 转成内部 context 结构(与旧 buildContextChunk 输出对齐)。
  // sourceId 保持 doc_type:doc_id:node_id,供 get_section / 引用卡片解析。
  function contextFromResult(result) {
    var crumb = result.breadcrumb
      ? String(result.breadcrumb).split(' > ')
      : [result.title || result.slug || '未命名章节'];
    var sourceId = (result.doc_type || 'doc') + ':' + result.slug + ':' + result.node_id;
    var context = {
      sourceId: sourceId,
      docType: result.doc_type || '',
      docId: result.slug || '',
      docTitle: crumb[0] || result.slug || '',
      docAuthor: '',
      nodeId: result.node_id || '',
      title: result.title || '',
      breadcrumb: crumb,
      text: result.text || '',
      sourceMd: result.source_md || '',
      lineNum: result.line_num,
      lineEnd: result.line_end,
      generation: result.generation || '',
      viewId: result.view_id || '',
      score: result.score || 0,
      url: ''
    };
    // 缓存给 get_section:按 sourceId 与 (docId,nodeId) 双键
    sectionCache[sourceId] = context;
    sectionCache[(result.slug || '') + ':' + (result.node_id || '')] = context;
    return context;
  }

  // 检索:走后端 /api/search,不再浏览器内跑 YuuRetrieval 多路检索。
  // 保留 mmrSelect 去冗余(后端顺序 + 同义相似度去重),其余精排由后端完成。
  // P1-7 追问感知:若新查询含代词("它/这个/那/他们/该/其")或很短,自动拼接
  // 上次检索的文档名,让后端能命中延续的话题。评审修复:按 tabId 隔离。
  function buildFollowupQuery(query, tabId) {
    var q = String(query || '').trim();
    var lastContexts = lastContextsByTab[tabId] || [];
    var pronoun = /(它|它们|他|他们|她|这个|那个|这些|那些|该|其|上面|上述|这)/.test(q);
    var short = q.length <= 8;
    if (!lastContexts.length) return q;
    if (!pronoun && !short) return q;
    // 收集上次命中的文档标题(去重,最多 3)
    var seen = {}, names = [];
    for (var i = 0; i < lastContexts.length; i++) {
      var t = lastContexts[i].docTitle;
      if (t && !seen[t]) { seen[t] = true; names.push(t); }
      if (names.length >= 3) break;
    }
    if (!names.length) return q;
    return q + ' (' + names.join(' ') + ')';
  }

  async function retrieveContext(query, tabId) {
    var results = await searchLibrary(buildFollowupQuery(query, tabId), 12);
    if (!results || !results.length) {
      return { contexts: [], docCount: 0, thin: true, confidence: 'low', hits: [] };
    }

    var candidates = [];
    var seen = {};
    var uniqueDocs = {};
    for (var i = 0; i < results.length; i++) {
      var result = results[i];
      var key = (result.doc_key || result.slug || '') + ':' + (result.node_id || '');
      if (seen[key]) continue;
      seen[key] = true;
      if (result.doc_key || result.slug) uniqueDocs[result.doc_key || result.slug] = true;
      var context = contextFromResult(result);
      // MMR 需要 rerankScore:后端未返回,退化为 score(保持后端排序的相对次序)
      context.rerankScore = Number(context.score) || 0;
      candidates.push(context);
    }

    var contexts = candidates.length > 1 ? mmrSelect(candidates, 0.6, 8) : candidates;
    var sourceCount = Object.keys(uniqueDocs).length;
    // 置信度:后端打分尺度不暴露(双轨不一),无法复刻 min-max 归一化的绝对信号,
    // 用不依赖尺度的启发式——命中数与来源数。命中多且来源多 → 高置信。
    var confidence = !contexts.length ? 'low'
      : (contexts.length >= 3 && sourceCount >= 2) ? 'high'
      : (contexts.length >= 2) ? 'medium'
      : 'low';

    var finalContexts = contexts;
    if (confidence === 'low' && contexts.length > 2) {
      finalContexts = await llmRerank(query, contexts);
    }

    return {
      contexts: finalContexts,
      docCount: sourceCount,
      thin: contexts.length < 2,
      confidence: confidence,
      hits: results.slice(0, 12)
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

  // P1-9: 从低置信检索生成 2-3 个追问建议(换角度 / 更具体 / 引向相邻章节)
  function buildFollowUps(query, contexts) {
    var docs = [];
    var seen = {};
    for (var i = 0; i < contexts.length; i++) {
      var t = contexts[i].docTitle;
      if (t && !seen[t]) { seen[t] = true; docs.push(t); }
      if (docs.length >= 2) break;
    }
    var q = String(query || '').slice(0, 40);
    var out = [];
    if (docs.length) {
      out.push('深入讲讲「' + docs[0] + '」里的相关内容');
      if (docs[1]) out.push('「' + docs[1] + '」和这个问题有什么关系？');
    }
    out.push('换个角度：用更具体的术语重新描述这个问题');
    if (out.length < 3) out.push('这个问题还能从哪些文档里找到依据？');
    return out.slice(0, 3);
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
    },
    {
      type: 'function',
      function: {
        name: 'search_local_files',
        description: '在本机文件系统中全文检索 docx/pptx/xlsx/txt 文件内容。当用户问到本机文档、本地文件、工作资料、PPT内容、Excel数据、Word文档中的具体内容时使用。返回结果含文件路径、页码、行号、内容片段,精确定位到来源位置。',
        parameters: {
          type: 'object',
          properties: {
            query: { type: 'string', description: '检索关键词,用文档中可能出现的原词效果最好' }
          },
          required: ['query']
        }
      }
    }
  ];

  async function retrieveContextAsText(query, budgetCtx, sourceCtx, tabId) {
    var result = await retrieveContext(query, tabId);
    var confidence = result.confidence;
    var contexts = result.contexts;
    // P1-10: 无结果时给具体恢复指引(Codex 式)
    if (!contexts.length) {
      return {
        text: '未找到相关内容。\n\n排查建议：\n- 换更简洁的关键词重试(用文档中可能出现的原词)\n- 检查检索范围(输入框左侧 ⊚ 图标):确认书籍/论文/笔记已勾选\n- 若文档是最近上传,可能需先重建索引(侧栏→索引管理→重建索引)',
        contexts: [], confidence: confidence
      };
    }
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

  // ── 本机文件检索(调用后端 /api/filesearch/search) ──────────────────
  async function retrieveLocalFiles(query, sourceCtx) {
    // 评审修复:尊重检索范围 — 未勾选"本机文件"时不检索本地文件
    var scope = getSearchScope();
    if (!scope.local) {
      return { text: '本机文件检索已关闭(在输入框 ⊚ 检索范围中勾选「本机文件」可开启)', __contexts: [] };
    }
    var url = BASE + '/api/filesearch/search?q=' + encodeURIComponent(query) + '&limit=5';
    try {
      var resp = await fetch(url);
      if (!resp.ok) {
        var errBody = '';
        try { errBody = await resp.text(); } catch (_) {}
        throw new Error('HTTP ' + resp.status + (errBody ? ': ' + errBody.slice(0, 200) : ''));
      }
      var data = await resp.json();
    } catch (e) {
      return { text: '本机文件检索失败: ' + e.message + '\n\n排查建议:\n- 确认本机检索已构建索引(边栏→本机检索→构建索引)\n- 确认后端服务正常运行在 127.0.0.1:8765', __contexts: [] };
    }

    var results = data.results || [];
    if (!results.length) {
      return { text: '未在本机文件中找到匹配内容。query=' + query, __contexts: [] };
    }

    var contexts = [];
    var blocks = [];
    for (var i = 0; i < results.length; i++) {
      var item = results[i];
      var sourceId = 'localfile:' + item.file_path + ':' + (item.page || 0) + ':' + (item.line_start || 0);
      var num;
      if (sourceCtx && sourceCtx.registry) {
        if (sourceCtx.registry.has(sourceId)) {
          num = sourceCtx.registry.get(sourceId);
        } else {
          sourceCtx.counter[0] += 1;
          num = sourceCtx.counter[0];
          sourceCtx.registry.set(sourceId, num);
        }
      } else {
        num = i + 1;
      }

      // 构建面包屑路径: 文件名 > 页码 > 行号
      var pageLabel = item.page_label || (item.page ? '第' + item.page + '页' : '');
      var lineLabel = item.line_start ? 'L' + item.line_start + (item.line_end && item.line_end !== item.line_start ? '-' + item.line_end : '') : '';
      var crumbParts = [item.file_name || '本机文件'];
      if (pageLabel) crumbParts.push(pageLabel);
      if (lineLabel) crumbParts.push(lineLabel);

      // 构建展示文本(含路径、页码、行号、片段)
      var locationLine = '**文件:** `' + item.file_path + '`';
      if (pageLabel) locationLine += '  |  **页:** ' + pageLabel;
      if (lineLabel) locationLine += '  |  **行:** ' + lineLabel;
      var blockText = '[' + num + '] ' + locationLine + '\n\n' + (item.snippet || item.text || '');

      contexts.push({
        sourceId: sourceId,
        displayNum: num,
        docTitle: item.file_name || '本机文件',
        breadcrumb: crumbParts,
        text: item.snippet || item.text || '',
        url: '',
        isLocalFile: true,
        filePath: item.file_path,
        page: item.page,
        pageLabel: pageLabel,
        lineStart: item.line_start,
        lineEnd: item.line_end,
        score: item.score
      });
      blocks.push(blockText);
    }

    var text = blocks.join('\n\n---\n\n') + '\n\n*本机文件匹配: ' + data.total + ' 条,返回 ' + results.length + ' 条*';
    return { text: text, __contexts: contexts };
  }

  async function executeTool(name, args, budgetCtx, sourceCtx, tabId) {
    if (name === 'search_library') {
      // 防御:模型返回的 tool arguments 可能缺 query 字段(空 {} 或格式异常),
      // 此时 args.query 为 undefined。若放任传给 retrieveContext → lexicalRerank,
      // 会抛 "Cannot read properties of undefined (reading 'match')" 导致整条 ReAct 崩溃。
      // 上游兜底:参数缺失时返回提示,让模型在下一轮换关键词重试。
      var searchQuery = args && args.query;
      if (!searchQuery || typeof searchQuery !== 'string') {
        return { text: '检索参数 query 缺失或非字符串,请换关键词重新调用 search_library。', __contexts: [] };
      }
      var r = await retrieveContextAsText(searchQuery, budgetCtx, sourceCtx, tabId);
      r.__contexts = r.contexts;
      return r;
    }
    if (name === 'search_local_files') {
      var localQuery = args && args.query;
      if (!localQuery || typeof localQuery !== 'string') {
        return { text: '检索参数 query 缺失或非字符串,请换关键词重新调用 search_local_files。', __contexts: [] };
      }
      var localResult = await retrieveLocalFiles(localQuery, sourceCtx);
      return localResult;
    }
    if (name === 'get_section') {
      var docId = args.doc_id || '';
      var nodeId = args.node_id || '';
      var sectionSourceId = (docId && nodeId) ? 'doc:' + docId + ':' + nodeId : '';
      var crumbParts = [];
      var docTitle = docId;
      var fullText = '';

      // 路径一(V3 自洽):source_md + line_num/line_end 直接取章。
      // /api/search 的 V3 命中自带 source_md,sourceId 形如 type:doc:node,
      // 但模型传给 get_section 的 doc_id 是 source_id 第二段(即 docId)。此处按
      // (docId,nodeId) 查,优先覆盖 V3 返回的引用(legacy 结果无 source_md 时兜底 doc-tree)。
      var apiCtx = sectionCache[docId + ':' + nodeId];
      var mdSource = apiCtx ? apiCtx.sourceMd : '';
      var lineStart = apiCtx ? apiCtx.lineNum : 0;
      var lineEnd = apiCtx ? apiCtx.lineEnd : 0;
      if (mdSource) {
        var fetched = await fetchMdSection(mdSource, lineStart, lineEnd);
        if (fetched) {
          fullText = fetched;
          crumbParts = apiCtx.breadcrumb || [];
          docTitle = apiCtx.docTitle || docId;
        }
      }

      if (!fullText) {
        // 路径二(legacy 索引引用兜底):doc-tree 定位节点
        await loadDocTree(docId);
        var doc = docCache[docId];
        if (!doc) {
          return { text: '文档 ' + docId + ' 未找到或加载失败' };
        }
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
        fullText = await fetchMdSection(node.source_md, node.line_num, node.line_end);
        if (!fullText) fullText = node.summary || '(正文获取失败，仅显示摘要：' + (node.summary || '无') + ')';
        crumbParts = node._crumb || [node.title];
        docTitle = doc.tree.title || docId;
      }

      return {
        text: '### ' + crumbParts.join(' > ') + '\n*来源: ' + docTitle + '*\n\n' + fullText,
        __contexts: [{
          sourceId: sectionSourceId || docId + ':' + nodeId,
          text: fullText,
          docTitle: docTitle,
          breadcrumb: crumbParts,
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
      '- 你有四个工具：search_library（检索文档）、search_local_files（检索本机文件）、get_section（取完整章节）、rewrite_query（改写查询）\n' +
      '- 回答用户问题前，**必须先调用检索工具**，不要凭记忆回答\n' +
      '- 只能基于检索到的内容回答，不要使用外部知识编造\n\n' +
      '## 检索策略（重要）\n' +
      '第一次用用户原话检索。如果结果不足，换策略重试：\n' +
      '- **查询重写**：换成文档里可能出现的专业术语重搜（如"那个相变"→"量子相变 Rabi 模型"）\n' +
      '- **子查询分解**：复合问题拆成子问题分别检索（如"Berry phase 和线性响应的关系"→分两搜）\n' +
      '- **步退查询**：太具体搜不到时，先用更宽泛的概念搜背景知识\n' +
      '- 可调 rewrite_query 工具生成改写建议，也可直接换关键词调 search_library\n' +
      '- 找到相关章节但内容被截断时，用 get_section 取完整内容（需 doc_id 和 node_id）\n\n' +
      '## 本机文件检索\n' +
      '- 当用户问到**本机文档、本地文件、工作资料、PPT内容、Excel数据、Word文档**中的具体内容时,调 search_local_files\n' +
      '- 本机文件检索覆盖 docx/pptx/xlsx/txt 等格式,已构建全文索引\n' +
      '- 返回结果含文件路径、页码、行号、内容片段,精确定位到来源位置\n' +
      '- 可以同时调 search_library(图书馆) 和 search_local_files(本机文件) 获取不同来源的信息\n\n' +
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

  // ── 高级思考动画系统 (纯 CSS, 持续丝滑) ──
  // 思考器 + body 是兄弟节点,更新内容只操作 body.innerHTML,
  // thinker 永远不离开 DOM → CSS 动画不重启 → 无闪烁。

  var THINKER_LABELS = {
    working: 'thinking...',
    searching: 'searching...',
    composing: 'thinking...'
  };

  // 创建思考器 DOM 元素 — 简洁圆点加载动画("thinking...." 点)
  // 三个圆点依次弹跳,纯 CSS(合成线程),无 canvas、无 rAF → 天然不卡、无泄漏
  function createThinker(state) {
    var el = document.createElement('div');
    el.className = 'lqd-thinker';
    el.innerHTML =
      '<div class="lqd-thinker-dots" aria-hidden="true">' +
        '<i></i><i></i><i></i>' +
      '</div>' +
      '<span class="lqd-thinker-label">' + (THINKER_LABELS[state] || THINKER_LABELS.working) + '</span>';
    return el;
  }

  // 在 contentEl 顶部插入思考器 (如果尚不存在)
  function showThinker(contentEl, state) {
    if (!contentEl) return null;
    var existing = contentEl.querySelector(':scope > .lqd-thinker');
    if (existing) {
      var label = existing.querySelector('.lqd-thinker-label');
      if (label) label.textContent = THINKER_LABELS[state] || THINKER_LABELS.working;
      return existing;
    }
    var thinker = createThinker(state);
    contentEl.insertBefore(thinker, contentEl.firstChild);
    return thinker;
  }

  // 淡出移除思考器
  function hideThinker(contentEl) {
    if (!contentEl) return;
    var thinker = contentEl.querySelector(':scope > .lqd-thinker');
    if (!thinker) return;
    thinker.classList.add('lqd-thinker--out');
    setTimeout(function () {
      if (thinker.parentNode) thinker.parentNode.removeChild(thinker);
    }, 300);
  }

  // ── tool-step 加载指示:纯 CSS 圆点(与思考器同款,合成线程,无 rAF) ──
  function hydrateToolOrbs(contentEl) {
    // 纯 CSS 圆点,无需水合
  }
  function destroyToolOrbs(contentEl) {
    // 纯 CSS 圆点,无需销毁
  }

  // 更新 contentEl 内容,但保留思考器不被 innerHTML 重建销毁
  // 思考器是 contentEl 的直接子节点,更新只作用于其后的 body 容器
  // ⚠️ 性能:renderThinkingAndText 会把整段累积 markdown 过 marked 解析,
  // 流式高频触发时若每帧都重渲,主线程被占满 → 动画卡顿。
  // 策略:最小间隔节流(≥200ms 才真正重渲一次),让思考文字以低频刷新,
  // 动画(纯 CSS 合成线程)完全不受影响。
  var _throttledRender = null; // { contentEl, thinking, text, toolTrail, raf }
  var _lastRenderAt = 0;
  var MIN_RENDER_INTERVAL = 200; // ms
  function scheduleContentRender(contentEl, thinking, text, toolTrail) {
    if (!contentEl) return;
    // 若距上次实际渲染不足最小间隔,只累积状态、交由稍后渲染
    _throttledRender = { contentEl: contentEl, thinking: thinking, text: text, toolTrail: toolTrail };
    if (_throttledRender.raf) return;
    var elapsed = Date.now() - _lastRenderAt;
    var delay = elapsed >= MIN_RENDER_INTERVAL ? 0 : (MIN_RENDER_INTERVAL - elapsed);
    _throttledRender.raf = true;
    setTimeout(function () {
      if (!_throttledRender) return;
      _throttledRender.raf = false;
      var state = _throttledRender;
      _throttledRender = null;
      if (!state) return;
      // 若 contentEl 已脱离文档,跳过(避免对已销毁标签做无谓重渲)
      if (!document.body.contains(state.contentEl)) return;
      _lastRenderAt = Date.now();
      flushContentRender(state.contentEl, state.thinking, state.text, state.toolTrail);
    }, delay);
  }
  function flushContentRender(contentEl, thinking, text, toolTrail) {
    var thinker = contentEl.querySelector(':scope > .lqd-thinker');
    var html = window.LqdChatMessages.renderThinkingAndText(thinking, text, toolTrail);
    if (thinker) {
      // 思考器存在:取其后的所有兄弟节点,用一个 body 容器替换
      var body = contentEl.querySelector(':scope > .lqd-thinker-body');
      if (!body) {
        body = document.createElement('div');
        body.className = 'lqd-thinker-body';
        contentEl.insertBefore(body, thinker.nextSibling);
      }
      body.innerHTML = html;
    } else {
      contentEl.innerHTML = html;
    }
  }
  function updateContentPreservingThinker(contentEl, thinking, text, toolTrail) {
    // 流式场景走 rAF 节流;一次性完整重渲(如最终落定)直接刷新
    scheduleContentRender(contentEl, thinking, text, toolTrail);
  }
  // 最终落定:立即渲染(取消挂起的节流帧,避免闪烁)
  function flushContentNow(contentEl, thinking, text, toolTrail) {
    if (_throttledRender && _throttledRender.contentEl === contentEl) {
      _throttledRender = null;
    }
    flushContentRender(contentEl, thinking, text, toolTrail);
  }

  // ── 代码块包装:为裸 <pre> 注入 .lqd-codebox 复制按钮 ──
  // shared/render.js 的围栏代码块已自带 codebox,这里兜底的只剩
  // messages.js fallback renderMarkdown 生成的裸 pre。
  function _b64encodeCode(s) {
    try {
      return btoa(unescape(encodeURIComponent(String(s))));
    } catch (_) {
      return '';
    }
  }
  function wrapCodeBlocks(contentEl) {
    if (!contentEl || !contentEl.querySelectorAll) return;
    var pres = contentEl.querySelectorAll('pre');
    for (var i = 0; i < pres.length; i++) {
      var pre = pres[i];
      if (pre.closest('.lqd-codebox')) continue;
      var code = pre.querySelector('code');
      var text = (code || pre).textContent || '';
      var box = document.createElement('div');
      box.className = 'lqd-codebox';
      box.setAttribute('data-code', _b64encodeCode(text));
      var header = document.createElement('div');
      header.className = 'lqd-codebox-bar';
      header.innerHTML =
        '<span class="lqd-codebox-dots"><i></i><i></i><i></i></span>' +
        '<span class="lqd-codebox-lang">code</span>' +
        '<button class="lqd-codebox-copy" type="button" title="复制代码">' +
          '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">' +
          '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>' +
        '</button>';
      pre.parentNode.insertBefore(box, pre);
      box.appendChild(header);
      box.appendChild(pre);
    }
  }

  async function sendMessage(query, refs, tabId) {
    var messagesEl = refs.messagesEl;
    var composerInput = refs.composerInput;
    var sendBtn = refs.sendBtn;
    var stopBtn = refs.stopBtn;
    var originTabId = tabId; // 会话写入始终落在发起 tab,避免多会话串写(评审修复)

    await window.LqdSettings.load();
    var apiKey = await window.LqdSettings.fetchApiKey();
    if (window.LqdSettings._cache) window.LqdSettings._cache.api_key = apiKey;
    var cfg = window.LqdSettings.resolve();

    if (!cfg.apiKey) {
      window.LqdChatMessages.appendMessageBubble('assistant', '请先前往「配置」页设置 LLM API Key。', messagesEl);
      return;
    }

    // 停止生成:AbortController,中止时所有进行中的流抛 AbortError
    var abortCtrl = new AbortController();
    var aborted = false;
    function onAbort() { aborted = true; }
    abortCtrl.signal.addEventListener('abort', onAbort);

    window.LqdChatMessages.hideEmpty(refs.emptyEl, messagesEl);
    window.LqdChatMessages.appendMessageBubble('user', query, messagesEl);
    window.LqdChatSession.appendToCurrent('user', query, null, originTabId);

    try {
      await loadIndexes();
    } catch (e) {
      window.LqdChatMessages.appendMessageBubble('assistant', '索引加载失败：' + e.message, messagesEl);
      return;
    }

    var contentEl = window.LqdChatMessages.appendMessageBubble('assistant', '', messagesEl);
    window.LqdChatMessages.setBusy(true, sendBtn, composerInput);

    // 显示停止按钮,绑定点击 → 中止生成
    if (stopBtn) {
      stopBtn.hidden = false;
      stopBtn.onclick = function () { abortCtrl.abort(); };
    }

    // 挂载纯 CSS 思考器动画 (持续显示,不随 innerHTML 重建而中断)
    showThinker(contentEl, 'working');

    var thinkingOn = thinkingEnabled();
    var debugOn = debugEnabled();
    var systemPrompt = buildAgentSystemPrompt();
    var messages = window.LqdChatSession.loadCurrent(originTabId).slice(-6);
    var MAX_LOOPS = 4;

    var budgetCtx = {
      historyTokens: messages.reduce(function (s, m) { return s + estimateTokens(m.content || ''); }, 0),
      systemTokens: estimateTokens(systemPrompt)
    };

    var finalText = '';
    var finalThinking = '';
    var didSearch = false;
    var confidenceLow = false; // P1-9: 低置信触发追问建议
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
        if (aborted) break;
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
          maxTokens: thinkingOn ? 8192 : 4096,
          signal: abortCtrl.signal
        });

        var _dbg = window.LQD_DEBUG_SSE;
        var _recv = _dbg ? { thinking: 0, text: 0, tool_calls: 0, stop: 0 } : null;
        try {
          for await (var chunk of stream) {
            if (aborted) break;
            if (_dbg) {
              if (chunk.type === 'thinking') _recv.thinking++;
              else if (chunk.type === 'text') _recv.text++;
              else if (chunk.type === 'tool_calls') _recv.tool_calls++;
              else if (chunk.type === 'stop') _recv.stop++;
            }
            if (chunk.type === 'thinking') {
              roundThinking += chunk.text;
              finalThinking += chunk.text;
              updateContentPreservingThinker(contentEl, finalThinking, roundText, toolTrail);
            } else if (chunk.type === 'text') {
              roundText += chunk.text;
              finalText = roundText;
              // 有文本输出时移除思考器(淡出)
              hideThinker(contentEl);
              // 流式期间节流重渲(合并为每帧一次);KaTeX 等公式完整后再统一渲染,
              // 避免每 chunk 都跑 renderMathInElement 拖垮主线程
              updateContentPreservingThinker(contentEl, finalThinking, finalText, toolTrail);
            } else if (chunk.type === 'tool_calls') {
              toolCalls = chunk.calls;
            } else if (chunk.type === 'stop') {
              stopReason = chunk.reason;
            }
            if (window.LqdChatMessages && window.LqdChatMessages.scrollToBottomIfAllowed) {
              window.LqdChatMessages.scrollToBottomIfAllowed(messagesEl);
            } else {
              messagesEl.scrollTop = messagesEl.scrollHeight;
            }
          }
        } catch (streamErr) {
          if (aborted) break;
          throw streamErr;
        }
        if (_dbg) console.log('[AGENT] loop', loop, 'recv', _recv, 'roundThinking.len', roundThinking.length, 'finalThinking.len', finalThinking.length, 'toolCalls', toolCalls);
        if (aborted) break;

        // 没有工具调用 → 本轮是最终回答，退出循环
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
          // 切换思考器到 searching 状态
          showThinker(contentEl, 'searching');
          // 检索执行期间:该步骤显示纯 CSS 圆点加载;完成后换为完成态
          toolTrail.push(
            '<div class="lqd-tool-step lqd-tool-step--loading"><span class="lqd-tool-spinner"></span><span class="lqd-tool-step-text">检索: <code>' + window.LqdChatCitations.escHtml(args.query || tc.arguments) + '</code></span>' +
            '<div class="lqd-tool-progress"><div class="lqd-tool-progress-bar"></div></div></div>'
          );
          var stepIdx = toolTrail.length - 1;
          updateContentPreservingThinker(contentEl, finalThinking, finalText, toolTrail);
          // 水合 tool-step 的 orb 画布
          hydrateToolOrbs(contentEl);
          if (tc.name === 'search_library' || tc.name === 'search_local_files') didSearch = true;

          var toolResult = await executeTool(tc.name, args, budgetCtx, { registry: sourceRegistry, counter: nextSourceNum }, originTabId);
          // 检索完成:先销毁 orb 画布(释放 rAF),再重渲染替换为完成图标
          // (顺序关键:updateContentPreservingThinker 会 innerHTML 重建,若先渲染再销毁
          //  orb 已从 DOM 脱离,destroy 找不到 → rAF 泄漏堆积 → 卡顿)
          destroyToolOrbs(contentEl);
          toolTrail[stepIdx] = toolTrail[stepIdx]
            .replace(' lqd-tool-step--loading', ' lqd-tool-step--settled')
            .replace('<span class="lqd-tool-spinner"></span>', '<span class="lqd-tool-done">' + (window.LqdIcons ? window.LqdIcons.icon('check') : '✓') + '</span>')
            .replace('<div class="lqd-tool-progress"><div class="lqd-tool-progress-bar"></div></div>', '');
          updateContentPreservingThinker(contentEl, finalThinking, finalText, toolTrail);
          // 评审修复:工具路径的检索也要记录低置信(追问建议触发条件)
          if (tc.name === 'search_library' && toolResult.confidence === 'low') confidenceLow = true;
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
        // 重新显示思考器为 composing 状态
        showThinker(contentEl, 'composing');
        finalText = '';
      }

      if (!didSearch && !aborted) {
        // 强制检索:先显示加载中的步骤,完成后再换为完成态
        showThinker(contentEl, 'searching');
        toolTrail.push(
          '<div class="lqd-tool-step lqd-tool-step--loading"><span class="lqd-tool-spinner"></span><span class="lqd-tool-step-text">检索: <code>' + window.LqdChatCitations.escHtml(query) + '</code></span>' +
          '<div class="lqd-tool-progress"><div class="lqd-tool-progress-bar"></div></div></div>'
        );
        var forcedStepIdx = toolTrail.length - 1;
        updateContentPreservingThinker(contentEl, finalThinking, finalText, toolTrail);
        hydrateToolOrbs(contentEl);
        var forced = await retrieveContextAsText(query, budgetCtx, { registry: sourceRegistry, counter: nextSourceNum }, originTabId);
        // 检索完成:先销毁 orb,再重渲染替换为完成图标(防 rAF 泄漏)
        destroyToolOrbs(contentEl);
        toolTrail[forcedStepIdx] = toolTrail[forcedStepIdx]
          .replace(' lqd-tool-step--loading', ' lqd-tool-step--settled')
          .replace('<span class="lqd-tool-spinner"></span>', '<span class="lqd-tool-done">' + (window.LqdIcons ? window.LqdIcons.icon('check') : '✓') + '</span>')
          .replace('<div class="lqd-tool-progress"><div class="lqd-tool-progress-bar"></div></div>', '');
        updateContentPreservingThinker(contentEl, finalThinking, finalText, toolTrail);
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
        // P1-9: 记录低置信(追问建议触发条件)
        if (forced.confidence === 'low') confidenceLow = true;

        // 强制检索:同时检索本机文件
        toolTrail.push(
          '<div class="lqd-tool-step lqd-tool-step--loading"><span class="lqd-tool-spinner"></span><span class="lqd-tool-step-text">本机文件检索: <code>' + window.LqdChatCitations.escHtml(query) + '</code></span>' +
          '<div class="lqd-tool-progress"><div class="lqd-tool-progress-bar"></div></div></div>'
        );
        var localStepIdx = toolTrail.length - 1;
        updateContentPreservingThinker(contentEl, finalThinking, finalText, toolTrail);
        hydrateToolOrbs(contentEl);
        try {
          var localForced = await retrieveLocalFiles(query, { registry: sourceRegistry, counter: nextSourceNum });
          // 检索完成:先销毁 orb,再重渲染替换为完成图标(防 rAF 泄漏)
          destroyToolOrbs(contentEl);
          toolTrail[localStepIdx] = toolTrail[localStepIdx]
            .replace(' lqd-tool-step--loading', ' lqd-tool-step--settled')
            .replace('<span class="lqd-tool-spinner"></span>', '<span class="lqd-tool-done">' + (window.LqdIcons ? window.LqdIcons.icon('check') : '✓') + '</span>')
            .replace('<div class="lqd-tool-progress"><div class="lqd-tool-progress-bar"></div></div>', '');
          updateContentPreservingThinker(contentEl, finalThinking, finalText, toolTrail);
          if (localForced.__contexts) {
            for (var m = 0; m < localForced.__contexts.length; m++) {
              var lc = localForced.__contexts[m];
              if (!seenSourceIds.has(lc.sourceId)) {
                allContexts.push(lc);
                seenSourceIds.add(lc.sourceId);
              }
            }
          }
        } catch (_) { /* ignore */ }
        destroyToolOrbs(contentEl);
        toolTrail[localStepIdx] = (toolTrail[localStepIdx] || '').
          replace(' lqd-tool-step--loading', ' lqd-tool-step--settled').
          replace('<span class="lqd-tool-spinner"></span>', '<span class="lqd-tool-done">' + (window.LqdIcons ? window.LqdIcons.icon('check') : '✓') + '</span>').
          replace('<div class="lqd-tool-progress"><div class="lqd-tool-progress-bar"></div></div>', '');
        updateContentPreservingThinker(contentEl, finalThinking, finalText, toolTrail);
      }

      if (!finalText.trim() && allContexts.length) {
        // 重新显示思考器为 composing 状态
        showThinker(contentEl, 'composing');
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
            hideThinker(contentEl);
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
            maxTokens: 4096,
            signal: abortCtrl.signal // 评审修复:停止按钮要能中止总结阶段
          });
          for await (var chunk2 of summaryStream) {
            if (aborted) break; // 评审修复:中止后立即退出
            if (chunk2.type === 'text') {
              summaryText += chunk2.text;
              finalText = summaryText;
              // 有文本输出时移除思考器(淡出)
              hideThinker(contentEl);
              // 节流重渲(每帧一次),KaTeX 在最终落定统一渲染
              updateContentPreservingThinker(contentEl, finalThinking, finalText, toolTrail);
            }
          }
        } catch (summaryErr) {
          if (!aborted) throw summaryErr; // 非中止错误才上抛
        }
        clearTimeout(summaryTimeout);
      }

      if (!finalText.trim()) {
        hideThinker(contentEl);
        if (allContexts.length) {
          finalText = '已检索到相关内容，但未能生成回答。请尝试换个问法重试。';
        } else {
          finalText = '未找到相关内容。\n\n排查建议：\n- 换更简洁的关键词重试(用文档中可能出现的原词)\n- 检查检索范围(输入框左侧 ⊚ 图标):确认书籍/论文/笔记已勾选\n- 若文档是最近上传,可能需先重建索引(侧栏→索引管理→重建索引)';
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

      if (aborted) {
        // 用户主动停止:保留已生成的部分回答,标注已停止
        if (finalText && finalText.trim()) {
          finalText += '\n\n> ⏹️ 已停止生成(部分内容已保留)';
        } else {
          finalText = '已停止生成。';
        }
        if (window.LqdToast) {
          window.LqdToast.show({ type: 'info', message: '已停止生成', duration: 2000 });
        }
      }

      // P1-9: 低置信时追加追问建议(Codex 式 follow-up chips)
      var followUpsHtml = '';
      if (allContexts.length && confidenceLow) {
        var fu = buildFollowUps(query, allContexts);
        if (fu.length) {
          followUpsHtml = '<div class="lqd-chat-followups">' +
            fu.map(function (q) { return '<button class="lqd-chat-followup" data-q="' + window.LqdChatCitations.escHtml(q) + '">' + window.LqdChatCitations.escHtml(q) + '</button>'; }).join('') +
            '</div>';
        }
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
          followUpsHtml +
          window.LqdChatCitations.renderDebugCard(debugHits, allContexts.slice(0, 8), systemPrompt, 'agent');
      } else {
        contentEl.innerHTML = window.LqdChatMessages.renderThinkingAndText(finalThinking, finalText, toolTrail) + citationsHtml + followUpsHtml;
      }
      // 取消挂起的节流帧,避免其用旧内容覆盖最终结果
      if (_throttledRender && _throttledRender.contentEl === contentEl) {
        _throttledRender = null;
      }
      wrapCodeBlocks(contentEl);
      window.LqdChatMessages.reRenderKatex(contentEl);
      // 绑定追问 chip 点击 → 自动发送
      if (followUpsHtml) {
        var fuEls = contentEl.querySelectorAll('.lqd-chat-followup');
        for (var fi = 0; fi < fuEls.length; fi++) {
          (function (btn) {
            btn.addEventListener('click', function () {
              var q = btn.getAttribute('data-q');
              if (!q) return;
              if (composerInput) {
                composerInput.value = q;
                composerInput.dispatchEvent(new Event('input'));
                window.LqdChatComposer.focus(composerInput);
                composerInput.setSelectionRange(q.length, q.length);
              }
            });
          })(fuEls[fi]);
        }
      }

      // 存消息时把引用卡片 HTML 一并存入(citations 字段),
      // 供切回标签重渲染时恢复流式输出时的"正文 + 参考来源"完整结构。
      window.LqdChatSession.appendToCurrent('assistant', finalText, { citations: citationsHtml }, originTabId);
      emitContext(allContexts);
      // P1-7: 记录本次检索上下文供追问感知(按 tab 隔离,评审修复)
      lastContextsByTab[originTabId] = allContexts.slice();
      if (window.LqdEvents) {
        window.LqdEvents.emit('chat:message', { role: 'assistant', content: finalText });
      }
    } catch (e) {
      // 用户主动停止:不当作错误展示
      if (aborted) {
        if (!finalText || !finalText.trim()) finalText = '已停止生成。';
        if (window.LqdToast) {
          window.LqdToast.show({ type: 'info', message: '已停止生成', duration: 2000 });
        }
      } else {
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
    }
    // 流式结束,移除 busy 标记(工作流 G)
    var bubble = contentEl.parentNode;
    if (bubble) bubble.removeAttribute('aria-busy');
    window.LqdChatMessages.setBusy(false, sendBtn, composerInput);
    // 隐藏停止按钮
    if (stopBtn) {
      stopBtn.hidden = true;
      stopBtn.onclick = null;
    }
    // 发送完成后焦点回到输入框
    if (composerInput && document.body.contains(composerInput)) {
      requestAnimationFrame(function () {
        if (document.body.contains(composerInput)) composerInput.focus();
      });
    }
  }

  window.LqdChatAgent = {
    loadIndexes: loadIndexes,
    searchLibrary: searchLibrary,
    retrieveContext: retrieveContext,
    retrieveContextAsText: retrieveContextAsText,
    executeTool: executeTool,
    LIBRARY_TOOLS: LIBRARY_TOOLS,
    buildSystemPrompt: buildSystemPrompt,
    buildAgentSystemPrompt: buildAgentSystemPrompt,
    sendMessage: sendMessage,
    debugEnabled: debugEnabled,
    thinkingEnabled: thinkingEnabled,
    getSearchScope: getSearchScope,
    setSearchScope: setSearchScope,
    scopeDocTypes: scopeDocTypes,
    wrapCodeBlocks: wrapCodeBlocks
  };
})();
