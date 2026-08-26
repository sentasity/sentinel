# sentinel

Sentry alert receiver and unattended investigation engine. Start with [README.md](README.md) for what this is and how it works. The documentation itself lives on the site under [website/](website/), which is its canonical home; the four files under `docs/` are one-line pointers to it.

## Commands

```bash
.venv/bin/python -m pytest -q
```

Run it before every push. CI runs the same suite on 3.12 and 3.13 plus a
gitleaks scan (`.github/workflows/`), but it is a backstop, not the place to
find out. `pre-commit install` (once per clone) runs gitleaks and the
public-tree test on every commit; keep it installed, since a pushed branch on
a public repo is published even if it never merges. Deploys are manual
`cdk deploy`, see [infra/README.md](infra/README.md).

The documentation site is a separate Node project:

```bash
cd website && npm install && npm run dev
```

Before pushing a change under `website/`, run its two checkers, which CI also runs:

```bash
cd website && npx astro build && node --test scripts/ && node scripts/check-sidebar.mjs && node scripts/check-links.mjs
```

## Docs discipline

Update the docs in the same change as the code, always. Everything below is under
`website/src/content/docs/`:

- Pipeline or behavior changes: update `operate/architecture.mdx` and `how-it-works.mdx`, and README.md if the high-level story shifts.
- Security-model changes: update `security-model.mdx`. It is the page an evaluator reads first, and it must stay traceable to what the code actually does.
- New or changed credentials, runbooks, or operational checks: update `operate/runbooks.mdx`.
- Setup-flow changes: update `deploy/microsoft.mdx` or `deploy/sentry.mdx` (tests assert these name only real SSM parameters).
- Config changes: update `deploy/configure.mdx`, which quotes `config/receiver.yaml.example`'s own comments.
- Card renderer or findings reply shape changes: regenerate the fixtures with `.venv/bin/python scripts/preview_card.py --out fixtures/cards`. `evidence.mdx` renders those files directly, so they are site content as much as test fixtures, and `tests/test_card_goldens.py` fails if they and the renderer disagree.
- Adding a page: add it to `website/sidebar.config.mjs` too. There is no autogeneration, and `check-sidebar.mjs` fails the build on a page in no group or an entry with no page.
- README.md stays high-level and timeless: no status lines, issue numbers, or decision history. That context belongs in Claude memories and the tracking epic.

Nothing published on the site may name a real deployment: no live org slug, integration slug, endpoint, channel id, project name, or credential-manager path. The fixtures describe a fictional `acme-tools/checkout` deployment for the same reason. `tests/test_public_tree.py` scans every tracked file, the site included.

## Brand

`docs/brand/` holds the generators; every asset under it is built, not hand-edited.
The adopted mark is `perch`. Regenerate with `cd docs/brand && python3 build_assets.py`,
`python3 build_header.py`, and `python3 build_social_card.py`. The owl's colours are
defined twice, in `gen_round3.py` and in `build_header.py`'s inlined `OWL` constant, so
a palette change has to be made in both.
