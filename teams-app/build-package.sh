#!/usr/bin/env bash
# Build the uploadable Teams app package.
#
#   BOT_APP_ID=<entra-app-id> teams-app/build-package.sh
#
# Produces teams-app/sentinel.zip with the bot id substituted in. The two
# icons are committed alongside this script and are generated from
# docs/brand/assets/perch-cream; the script still refuses to build without
# them because Teams rejects the upload at the far end.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${HERE}/sentinel.zip"

if [[ -z "${BOT_APP_ID:-}" ]]; then
  echo "error: BOT_APP_ID is unset" >&2
  exit 1
fi

for icon in color.png outline.png; do
  if [[ ! -f "${HERE}/${icon}" ]]; then
    echo "error: missing ${HERE}/${icon} (see docs/SETUP-MICROSOFT.md)" >&2
    exit 1
  fi
done

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

sed "s/REPLACE_WITH_BOT_APP_ID/${BOT_APP_ID}/g" "${HERE}/manifest.json" \
  > "${STAGE}/manifest.json"
cp "${HERE}/color.png" "${HERE}/outline.png" "${STAGE}/"

rm -f "$OUT"
(cd "$STAGE" && zip -q -r "$OUT" manifest.json color.png outline.png)
echo "built $OUT"
