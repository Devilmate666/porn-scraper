#!/usr/bin/env bash
# Finds (or creates) the two KV namespaces and writes their ids to $GITHUB_OUTPUT.
# Needs CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID in the environment.
set -euo pipefail
API="https://api.cloudflare.com/client/v4/accounts/${CLOUDFLARE_ACCOUNT_ID}/storage/kv/namespaces"
AUTH="Authorization: Bearer ${CLOUDFLARE_API_TOKEN}"

LIST=""
for i in 1 2 3; do                               # Cloudflare's API has the odd hiccup: retry
  LIST=$(curl -sS --retry 3 --retry-delay 2 -H "$AUTH" "$API?per_page=100" || true)
  echo "$LIST" | jq -e '.success == true' >/dev/null 2>&1 && break
  echo "KV list attempt $i failed: ${LIST:0:200}"; sleep $((i * 3))
done
echo "$LIST" | jq -e '.success == true' >/dev/null || { echo "KV list failed"; exit 1; }

ensure_ns() {
  local name="$1" id
  # accept old names too, so existing namespaces (and their data) are reused
  id=$(echo "$LIST" | jq -r --arg a "$name" --arg b "porn-archive-api-$name" --arg c "porn-archive-$name" \
    '[.result[] | select(.title==$a or .title==$b or .title==$c)][0].id // empty')
  if [ -z "$id" ]; then
    echo "Creating namespace porn-archive-$name" >&2
    id=$(curl -sS -X POST -H "$AUTH" -H "Content-Type: application/json" \
      -d "{\"title\":\"porn-archive-$name\"}" "$API" | jq -r '.result.id // empty')
  fi
  [ -n "$id" ] || { echo "Could not find/create namespace $name" >&2; exit 1; }
  echo "$id"
}
CACHE_ID=$(ensure_ns CACHE)
SCRAPE_ID=$(ensure_ns SCRAPE_DATA)
echo "CACHE=$CACHE_ID SCRAPE_DATA=$SCRAPE_ID"
echo "cache_ns_id=$CACHE_ID" >> "$GITHUB_OUTPUT"
echo "scrape_ns_id=$SCRAPE_ID" >> "$GITHUB_OUTPUT"
