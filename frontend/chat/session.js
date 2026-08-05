/**
 * LQ-D — Chat Session
 *
 * 当前会话（sessionStorage）与归档历史（localStorage）CRUD。
 * 键从旧 yuu_chat_* 迁移到 lqd_chat_*。
 */
(function () {
  'use strict';

  var CHAT_SESSION_PREFIX = 'lqd_chat_session_'; // H4: 按 tab.id 隔离
  var SESSIONS_ARCHIVE_KEY = 'lqd_chat_sessions_archive';
  var MAX_ARCHIVED = 20;

  function safeParse(raw) {
    try {
      return raw ? JSON.parse(raw) : null;
    } catch (_) {
      return null;
    }
  }

  // H4: 会话键按 tab.id 隔离,每个 chat 标签有独立会话
  function sessionKey(tabId) {
    return CHAT_SESSION_PREFIX + (tabId || 'default');
  }

  // H4: 获取当前 chat 标签 id(从 LqdTabs.active 或传入)
  function currentTabId(tabId) {
    if (tabId) return tabId;
    if (window.LqdTabs) {
      var t = window.LqdTabs.active();
      if (t && t.type === 'chat') return t.id;
    }
    return 'default';
  }

  function loadCurrent(tabId) {
    return safeParse(sessionStorage.getItem(sessionKey(currentTabId(tabId)))) || [];
  }

  function saveCurrent(messages, tabId) {
    try {
      sessionStorage.setItem(sessionKey(currentTabId(tabId)), JSON.stringify(messages || []));
    } catch (_) { /* ignore */ }
  }

  function clearCurrent(tabId) {
    sessionStorage.removeItem(sessionKey(currentTabId(tabId)));
  }

  function appendToCurrent(role, content, extra, tabId) {
    var messages = loadCurrent(tabId);
    var msg = { role: role, content: content };
    // extra: 附加字段(如 assistant 的 citations 引用卡片 HTML),
    // 供切回标签重渲染时恢复流式输出时的完整结构。
    if (extra && typeof extra === 'object') {
      for (var k in extra) {
        if (Object.prototype.hasOwnProperty.call(extra, k)) msg[k] = extra[k];
      }
    }
    messages.push(msg);
    if (messages.length > 20) messages = messages.slice(-20);
    saveCurrent(messages, tabId);
    return messages;
  }

  // 智能标题:从首条用户消息抽取(去首尾空白/多余标点,截 24 字符)
  function smartTitle(messages) {
    var first = '';
    for (var i = 0; i < messages.length; i++) {
      if (messages[i].role === 'user' && messages[i].content) {
        first = messages[i].content;
        break;
      }
    }
    if (!first) first = (messages[0] && messages[0].content) || '(空会话)';
    first = String(first).replace(/\s+/g, ' ').trim();
    // 去开头动作词(命令/重复标点),让标题更像一句话
    first = first.replace(/^(请|帮我|能否|麻烦|总结|解释|介绍一下?)\s*/, '');
    first = first.replace(/[。！？!?]+$/, '');
    if (first.length > 24) {
      first = first.slice(0, 23) + '…';
    }
    return first || '(空会话)';
  }

  function archiveCurrent(messages) {
    messages = messages || loadCurrent();
    if (!messages || !messages.length) return;
    var sessions = listArchived();
    var title = smartTitle(messages);
    // M7: 检查是否已归档过(避免重复归档产生副本)
    var existingId = messages._archivedId;
    if (existingId) {
      for (var k = 0; k < sessions.length; k++) {
        if (sessions[k].id === existingId) {
          sessions[k].messages = messages.slice();
          sessions[k].title = title;
          sessions[k].date = new Date().toISOString();
          try { localStorage.setItem(SESSIONS_ARCHIVE_KEY, JSON.stringify(sessions)); } catch (e) {
            // L9: localStorage 写入失败(配额满/隐私模式)不再静默,提示用户
            if (window.console && window.console.warn) {
              window.console.warn('[LqdChatSession] archive write failed (quota?)', e);
            }
          }
          return;
        }
      }
    }
    sessions.unshift({
      id: Date.now().toString(36),
      title: title,
      date: new Date().toISOString(),
      messages: messages.slice(),
      pinned: false
    });
    if (sessions.length > MAX_ARCHIVED) sessions.length = MAX_ARCHIVED;
    // 重排:pinned 在前
    var pinned = [];
    var unpinned = [];
    for (var p = 0; p < sessions.length; p++) {
      if (sessions[p].pinned) pinned.push(sessions[p]);
      else unpinned.push(sessions[p]);
    }
    try {
      localStorage.setItem(SESSIONS_ARCHIVE_KEY, JSON.stringify(pinned.concat(unpinned)));
    } catch (_) { /* ignore */ }
  }

  function listArchived() {
    return safeParse(localStorage.getItem(SESSIONS_ARCHIVE_KEY)) || [];
  }

  function restoreArchived(id) {
    var sessions = listArchived();
    var s = null;
    for (var i = 0; i < sessions.length; i++) {
      if (sessions[i].id === id) { s = sessions[i]; break; }
    }
    if (!s) return null;
    // 直接从归档加载消息到当前会话,不自动归档当前内容(避免用户感知为"复制")
    var restored = (s.messages || []).slice();
    restored._archivedId = id; // 标记来源,切换回"新对话"时更新而非新增
    saveCurrent(restored);
    return s;
  }

  function removeArchived(id) {
    var sessions = listArchived().filter(function (x) { return x.id !== id; });
    try {
      localStorage.setItem(SESSIONS_ARCHIVE_KEY, JSON.stringify(sessions));
    } catch (_) { /* ignore */ }
    return sessions;
  }

  function pinArchived(id) {
    var sessions = listArchived();
    for (var i = 0; i < sessions.length; i++) {
      if (sessions[i].id === id) { sessions[i].pinned = true; break; }
    }
    // 重排:pinned 在前
    var pinned = [];
    var unpinned = [];
    for (var j = 0; j < sessions.length; j++) {
      if (sessions[j].pinned) pinned.push(sessions[j]);
      else unpinned.push(sessions[j]);
    }
    try {
      localStorage.setItem(SESSIONS_ARCHIVE_KEY, JSON.stringify(pinned.concat(unpinned)));
    } catch (_) { /* ignore */ }
  }

  function unpinArchived(id) {
    var sessions = listArchived();
    for (var i = 0; i < sessions.length; i++) {
      if (sessions[i].id === id) { sessions[i].pinned = false; break; }
    }
    var pinned = [];
    var unpinned = [];
    for (var j = 0; j < sessions.length; j++) {
      if (sessions[j].pinned) pinned.push(sessions[j]);
      else unpinned.push(sessions[j]);
    }
    try {
      localStorage.setItem(SESSIONS_ARCHIVE_KEY, JSON.stringify(pinned.concat(unpinned)));
    } catch (_) { /* ignore */ }
  }

  function renameArchived(id, title) {
    var sessions = listArchived();
    for (var i = 0; i < sessions.length; i++) {
      if (sessions[i].id === id) {
        sessions[i].title = title || sessions[i].title;
        break;
      }
    }
    try {
      localStorage.setItem(SESSIONS_ARCHIVE_KEY, JSON.stringify(sessions));
    } catch (_) { /* ignore */ }
    return sessions;
  }

  function getAll() {
    return listArchived();
  }

  function clearAll() {
    try {
      localStorage.removeItem(SESSIONS_ARCHIVE_KEY);
      // H4: 清除所有 chat session 键
      for (var i = 0; i < sessionStorage.length; i++) {
        var key = sessionStorage.key(i);
        if (key && key.indexOf(CHAT_SESSION_PREFIX) === 0) {
          sessionStorage.removeItem(key);
        }
      }
    } catch (_) { /* ignore */ }
  }

  // H5: 关闭 chat 标签时调用,把会话归档后清除
  function archiveAndClear(tabId) {
    var messages = loadCurrent(tabId);
    if (messages && messages.length) {
      archiveCurrent(messages);
    }
    clearCurrent(tabId);
  }

  window.LqdChatSession = {
    loadCurrent: loadCurrent,
    saveCurrent: saveCurrent,
    clearCurrent: clearCurrent,
    appendToCurrent: appendToCurrent,
    archiveCurrent: archiveCurrent,
    archiveAndClear: archiveAndClear,
    listArchived: listArchived,
    getAll: getAll,
    clearAll: clearAll,
    restoreArchived: restoreArchived,
    removeArchived: removeArchived,
    pinArchived: pinArchived,
    unpinArchived: unpinArchived,
    renameArchived: renameArchived,
    MAX_ARCHIVED: MAX_ARCHIVED
  };
})();
