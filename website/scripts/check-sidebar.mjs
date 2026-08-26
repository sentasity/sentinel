#!/usr/bin/env node
// Sidebar / page consistency checker for the Sentinel docs site.
//
// Cross-references the Starlight `sidebar` config in astro.config.mjs against
// the real page set under src/content/docs/. Reports two failure classes and
// exits non-zero if either is non-empty:
//   - Dangling sidebar entry: a slug the sidebar references but no page exists.
//   - Orphaned page: a page on disk reachable from no sidebar group.
//
// Pure functions (findDangling/findOrphans/normalizeSlug/flattenSidebar) are
// exported and unit-tested in check-sidebar.test.mjs; main() wires them to the
// filesystem and astro.config.mjs.

import { readdir } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const WEBSITE_ROOT = path.resolve(SCRIPT_DIR, '..');
const DOCS_ROOT = path.join(WEBSITE_ROOT, 'src', 'content', 'docs');
const SIDEBAR_CONFIG_PATH = path.join(WEBSITE_ROOT, 'sidebar.config.mjs');

// Pages intentionally absent from the sidebar; never reported as orphans.
const ALLOWLIST = new Set(['index', '404']);

const CONTENT_EXTENSIONS = new Set(['.md', '.mdx']);

// --- Pure logic (unit-tested) ----------------------------------------------

// Strip leading/trailing slashes and a trailing `/index` so sidebar `link`
// values and autogenerate directories normalize to the same bare slug form as
// derived page slugs (e.g. '/course/design/' -> 'course/design').
export function normalizeSlug(raw) {
  let slug = String(raw).trim();
  slug = slug.replace(/^\/+/, '').replace(/\/+$/, '');
  if (slug === 'index') return 'index';
  if (slug.endsWith('/index')) slug = slug.slice(0, -'/index'.length);
  return slug;
}

// Dangling = referenced by sidebar but no page on disk. Sorted, deduped.
export function findDangling(pageSlugs, sidebarSlugs) {
  const dangling = [];
  for (const slug of sidebarSlugs) {
    if (!pageSlugs.has(slug)) dangling.push(slug);
  }
  return [...new Set(dangling)].sort();
}

// Orphans = page on disk not in the sidebar and not allowlisted. Sorted, deduped.
export function findOrphans(pageSlugs, sidebarSlugs, allowlist) {
  const orphans = [];
  for (const slug of pageSlugs) {
    if (sidebarSlugs.has(slug)) continue;
    if (allowlist.has(slug)) continue;
    orphans.push(slug);
  }
  return [...new Set(orphans)].sort();
}

// Flatten a Starlight `sidebar` array into a Set of normalized slugs.
// Handles three node shapes:
//   - { link: '/course/design/' }            -> normalized link slug
//   - { items: [...] }                       -> recurse into items
//   - { autogenerate: { directory: 'reference' } } -> expand via expandDir
// `expandDir(directory)` returns an array of normalized slugs for that
// directory (async FS in main(); injectable so this stays testable).
export async function flattenSidebar(sidebar, expandDir) {
  const slugs = new Set();
  const visit = async (nodes) => {
    for (const node of nodes) {
      if (node == null) continue;
      if (typeof node.link === 'string') {
        slugs.add(normalizeSlug(node.link));
      }
      if (node.autogenerate && typeof node.autogenerate.directory === 'string') {
        for (const slug of await expandDir(node.autogenerate.directory)) {
          slugs.add(normalizeSlug(slug));
        }
      }
      if (Array.isArray(node.items)) {
        await visit(node.items);
      }
    }
  };
  await visit(Array.isArray(sidebar) ? sidebar : []);
  return slugs;
}

// --- Filesystem + config plumbing ------------------------------------------

// Recursively list .md/.mdx files under `dir`, returning slugs relative to
// DOCS_ROOT (POSIX separators, extension stripped, `index` normalized).
async function listSlugs(dir) {
  const slugs = [];
  let entries;
  try {
    entries = await readdir(dir, { withFileTypes: true });
  } catch (err) {
    if (err.code === 'ENOENT') return slugs;
    throw err;
  }
  for (const entry of entries) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      slugs.push(...(await listSlugs(full)));
      continue;
    }
    const ext = path.extname(entry.name);
    if (!CONTENT_EXTENSIONS.has(ext)) continue;
    const rel = path.relative(DOCS_ROOT, full).split(path.sep).join('/');
    slugs.push(normalizeSlug(rel.slice(0, -ext.length)));
  }
  return slugs;
}

async function main() {
  // 1. Real pages.
  const pageSlugs = new Set(await listSlugs(DOCS_ROOT));

  // 2. Sidebar entries. Import the named `sidebar` export from sidebar.config.mjs.
  //    That file is intentionally free of Astro/Starlight imports so Node can
  //    load it directly (Starlight 0.39+ exposes .ts entry points that Node
  //    cannot evaluate without a TypeScript loader).
  //    Dynamic import is used so the pure functions above remain importable by
  //    the test suite without any filesystem or module side-effects.
  const sidebarModule = await import(pathToFileURL(SIDEBAR_CONFIG_PATH).href);
  let sidebar = sidebarModule.sidebar;
  if (!sidebar) {
    console.error(
      'check-sidebar: could not resolve `sidebar` from sidebar.config.mjs. ' +
        'Ensure sidebar.config.mjs exports `export const sidebar = [...]`.',
    );
    process.exit(1);
  }

  const expandDir = (directory) =>
    listSlugs(path.join(DOCS_ROOT, directory));
  const sidebarSlugs = await flattenSidebar(sidebar, expandDir);

  // 3. Compute failure classes.
  const dangling = findDangling(pageSlugs, sidebarSlugs);
  const orphans = findOrphans(pageSlugs, sidebarSlugs, ALLOWLIST);

  // 4. Report and exit.
  if (dangling.length === 0 && orphans.length === 0) {
    console.log('check-sidebar: OK — sidebar and pages are consistent.');
    return;
  }
  if (dangling.length > 0) {
    console.error('check-sidebar: dangling sidebar entries (no page on disk):');
    for (const slug of dangling) console.error(`  - ${slug}`);
  }
  if (orphans.length > 0) {
    console.error('check-sidebar: orphaned pages (not reachable from the sidebar):');
    for (const slug of orphans) console.error(`  - ${slug}`);
  }
  process.exit(1);
}

// Run main() only when invoked directly (not when imported by the test file).
if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((err) => {
    console.error(err);
    process.exit(1);
  });
}
