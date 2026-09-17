#!/usr/bin/env bash
#
# serve.sh — Run the dashboard behind gunicorn and expose it through a
# Cloudflare quick tunnel.
#
#   ./serve.sh            # gunicorn on 127.0.0.1:$UI_PORT + public tunnel URL
#   ./serve.sh --local    # gunicorn only, no tunnel
#
# The app keeps the crawl subprocess state in memory, so it MUST run as a
# single worker. Threads (not workers) give it the concurrency it needs to
# serve status polls while a crawl is running -- a second worker would have its
# own empty state and report "no crawl running" at random.
#
# The tunnel reaches gunicorn on loopback, so the app itself is never bound to
# a public interface. Anyone with the URL can use it, including the Crawl
# button: that is deliberate here, and it is why the URL should be treated as
# a secret. Ctrl-C stops both processes and the public URL dies with them.

set -euo pipefail
# app.py lives in stock_sync/ together with its templates/, and gunicorn
# resolves "app:app" against the working directory. The app itself anchors its
# data paths on the project root, so it does not care where it is started from.
cd "$(dirname "$0")/stock_sync"

PORT="${UI_PORT:-5050}"
WORKERS=1
THREADS="${UI_THREADS:-8}"
TIMEOUT="${UI_TIMEOUT:-180}"   # a crawl request returns fast, but be generous

command -v cloudflared >/dev/null 2>&1 || CLOUDFLARED_MISSING=1

# gunicorn only warns and retries when the port is taken, so the tunnel would
# come up pointing at whatever else is listening. Refuse instead.
if lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Port ${PORT} is already in use:" >&2
    lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN >&2
    echo "Stop it first, or set UI_PORT to another port." >&2
    exit 1
fi

echo "Starting gunicorn on 127.0.0.1:${PORT} (${WORKERS} worker, ${THREADS} threads)"
python3 -m gunicorn \
    --bind "127.0.0.1:${PORT}" \
    --workers "${WORKERS}" \
    --threads "${THREADS}" \
    --timeout "${TIMEOUT}" \
    --access-logfile - \
    --error-logfile - \
    app:app &
GUNICORN_PID=$!

cleanup() {
    echo
    echo "Shutting down..."
    [ -n "${TUNNEL_PID:-}" ] && kill "${TUNNEL_PID}" 2>/dev/null || true
    kill "${GUNICORN_PID}" 2>/dev/null || true
    wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Wait for it to actually answer, not merely to have been spawned.
for _ in $(seq 1 20); do
    if curl -fsS -o /dev/null "http://127.0.0.1:${PORT}/api/crawl/status" 2>/dev/null; then
        break
    fi
    kill -0 "${GUNICORN_PID}" 2>/dev/null || { echo "gunicorn died on startup" >&2; exit 1; }
    sleep 1
done
echo "Local:  http://127.0.0.1:${PORT}"

if [ "${1:-}" = "--local" ]; then
    wait "${GUNICORN_PID}"
    exit 0
fi

if [ -n "${CLOUDFLARED_MISSING:-}" ]; then
    echo "cloudflared not installed - running locally only (brew install cloudflared)" >&2
    wait "${GUNICORN_PID}"
    exit 0
fi

echo "Opening Cloudflare tunnel..."
cloudflared tunnel --url "http://127.0.0.1:${PORT}" --no-autoupdate 2>&1 |
    tee /tmp/encar-tunnel.log &
TUNNEL_PID=$!

wait "${GUNICORN_PID}"
