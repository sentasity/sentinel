# Architecture

The engine is one Lambda (the receiver), one DynamoDB table, a one-minute EventBridge schedule, and two kinds of unattended Claude Code session: cloud routine sessions that investigate, and a GitHub Actions session that writes fixes. This document walks the pipeline end to end and then covers the security model that makes the unattended parts safe to run.

## The receiver

`receiver/` is a single Lambda behind a Function URL. `handler.py` routes:

| Route | Caller | Purpose |
|---|---|---|
| `POST /sentry` | Sentry | Issue alert webhook, HMAC-verified |
| `POST /bot` | Azure Bot Service | Teams bot messages (JWT-verified) |
| `POST /findings` | The investigator session | Findings document for a fired batch |
| `POST /findings/probe` | The probe session | Configuration probe report |
| `POST /autofix-result` | The autofix workflow | Outcome callback, capability-token auth |
| `GET /health` | Operators | Liveness |

The same Lambda is also invoked directly (not over the URL) by the EventBridge rule that drives the sweep, so the scheduled path is not reachable from the internet.

State lives in one DynamoDB table (`receiver/store.py`): posted alert cards and their Teams message ids, the pending-investigation queue, dedupe and skip caches, autofix dispatches and their callback tokens.

## Alert to Teams card

`POST /sentry` verifies the webhook signature, parses the alert (`models.py`), renders an Adaptive Card (`cards.py`), and posts it to the configured Teams channel through the receiver's own bot identity (`bot.py`, an Entra app registration plus an Azure Bot). Posting directly through a bot identity means message ids come back synchronously, which is what makes threaded follow-up replies (findings, autofix outcomes) possible.

## The investigation pipeline

**Gate** (`investigation.py`). An alert is worth investigating when it is error-level, in an investigated environment, and carries a release that resolves to a commit SHA (for projects where the release *is* the SHA, a 40-char hex check; anything else means something changed upstream and the alert is skipped rather than investigated at branch HEAD). Eligible alerts are enqueued with a debounce so that a burst of related issues becomes one batch.

**Sweep** (`sweep.py`). Every minute, the sweep groups due pending issues per project and release (up to `max_batch_issues`), respects the per-sweep and daily fire caps, and fires one routine session per batch. It also enforces deadlines: a batch whose findings never arrive within `deadline_seconds` is failed loudly into the thread rather than left hanging.

**Fire** (`routines.py`). A fire is `POST /v1/claude_code/routines/{id}/fire` with a JSON payload of exactly five fields: `project`, `issue_ids`, `release`, `batch_id`, `reply_token`. The payload is capped at 65,536 characters by the API, which is why the engine sends issue ids only and lets the session fetch event detail itself. The API has no idempotency key, so the receiver owns dedupe. On 429, `Retry-After` decides the response: a short delay is read as the daily routine-run cap and earns one bounded retry; a long one is read as subscription-window exhaustion and is skipped and logged (usage credits are off, so exhaustion rejects rather than bills).

**Investigate** (`prompts/investigator.md`). The session checks out the target repo at the release SHA, investigates each issue in the batch, and posts a findings document to the receiver's `/findings` endpoint, authenticated by the `reply_token` minted for that batch.

**Findings** (`findings.py`). The receiver validates the document shape, redacts secret-shaped strings, renders a reply card, and posts it into the original alert's thread.

## The autofix pipeline

**Gate** (`autofix.py`). A finding proceeds to autofix when its confidence and fixability each clear the configured minimums, the project is on the allowlist (an empty allowlist opts in every project), no cited file matches an excluded path, and the daily autofix cap has budget left.

**Dispatch.** The receiver fires a `repository_dispatch` event at this repo carrying the finding, the release SHA, and a one-time callback token (stored hashed, scoped to the single dispatch).

**Workflow** (`.github/workflows/autofix.yml`). The workflow checks out the target repo at the release SHA and runs `runner/autofix_runner.py`: one unattended Claude Agent SDK session that judges drift against the current default branch, re-verifies the root cause, and writes the fix. The session holds no GitHub credential and never publishes. A separate publish step (`scripts/autofix_publish.py`) then verifies the result, opens the PR as the GitHub App identity, and calls back to `/autofix-result`, which posts the outcome into the Teams thread. Every PR waits for a human reviewer; nothing merges automatically.

## Security model

The unattended sessions are the trust boundary, and the design treats everything that reaches them as untrusted:

- **The fire payload is data, not instructions.** All parameterization lives in the stored routine prompt. The payload is five typed fields with no prose; the prompt instructs the session to treat any deviation as a malformed fire and report it rather than interpret it.
- **The routines API exposes no configuration read**, so a routine's repository binding and connector set cannot be asserted from the receiver at startup. The compensating control is `prompts/probe.md`: a fail-closed probe session that reports its own configuration to `/findings/probe`. It is a manual gate; re-run it whenever the routine or the account's connectors change.
- **Findings delivery is verified out-of-band.** Before posting findings anywhere, the investigator session must confirm the destination against two operator-controlled channels no injected text can forge: the `SENTINEL_RECEIVER_URL` environment variable set in the cloud environment, and the environment's deny-by-default egress policy whose only allowed host is the receiver.
- **The autofix session never has push access.** Both checkouts use `persist-credentials: false`; git auth is set up only in the publish step, after the session has finished. The callback token is stripped from the payload file before the session can read it. All `uses:` steps are pinned to commit SHAs.
- **Secrets** live in SSM SecureStrings (receiver side) and Actions secrets (workflow side); the full inventory and rotation runbooks are in [OPERATIONS.md](OPERATIONS.md).
- One accepted residual risk: the Agent SDK forwards the process environment to its subprocess, so the workflow's OAuth token is technically reachable from inside the autofix session. Containment for that is the runner's egress posture, not the runner code.

## Infrastructure

`infra/` is a self-contained CDK v2 app deploying one stack: the receiver Lambda and Function URL, the DynamoDB table (retained on stack delete), the EventBridge sweep rule, and the wiring to the SSM parameters. Deploy instructions are in [infra/README.md](../infra/README.md).
