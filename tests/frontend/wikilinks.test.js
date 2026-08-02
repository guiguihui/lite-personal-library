const test = require('node:test');
const assert = require('node:assert/strict');

global.window = {};
global.document = { addEventListener: function () {} };
const wikilinks = require('../../frontend/shared/wikilinks.js');

test('preprocess renders canonical links and protects code', function () {
  const html = wikilinks.preprocess('[[paper:p#Heading|Paper]] and `[[note:no]]`');
  assert.match(html, /data-target="paper:p"/);
  assert.match(html, /data-anchor="Heading"/);
  assert.match(html, />Paper<\/a>/);
  assert.match(html, /`\[\[note:no\]\]`/);
});

test('split keeps aliases and heading anchors', function () {
  assert.deepEqual(wikilinks.split('note:a#Part|Alias'), { target: 'note:a', anchor: 'Part', alias: 'Alias' });
});
