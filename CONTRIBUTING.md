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
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

That's the whole gate. There's no CI, so a green suite locally is what "it
works" means here. It covers the receiver logic, the Teams card goldens, the
CDK synthesis, and the stored prompts.
