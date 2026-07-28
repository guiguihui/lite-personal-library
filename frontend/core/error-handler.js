/**
 * LQ-D — Global Error Handler(全局错误捕获,零依赖,必须最先加载)
 *
 * 背景:"Cannot read properties of undefined (reading 'match')" 这类错误
 * 此前修了 4 次都靠猜根因,因为错误冒泡到 UI 时只剩 e.message,堆栈丢失。
 * 本模块做两件事:
 *   1. 捕获 window error / unhandledrejection,把完整堆栈打到 console
 *      (WebView2 debug 模式 F12 可见;浏览器同理)
 *   2. 暴露 LqdErrors.format/report 给各模块,在错误展示处附带堆栈
 *
 * 零依赖:不引用任何其他 LQD 模块,defer 脚本中排第一个加载即可。
 */
(function () {
  'use strict';

  /**
   * 把任意异常格式化为 "message\nstack" 字符串。
   * @param {*} err
   * @returns {string}
   */
  function format(err) {
    if (err == null) return '(unknown error)';
    if (err.stack) return err.message ? err.message + '\n' + err.stack : String(err.stack);
    if (err.message) return String(err.message);
    try {
      return String(err);
    } catch (_) {
      return '(unprintable error)';
    }
  }

  /**
   * 打印完整堆栈到 console。
   * @param {*} err
   * @param {string} where 来源模块标识(如 'sendMessage' / 'renderKatex')
   */
  function report(err, where) {
    console.error('[LQ-D:' + (where || 'global') + ']\n' + format(err));
  }

  /** 安装全局捕获(幂等)。 */
  function install() {
    if (window.__lqdErrorHandlerInstalled) return;
    window.__lqdErrorHandlerInstalled = true;
    window.addEventListener('error', function (ev) {
      // 资源加载失败(script/link)的 error 事件没有 .error,只有 message
      report(ev.error || ev.message || '(resource load error)', 'onerror');
    });
    window.addEventListener('unhandledrejection', function (ev) {
      report(ev.reason, 'unhandledrejection');
    });
  }

  install();
  window.LqdErrors = { format: format, report: report, install: install };
})();
