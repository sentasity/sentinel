import { test } from 'node:test';
import assert from 'node:assert/strict';

import { isInternal, candidatesFor } from './check-links.mjs';

test('isInternal: only /sentinel-rooted links count', () => {
  assert.equal(isInternal('/sentinel/get-started/'), true);
  assert.equal(isInternal('/sentinel'), true);
  assert.equal(isInternal('https://github.com/sentasity/sentinel'), false);
  assert.equal(isInternal('/other/'), false);
  assert.equal(isInternal('#section'), false);
  assert.equal(isInternal('mailto:x@example.com'), false);
});

test('candidatesFor: trailing-slash route → index.html', () => {
  assert.deepEqual(candidatesFor('/get-started/'), ['get-started/index.html']);
});

test('candidatesFor: extensionless route → index.html or page.html', () => {
  assert.deepEqual(candidatesFor('/reference/config'), [
    'reference/config/index.html',
    'reference/config.html',
  ]);
});

test('candidatesFor: explicit asset keeps its extension', () => {
  assert.deepEqual(candidatesFor('/_astro/common.css'), ['_astro/common.css']);
  assert.deepEqual(candidatesFor('/sitemap-index.xml'), ['sitemap-index.xml']);
});

test('candidatesFor: base root → index.html', () => {
  assert.deepEqual(candidatesFor(''), ['index.html']);
  assert.deepEqual(candidatesFor('/'), ['index.html']);
});

test('candidatesFor: query and fragment are stripped before resolving', () => {
  assert.deepEqual(candidatesFor('/brainstorm/#gates'), ['brainstorm/index.html']);
  assert.deepEqual(candidatesFor('/reference/c-plan?x=1'), [
    'reference/c-plan/index.html',
    'reference/c-plan.html',
  ]);
});
