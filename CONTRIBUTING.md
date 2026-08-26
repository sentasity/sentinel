# Contributing

Sentinel watches a Sentry project and investigates new issues unattended. The
[README](README.md) has the overview, and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
has the component walkthrough and the security model.

A note on expectations, since this is a small project maintained alongside
other work. Issues are welcome and I read all of them. Pull requests are
welcome too, though for anything beyond a small fix, please open an issue
first. The roadmap lives elsewhere and I'd rather you didn't build something
that's already half-written. No promises on response time either way.

## Running the tests

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

Any Python 3.12 or newer works; `pyproject.toml` enforces that. The Lambda
runs 3.12 and CI covers 3.12 and 3.13, so either is a fine place to develop.

That's the whole gate, and it covers the receiver logic, the Teams card
goldens, the CDK synthesis, and the stored prompts. CI runs the same suite on
every push and pull request, plus a secret scan, so a green run locally should
mean a green run there.

## Pre-commit hooks

```bash
.venv/bin/pre-commit install
```

One-time, per clone. Every commit then runs gitleaks and the public-tree
invariants (`tests/test_public_tree.py`) before it lands, which matters here
because pushing any branch to a public repository is publishing. CI runs the
same two checks, so the hooks cost you nothing you wouldn't hit later anyway;
they just move the failure to before the content leaves your machine.
