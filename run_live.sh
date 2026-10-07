#!/usr/bin/env bash
# Start your Flask backend and expose it to the Worker through a Cloudflare Tunnel.
# While this runs, the site behaves exactly like your always-running local server (live search, endless scroll,
# live TV). When it is off, the Worker automatically falls back to the GitHub-Actions cache.
#
#   ./run_live.sh            -> quick tunnel (random URL, changes every start)
#   TUNNEL=mytunnel ./run_live.sh   -> named tunnel (stable URL, see LIVE_MODE.md)
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-5000}"

python app.py &
APP=$!
trap 'kill $APP 2>/dev/null || true' EXIT
sleep 3

if [ -n "${TUNNEL:-}" ]; then
  cloudflared tunnel run --url "http://localhost:$PORT" "$TUNNEL"
else
  echo "Copy the https://*.trycloudflare.com URL below into the BACKEND_URL GitHub secret, then re-run the workflow."
  cloudflared tunnel --url "http://localhost:$PORT"
fi
