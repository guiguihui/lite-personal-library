const test = require('node:test');
const assert = require('node:assert/strict');

global.window = {};
const ids = require('../../frontend/core/tab-ids.js');

test('restored numeric tab ids advance the allocator', function () {
  ids.reset();
  ids.reserve('tab-1');
  ids.reserve('tab-2');
  assert.equal(ids.next(function () { return false; }), 'tab-3');
});

test('allocator skips an id that already exists', function () {
  ids.reset();
  const id = ids.next(function (candidate) { return candidate === 'tab-1' || candidate === 'tab-2'; });
  assert.equal(id, 'tab-3');
});

test('nonstandard restored ids do not disturb numeric allocation', function () {
  ids.reset();
  ids.reserve('library-home');
  assert.equal(ids.next(function () { return false; }), 'tab-1');
});
