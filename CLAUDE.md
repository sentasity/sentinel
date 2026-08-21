# sentinel

Sentry alert receiver and unattended investigation engine. Start with [README.md](README.md) for what this is and how it works; [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the component walkthrough and security model; [docs/OPERATIONS.md](docs/OPERATIONS.md) for runbooks.

## Commands

```bash
.venv/bin/python -m pytest -q
```

That's the only gate: this repo has no CI. Deploys are manual `cdk deploy`, see [infra/README.md](infra/README.md).

## Docs discipline

Update the docs in the same change as the code, always:

- Pipeline, behavior, or security-model changes: update docs/ARCHITECTURE.md, and README.md if the high-level story shifts.
- New or changed credentials, runbooks, or operational checks: update docs/OPERATIONS.md.
- Setup-flow changes: update docs/SETUP-MICROSOFT.md or docs/SETUP-SENTRY.md (tests assert these name only real SSM parameters).
- README.md stays high-level and timeless: no status lines, issue numbers, or decision history. That context belongs in Claude memories and the tracking epic.
