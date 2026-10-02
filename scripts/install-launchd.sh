#!/usr/bin/env bash
#
# install-launchd.sh — Keep the dashboard online and crawl daily, on this Mac.
#
#   scripts/install-launchd.sh                          # crawl bmw_3series_g20 at 06:00
#   scripts/install-launchd.sh --models "bmw_3series_g20 benz" --at 05:30
#   scripts/install-launchd.sh --uninstall
#
# Two LaunchAgents, in ~/Library/LaunchAgents:
#
#   com.encar.dashboard   serve.sh (gunicorn + Cloudflare tunnel), started at
#                         login and restarted whenever it exits. Wrapped in
#                         `caffeinate -is` so the Mac does not idle-sleep while
#                         it is serving -- a sleeping Mac is an offline site.
#   com.encar.crawl       `main.py crawl` for the given models, once a day.
#
# Logs: logs/serve.log, logs/encar.log. The quick-tunnel URL: logs/tunnel-url.txt.
# A closed laptop lid still sleeps the Mac unless it is on power with an
# external display; keep it open, or use a Mac that stays on.

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
DASH="com.encar.dashboard"
CRAWL="com.encar.crawl"
PYTHON="$(command -v python3)"
MODELS="bmw_3series_g20"
AT="06:00"

while [ $# -gt 0 ]; do
    case "$1" in
        --models) MODELS="$2"; shift 2 ;;
        --at) AT="$2"; shift 2 ;;
        --uninstall)
            for label in "$DASH" "$CRAWL"; do
                launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
                rm -f "$AGENTS/$label.plist"
            done
            echo "Removed $DASH and $CRAWL."
            exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

HOUR="$((10#${AT%%:*}))"
MINUTE="$((10#${AT##*:}))"
mkdir -p "$AGENTS" "$ROOT/logs"

# launchd starts with a bare PATH; cloudflared lives in Homebrew's.
ENV_PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

model_args=""
for m in $(echo "$MODELS" | tr ',' ' '); do
    model_args="$model_args<string>--model</string><string>$m</string>"
done

cat > "$AGENTS/$DASH.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$DASH</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/caffeinate</string><string>-is</string>
    <string>$ROOT/serve.sh</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>$ENV_PATH</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$ROOT/logs/serve.log</string>
  <key>StandardErrorPath</key><string>$ROOT/logs/serve.log</string>
</dict></plist>
EOF

cat > "$AGENTS/$CRAWL.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$CRAWL</string>
  <key>ProgramArguments</key><array>
    <string>$PYTHON</string><string>$ROOT/main.py</string><string>crawl</string>
    $model_args
    <string>--log-file</string><string>$ROOT/logs/encar.log</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>$ENV_PATH</string></dict>
  <key>StartCalendarInterval</key><dict>
    <key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>$MINUTE</integer>
  </dict>
  <key>StandardOutPath</key><string>$ROOT/logs/crawl-launchd.log</string>
  <key>StandardErrorPath</key><string>$ROOT/logs/crawl-launchd.log</string>
</dict></plist>
EOF

for label in "$DASH" "$CRAWL"; do
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$AGENTS/$label.plist"
done

echo "Installed:"
echo "  $DASH  -> dashboard + tunnel, always on (logs/serve.log)"
echo "  $CRAWL -> crawl $MODELS daily at $(printf '%02d:%02d' "$HOUR" "$MINUTE") (logs/encar.log)"
echo "Public URL: cat $ROOT/logs/tunnel-url.txt   (may take ~10s to appear)"
