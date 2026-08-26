# Operations

Runbooks for the credentials the engine holds and the checks that prove the pipeline still works. Deploying the receiver itself is covered in [infra/README.md](../infra/README.md); the one-time Microsoft and Sentry setup lives in [SETUP-MICROSOFT.md](SETUP-MICROSOFT.md) and [SETUP-SENTRY.md](SETUP-SENTRY.md).

## Credential inventory

| Credential | Where it lives | Purpose |
|---|---|---|
| `AUTOFIX_APP_PRIVATE_KEY` | Actions secret, engine repo | Mints the short-lived App installation token used to check out the target repo and publish the PR |
| `CLAUDE_CODE_OAUTH_TOKEN` | Actions secret, engine repo | Authenticates the unattended autofix session and the disclosure scan against the Claude subscription |
| `AUTOFIX_APP_ID` | Actions variable, engine repo | The App ID passed to `create-github-app-token` |
| `/sentinel/github-app-private-key` | SSM SecureString, receiver | The same App private key, read by the receiver at cold start |
| `SENTRY_WEBHOOK_SECRET`, `SENTRY_API_TOKEN`, `BOT_CLIENT_SECRET`, `ROUTINE_TRIGGER_TOKEN` | SSM SecureStrings, receiver | Webhook verification, Sentry API access, the bot identity, and the routine fire token |
| Callback capability tokens | DynamoDB, hashed, one per dispatch | Bearer credential the autofix workflow presents to `/autofix-result`; scoped to a single dispatch, not reusable |

The routine trigger token also has a copy in 1Password and is never stored in this repo.

## Runbook: rotating the GitHub App private key

