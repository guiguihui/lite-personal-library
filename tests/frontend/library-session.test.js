const test = require('node:test');
const assert = require('node:assert/strict');

global.window = {};
const sessions = require('../../frontend/library/session.js');

test('sessions isolate document state by tab id', function () {
  const a = sessions.create('tab-a', { type: 'note', slug: 'a' });
  const b = sessions.create('tab-b', { type: 'paper', slug: 'b' });

  assert.equal(a.type, 'notes');
  assert.equal(a.slug, 'a');
  assert.equal(b.type, 'papers');
  assert.equal(b.slug, 'b');
  assert.notEqual(a, b);
});

test('begin invalidates the previous request token', function () {
  const session = sessions.create('tab-request', {});
  const first = sessions.begin(session);
  const second = sessions.begin(session);

  assert.equal(sessions.isCurrent(session, first.version), false);
  assert.equal(sessions.isCurrent(session, second.version), true);
});

test('disposing one session aborts only its request', function () {
  const a = sessions.create('tab-dispose-a', {});
  const b = sessions.create('tab-dispose-b', {});
  const requestA = sessions.begin(a);
  const requestB = sessions.begin(b);

  sessions.dispose('tab-dispose-a');

  assert.equal(requestA.signal && requestA.signal.aborted, true);
  assert.equal(requestB.signal && requestB.signal.aborted, false);
  assert.equal(sessions.get('tab-dispose-a'), null);
  assert.equal(sessions.get('tab-dispose-b'), b);
});
