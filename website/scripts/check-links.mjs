#!/usr/bin/env node
// Internal link + asset checker for the Sentinel docs site.
//
// The site is built with `base: '/sentinel/'`, so every internal link in the
// emitted HTML is rooted at `/sentinel/...`. GitHub Pages serves `dist/` AT that
// base, i.e. `/sentinel/foo/` maps to `dist/foo/index.html` (the `/sentinel/`
// prefix is the serving root, not a `dist/sentinel/` subdirectory). This checker
// resolves each internal link against `dist/` accordingly and fails on any that
// don't map to a built file. External links (http/https) and non-base paths are
// ignored, matching the design's "gate internal links only" decision.
//
// Pure functions are exported for unit testing; main() wires them to the dist
// filesystem and exits non-zero on any broken link.

import { readdir, readFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const WEBSITE_ROOT = path.resolve(SCRIPT_DIR, '..');
const DIST = path.join(WEBSITE_ROOT, 'dist');
const BASE = '/sentinel/';

// --- Pure logic (unit-testable) --------------------------------------------

// Is this an internal link rooted at the site base? (Not external, not a
// mailto:/tel:/#fragment-only/data: link.)
export function isInternal(href) {
  return href === '/sentinel' || href.startsWith(BASE);
}

// Map an internal href to the list of candidate dist-relative file paths that
// would satisfy it. The caller checks existence. `rel` is the href with the
// `/sentinel` prefix already removed (so it starts with `/` or is empty).
export function candidatesFor(rel) {
  let p = rel.replace(/[?#].*$/, '').replace(/^\/+/, ''); // drop query/fragment + leading slash
  if (p === '') return ['index.html'];
  if (p.endsWith('/')) return [`${p}index.html`];
  if (path.posix.extname(p)) return [p]; // explicit file / asset (.css, .svg, .xml, ...)
  return [`${p}/index.html`, `${p}.html`]; // extensionless route
}

// --- Filesystem plumbing ----------------------------------------------------

async function walkHtml(dir) {
  const out = [];
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...(await walkHtml(full)));
    else if (entry.name.endsWith('.html')) out.push(full);
  }
  return out;
}

async function main() {
  if (!existsSync(DIST)) {
    console.error(`check-links: dist not found at ${DIST}. Run \`astro build\` first.`);
    process.exit(2);
  }

  const htmlFiles = await walkHtml(DIST);
  const attrRe = /(?:href|src)="([^"]*)"/g;
  const broken = new Map(); // href -> Set(source files)

  for (const file of htmlFiles) {
    const html = await readFile(file, 'utf8');
    const source = path.relative(DIST, file);
    let m;
    while ((m = attrRe.exec(html)) !== null) {
      const href = m[1];
      if (!isInternal(href)) continue;
      const rel = href.slice('/sentinel'.length); // keep the remainder's leading slash
      const ok = candidatesFor(rel).some((c) => existsSync(path.join(DIST, c)));
      if (!ok) {
        if (!broken.has(href)) broken.set(href, new Set());
        broken.get(href).add(source);
      }
    }
  }

  if (broken.size === 0) {
    console.log(`check-links: OK — all internal links resolve (${htmlFiles.length} pages scanned).`);
    return;
  }

  console.error(`check-links: ${broken.size} broken internal link(s):\n`);
  for (const [href, sources] of [...broken].sort()) {
    console.error(`  ${href}`);
    for (const s of [...sources].sort()) console.error(`      ← ${s}`);
  }
  process.exit(1);
}

// Run main() only when invoked directly (so the pure exports stay importable).
if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  await main();
}
