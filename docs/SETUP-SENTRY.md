# Sentry-side setup (manual, one time)

Two things get created by hand in Sentry: the internal integration that
webhooks alerts to the receiver, and the `automation` project the receiver
reports its own errors to. Everything after that is scripted.

## 1. The `sentinel` internal integration

Sentry → Settings → Developer Settings → Custom Integrations → Create New
Integration → **Internal Integration**.

- Name: `sentinel`
- Webhook URL: `<FunctionUrl>sentry` (the CDK stack output, with `sentry` appended)
- Alert Rule Action: **enabled**. This is what makes the integration appear as
  a pickable action inside issue alert rules
- Permissions: **Issue & Event: Read**. Delivery itself needs no scope; the read
  scope is what lets the receiver resolve an issue's short id from the numeric
  id in the webhook body, which the `event_alert` payload does not carry.
- Webhooks: no resource subscriptions needed. Alert-rule deliveries are driven
  by the rules themselves, not by a subscription.
- Small Icon: upload `docs/brand/assets/perch-cream/perch-cream-sentry-256.png`.
  Cosmetic, and only shown in UI components. Sentry accepts black and transparent
  pixels only, which is what that file is for. See
  [docs/brand/assets/README.md](brand/assets/README.md).

Save, then record two values:

- the **Client Secret**. Sentry signs every delivery with
  `sentry-hook-signature`, an HMAC-SHA256 of the raw body under this secret
- the **Token** (auth token), used for the issue short-id lookup

## 2. Store both in SSM

```bash
SENTRY_WEBHOOK_SECRET='<client secret>' \
SENTRY_API_TOKEN='<auth token>' \
BOT_CLIENT_SECRET='<from docs/SETUP-MICROSOFT.md>' \
  scripts/put-parameters.sh
```

Writes `/sentinel/sentry-webhook-secret` and
`/sentinel/sentry-api-token`.

## 3. The `automation` project

Sentry → Projects → Create Project.

- Platform: Python
- Name: `automation`
- Team: the existing Sentasity team

Then Settings → Alerts → configure the default issue alert to notify **by email
only**. Do not attach the `sentinel` action to any rule in this project: the
receiver's own errors must not travel through the receiver.

Copy the project's DSN into `config/receiver.yaml` under
`observability.automation_dsn`, then redeploy so the Lambda picks it up.

## 4. Capture the action template

The migration script writes the sentinel action into each alert, and the exact
JSON Sentry stores for the action is captured rather than assumed.

A note on names and ids before the commands. Sentry's UI and docs call these
**Monitors** and **Alerts**; the API paths call them `detectors` and `workflows`.
They are the same objects: a Monitor decides when an issue is created, an Alert
decides who gets notified. The UI serves alerts from `/monitors/alerts/<id>/`,
and those ids *are* the workflow ids this API uses, unlike the retired rules API
where the two id spaces differed. So an id copied from the address bar is usable
here.

The scanners staging alerts are already migrated, so the template can be read
straight off a live one. If you are ever starting from scratch instead, add the
"Send a notification via sentinel" action to one alert in the UI first, then
run this and remove the hand-added action afterwards.

```bash
curl -sS -H "Authorization: Bearer $SENTRY_ACCESS_TOKEN" \
  https://sentry.io/api/0/organizations/sentasity/workflows/ \
  | python3 -c "import sys,json; wfs=json.load(sys.stdin); acts=[a for w in wfs for af in w['actionFilters'] for a in af['actions'] if a['type']=='webhook']; a=dict(acts[0]); a.pop('id', None); print(json.dumps(a, indent=2))" \
  > fixtures/sentry-workflow-action.json
```

An internal integration with no alert-rule UI schema registers as a plain
`webhook` action keyed by the integration's service slug (`alert-relay-98aa06`),
which is why the selector above matches on `type` rather than on a name.

