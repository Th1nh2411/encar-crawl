#!/usr/bin/env bash
#
# install-launchd.sh — Keep the dashboard online on this Mac.
#
#   scripts/install-launchd.sh               # dashboard + tunnel, always on
#   scripts/install-launchd.sh --uninstall
#
# One LaunchAgent, ~/Library/LaunchAgents/com.encar.dashboard.plist: serve.sh
# (gunicorn + Cloudflare tunnel), started at login and restarted whenever it
# exits. There is no scheduled crawl: crawls start from the dashboard.
#
# Log: logs/serve.log. The quick-tunnel URL: logs/tunnel-url.txt.
# The site only runs while the Mac is awake: a sleeping Mac
# is an offline site. Set its sleep settings accordingly.

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
DASH="com.encar.dashboard"
# Removed on install too: earlier versions also scheduled a daily crawl.
CRAWL="com.encar.crawl"

while [ $# -gt 0 ]; do
    case "$1" in
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

mkdir -p "$AGENTS" "$ROOT/logs"

# launchd starts with a bare PATH; cloudflared lives in Homebrew's.
ENV_PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

cat > "$AGENTS/$DASH.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$DASH</string>
  <key>ProgramArguments</key><array>
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


launchctl bootout "gui/$(id -u)/$CRAWL" 2>/dev/null || true
rm -f "$AGENTS/$CRAWL.plist"

for label in "$DASH"; do
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
    # bootout returns before the old job is gone; bootstrapping over it fails
    # with "5: Input/output error".
    for _ in $(seq 1 20); do
        launchctl list "$label" >/dev/null 2>&1 || break
        sleep 0.5
    done
    launchctl bootstrap "gui/$(id -u)" "$AGENTS/$label.plist"
done

echo "Installed $DASH -> dashboard + tunnel, always on (logs/serve.log)"
echo "Public URL: cat $ROOT/logs/tunnel-url.txt   (may take ~10s to appear)"
