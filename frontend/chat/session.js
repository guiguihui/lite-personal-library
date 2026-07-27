/**
 * LQ-D — Chat Session
 *
 * 当前会话（sessionStorage）与归档历史（localStorage）CRUD。
 * 键从旧 yuu_chat_* 迁移到 lqd_chat_*。
 */
(function () {
  'use strict';

  var CHAT_SESSION_KEY = 'lqd_chat_session';
  var SESSIONS_ARCHIVE_KEY = 'lqd_chat_sessions_archive';
  var MAX_ARCHIVED = 20;

  function safeParse(raw) {
    try {
      return raw ? JSON.parse(raw) : null;
    } catch (_) {
      return null;
    }
  }

  function loadCurrent() {
    return safeParse(sessionStorage.getItem(CHAT_SESSION_KEY)) || [];
  }

  function saveCurrent(messages) {
    try {
      sessionStorage.setItem(CHAT_SESSION_KEY, JSON.stringify(messages || []));
    } catch (_) { /* ignore */ }
  }

  function clearCurrent() {
    sessionStorage.removeItem(CHAT_SESSION_KEY);
  }

  function appendToCurrent(role, content) {
    var messages = loadCurrent();
    messages.push({ role: role, content: content });
    if (messages.length > 20) messages = messages.slice(-20);
    saveCurrent(messages);
    return messages;
  }

  function archiveCurrent(messages) {
    messages = messages || loadCurrent();
    if (!messages || !messages.length) return;
    var sessions = listArchived();
    var title = '';
    for (var i = 0; i < messages.length; i++) {
      if (messages[i].role === 'user' && messages[i].content) {
        title = messages[i].content.slice(0, 50);
        break;
      }
    }
    if (!title) title = messages[0] && messages[0].content ? messages[0].content.slice(0, 50) : '(空会话)';
    sessions.unshift({
      id: Date.now().toString(36),
      title: title,
      date: new Date().toISOString(),
      messages: messages.slice()
    });
    if (sessions.length > MAX_ARCHIVED) sessions.length = MAX_ARCHIVED;
    try {
      localStorage.setItem(SESSIONS_ARCHIVE_KEY, JSON.stringify(sessions));
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
    var current = loadCurrent();
    if (current && current.length) archiveCurrent(current);
    saveCurrent((s.messages || []).slice());
    return s;
  }

  function removeArchived(id) {
    var sessions = listArchived().filter(function (x) { return x.id !== id; });
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
      sessionStorage.removeItem(CHAT_SESSION_KEY);
    } catch (_) { /* ignore */ }
  }

  window.LqdChatSession = {
    loadCurrent: loadCurrent,
    saveCurrent: saveCurrent,
    clearCurrent: clearCurrent,
    appendToCurrent: appendToCurrent,
    archiveCurrent: archiveCurrent,
    listArchived: listArchived,
    getAll: getAll,
    clearAll: clearAll,
    restoreArchived: restoreArchived,
    removeArchived: removeArchived,
    MAX_ARCHIVED: MAX_ARCHIVED
  };
})();
