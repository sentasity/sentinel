import { test } from 'node:test';
import assert from 'node:assert/strict';

import { findDangling, findOrphans } from './check-sidebar.mjs';

// Allowlist mirrors the production allowlist in check-sidebar.mjs: the splash
// landing page (`index`) and the 404 page are intentionally absent from the
// sidebar and must never be reported as orphans.
const ALLOWLIST = new Set(['index', '404']);

test('clean case: sidebar and pages agree → no dangling, no orphans', () => {
  const pageSlugs = new Set([
    'index',
    'course/quickstart/01-install',
    'course/design',
    'reference/c-design',
  ]);
  const sidebarSlugs = new Set([
    'course/quickstart/01-install',
    'course/design',
    'reference/c-design',
  ]);

  assert.deepEqual(findDangling(pageSlugs, sidebarSlugs), []);
  assert.deepEqual(findOrphans(pageSlugs, sidebarSlugs, ALLOWLIST), []);
});

test('dangling sidebar entry: slug in sidebar with no page on disk is reported', () => {
  const pageSlugs = new Set([
    'index',
    'course/design',
  ]);
  const sidebarSlugs = new Set([
    'course/design',
    'course/removed-page', // referenced by sidebar but file was deleted
  ]);

  assert.deepEqual(findDangling(pageSlugs, sidebarSlugs), ['course/removed-page']);
});

test('orphaned page: page on disk not reachable from any sidebar group is reported', () => {
  const pageSlugs = new Set([
    'index',
    'course/design',
    'reference/c-new', // new page, author forgot to add to sidebar (autogen miss)
  ]);
  const sidebarSlugs = new Set([
    'course/design',
  ]);

  assert.deepEqual(findOrphans(pageSlugs, sidebarSlugs, ALLOWLIST), ['reference/c-new']);
});

test('allowlisted pages (index/splash, 404) are NOT flagged as orphaned', () => {
  const pageSlugs = new Set([
    'index', // splash, intentionally not in sidebar
    '404',   // 404 page, intentionally not in sidebar
    'course/design',
  ]);
  const sidebarSlugs = new Set([
    'course/design',
  ]);

  assert.deepEqual(findOrphans(pageSlugs, sidebarSlugs, ALLOWLIST), []);
});

test('orphan detection is sorted and deduplicated', () => {
  const pageSlugs = new Set([
    'reference/c-zeta',
    'reference/c-alpha',
    'course/design',
  ]);
  const sidebarSlugs = new Set(['course/design']);

  assert.deepEqual(
    findOrphans(pageSlugs, sidebarSlugs, new Set()),
    ['reference/c-alpha', 'reference/c-zeta'],
  );
});