Sentry mints the service slug from the integration's name at creation and never
changes it, so this deployment keeps `alert-relay-98aa06` after the rename while
a fresh setup gets a `sentinel-*` slug. `scripts/migrate_rules.py` reads the
slug from the live integration rather than assuming either value.

**The `id` is stripped deliberately.** Sentry assigns an id to every action, so a
template that carried one would never compare equal to a live action, duplicate
detection would miss, and a rule already carrying sentinel would end up with
two copies posting every alert twice. `swap_workflow_action` compares type plus
target identifier for the same reason.

Commit that file. Every later `apply` reads its template from it, so no service
slug is ever typed by hand.

## 5. Staged alert swap

Start with the quietest slice and widen only after a real alert renders correctly.
Dry run each slice first: it exercises the whole read and plan path, writes
nothing, and prints exactly what would change.

```bash
export SENTRY_ACCESS_TOKEN=$(op read "op://Sentasity/sentry-admin-user-token/credential")
.venv/bin/python -m scripts.migrate_rules apply --project scanners --environment prod --dry-run
.venv/bin/python -m scripts.migrate_rules apply --project scanners --environment prod
# soak: wait for an organic alert, or force one, and check Prod Alerts
.venv/bin/python -m scripts.migrate_rules apply --project backend-api
.venv/bin/python -m scripts.migrate_rules apply --project frontend
.venv/bin/python -m scripts.migrate_rules apply --project processing
```

The scanners **staging** slice is already applied, which is why the sequence
starts at scanners/prod. Already-migrated alerts are reported as `skipped` rather
than aborting the slice, so re-running a project is safe.

18 alerts in total. The 8 per-developer alerts are never selected: they keep the stock
integration and the dev-notifications channel.

Note the prod slices also drop the `level >= error` condition, so those alerts
begin delivering warnings as well as errors. That is deliberate, but it will
visibly raise volume in Prod Alerts.

Each `apply` prints the backup path it wrote first. Keep those files; they are
the rollback artifact. The filename carries the project, the environment slice,
and a UTC timestamp (`rule-backup-scanners-prod-2026-08-13T221500Z.json`), so the
passes above never overwrite each other. `apply` refuses to start if the backup
path already exists, and refuses to touch a single live alert until every alert
in the slice has planned successfully.

## 6. Rollback drill

Run this once, deliberately, on the first swapped slice, so rollback is proven
rather than assumed. `rollback` takes no `--project`: a workflow id is unique
across the org, and the backup carries it.

```bash
.venv/bin/python -m scripts.migrate_rules rollback \
  --backup fixtures/rule-backup-scanners-prod-<timestamp>.json
# confirm the stock grey card returns in Prod Alerts, then re-apply
.venv/bin/python -m scripts.migrate_rules apply --project scanners --environment prod
```

## Notes on the workflows API

The legacy `/projects/{org}/{project}/rules/` endpoints were retired on
17 August 2026 and return HTTP 410. There are no backwards-compatible endpoints
and no exemptions. Everything above goes through
`/organizations/{org}/workflows/`, documented at
<https://docs.sentry.io/api/monitors/>.

Three things about that API are worth knowing before editing the script, all
established by probing a disposable workflow:

- **The wire format is camelCase.** The docs list body parameters as
  `detector_ids` and `action_filters`, which is the serializer's internal
  naming. Send `detectorIds` and `actionFilters`, matching what GET returns.
- **PUT is a partial update, except for `enabled`.** Omitted keys are left
  alone, so a narrow payload will not unbind an alert from its Monitor. But
  omitting `enabled` resets it to `true`, silently re-enabling a disabled alert.
  It stays in `PUT_KEYS` so that cannot happen.
- **Workflows are org-scoped, not project-scoped.** There is no project in the
  URL. `migrate_rules.py` resolves `--project` by joining the alert's
  `detectorIds` against `/organizations/{org}/detectors/`. Each project has an
  `error` Monitor and an `issue_stream` Monitor; all 18 alerts hang off
  `issue_stream`, and the migration never changes `detectorIds`.
