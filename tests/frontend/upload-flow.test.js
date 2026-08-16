const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

function source(relative) {
  return fs.readFileSync(path.join(__dirname, '../..', relative), 'utf8');
}

test('browser files use multipart upload while native paths keep full endpoint', () => {
  const upload = source('frontend/upload/upload.js');
  assert.match(upload, /new FormData\(\)/);
  assert.match(upload, /endpoint = '\/api\/ingest\/upload'/);
  assert.match(upload, /endpoint = '\/api\/ingest\/full'/);
  assert.match(upload, /pollBuild\(itemId, buildJobId\)/);
});

test('manage form delegates browser bytes to the same multipart endpoint', () => {
  const manage = source('frontend/manage/manage.js');
  assert.match(manage, /fetch\('\/api\/ingest\/upload'/);
  assert.doesNotMatch(manage, /fetch\('\/api\/ingest\/full'[\s\S]{0,180}file\.name/);
  assert.doesNotMatch(manage, /\.docx/);
});
