/**
 * LQ-D â€” Chat Agent
 *
 * ReAct å·¥å…·å¾ªç¯ã€æ£€ç´¢ä¸Šä¸‹æ–‡ã€å‘é€æ¶ˆæ¯ï¼ˆåŸ handleSend æ”¹åä¸º sendMessageï¼‰ã€‚
 * ä¾èµ–: LqdSettings / LqdChatLLM / LqdChatSession / LqdChatMessages / LqdChatCitations / LqdEvents / YuuRetrievalã€‚
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

  // â”€â”€ Index loading â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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
    if (!bm25Stats) return 0;  // stats æœªæ„å»º(nodeIndex æœªåŠ è½½ç­‰),è¿”å› 0 ä¸å‚ä¸æ‰“åˆ†
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

  // â”€â”€ Token budget packing â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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

  // â”€â”€ MD fetch / doc tree â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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
      // ç¡®ä¿ bm25Stats å·²æ„å»ºã€‚search() èµ°å€’æ’ç´¢å¼•è·¯å¾„(searchMultiPath)æ—¶
      // ä¸ä¼šæ„å»º bm25Stats,ä½†ä¸‹é¢ RM3 é‡æ‰“åˆ†è¦ç”¨ node çº§ bm25Score,
      // stats ä¸º null ä¼šæŠ¥ "Cannot read properties of null (fieldAvgLen)"ã€‚
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
        snippet = (start > 0 ? 'â€¦' : '') + fullText.slice(start, start + 400);
      } else {
        snippet = fullText.slice(0, 400);
      }
      return '[' + (i + 1) + '] ' + crumb + '\nç‰‡æ®µï¼š' + snippet;
    }).join('\n---\n');
    var userPrompt = 'æŸ¥è¯¢ï¼š' + query + '\n\nå€™é€‰æ–‡æ¡£ï¼š\n' + docs + '\n\nä¸ºæ¯ä¸ªæ–‡æ¡£æ‰“ 0-10 åˆ†ï¼ˆ10 æœ€ç›¸å…³ï¼‰ï¼Œæ ¼å¼"[ç¼–å·] åˆ†æ•°"ï¼Œæ¯è¡Œä¸€ä¸ªã€‚åªè¿”å›è¯„åˆ†ã€‚';
    try {
      var resp = await window.LqdChatLLM.callLLMSync(
        'ä½ æ˜¯æ–‡æ¡£ç›¸å…³æ€§è¯„ä¼°ä¸“å®¶ã€‚æ ¹æ®æŸ¥è¯¢è¯„ä¼°æ–‡æ¡£ç›¸å…³æ€§ã€‚',
        userPrompt
      );
      if (!resp) return contexts;
      var scores = new Map();
      var lines = resp.split('\n');
      for (var i = 0; i < lines.length; i++) {
        var m = lines[i].match(/\[(\d+)\]\s*[:ï¼š]?\s*(\d+(?:\.\d+)?)/);
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

  // â”€â”€ System prompts â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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
      var sentences = text.split(/(?<=[ã€‚ï¼ï¼Ÿï¼›!?])/g).filter(function (s) { return s.trim(); });
      for (var i = 0; i < sentences.length; i++) {
        if ((out + sentences[i]).length > maxChars) break;
        out += sentences[i];
      }
    }
    if (!out) out = text.slice(0, maxChars);
    return out + '\n\nâ€¦[å·²æŒ‰è¯­ä¹‰è¾¹ç•Œæˆªæ–­ï¼Œå¯è¿½é—®è·å–å®Œæ•´å†…å®¹]â€¦';
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
      var meta = [dc[0].docAuthor, dc[0].docType].filter(Boolean).join(' Â· ');
      return '- **' + name + '**' + (meta ? ' (' + meta + ')' : '') + ' â€” ' + dc.length + ' ä¸ªç›¸å…³æ®µè½';
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
      var block = '### [' + n + '] ' + crumb + '\n*æ¥æº: ' + c.docTitle + ' | source_id: ' + c.sourceId + '*\n';
      var nearby = [];
      if (c.parentTitle && c.breadcrumb.length > 1) nearby.push('ä¸Šçº§: ' + c.parentTitle);
      if (c.siblingTitles.length) nearby.push('åŒçº§: ' + c.siblingTitles.join(' / '));
      if (c.childTitles.length) nearby.push('å­èŠ‚: ' + c.childTitles.join(' / '));
      if (nearby.length) block += '*' + nearby.join('  |  ') + '*\n';
      block += '\n' + text;
 ß~ö¶‰Ëkºwµç} ¤ì(€€€É•ÑÕÉ¸€Ÿ’öƒšb¼€¨©1Dµ¨¨ƒj~—¢¾–*§š&/¾ò3–~ë’ê;’â«’êëšVÃ–¶_–nû’æ›¦šjIƒ¦^»¶SÎïî	q¹q¸œ€¬(€€€€€€œŒŒƒ–nû’æ›¦šn»–öW¾ò œ€¬Ñ½Œ¹‘½½Õ¹Ğ€¬€œƒ¾šZš†¾ò3šÒ‹–&7–#šÖ?¢#nã–Ï¦Š–~¾ò%q¸œ€¬Ñ½Œ¹Ñ•áĞ€¬€q¹q¸œ€¬(€€€€€€œŒŒƒ–Ş—’ösšZç–ò=q¸œ€¬(€€€€€€œ´ƒ’öƒšr'’â'’â«–Ş—–ß¾òiÍ•…É¡}±¥‰É…Éç¾ò#šÒ‹¾ò'•Ñ}Í•Ñ¥½»¾ò#–>[–º3šVÓ®ƒ¢*¾ò'É•İÉ¥Ñ•}ÅÕ•Éç¾ò#šRç–gš~—¢¾‹¾ò%q¸œ€¬(€€€€€€œ´ƒ–n{¶SR£š"ß¦^»¦Šc–&7¾ò0¨«–ş¦†ï–#¢ÂR Í•…É¡}±¥‰É…ÉäƒšÒˆ¨«¾ò3’â7¢š–·¢ºÃ–ş–n{¶Qq¸œ€¬(€€€€€€œ´ƒ–>«¢÷–~ë’ê;šÒ‹–"Ãj––ºç–n{¶S¾ò3’â7¢š’öÿR£–’[¦£~—¢¾ò[¦q¹q¸œ€¬(€€€€€€œŒŒƒšÒ‹¶[V—¾ò#¦7¢š¾ò%q¸œ€¬(€€€€€€Ÿ²³’âš²‡R£R£š"ß–:¢¾wšÒ‹–ššzsîOšzs’â7¢ÚÏ¾ò3š6‹¶[V—¦7¢¾W¾òiq¸œ€¬(€€€€€€œ´€¨«š~—¢¾‹¦7–d¨«¾òkš6‹š"CšZš†¦3–>¿¢÷–ë:Ãj’âO’âkšr¿¢¾·¦7šBs¾ò#–š‹¦
’â«nã–>`‹ŠH‹¦?–¶Cnã–>`I…‰¤ƒš¢‡–z,‹¾ò%q¸œ€¬(€€€€€€œ´€¨«–¶Cš~—¢¾‹–"¢Œ¨«¾òk–’7–B#¦^»¦Šcš.š"C–¶C¦^»¦Šc–"–"¯šÒ‹¾ò#–š‰	•ÉÉäÁ¡…Í”ƒ–J3êÿšŸ–N7–êSj–ÏÎì‹ŠK–"’â“šBs¾ò%q¸œ€¬(€€€€€€œ´€¨«š¶—¦š~—¢¾ˆ¨«¾òk–’«–ß’öOšBs’â7–"Ãš^Û¾ò3–#R£šnÓ–º÷šÎojšš–ş×šBs¢3šf¿~—¢¾q¸œ€¬(€€€€€€œ´ƒ–>¿¢ÂÉ•İÉ¥Ñ•}ÅÕ•Éäƒ–Ş—–ßRš"CšRç–g–îë¢º»¾ò3’æ–>¿nÓš:—š6‹–Ï¦R»¢¾7¢ÂÍ•…É¡}±¥‰É…Éåq¸œ€¬(€€€€€€œ´ƒš&û–"Ãnã–Ï®ƒ¢*’ö––ºç¢Š¯š"«šZ·š^Û¾ò3R •Ñ}Í•Ñ¥½¸ƒ–>[–º3šVÓ––ºç¾ò#¦r ‘½}¥ƒ–J0¹½‘•}¥“¾ò%q¹q¸œ€¬(€€€€€€œŒŒƒ–n{¶S¢–"eq¸œ€¬(€€€€€€œ´ƒš¾?’â«–Ï¦R»¢ºëšZ·š‚šÎ£šv—šêCò[–>Üm9w¾ò3–¾ç–êSšÒ‹îOšzs’â·jm9uq¸œ€¬(€€€€€€œ´ƒ–n{¶Sšr¯–Âû–"_–ë–>¢šv—šêC¾ò3š‚ó–ò?¾òiq¹lÅtƒšZš†–B4€øƒ®ƒ¢*€øƒ¢*–B5q¸œ€¬(€€€€€€œ´ƒšÒ‹îOšzs’â7¢ÚÏš^Ûšb;†»¢¾Óšb8‹–öO–&7–nû’æ›¦š’â·šÊ‡šr'¢ÚÏ–’’úwš6¸‹¾ò3’â7¢š†³¶Qq¸œ€¬(€€€€€€œ´ƒ–n{¶S’öÿR£’â·šZ¾ò3’âO’âkšr¿¢¾·’şwVg–:šZ–³–ò?R 1…Q•c¾òk¢†3–qp ¸¸¹qp§¾ò3¢†3¦^Ğqql¸¸¹qqtœì(€ô((€€¼¼ƒŠRŠR M•¹€¼I•Ğ±½½ÀƒŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠR (€™Õ¹Ñ¥½¸‘•‰Õ¹…‰±• ¤ì(€€€É•ÑÕÉ¸±½…±MÑ½É…”¹•Ñ%Ñ•´ ±Å‘}¡…Ñ}‘•‰Õœœ¤€ôôô€œÄœì(€ô((€™Õ¹Ñ¥½¸Ñ¡¥¹­¥¹¹…‰±• ¤ì(€€€É•ÑÕÉ¸±½…±MÑ½É…”¹•Ñ%Ñ•´ ±Å‘}¡…Ñ}Ñ¡¥¹­¥¹œœ¤€„ôô€œÀœì(€ô((€…Íå¹Œ™Õ¹Ñ¥½¸Í•¹‘5•ÍÍ…”¡ÅÕ•Éä°É•™Ì¤ì(€€€Ù…Èµ•ÍÍ…•Í°€ôÉ•™Ì¹µ•ÍÍ…•Í°ì(€€€Ù…È½µÁ½Í•É%¹ÁÕĞ€ôÉ•™Ì¹½µÁ½Í•É%¹ÁÕĞì(€€€Ù…ÈÍ•¹‘	Ñ¸€ôÉ•™Ì¹Í•¹‘	Ñ¸ì((€€€…İ…¥Ğİ¥¹‘½Ü¹1Å‘M•ÑÑ¥¹Ì¹±½… ¤ì(€€€Ù…È…Á¥-•ä€ô…İ…¥Ğİ¥¹‘½Ü¹1Å‘M•ÑÑ¥¹Ì¹™•Ñ¡Á¥-•ä ¤ì(€€€¥˜€¡İ¥¹‘½Ü¹1Å‘M•ÑÑ¥¹Ì¹}…¡”¤İ¥¹‘½Ü¹1Å‘M•ÑÑ¥¹Ì¹}…¡”¹…Á¥}­•ä€ô…Á¥-•äì(€€€Ù…È™œ€ôİ¥¹‘½Ü¹1Å‘M•ÑÑ¥¹Ì¹É•Í½±Ù” ¤ì((€€€¥˜€ …™œ¹…Á¥-•ä¤ì(€€€€€İ¥¹‘½Ü¹1Å‘¡…Ñ5•ÍÍ…•Ì¹…ÁÁ•¹‘5•ÍÍ…•	Õ‰‰±” …ÍÍ¥ÍÑ…¹Ğœ°€Ÿ¢¾ß–#–&7–ú3¦7ö»7¦†×¢ºûö¸114A$-•çœ°µ•ÍÍ…•Í°¤ì(€€€€€É•ÑÕÉ¸ì(€€€ô((€€€İ¥¹‘½Ü¹1Å‘¡…Ñ5•ÍÍ…•Ì¹¡¥‘•µÁÑä¡É•™Ì¹•µÁÑå°°µ•ÍÍ…•Í°¤ì(€€€İ¥¹‘½Ü¹1Å‘¡…Ñ5•ÍÍ…•Ì¹…ÁÁ•¹‘5•ÍÍ…•	Õ‰‰±” ÕÍ•Èœ°ÅÕ•Éä°µ•ÍÍ…•Í°¤ì(€€€İ¥¹‘½Ü¹1Å‘¡…ÑM•ÍÍ¥½¸¹…ÁÁ•¹‘Q½ÕÉÉ•¹Ğ ÕÍ•Èœ°ÅÕ•Éä¤ì((€€€ÑÉäì(€€€€€…İ…¥Ğ±½…‘%¹‘•á•Ì ¤ì(€€€ô…Ñ €¡”¤ì(€€€€€İ¥¹‘½Ü¹1Å‘¡…Ñ5•ÍÍ…•Ì¹…ÁÁ•¹‘5•ÍÍ…•	Õ‰‰±” …ÍÍ¥ÍÑ…¹Ğœ°€ŸÒ‹–òW–*ƒ¢ö÷–’Ç¢Ò—¾òhœ€¬”¹µ•ÍÍ…”°µ•ÍÍ…•Í°¤ì(€€€€€É•ÑÕÉ¸ì(€€€ô((€€€Ù…È½¹Ñ•¹Ñ°€ôİ¥¹‘½Ü¹1Å‘¡…Ñ5•ÍÍ…•Ì¹…ÁÁ•¹‘5•ÍÍ…•	Õ‰‰±” …ÍÍ¥ÍÑ…¹Ğœ°€œñ•´û––’’â·Š›Š˜ğ½•´øœ°µ•ÍÍ…•Í°¤ì(€€€İ¥¹‘½Ü¹1Å‘¡…Ñ5•ÍÍ…•Ì¹Í•Ñ	ÕÍä¡ÑÉÕ”°Í•¹‘	Ñ¸°½µÁ½Í•É%¹ÁÕĞ¤ì((€€€Ù…ÈÑ¡¥¹­¥¹=¸€ôÑ¡¥¹­¥¹¹…‰±• ¤ì(€€€Ù…È‘•‰Õ=¸€ô‘•‰Õ¹…‰±• ¤ì(€€€Ù…ÈÍåÍÑ•µAÉ½µÁĞ€ô‰Õ¥±‘•¹ÑMåÍÑ•µAÉ½µÁĞ ¤ì(€€€Ù…Èµ•ÍÍ…•Ì€ôİ¥¹‘½Ü¹1Å‘¡…ÑM•ÍÍ¥½¸¹±½…‘ÕÉÉ•¹Ğ ¤¹Í±¥” ´Ø¤ì(€€€Ù…È5a}1==AL€ô€Ğì((€€€Ù…È‰Õ‘•ÑÑà€ôì(€€€€€¡¥ÍÑ½ÉåQ½­•¹Ìèµ•ÍÍ…•Ì¹É•‘Õ”¡™Õ¹Ñ¥½¸€¡Ì°´¤ìÉ•ÑÕÉ¸Ì€¬•ÍÑ¥µ…Ñ•Q½­•¹Ì¡´¹½¹Ñ•¹Ğñğ€œœ¤ìô°€À¤°(€€€€€ÍåÍÑ•µQ½­•¹Ìè•ÍÑ¥µ…Ñ•Q½­•¹Ì¡ÍåÍÑ•µAÉ½µÁĞ¤(€€€ôì((€€€Ù…È™¥¹…±Q•áĞ€ô€œœì(€€€Ù…È™¥¹…±Q¡¥¹­¥¹œ€ô€œœì(€€€Ù…È‘¥‘M•…É €ô™…±Í”ì(€€€Ù…È…±±½¹Ñ•áÑÌ€ômtì(€€€Ù…ÈÍ••¹M½ÕÉ•%‘Ì€ô¹•ÜM•Ğ ¤ì(€€€Ù…ÈÑ½½±QÉ…¥°€ômtì(€€€Ù…ÈÍ½ÕÉ•I•¥ÍÑÉä€ô¹•Ü5…À ¤ì(€€€Ù…È¹•áÑM½ÕÉ•9Õ´€ôlÁtì((€€€Ù…È•µ¥Ñ½¹Ñ•áĞ€ô™Õ¹Ñ¥½¸€¡½¹Ñ•áÑÌ¤ì(€€€€€¥˜€¡İ¥¹‘½Ü¹1Å‘Ù•¹ÑÌ¤ì(€€€€€€€İ¥¹‘½Ü¹1Å‘Ù•¹ÑÌ¹•µ¥Ğ ¡…Ğé½¹Ñ•áĞœ°ìÅÕ•ÉäèÅÕ•Éä°½¹Ñ•áÑÌè½¹Ñ•áÑÌô¤ì(€€€€€ô(€€€ôì((€€€ÑÉäì(€€€€€Ù…È…•¹ÑMÑ…ÉĞ€ô…Ñ”¹¹½Ü ¤ì(€€€€€™½È€¡Ù…È±½½À€ô€Àì±½½À€ğ5a}1==ALì±½½À¬¬¤ì(€€€€€€€¥˜€¡…Ñ”¹¹½Ü ¤€´…•¹ÑMÑ…ÉĞ€ø€ÄÈÀÀÀÀ¤ì(€€€€€€€€€½¹Ñ•¹Ñ°¹¥¹¹•É!Q50€ôİ¥¹‘½Ü¹1Å‘¡…Ñ5•ÍÍ…•Ì¹É•¹‘•ÉQ¡¥¹­¥¹¹‘Q•áĞ (€€€€€€€€€€€™¥¹…±Q¡¥¹­¥¹œ°€œñ•´ûšÒ‹¢Úš^Û¾ò3š¶–r£R£–ŞË¢:ß–>[j––ºçRš"C–n{¶SŠ›Š˜ğ½•´øœ°Ñ½½±QÉ…¥°(€€€€€€€€€€¤ì(€€€€€€€€€‰É•…¬ì(€€€€€€€ô(€€€€€€€Ù…ÈÉ½Õ¹‘Q•áĞ€ô€œœì(€€€€€€€Ù…ÈÉ½Õ¹‘Q¡¥¹­¥¹œ€ô€œœì(€€€€€€€Ù…ÈÑ½½±…±±Ì€ô¹Õ±°ì(€€€€€€€Ù…ÈÍÑ½ÁI•…Í½¸€ô¹Õ±°ì((€€€€€€€Ù…ÈÍÑÉ•…´€ôİ¥¹‘½Ü¹1Å‘¡…Ñ114¹ÍÑÉ•…µQ•áĞ¡ì(€€€€€€€€€ÁÉ½Ù¥‘•Èè™œ¹ÁÉ½Ù¥‘•È°(€€€€€€€€€µ½‘•°è™œ¹µ½‘•°°(€€€€€€€€€‰…Í•UÉ°è™œ¹‰…Í•UÉ°°(€€€€€€€€€…Á¥-•äè™œ¹…Á¥-•ä°(€€€€€€€€€ÁÉ½Ñ½½°è™œ¹ÁÉ½Ñ½½°°(€€€€€€€€€Á…Ñ¡5½‘”è™œ¹Á…Ñ¡5½‘”°(€€€€€€€€€ÍåÍÑ•´èÍåÍÑ•µAÉ½µÁĞ°(€€€€€€€€€µ•ÍÍ…•Ìèµ•ÍÍ…•Ì°(€€€€€€€€€Ñ½½±Ìè1%	IIe}Q==1L°(€€€€€€€€€Ñ¡¥¹­¥¹œèÑ¡¥¹­¥¹=¸°(€€€€€€€€€µ…áQ½­•¹ÌèÑ¡¥¹­¥¹=¸€ü€àÄäÈ€è€ĞÀäØ(€€€€€€€ô¤ì((€€€€€€€Ù…È}‘‰œ€ôİ¥¹‘½Ü¹1E}	U}MMì(€€€€€€€Ù…È}É•Ø€ô}‘‰œ€üìÑ¡¥¹­¥¹œè€À°Ñ•áĞè€À°Ñ½½±}…±±Ìè€À°ÍÑ½Àè€Àô€è¹Õ±°ì(€€€€€€€™½È…İ…¥Ğ€¡Ù…È¡Õ¹¬½˜ÍÑÉ•…´¤ì(€€€€€€€€€¥˜€¡}‘‰œ¤ì(€€€€€€€€€€€¥˜€¡¡Õ¹¬¹ÑåÁ”€ôôô€Ñ¡¥¹­¥¹œœ¤}É•Ø¹Ñ¡¥¹­¥¹œ¬¬ì(€€€€€€€€€€€•±Í”¥˜€¡¡Õ¹¬¹ÑåÁ”€ôôô€Ñ•áĞœ¤}É•Ø¹Ñ•áĞ¬¬ì(€€€€€€€€€€€•±Í”¥˜€¡¡Õ¹¬¹ÑåÁ”€ôôô€Ñ½½±}…±±Ìœ¤}É•Ø¹Ñ½½±}…±±Ì¬¬ì(€€€€€€€€€€€•±Í”¥˜€¡¡Õ¹¬¹ÑåÁ”€ôôô€ÍÑ½Àœ¤}É•Ø¹ÍÑ½À¬¬ì(€€€€€€€€€ô(€€€€€€€€€¥˜€¡¡Õ¹¬¹ÑåÁ”€ôôô€Ñ¡¥¹­¥¹œœ¤ì(€€€€€€€€€€€É½Õ¹‘Q¡¥¹­¥¹œ€¬ô¡Õ¹¬¹Ñ•áĞì(€€€€€€€€€€€™¥¹…±Q¡¥¹­¥¹œ€¬ô¡Õ¹¬¹Ñ•áĞì(€€€€€€€€€€€½¹Ñ•¹Ñ°¹¥¹¹•É!Q50€ôİ¥¹‘½Ü¹1Å‘¡…Ñ5•ÍÍ…•Ì¹É•¹‘•ÉQ¡¥¹­¥¹¹‘Q•áĞ¡™¥¹…±Q¡¥¹­¥¹œ°É½Õ¹‘Q•áĞ°Ñ½½±QÉ…¥°¤ì(€€€€€€€€€ô•±Í”¥˜€¡¡Õ¹¬¹ÑåÁ”€ôôô€Ñ•áĞœ¤ì(€€€€€€€€€€€É½Õ¹‘Q•áĞ€¬ô¡Õ¹¬¹Ñ•áĞì(€€€€€€€€€€€™¥¹…±Q•áĞ€ôÉ½Õ¹‘Q•áĞì(€€€€€€€€€€€½¹Ñ•¹Ñ°¹¥¹¹•É!Q50€ôİ¥¹‘½Ü¹1Å‘¡…Ñ5•ÍÍ…•Ì¹É•¹‘•ÉQ¡¥¹­¥¹¹‘Q•áĞ¡™¥¹…±Q¡¥¹­¥¹œ°™¥¹…±Q•áĞ°Ñ½½±QÉ…¥°¤ì(€€€€€€€€€€€İ¥¹‘½Ü¹1Å‘¡…Ñ5•ÍÍ…•Ì¹É•I•¹‘•É-…Ñ•à¡½¹Ñ•¹Ñ°¤ì(€€€€€€€€€ô•±Í”¥˜€¡¡Õ¹¬¹ÑåÁ”€ôôô€Ñ½½±}…±±Ìœ¤ì(€€€€€€€€€€€Ñ½½±…±±Ì€ô¡Õ¹¬¹…±±Ìì(€€€€€€€€€ô•±Í”¥˜€¡¡Õ¹¬¹ÑåÁ”€ôôô€ÍÑ½Àœ¤ì(€€€€€€€€€€€ÍÑ½ÁI•…Í½¸€ô¡Õ¹¬¹É•…Í½¸ì(€€€€€€€€€ô(€€€€€€€€€µ•ÍÍ…•Í°¹ÍÉ½±±Q½À€ôµ•ÍÍ…•Í°¹ÍÉ½±±!•¥¡Ğì(€€€€€€€ô(€€€€€€€¥˜€¡}‘‰œ¤½¹Í½±”¹±½œ m9Qt±½½Àœ°±½½À°€É•Øœ°}É•Ø°€É½Õ¹‘Q¡¥¹­¥¹œ¹±•¸œ°É½Õ¹‘Q¡¥¹­¥¹œ¹±•¹Ñ °€™¥¹…±Q¡¥¹­¥¹œ¹±•¸œ°™¥¹…±Q¡¥¹­¥¹œ¹±•¹Ñ °€Ñ½½±…±±Ìœ°Ñ½½±…±±Ì¤ì((€€€€€€€€¼¼ƒšÊ‡šr'–Ş—–ß¢ÂR ƒŠHƒšr³¢ö»šb¿šrî#–n{¶S¾ò3¦–ë–ú«:¼(€€€€€€€€¼¼ƒšÎ£š<ë’â7¢÷’úw¢ÖXÍÑ½ÁI•…Í½¸ƒ–óŠSŠQ¹Ñ¡É½Á¥Œƒ–6?¢º¸ÍÑ½Á}É•…Í½¸ƒšb¼€‰Ñ½½±}ÕÍ”ˆ(€€€€€€€€¼¼€£’âPÑ½½±}ÕÍ”ƒš^ØÉ•…‘MMƒ–>¨å¥•±Ñ½½±}…±±Ìƒ’â4å¥•±ÍÑ½À±ÍÑ½ÁI•…Í½¸ƒ’şwš2¹Õ±°¤°(€€€€€€€€¼¼=Á•¹$ƒ–6?¢º¸™¥¹¥Í¡}É•…Í½¸ƒšb¼€‰Ñ½½±}…±±Ì‹’â“¢–ó’â7–B0³î’âR Ñ½½±…±±Ìƒšb¿–B›–¶c–r£–"“šZ·(€€€€€€€¥˜€ …Ñ½½±…±±Ìñğ€…Ñ½½±…±±Ì¹±•¹Ñ ¤‰É•…¬ì((€€€€€€€µ•ÍÍ…•Ì¹ÁÕÍ ¡ì(€€€€€€€€€É½±”è€…ÍÍ¥ÍÑ…¹Ğœ°(€€€€€€€€€½¹Ñ•¹ĞèÉ½Õ¹‘Q•áĞñğ¹Õ±°°(€€€€€€€€€Ñ½½±}…±±ÌèÑ½½±…±±Ì¹µ…À¡™Õ¹Ñ¥½¸€¡ÑŒ¤ì(€€€€€€€€€€€É•ÑÕÉ¸ì¥èÑŒ¹¥°ÑåÁ”è€™Õ¹Ñ¥½¸œ°™Õ¹Ñ¥½¸èì¹…µ”èÑŒ¹¹…µ”°…ÉÕµ•¹ÑÌèÑŒ¹…ÉÕµ•¹ÑÌôôì(€€€€€€€€€ô¤(€€€€€€€ô¤ì((€€€€€€€™½È€¡Ù…È¤€ô€Àì¤€ğÑ½½±…±±Ì¹±•¹Ñ ì¤¬¬¤ì(€€€€€€€€€Ù…ÈÑŒ€ôÑ½½±…±±Ím¥tì(€€€€€€€€€Ù…È…ÉÌ€ôíôì(€€€€€€€€€ÑÉäì…ÉÌ€ô)M=8¹Á…ÉÍ”¡ÑŒ¹…ÉÕµ•¹ÑÌñğ€íôœ¤ìô…Ñ €¡|¤ì€¼¨¥¹½É”€¨¼ô(€€€€€€€€€Ñ½½±QÉ…¥°¹ÁÕÍ  (€€€€€€€€€€€€œñ‘¥Ø±…ÍÌô‰±ÅµÑ½½°µÍÑ•ÀˆûÂ~R4ƒšÒˆè€ñ½‘”øœ€¬İ¥¹‘½Ü¹1Å‘¡…Ñ¥Ñ…Ñ¥½¹Ì¹•Í!Ñµ°¡…ÉÌ¹ÅÕ•ÉäñğÑŒ¹…ÉÕµ•¹ÑÌ¤€¬€œğ½½‘”øğ½‘¥Øøœ(€€€€€€€€€€¤ì(€€€€€€€€€½¹Ñ•¹Ñ°¹¥¹¹•É!Q50€ôİ¥¹‘½Ü¹1Å‘¡…Ñ5•ÍÍ…•Ì¹É•¹‘•ÉQ¡¥¹­¥¹¹‘Q•áĞ¡™¥¹…±Q¡¥¹­¥¹œ°™¥¹…±Q•áĞ°Ñ½½±QÉ…¥°¤ì(€€€€€€€€€¥˜€¡ÑŒ¹¹…µ”€ôôô€Í•…É¡}±¥‰É…Éäœ¤‘¥‘M•…É €ôÑÉÕ”ì((€€€€€€€€€Ù…ÈÑ½½±I•ÍÕ±Ğ€ô…İ…¥Ğ•á•ÕÑ•Q½½°¡ÑŒ¹¹…µ”°…ÉÌ°‰Õ‘•ÑÑà°ìÉ•¥ÍÑÉäèÍ½ÕÉ•I•¥ÍÑÉä°½Õ¹Ñ•Èè¹•áÑM½ÕÉ•9Õ´ô¤ì(€€€€€€€€€¥˜€¡Ñ½½±I•ÍÕ±Ğ¹}}½¹Ñ•áÑÌ¤ì(€€€€€€€€€€€™½È€¡Ù…È¨€ô€Àì¨€ğÑ½½±I•ÍÕ±Ğ¹}}½¹Ñ•áÑÌ¹±•¹Ñ ì¨¬¬¤ì(€€€€€€€€€€€€€Ù…ÈŒ€ôÑ½½±I•ÍÕ±Ğ¹}}½¹Ñ•áÑÍm©tì(€€€€€€€€€€€€€¥˜€ …Í••¹M½ÕÉ•%‘Ì¹¡…Ì¡Œ¹Í½ÕÉ•%¤¤ì(€€€€€€€€€€€€€€€…±±½¹Ñ•áÑÌ¹ÁÕÍ ¡Œ¤ì(€€€€€€€€€€€€€€€Í••¹M½ÕÉ•%‘Ì¹…‘¡Œ¹Í½ÕÉ•%¤ì(€€€€€€€€€€€€€ô(€€€€€€€€€€€ô(€€€€€€€€€ô(€€€€€€€€€µ•ÍÍ…•Ì¹ÁÕÍ ¡ì(€€€€€€€€€€€É½±”è€Ñ½½°œ°(€€€€€€€€€€€Ñ½½±}…±±}¥èÑŒ¹¥°(€€€€€€€€€€€½¹Ñ•¹ĞèÑ½½±I•ÍÕ±Ğ¹Ñ•áĞñğ)M=8¹ÍÑÉ¥¹¥™ä¡Ñ½½±I•ÍÕ±Ğ¤(€€€€€€€€€ô¤ì(€€€€€€€ô(€€€€€€€½¹Ñ•¹Ñ°¹¥¹¹•É!Q50€ôİ¥¹‘½Ü¹1Å‘¡…Ñ5•ÍÍ…•Ì¹É•¹‘•ÉQ¡¥¹­¥¹¹‘Q•áĞ (€€€€€€€€€™¥¹…±Q¡¥¹­¥¹œ°€œñ•´û–ŞËšÒ‹¾ò3š¶–r£îó–B#–n{¶SŠ›Š˜ğ½•´øœ°Ñ½½±QÉ…¥°(€€€€€€€€¤ì(€€€€€€€™¥¹…±Q•áĞ€ô€œœì(€€€€€ô((€€€€€¥˜€ …‘¥‘M•…É ¤ì(€€€€€€€Ù…È™½É•€ô…İ…¥ĞÉ•ÑÉ¥•Ù•½¹Ñ•áÑÍQ•áĞ¡ÅÕ•Éä°‰Õ‘•ÑÑà°ìÉ•¥ÍÑÉäèÍ½ÕÉ•I•¥ÍÑÉä°½Õ¹Ñ•Èè¹•áÑM½ÕÉ•9Õ´ô¤ì(€€€€€€€¥˜€¡™½É•¹}}½¹Ñ•áÑÌ¤ì(€€€€€€€€€™½É•¹}}½¹Ñ•áÑÌ€ô™½É•¹½¹Ñ•áÑÌì(€€€€€€€€€™½È€¡Ù…È¨€ô€Àì¨€ğ™½É•¹}}½¹Ñ•áÑÌ¹±•¹Ñ ì¨¬¬¤ì(€€€€€€€€€€€Ù…ÈŒÈ€ô™½É•¹}}½¹Ñ•áÑÍm©tì(€€€€€€€€€€€¥˜€ …Í••¹M½ÕÉ•%‘Ì¹¡…Ì¡ŒÈ¹Í½ÕÉ•%¤¤ì(€€€€€€€€€€€€€…±±½¹Ñ•áÑÌ¹ÁÕÍ ¡ŒÈ¤ì(€€€€€€€€€€€€€Í••¹M½ÕÉ•%‘Ì¹…‘¡ŒÈ¹Í½ÕÉ•%¤ì(€€€€€€€€€€€ô(€€€€€€€€€ô(€€€€€€€ô(€€€€€€€Ñ½½±QÉ…¥°¹ÁÕÍ  œñ‘¥Ø±…ÍÌô‰±ÅµÑ½½°µÍÑ•ÀˆûÂ~R4ƒšÒˆ£–òë–"Ø¤è€ñ½‘”øœ€¬İ¥¹‘½Ü¹1Å‘¡…Ñ¥Ñ…Ñ¥½¹Ì¹•Í!Ñµ°¡ÅÕ•Éä¤€¬€œğ½½‘”øğ½‘¥Øøœ¤ì(€€€€€ô((€€€€€¥˜€ …™¥¹…±Q•áĞ¹ÑÉ¥´ ¤€˜˜…±±½¹Ñ•áÑÌ¹±•¹Ñ ¤ì(€€€€€€€½¹Ñ•¹Ñ°¹¥¹¹•É!Q50€ôİ¥¹‘½Ü¹1Å‘¡…Ñ5•ÍÍ…•Ì¹É•¹‘•ÉQ¡¥¹­¥¹¹‘Q•áĞ (€€€€€€€€€™¥¹…±Q¡¥¹­¥¹œ°€œñ•´ûšÒ‹–º3š"C¾ò3š¶–r£îó–B#–n{¶SŠ›Š˜ğ½•´øœ°Ñ½½±QÉ…¥°(€€€€€€€€¤ì(€€€€€€€Ù…ÈÑá	±½­Ì€ô…±±½¹Ñ•áÑÌ¹µ…À¡™Õ¹Ñ¥½¸€¡Œ¤ì(€€€€€€€€€Ù…È¹Õ´€ôŒ¹‘¥ÍÁ±…å9Õ´ñğ…±±½¹Ñ•áÑÌ¹¥¹‘•á=˜¡Œ¤€¬€Äì(€€€€€€€€€É•ÑÕÉ¸€œŒŒŒlœ€¬¹Õ´€¬€t€œ€¬Œ¹‰É•…‘ÉÕµˆ¹©½¥¸ œ€ø€œ¤€¬€q¸«šv—šê@è€œ€¬Œ¹‘½Q¥Ñ±”€¬€œ©q¹q¸œ€¬ÑÉÕ¹…Ñ•Ñ	½Õ¹‘…Éä¡Œ¹Ñ•áĞ°5a}MQ%=9}!IL¤ì(€€€€€€€ô¤¹©½¥¸ q¹q¸´´µq¹q¸œ¤ì(€€€€€€€Ù…ÈÍÕµµ…Éå5•ÍÍ…•Ì€ôl(€€€€€€€€€ìÉ½±”è€ÕÍ•Èœ°½¹Ñ•¹ĞèÅÕ•Éäô°(€€€€€€€€€ìÉ½±”è€ÕÍ•Èœ°½¹Ñ•¹Ğè€Ÿ–~ë’ê;’î—’â/šÒ‹–"Ãj–nû’æ›¦š––ºç¾ò3–n{¶S’â+¦v‹j¦^»¦Šc	q¹q¸ŒŒƒ–n{¶S¢–"eq¸´ƒš¾?’â«–Ï¦R»¢ºëšZ·R m9tƒš‚šÎ£šv—šêCò[–>ß¾ò#–¾ç–êS’â/šZçjm9w¾ò0¨«–>«–gò[–>ß¾ò3’â7¢š¢«–ŞÇ–dÕÉ°ƒš"[¦Nûš:”¨«¾ò%q¸´ƒ–n{¶Sšr¯–Âû–"_–ë–>¢šv—šêC¾ò3š‚ó–ò?¾òim9tƒšZš†–B4€øƒ®ƒ¢*	q¸´ƒ–>«–~ë’ê8½¹Ñ•áĞƒ–n{¶S¾ò3’â7¢šò[¦q¸´ƒ–n{¶S’öÿR£’â·šZ¾ò3–³–ò?R -…Q•c¾òk¢†3–€¸¸¸“¾ò#’î~·²›–>ß¾ò'¾ò3–’7šv¿–’k¢†3–³–ò?R£¢†3¦^Ğ€¸¸¸“šš¶‹¦Vÿ–³–ò?–†{¢şo¢†3–q¹q¸ŒŒ½¹Ñ•áÓ¾ò#š2'nã–Ï–ê›š:K–ê?¾ò%q¹q¸œ€¬Ñá	±½­Ìô(€€€€€€€tì(€€€€€€€Ù…ÈÍÕµµ…ÉåQ•áĞ€ô€œœì(€€€€€€€Ù…ÈÍÕµµ…ÉåQ¥µ•½ÕĞ€ôÍ•ÑQ¥µ•½ÕĞ¡™Õ¹Ñ¥½¸€ ¤ì(€€€€€€€€€¥˜€ …™¥¹…±Q•áĞ¹ÑÉ¥´ ¤¤ì(€€€€€€€€€€€™¥¹…±Q•áĞ€ô€Ÿ–ŞËšÒ‹–"Ãnã–Ï––ºç¾ò3’öRš"C–n{¶S¢Úš^Û¢¾ß–Âw¢¾Wš6‹’â«¦^»šÎW¾ò3š"[nÓš:—–r£’æ›šzÛ’â·šÖ?¢#nã–Ï®ƒ¢*œì(€€€€€€€€€€€½¹Ñ•¹Ñ°¹¥¹¹•É!Q50€ôİ¥¹‘½Ü¹1Å‘¡…Ñ5•ÍÍ…•Ì¹É•¹‘•ÉQ¡¥¹­¥¹¹‘Q•áĞ¡™¥¹…±Q¡¥¹­¥¹œ°™¥¹…±Q•áĞ°Ñ½½±QÉ…¥°¤ì(€€€€€€€€€€€İ¥¹‘½Ü¹1Å‘¡…Ñ5•ÍÍ…•Ì¹É•I•¹‘•É-…Ñ•à¡½¹Ñ•¹Ñ°¤ì(€€€€€€€€€ô(€€€€€€€ô°€ÌÀÀÀÀ¤ì(€€€€€€€ÑÉäì(€€€€€€€€€Ù…ÈÍÕµµ…ÉåMÑÉ•…´€ôİ¥¹‘½Ü¹1Å‘¡…Ñ114¹ÍÑÉ•…µQ•áĞ¡ì(€€€€€€€€€€€ÁÉ½Ù¥‘•Èè™œ¹ÁÉ½Ù¥‘•È°(€€€€€€€€€€€µ½‘•°è™œ¹µ½‘•°°(€€€€€€€€€€€‰…Í•UÉ°è™œ¹‰…Í•UÉ°°(€€€€€€€€€€€…Á¥-•äè™œ¹…Á¥-•ä°(€€€€€€€€€€€ÁÉ½Ñ½½°è™œ¹ÁÉ½Ñ½½°°(€€€€€€€€€€€Á…Ñ¡5½‘”è™œ¹Á…Ñ¡5½‘”°(€€€€€€€€€€€ÍåÍÑ•´èÍåÍÑ•µAÉ½µÁĞ°(€€€€€€€€€€€µ•ÍÍ…•ÌèÍÕµµ…Éå5•ÍÍ…•Ì°(€€€€€€€€€€€Ñ½½±ÌèÕ¹‘•™¥¹•°(€€€€€€€€€€€Ñ¡¥¹­¥¹œè™…±Í”°(€€€€€€€€€€€µ…áQ½­•¹Ìè€ĞÀäØ(€€€€€€€€€ô¤ì(€€€€€€€€€™½È…İ…¥Ğ€¡Ù…È¡Õ¹¬È½˜ÍÕµµ…ÉåMÑÉ•…´¤ì(€€€€€€€€€€€¥˜€¡¡Õ¹¬È¹ÑåÁ”€ôôô€Ñ•áĞœ¤ì(€€€€€€€€€€€€€ÍÕµµ…ÉåQ•áĞ€¬ô¡Õ¹¬È¹Ñ•áĞì(€€€€€€€€€€€€€™¥¹…±Q•áĞ€ôÍÕµµ…ÉåQ•áĞì(€€€€€€€€€€€€€½¹Ñ•¹Ñ°¹¥¹¹•É!Q50€ôİ¥¹‘½Ü¹1Å‘¡…Ñ5•ÍÍ…•Ì¹É•¹‘•ÉQ¡¥¹­¥¹¹‘Q•áĞ¡™¥¹…±Q¡¥¹­¥¹œ°™¥¹…±Q•áĞ°Ñ½½±QÉ…¥°¤ì(€€€€€€€€€€€€€İ¥¹‘½Ü¹1Å‘¡…Ñ5•ÍÍ…•Ì¹É•I•¹‘•É-…Ñ•à¡½¹Ñ•¹Ñ°¤ì(€€€€€€€€€€€ô(€€€€€€€€€ô(€€€€€€€ô…Ñ €¡|¤ì€¼¨¥¹½É”€¨¼ô(€€€€€€€±•…ÉQ¥µ•½ÕĞ¡ÍÕµµ…ÉåQ¥µ•½ÕĞ¤ì(€€€€€ô((€€€€€¥˜€ …™¥¹…±Q•áĞ¹ÑÉ¥´ ¤¤ì(€€€€€€€¥˜€¡…±±½¹Ñ•áÑÌ¹±•¹Ñ ¤ì(€€€€€€€€€™¥¹…±Q•áĞ€ô€Ÿ–ŞËšÒ‹–"Ãnã–Ï––ºç¾ò3’öšr«¢÷Rš"C–n{¶S¢¾ß–Âw¢¾Wš6‹’â«¦^»šÎW¦7¢¾Wœì(€€€€€€€ô•±Í”ì(€€€€€€€€€™¥¹…±Q•áĞ€ô€Ÿšr«š&û–"Ãnã–Ï––ºç–>¿’î—–r£’æ›šzÛ’â·šÖ?¢#¾ò3š"[š6‹–Ï¦R»¢¾7¦7¢¾Wœì(€€€€€€€ô(€€€€€€€½¹Ñ•¹Ñ°¹¥¹¹•É!Q50€ôİ¥¹‘½Ü¹1Å‘¡…Ñ5•ÍÍ…•Ì¹É•¹‘•ÉQ¡¥¹­¥¹¹‘Q•áĞ¡™¥¹…±Q¡¥¹­¥¹œ°™¥¹…±Q•áĞ°Ñ½½±QÉ…¥°¤ì(€€€€€€€İ¥¹‘½Ü¹1Å‘¡…Ñ5•ÍÍ…•Ì¹É•I•¹‘•É-…Ñ•à¡½¹Ñ•¹Ñ°¤ì(€€€€€ô((€€€€€Ù…È¥Ñ…Ñ¥½¹Í!Ñµ°€ô€œœì(€€€€€¥˜€¡…±±½¹Ñ•áÑÌ¹±•¹Ñ ¤ì(€€€€€€€Ù…ÈÉ•™5…À€ôíôì(€€€€€€€™½È€¡Ù…È¤€ô€Àì¤€ğ…±±½¹Ñ•áÑÌ¹±•¹Ñ ì¤¬¬¤ì(€€€€€€€€€Ù…ÈŒÌ€ô…±±½¹Ñ•áÑÍm¥tì(€€€€€€€€€Ù…È¹Õ´€ôŒÌ¹‘¥ÍÁ±…å9Õ´ñğ¤€¬€Äì(€€€€€€€€€¥˜€¡ŒÌ¹ÕÉ°¤É•™5…Ám¹Õµt€ôìÑ¥Ñ±”èŒÌ¹‘½Q¥Ñ±”°‰É•…‘ÉÕµˆèŒÌ¹‰É•…‘ÉÕµˆ°ÕÉ°èŒÌ¹ÕÉ°ôì(€€€€€€€ô(€€€€€€€¥˜€¡=‰©•Ğ¹­•åÌ¡É•™5…À¤¹±•¹Ñ €ø€À¤ì(€€€€€€€€€™¥¹…±Q•áĞ€ôİ¥¹‘½Ü¹1Å‘¡…Ñ¥Ñ…Ñ¥½¹Ì¹¥¹©•ÑI•™•É•¹•1¥¹­Ì¡™¥¹…±Q•áĞ°É•™5…À¤ì(€€€€€€€ô(€€€€€€€¥Ñ…Ñ¥½¹Í!Ñµ°€ôİ¥¹‘½Ü¹1Å‘¡…Ñ¥Ñ…Ñ¥½¹Ì¹É•¹‘•É¥Ñ…Ñ¥½¹Ì¡…±±½¹Ñ•áÑÌ¤ì(€€€€€ô((€€€€€¥˜€¡‘•‰Õ=¸€˜˜…±±½¹Ñ•áÑÌ¹±•¹Ñ ¤ì(€€€€€€€Ù…È‘•‰Õ!¥ÑÌ€ô…±±½¹Ñ•áÑÌ¹Í±¥” À°€ÄÈ¤¹µ…À¡™Õ¹Ñ¥½¸€¡Œ°¤¤ì(€€€€€€€€€É•ÑÕÉ¸ì(€€€€€€€€€€€¹½‘”èì‘½}¥èŒ¹‘½Q¥Ñ±”°¹½‘•}¥èŒ¹¹½‘•%°‰É•…‘ÉÕµˆèŒ¹‰É•…‘ÉÕµˆô°(€€€€€€€€€€€Í½É”è€œüœ(€€€€€€€€€ôì(€€€€€€€ô¤ì(€€€€€€€½¹Ñ•¹Ñ°¹¥¹¹•É!Q50€ô(€€€€€€€€€İ¥¹‘½Ü¹1Å‘¡…Ñ5•ÍÍ…•Ì¹É•¹‘•ÉQ¡¥¹­¥¹¹‘Q•áĞ¡™¥¹…±Q¡¥¹­¥¹œ°™¥¹…±Q•áĞ°Ñ½½±QÉ…¥°¤€¬(€€€€€€€€€¥Ñ…Ñ¥½¹Í!Ñµ°€¬(€€€€€€€€€İ¥¹‘½Ü¹1Å‘¡…Ñ¥Ñ…Ñ¥½¹Ì¹É•¹‘•É•‰Õ…É¡‘•‰Õ!¥ÑÌ°…±±½¹Ñ•áÑÌ¹Í±¥” À°€à¤°ÍåÍÑ•µAÉ½µÁĞ°€…•¹Ğœ¤ì(€€€€€ô•±Í”ì(€€€€€€€½¹Ñ•¹Ñ°¹¥¹¹•É!Q50€ôİ¥¹‘½Ü¹1Å‘¡…Ñ5•ÍÍ…•Ì¹É•¹‘•ÉQ¡¥¹­¥¹¹‘Q•áĞ¡™¥¹…±Q¡¥¹­¥¹œ°™¥¹…±Q•áĞ°Ñ½½±QÉ…¥°¤€¬¥Ñ…Ñ¥½¹Í!Ñµ°ì(€€€€€ô(€€€€€İ¥¹‘½Ü¹1Å‘¡…Ñ5•ÍÍ…•Ì¹É•I•¹‘•É-…Ñ•à¡½¹Ñ•¹Ñ°¤ì((€€€€€İ¥¹‘½Ü¹1Å‘¡…ÑM•ÍÍ¥½¸¹…ÁÁ•¹‘Q½ÕÉÉ•¹Ğ …ÍÍ¥ÍÑ…¹Ğœ°™¥¹…±Q•áĞ¤ì(€€€€€•µ¥Ñ½¹Ñ•áĞ¡…±±½¹Ñ•áÑÌ¤ì(€€€€€¥˜€¡İ¥¹‘½Ü¹1Å‘Ù•¹ÑÌ¤ì(€€€€€€€İ¥¹‘½Ü¹1Å‘Ù•¹ÑÌ¹•µ¥Ğ ¡…Ğéµ•ÍÍ…”œ°ìÉ½±”è€…ÍÍ¥ÍÑ…¹Ğœ°½¹Ñ•¹Ğè™¥¹…±Q•áĞô¤ì(€€€€€ô(€€€ô…Ñ €¡”¤ì(€€€€€€¼¼ƒš&O–6Ã–º3šVÓ–‚š‚#–"À½¹Í½±”¡ÄÈƒ–>¿¢§ŠSŠSš¶“–&7–>«šbû’è”¹µ•ÍÍ…”°(€€€€€€¼¼€‰…¹¹½ĞÉ•…ÁÉ½Á•ÉÑ¥•Ì½˜Õ¹‘•™¥¹•ˆƒÆï¦Rg¢¾¿š^ƒšÎW–ºk’ö7šêC–’Ğ³š¾?š²‡’ş»¦÷¦vƒ2s(€€€€€¥˜€¡İ¥¹‘½Ü¹1Å‘ÉÉ½ÉÌ¤İ¥¹‘½Ü¹1Å‘ÉÉ½ÉÌ¹É•Á½ÉĞ¡”°€Í•¹‘5•ÍÍ…”œ¤ì(€€€€€Ù…ÈÍÑ…­!Ñµ°€ô€œœì(€€€€€¥˜€¡”€˜˜”¹ÍÑ…¬¤ì(€€€€€€€ÍÑ…­!Ñµ°€ô€œñ‘•Ñ…¥±Ì±…ÍÌô‰±Åµ•ÉÉ½ÈµÍÑ…¬ˆøñÍÕµµ…Éäû¦Rg¢¾¿–‚š‚ £š:Kš~—R ¤ğ½ÍÕµµ…ÉäøñÁÉ”øœ€¬(€€€€€€€€€İ¥¹‘½Ü¹1Å‘¡…Ñ¥Ñ…Ñ¥½¹Ì¹•Í!Ñµ°¡”¹ÍÑ…¬¤€¬€œğ½ÁÉ”øğ½‘•Ñ…¥±Ìøœì(€€€€€ô(€€€€€½¹Ñ•¹Ñ°¹¥¹¹•É!Q50€¬ô€œñ‰ÈøñÍÁ…¸ÍÑå±”ô‰½±½Èè‘ŒÈØÈØˆû¦Rg¢¾¼è€œ€¬(€€€€€€€İ¥¹‘½Ü¹1Å‘¡…Ñ¥Ñ…Ñ¥½¹Ì¹•Í!Ñµ°¡”€˜˜”¹µ•ÍÍ…”€ü”¹µ•ÍÍ…”€èMÑÉ¥¹œ¡”¤¤€¬€œğ½ÍÁ…¸øœ€¬ÍÑ…­!Ñµ°ì(€€€ô(€€€€¼¼ƒšÖ–ò?îOšv|³ï¦f‰ÕÍäƒš‚¢ºÀ£–Ş—’ösšÖ¤(€€€Ù…È‰Õ‰‰±”€ô½¹Ñ•¹Ñ°¹Á…É•¹Ñ9½‘”ì(€€€¥˜€¡‰Õ‰‰±”¤‰Õ‰‰±”¹É•µ½Ù•ÑÑÉ¥‰ÕÑ” …É¥„µ‰ÕÍäœ¤ì(€€€İ¥¹‘½Ü¹1Å‘¡…Ñ5•ÍÍ…•Ì¹Í•Ñ	ÕÍä¡™…±Í”°Í•¹‘	Ñ¸°½µÁ½Í•É%¹ÁÕĞ¤ì(€ô((€İ¥¹‘½Ü¹1Å‘¡…Ñ•¹Ğ€ôì(€€€±½…‘%¹‘•á•Ìè±½…‘%¹‘•á•Ì°(€€€É•ÑÉ¥•Ù•½¹Ñ•áĞèÉ•ÑÉ¥•Ù•½¹Ñ•áĞ°(€€€É•ÑÉ¥•Ù•½¹Ñ•áÑÍQ•áĞèÉ•ÑÉ¥•Ù•½¹Ñ•áÑÍQ•áĞ°(€€€•á•ÕÑ•Q½½°è•á•ÕÑ•Q½½°°(€€€1%	IIe}Q==1Lè1%	IIe}Q==1L°(€€€‰Õ¥±‘MåÍÑ•µAÉ½µÁĞè‰Õ¥±‘MåÍÑ•µAÉ½µÁĞ°(€€€‰Õ¥±‘•¹ÑMåÍÑ•µAÉ½µÁĞè‰Õ¥±‘•¹ÑMåÍÑ•µAÉ½µÁĞ°(€€€Í•¹‘5•ÍÍ…”èÍ•¹‘5•ÍÍ…”°(€€€‘•‰Õ¹…‰±•è‘•‰Õ¹…‰±•°(€€€Ñ¡¥¹­¥¹¹…‰±•èÑ¡¥¹­¥¹¹…‰±•(€ôì)ô¤ ¤ì(