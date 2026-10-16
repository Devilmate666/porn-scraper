#!/usr/bin/env bash
# Finds (or creates) the D1 database used for accounts + synced favorites and writes its id to $GITHUB_OUTPUT.
# Needs CLOUDFLARE_API_TOKEN (with "D1: Edit") and CLOUDFLARE_ACCOUNT_ID.
# NEVER fails the pipeline: without a database the site still deploys, only the login feature stays off.
set -uo pipefail
NAME="${D1_NAME:-porn-archive-db}"
API="https://api.cloudflare.com/client/v4/accounts/${CLOUDFLARE_ACCOUNT_ID}/d1/database"
AUTH="Authorization: Bearer ${CLOUDFLARE_API_TOKEN}"

ID=""
for i in 1 2 3; do
  RES=$(curl -sS --retry 3 --retry-delay 2 -H "$AUTH" "$API?name=${NAME}&per_page=50" || true)
  if echo "$RES" | jq -e '.success == true' >/dev/null 2>&1; then
    ID=$(echo "$RES" | jq -r --arg n "$NAME" '[.result[] | select(.name==$n)][0].uuid // empty')
    break
  fi
  echo "D1 list attempt $i failed: ${RES:0:200}"; sleep $((i * 3))
done

if [ -z "$ID" ] && echo "${RES:-}" | jq -e '.success == true' >/dev/null 2>&1; then
  echo "Creating D1 database $NAME" >&2
  ID=$(curl -sS -X POST -H "$AUTH" -H "Content-Type: application/json" -d "{\"name\":\"$NAME\"}" "$API" | jq -r '.result.uuid // empty')
fi

if [ -z "$ID" ]; then
  echo "::warning::D1 database unavailable (does the API token have the 'D1: Edit' permission?). Deploying without login/sync."
fi
echo "d1_id=$ID" >> "$GITHUB_OUTPUT"
echo "D1 id: ${ID:-none}"
exit 0
