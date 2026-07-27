/**
 * LQ-D — 上传队列状态机
 *
 * 管理批量上传队列:每个文件一个 item,状态 pending/running/done/failed。
 * 逐个 POST /api/ingest/full,每个文件一个 job,前端轮询状态。
 * 队列刷新页面丢失(桌面应用不频繁刷新,可接受)。
 */
(function () {
  'use strict';

  var STATUS = {
    PENDING: 'pending',
    RUNNING: 'running',
    DONE: 'done',
    FAILED: 'failed'
  };

  var state = {
    items: [],  // [{id, file, path, meta, status, jobId, log, stage}]
    listeners: [],
    counter: 0
  };

  function genId() {
    state.counter += 1;
    return 'item-' + Date.now() + '-' + state.counter;
  }

  function add(file, path, meta) {
    var item = {
      id: genId(),
      file: file,           // File 对象(浏览器 input)
      path: path,           // 真实路径(pywebview 文件对话框)
      name: file ? file.name : (path ? path.split(/[\\/]/).pop() : 'unknown'),
      meta: meta || {},     // {title, author, slug, tags, docType, strategy, pages, stages}
      status: STATUS.PENDING,
      jobId: null,
      log: [],
      stage: ''
    };
    state.items.push(item);
    notify();
    return item;
  }

  function remove(id) {
    state.items = state.items.filter(function (i) { return i.id !== id; });
    notify();
  }

  function update(id, patch) {
    var item = get(id);
    if (!item) return;
    Object.keys(patch).forEach(function (k) { item[k] = patch[k]; });
    notify();
  }

  function get(id) {
    return state.items.find(function (i) { return i.id === id; });
  }

  function next() {
    return state.items.find(function (i) { return i.status === STATUS.PENDING; });
  }

  function clearDone() {
    state.items = state.items.filter(function (i) {
      return i.status !== STATUS.DONE && i.status !== STATUS.FAILED;
    });
    notify();
  }

  function all() {
    return state.items.slice();
  }

  function onStatusChange(cb) {
    state.listeners.push(cb);
  }

  function notify() {
    state.listeners.forEach(function (cb) { try { cb(state.items); } catch (_) {} });
  }

  window.YuuUploadQueue = {
    STATUS: STATUS,
    add: add,
    remove: remove,
    update: update,
    get: get,
    next: next,
    clearDone: clearDone,
    all: all,
    onStatusChange: onStatusChange
  };
})();