1. Generate a second private key in the GitHub App settings. GitHub Apps allow two active keys at once, so this rotation is zero-downtime: the old key keeps authenticating while the new one rolls out.
2. Update the key everywhere it lives, one place at a time:
   - **1Password:** update the stored copy manually.
   - **Actions secret** (mints the App installation token the workflow uses to check out the target repo and publish PRs):
     ```bash
     gh secret set AUTOFIX_APP_PRIVATE_KEY --repo sentasity/sentinel < new-private-key.pem
     ```
     (or via the repo's Settings > Secrets and variables > Actions in the web UI)
   - **SSM parameter** (read by the receiver at cold start). `scripts/put-parameters.sh` writes all five receiver secrets together and its `put()` helper hard-exits the moment any of them is unset, so it is not a safe tool for rotating this one key alone unless you also have the other four ready to supply in the same run. Update the parameter directly instead:
     ```bash
     aws ssm put-parameter \
       --name /sentinel/github-app-private-key \
       --type SecureString \
       --value "$GITHUB_APP_PRIVATE_KEY" \
       --overwrite \
       --region us-east-1
     ```
3. Confirm the new key is actually the one in place, not just that GitHub accepts requests (both keys stay valid until you delete one, so a dispatch succeeding proves nothing about which key was used):
   - SSM: `aws ssm get-parameter --name /sentinel/github-app-private-key --with-decryption --region us-east-1 --query Parameter.Value --output text`, and diff the output against the new key file.
   - Actions secret: GitHub never returns a secret's value, so check `gh secret list --repo sentasity/sentinel` and confirm `AUTOFIX_APP_PRIVATE_KEY`'s `Updated` column reflects the change you just made.
   - Run the proof point below (or wait for a real dispatch) and confirm the "Mint App installation token" step and the Publish step both succeed, i.e. a PR actually opens.
4. Only once all three checks in step 3 pass, delete the old key from the App settings.

## Runbook: renewing the Claude OAuth token

Run `claude setup-token` on an operator machine and paste the output into the `CLAUDE_CODE_OAUTH_TOKEN` Actions secret (engine repo).

Expiry does not fail the "Run the fix session" step: `runner/autofix_runner.py`'s `main()` wraps the session in a broad exception handler and always returns 0, so that step shows green in the Actions UI even on an auth failure. What actually fails is downstream: `result.json` never reaches a `verified` status, `scripts/autofix_publish.py` reports `failed`, and the callback posts the "Autofix failed" thread reply. To diagnose, don't trust the session step's pass/fail badge; check the run's `::error::` annotations (the session step logs `autofix session crashed: <exc>` on a crash) and the Publish step's log, which prints the actual final status on its last line.

## The disclosure scan

`.github/workflows/disclosure-scan.yml` runs one read-only Claude session over the tree and reports what a public repository should not have handed a stranger: live credentials, real tenant and organization identifiers, deployment endpoints, named people, and the inventories that turn a runbook into a deployment map. It also flags security weaknesses in the change itself, such as an unpinned action or a credential reaching a step that does not need it.

It is the third gate, not the only one, and the other two stay authoritative for what they cover: gitleaks (`secret-scan.yml`) matches credential shapes, and `tests/test_public_tree.py` bans specific strings. Those catch what someone already thought to describe. This one reads.

**Scope.** Pull requests and pushes to `main` scan what the diff touches. A manual run scans the whole tracked tree:

```bash
gh workflow run disclosure-scan.yml -f scope=full
```

Run the full sweep before publishing anything new from the repo, and after any change to what the docs describe.

**Reading a result.** High-severity findings fail the job; medium and low are annotations only, because a model's judgment call should be read in review rather than block a merge. Findings name a file and a line and never quote the value: the job's log, its step summary, and the pull request annotations are all public, and reprinting a leaked secret there publishes it a second time. To see the actual value, open the file at the cited line.

**Accepted disclosures** live in [`.github/disclosure-allowlist.md`](../.github/disclosure-allowlist.md). Adding an entry changes what a public repository is willing to expose, so it gets reviewed like any other change. The scan reads that file as data: a pull request that tries to widen it into blanket permission is reported rather than obeyed.

**Known gaps.**

- Pull requests from forks cannot be scanned: GitHub does not give a fork's run the OAuth token, so the session has nothing to authenticate with. The job fails such a run rather than skipping it, because GitHub records a skipped job as "skipped" and branch protection counts a skipped required check as satisfied, which would let a fork's pull request merge looking scanned when nothing read it. Review the change by hand, or re-run the scan from a branch in this repository; either way the scan of record is the push to `main` after the merge.
- An expired `CLAUDE_CODE_OAUTH_TOKEN` fails the job loudly (unlike the autofix session, which returns 0 regardless). So does a session that ends without emitting a result block. Both are re-runnable; neither is ever reported as a pass, because a scan that did not run must not look like a scan that found nothing.

## Proof point: the autofix pipeline end to end

Re-run this after any change to `.github/workflows/autofix.yml` to confirm the pipeline still fires end to end. It dispatches a real workflow run and, on success, opens a real PR, so don't run it casually.

```bash
TARGET_REPO="$AUTOFIX_TARGET_OWNER/$AUTOFIX_TARGET_NAME"
RELEASE_SHA=$(gh api "repos/$TARGET_REPO/commits/$AUTOFIX_TARGET_BRANCH" --jq .sha)
jq -n --arg sha "$RELEASE_SHA" '{
  "event_type": "autofix",
  "client_payload": {
    "dispatch_id": "proofpoint-manual",
    "sentry_issue_id": "0",
    "sentry_short_id": "PROOF-1",
    "project": "example-project",
    "environment": "staging",
    "release_sha": $sha,
    "cited_files": ["path/to/a/real/file.py"],
    "findings_md": "Root cause: <one paragraph naming a file, a symbol, and a line range in the target repo>. Fix: <the concrete change to make>. Confidence high, fixability high.",
    "callback_url": "REPLACE_WITH_FUNCTION_URL/autofix-result",
    "callback_token": "proofpoint-invalid-token"
  }
}' | gh api repos/sentasity/sentinel/dispatches --input -
```

Export `AUTOFIX_TARGET_OWNER`, `AUTOFIX_TARGET_NAME`, and `AUTOFIX_TARGET_BRANCH` to
the same values the repository variables carry before running this; unset, they
expand to an empty repository name and the `gh api` call fails.

`cited_files` must name a file that really exists in the target repo at that
SHA: the workflow diffs exactly those paths to build the drift context, and a
path that does not resolve produces an empty drift patch rather than an error.

## Runbook: replaying an alert the receiver has already seen

Sentry will not re-send an alert for a known issue. New-issue rules fire on `first_seen` only, and a repeat event on an unchanged release finds the investigation row and stops before any Sentry call. So when you need the receiver to process a known issue again, after fixing something in the pipeline for example, sign a webhook body and post it yourself:

```bash
python -m scripts.replay_alert --issue 1000000007 --environment prod --dry-run
```

The body is `fixtures/sentry-webhook-alert.json` with the target issue's fields written over it from its stored `alert:<environment>` row, so you can only replay an issue the receiver has already posted. The signature comes from `receiver.handler.sign_body`, the same function `/sentry` verifies against, and the webhook secret is read from SSM rather than the environment so it never lands in shell history.

Start with `--dry-run`. It prints the exact body and target and sends nothing. A live replay posts a real card to that environment's real Teams channel.

Two rows make a replay a no-op for a repeat, and `--reset` deletes both before posting:

- `investigation:<env>#<release>` stops the enqueue, so no session fires and no findings arrive.
- `autofix:<env>#<release>` stops the autofix gate one stage later, as "already attempted for this release".

Each delete is conditional on the row existing, and the script prints `deleted:` or `absent:` per row. That distinction is the point: an unconditional `delete-item` against a mistyped sort key succeeds silently and reads as a successful clear. The `alert:<environment>` row is never touched, because it is the replay's own input.

Without `--reset`, a repeat issue posts a fresh card and nothing else happens. That is the right mode for a freshly staged error that has no rows yet.

## Configuration probe

The prompts in `prompts/` are templates. `REPLACE_WITH_FUNCTION_URL` stands in
for the receiver's Function URL and must be substituted with the real origin
when the prompt is pasted into the routine. The session cross-checks that
origin against the `SENTINEL_RECEIVER_URL` environment variable, so a prompt
pasted with the placeholder left in place fails the check and posts nothing.

The routines API exposes no configuration read, so the routine's repository binding and connector set can only be verified by firing a session that reports on itself (`prompts/probe.md`, reporting to `/findings/probe`; `scripts/probe_routine.py` fires it). Connector drift is not detected automatically: re-run the probe whenever the routine or the account's connectors change.

**The routine holds one prompt at a time, so probing is a swap.** In normal operation the routine carries `prompts/investigator.md`. Firing `scripts/probe_routine.py` against it does *not* run the probe: the script sends `{"probe": true}`, the investigator sees a payload missing all five required fields (`project`, `issue_ids`, `release`, `batch_id`, `reply_token`), and correctly refuses to interpret it, reporting a malformed fire instead. That is the investigator behaving as designed, not a probe result.

To actually run the probe:

1. Replace the routine's prompt with the contents of `prompts/probe.md`.
2. Run `ROUTINE_ID=... ROUTINE_TRIGGER_TOKEN=... scripts/probe_routine.py`.
3. Read the session transcript alongside the receiver's log group for the `/findings/probe` POST. The probe's pass condition is agreement between the two; a POST with no matching transcript proves nothing, which is why `handle_probe` can be unauthenticated.
4. Restore `prompts/investigator.md` as the routine's prompt. Until you do, live alerts fire a probe session instead of an investigation.

Step 4 is the one that bites: leaving the probe prompt in place silently degrades every subsequent investigation.

A malformed-fire report is still useful even though it is not a probe. It confirms the receiver URL the routine actually holds, that egress reaches that host, and that `/findings` rejects an unauthenticated POST, without touching the prompt at all. When all you need is "does the routine still point at the right receiver", fire the script and read the report rather than doing the swap.

## Falling back to stock Sentry alerts

To fall back to Sentry's stock Teams cards without touching AWS at all, run `scripts/migrate_rules.py rollback` (see [SETUP-SENTRY.md](SETUP-SENTRY.md)). The alert table is retained on stack delete, so `cdk destroy` leaves posted-message history intact.
