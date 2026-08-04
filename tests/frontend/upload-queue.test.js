const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

function loadQueue() {
  const window = {};
  const source = fs.readFileSync(
    path.join(__dirname, '../../frontend/upload/upload-queue.js'),
    'utf8'
  );
  vm.runInNewContext(source, { window, Date });
  return window.YuuUploadQueue;
}

test('clearDone preserves failed attempts', () => {
  const queue = loadQueue();
  const done = queue.add({ name: 'done.pdf' }, null, {});
  const failed = queue.add({ name: 'failed.pdf' }, null, {});
  queue.update(done.id, { status: 'done' });
  queue.update(failed.id, { status: 'failed' });
  queue.clearDone();
  assert.deepEqual(Array.from(queue.all(), (item) => item.id), [failed.id]);
});

test('retry resets a failed item as a new pending attempt', () => {
  const queue = loadQueue();
  const item = queue.add({ name: 'book.epub' }, null, {});
  queue.update(item.id, { status: 'failed', jobId: 'old', log: ['boom'] });
  queue.retry(item.id);
  const retried = queue.get(item.id);
  assert.equal(retried.status, 'pending');
  assert.equal(retried.jobId, null);
  assert.equal(retried.attempt, 2);
  assert.deepEqual(Array.from(retried.log), []);
});

test('counts reports all queue states', () => {
  const queue = loadQueue();
  const item = queue.add({ name: 'a.pdf' }, null, {});
  queue.update(item.id, (current) => ({ status: current.status === 'pending' ? 'running' : current.status }));
  assert.deepEqual({ ...queue.counts() }, { pending: 0, running: 1, done: 0, failed: 0 });
});
