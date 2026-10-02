#!/usr/bin/env bash
#
# serve.sh — Run the dashboard behind gunicorn and put it online through a
# Cloudflare tunnel.
#
#   ./serve.sh            # gunicorn on 127.0.0.1:$UI_PORT + public tunnel
#   ./serve.sh --local    # gunicorn only, no tunnel
#
# Two kinds of tunnel, picked from .env:
#
#   CLOUDFLARE_TUNNEL_TOKEN set   a named tunnel: a fixed hostname you own,
#                                 configured in the Cloudflare dashboard
#                                 (Zero Trust -> Networks -> Tunnels; route the
#                                 public hostname to http://localhost:5050)
#   not set                       a quick tunnel: a random *.trycloudflare.com
#                                 URL that changes on every start. Written to
#                                 logs/tunnel-url.txt so it can be found.
#
# The tunnel refuses to open without DASHBOARD_PASSWORD: the dashboard would
# be public, and its Crawl button can spend Bright Data credit.
#
# The app keeps the crawl subprocess state in memory, so it MUST run as a
# single worker; threads give it the concurrency to serve status polls while a
# crawl runs. If either process dies the script exits non-zero, so launchd
# (scripts/install-launchd.sh) restarts both together.

set -euo pipefail
# gunicorn resolves "encar.dashboard:app" against the working directory.
cd "$(dirname "$0")"

# .env is read by Python, which already knows its quoting rules.
env_value() {
    python3 -c "import os, encar.config; print(os.getenv('$1') or '')" 2>/dev/null
}

PORT="${UI_PORT:-$(env_value UI_PORT)}"
PORT="${PORT:-5050}"
THREADS="${UI_THREADS:-8}"
TIMEOUT="${UI_TIMEOUT:-180}"
URL_FILE="logs/tunnel-url.txt"
mkdir -p logs

MODE="${1:-tunnel}"
if [ "$MODE" != "--local" ]; then
    if [ -z "$(env_value DASHBOARD_PASSWORD)" ]; then
        echo "DASHBOARD_PASSWORD is not set in .env — refusing to put the dashboard" >&2
        echo "online without a login. Set it, or run ./serve.sh --local." >&2
        exit 1
    fi
    command -v cloudflared >/dev/null 2>&1 || {
        echo "cloudflared not installed (brew install cloudflared)" >&2; exit 1; }
fi

# gunicorn only warns and retries when the port is taken, so the tunnel would
# come up pointing at whatever else is listening. Refuse instead.
if lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Port ${PORT} is already in use:" >&2
    lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN >&2
    exit 1
fi

echo "Starting gunicorn on 127.0.0.1:${PORT} (1 worker, ${THREADS} threads)"
python3 -m gunicorn \
    --bind "127.0.0.1:${PORT}" \
    --workers 1 \
    --threads "${THREADS}" \
    --timeout "${TIMEOUT}" \
    --access-logfile - \
    --error-logfile - \
    encar.dashboard:app &
GUNICORN_PID=$!
TUNNEL_PID=""

cleanup() {
    [ -n "${TUNNEL_PID}" ] && kill "${TUNNEL_PID}" 2>/dev/null || true
    kill "${GUNICORN_PID}" 2>/dev/null || true
    wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Wait for it to answer (401 counts: it is up, it just wants a login).
for _ in $(seq 1 20); do
    code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/api/crawl/status" || true)"
    [ "$code" = "200" ] || [ "$code" = "401" ] && break
    kill -0 "${GUNICORN_PID}" 2>/dev/null || { echo "gunicorn died on startup" >&2; exit 1; }
    sleep 1
done
echo "Local:  http://127.0.0.1:${PORT}"

if [ "$MODE" = "--local" ]; then
    wait "${GUNICORN_PID}"
    exit $?
fi

TOKEN="$(env_value CLOUDFLARE_TUNNEL_TOKEN)"
if [ -n "$TOKEN" ]; then
    echo "Opening the named Cloudflare tunnel (hostname set in the Cloudflare dashboard)"
    echo "named tunnel — see the Cloudflare dashboard for the hostname" > "$URL_FILE"
    TUNNEL_TOKEN="$TOKEN" cloudflared tunnel --no-autoupdate run &
    TUNNEL_PID=$!
else
    echo "Opening a quick tunnel (URL changes on every start)"
    : > "$URL_FILE"
    # Into a file rather than a pipe, so $! is cloudflared itself and the
    # cleanup above can stop it.
    cloudflared tunnel --url "http://127.0.0.1:${PORT}" --no-autoupdate \
        > logs/tunnel.log 2>&1 &
    TUNNEL_PID=$!
fi

# bash 3.2 has no `wait -n`: poll, and leave as soon as either side is gone.
while kill -0 "${GUNICORN_PID}" 2>/dev/null && kill -0 "${TUNNEL_PID}" 2>/dev/null; do
    if [ ! -s "$URL_FILE" ]; then
        url="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' logs/tunnel.log 2>/dev/null | head -1 || true)"
        if [ -n "$url" ]; then
            echo "$url" > "$URL_FILE"
            echo "Public: $url   (also in $URL_FILE)"
        fi
    fi
    sleep 2
done
echo "A process exited — stopping so the supervisor can restart both" >&2
exit 1
