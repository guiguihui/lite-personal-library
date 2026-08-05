const test = require('node:test');
const assert = require('node:assert/strict');

test('chat retrieval uses only /api/search and preserves backend order', async function () {
  const requested = [];
  global.window = { LQD_CHAT_BASE: '' };
  global.fetch = async function (url) {
    requested.push(url);
    return {
      ok: true,
      async json() {
        return {
          query: 'alpha',
          results: [
            {
              doc_type: 'note', slug: 'first', node_id: '0001',
              title: 'First', breadcrumb: 'First > Section', text: 'one', score: 2
            },
            {
              doc_type: 'note', slug: 'second', node_id: '0002',
              title: 'Second', breadcrumb: 'Second > Section', text: 'two', score: 1
            }
          ]
        };
      }
    };
  };

  require('../../frontend/chat/agent.js');
  const result = await window.LqdChatAgent.retrieveContext('alpha');

  // 默认范围(书籍+论文+笔记全勾选)会显式带上 doc_types 过滤参数
  assert.deepEqual(requested, ['/api/search?q=alpha&limit=12&doc_types=books%2Cpapers%2Cnotes']);
  assert.deepEqual(result.contexts.map((item) => item.docId), ['first', 'second']);
  assert.equal(requested.some((url) => /pageindex|inverted-index|chunks\.json/.test(url)), false);
});
