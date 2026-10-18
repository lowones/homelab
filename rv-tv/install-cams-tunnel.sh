#!/bin/bash
# Keeps the ssh tunnel to go2rtc (on oracle) up so the camera pages work: a launchd agent that starts at
# login, restarts the tunnel when it drops (Starlink, oracle reboot) and retries every 15 s.
#   bash install-cams-tunnel.sh          install / reinstall
#   bash install-cams-tunnel.sh --print  show the launchd plist and change nothing
#   bash install-cams-tunnel.sh --remove stop and remove it
# Env: TV_CAMS_SSH_HOST (ssh alias or host of oracle, default oracle-r), TV_CAMS_PORT (default 1984).
set -euo pipefail
HOST="${TV_CAMS_SSH_HOST:-oracle-r}"
PORT="${TV_CAMS_PORT:-1984}"
LABEL=com.lowone.camtunnel
DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$DIR/.run/camtunnel.log"

render() {
cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/ssh</string>
    <string>-N</string>
    <string>-o</string><string>BatchMode=yes</string>
    <string>-o</string><string>ExitOnForwardFailure=yes</string>
    <string>-o</string><string>ConnectTimeout=15</string>
    <string>-o</string><string>ServerAliveInterval=15</string>
    <string>-o</string><string>ServerAliveCountMax=3</string>
    <string>-L</string><string>127.0.0.1:$PORT:127.0.0.1:$PORT</string>
    <string>$HOST</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>15</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
EOF
}

DOMAIN="gui/$(id -u)"
drop() {   # unload and WAIT until launchd has really dropped it (bootstrapping too early fails with error 5)
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  for _ in $(seq 1 20); do
    launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 || break
    sleep 0.5
  done
}

case "${1:-}" in
  --print) render; exit 0 ;;
  --remove) drop; rm -f "$PLIST"; echo "removed $LABEL"; exit 0 ;;
  "") ;;
  *) echo "usage: $0 [--print|--remove]"; exit 2 ;;
esac

echo "[1/4] can this Mac ssh to $HOST without a password?"
if ! /usr/bin/ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST" true; then
  echo "ssh $HOST failed. The agent runs ssh unattended, so it needs key login (no password prompt)."
  echo "Check:  ssh $HOST   works by hand, and ~/.ssh/config has the host and its IdentityFile."
  exit 1
fi

mkdir -p "$DIR/.run" "$HOME/Library/LaunchAgents"
render > "$PLIST"
plutil -lint "$PLIST" >/dev/null
echo "[2/4] wrote $PLIST"

drop
# A tunnel started by hand would hold the port and make the agent fail forever.
pkill -f "ssh .*-L [^ ]*$PORT:127.0.0.1:$PORT" 2>/dev/null || true
sleep 1
if ! launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null; then
  sleep 3
  launchctl bootstrap "$DOMAIN" "$PLIST" || {
    echo "launchctl bootstrap failed twice. Run this in Terminal on the Mac itself:"
    echo "the per-user launchd domain ($DOMAIN) needs a logged-in desktop session."
    exit 1
  }
fi
echo "[3/4] agent loaded (starts at login, restarts itself)"

for _ in $(seq 1 20); do
  /usr/bin/nc -z 127.0.0.1 "$PORT" 2>/dev/null && break
  sleep 0.5
done
if /usr/bin/nc -z 127.0.0.1 "$PORT" 2>/dev/null; then
  echo "[4/4] tunnel is up on 127.0.0.1:$PORT"
  if curl -fs -m 5 "http://127.0.0.1:$PORT/api" >/dev/null; then echo "     go2rtc on $HOST answers"
  else echo "     WARNING: the tunnel is up but go2rtc does not answer (is it running on oracle? journalctl -u go2rtc)"; fi
else
  echo "[4/4] WARNING: nothing listening on 127.0.0.1:$PORT yet. See $LOG"
  exit 1
fi
