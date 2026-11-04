#!/usr/bin/env bash
# Write the receiver's SecureString parameters into the target AWS account.
#
# Values are read from the environment, never from arguments, so nothing lands
# in shell history or a process listing. Run once per environment, and again
# whenever a secret is rotated.
#
#   SENTRY_WEBHOOK_SECRET=... SENTRY_API_TOKEN=... BOT_CLIENT_SECRET=... \
#     ROUTINE_TRIGGER_TOKEN=... GITHUB_APP_PRIVATE_KEY=... scripts/put-parameters.sh
#
# NTFY_TOPIC is optional: set it only when `aws.alarm_ntfy_server` is
# configured, to write the topic the alarm forwarder publishes to.
set -euo pipefail

PREFIX="${SSM_PREFIX:-/sentinel}"
REGION="${AWS_REGION:-us-east-1}"

put() {
  local key="$1" value="$2"
  if [[ -z "$value" ]]; then
    echo "error: $key is unset in the environment" >&2
    exit 1
  fi
  aws ssm put-parameter \
    --name "${PREFIX}/${key}" \
    --type SecureString \
    --value "$value" \
    --overwrite \
    --region "$REGION" >/dev/null
  echo "wrote ${PREFIX}/${key}"
}

put sentry-webhook-secret "${SENTRY_WEBHOOK_SECRET:-}"
put sentry-api-token "${SENTRY_API_TOKEN:-}"
put bot-client-secret "${BOT_CLIENT_SECRET:-}"
put routine-trigger-token "${ROUTINE_TRIGGER_TOKEN:-}"
put github-app-private-key "${GITHUB_APP_PRIVATE_KEY:-}"
if [[ -n "${NTFY_TOPIC:-}" ]]; then
  put ntfy-topic "$NTFY_TOPIC"
fi

echo "done. Verify with: aws ssm get-parameters-by-path --path ${PREFIX} --region ${REGION}"
