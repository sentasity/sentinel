# Deploying the receiver

Manual `cdk deploy` for now; engine-repo CI is a follow-up.

## Prerequisites

- AWS credentials for the target AWS account, region `us-east-1`.
- Docker running (OrbStack is fine): the Lambda asset is bundled in the
  `public.ecr.aws/sam/build-python3.12` image.
- `config/receiver.yaml` has no `REPLACE_WITH_*` values left. `app.py`
  runs `assert_ready` before synth and refuses otherwise.
- The three SecureStrings exist: `scripts/put-parameters.sh`.

## Deploy

```bash
uv venv --python 3.12 .venv-cdk
uv pip install --python .venv-cdk/bin/python -r infra/requirements.txt
cd infra && npx cdk@2 deploy sentinel-receiver
```

The stack outputs `FunctionUrl`. Its `/sentry` route goes into the Sentry
internal integration (`docs/SETUP-SENTRY.md`); its `/bot` route goes into the
Azure Bot messaging endpoint (`docs/SETUP-MICROSOFT.md`).

## Health check

```bash
BASE=$(aws cloudformation describe-stacks \
  --stack-name sentinel-receiver \
  --query 'Stacks[0].Outputs[?OutputKey==`FunctionUrl`].OutputValue' \
  --output text)
curl -fsS "${BASE}health"
```

Expected: `ok`.

## Rollback

The alert table is `RETAIN`, so `cdk destroy` leaves posted-message history
intact. To fall back to Sentry's stock Teams cards without touching AWS at all,
run `scripts/migrate_rules.py rollback` (see `docs/SETUP-SENTRY.md`).
