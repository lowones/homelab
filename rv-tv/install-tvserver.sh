#!/bin/bash
# Install the RV TV service on the Mac mini. Run as your normal user (no sudo), from the
# repo checkout:   cd ~/dev/homelab/rv-tv && bash install-tvserver.sh
# Safe to re-run: keeps an existing token and tv.env.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL=com.lowone.tvserver
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
cd "$DIR"

[ -f tvserver.py ] || { echo "tvserver.py not found in $DIR"; exit 1; }
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
echo "[1/4] python deps installed"

if [ ! -f tv_key.txt ]; then
  echo "TV is not paired yet. Run:  .venv/bin/python tvserver.py pair   (accept the prompt on the TV), then re-run this."
  exit 1
fi

ts_ip=""
for t in tailscale /Applications/Tailscale.app/Contents/MacOS/Tailscale; do
  if command -v "$t" >/dev/null 2>&1 || [ -x "$t" ]; then
    ts_ip=$("$t" ip -4 2>/dev/null | head -n1 || true)
    [ -n "$ts_ip" ] && break
  fi
done

if [ ! -f tv.env ]; then
  token=$(openssl rand -hex 24)
  bind=${ts_ip:-127.0.0.1}
  umask 077
  cat > tv.env <<EOF
TV_IP=192.168.1.201
TV_MAC=ec:31:11:a2:e5:16
TV_TOKEN=$token
TV_BIND=$bind
TV_SERVER_PORT=8780
EOF
  echo "[2/4] wrote tv.env (token generated, bind=$bind)"
  [ -n "$ts_ip" ] || echo "     WARNING: Tailscale IP not found, bound to 127.0.0.1 only. Edit TV_BIND in tv.env once Tailscale is up."
else
  echo "[2/4] tv.env already exists, left as is"
fi
chmod 600 tv.env
chmod +x tv 2>/dev/null || true

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$DIR/.venv/bin/python</string>
    <string>$DIR/tvserver.py</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$DIR/tvserver.log</string>
  <key>StandardErrorPath</key><string>$DIR/tvserver.log</string>
</dict>
</plist>
EOF
plutil -lint "$PLIST" >/dev/null
echo "[3/4] wrote $PLIST"

# Unload any running copy and WAIT until launchd has really dropped it. Bootstrapping while
# the old one is still tearing down fails with "Bootstrap failed: 5: Input/output error".
DOMAIN="gui/$(id -u)"
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
for _ in $(seq 1 20); do
  launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 || break
  sleep 0.5
done
if ! launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null; then
  sleep 3   # one retry; the first attempt can still lose the race
  launchctl bootstrap "$DOMAIN" "$PLIST" || {
    echo "launchctl bootstrap failed twice."
    echo "If you are connected over SSH, run this script in Terminal on the Mac itself:"
    echo "the per-user launchd domain ($DOMAIN) needs a logged-in desktop session."
    exit 1
  }
fi
echo "[4/4] service loaded"

# Location agent: refreshes .run/location.raw every 10 min from CoreLocationCLI so the weather
# follows the Mac's own position. Needs:  brew install --cask corelocationcli  and
# System Settings > Privacy & Security > Location Services > allow CoreLocationCLI.
if command -v CoreLocationCLI >/dev/null 2>&1 || [ -x /opt/homebrew/bin/CoreLocationCLI ]; then
  LLABEL=com.lowone.tvlocation
  LPLIST="$HOME/Library/LaunchAgents/$LLABEL.plist"
  mkdir -p "$DIR/.run"
  chmod +x "$DIR/locationagent.sh"
  cat > "$LPLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LLABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$DIR/locationagent.sh</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>600</integer>
  <key>StandardOutPath</key><string>$DIR/.run/locationagent.log</string>
  <key>StandardErrorPath</key><string>$DIR/.run/locationagent.log</string>
</dict>
</plist>
EOF
  plutil -lint "$LPLIST" >/dev/null
  launchctl bootout "$DOMAIN/$LLABEL" 2>/dev/null || true
  sleep 1
  launchctl bootstrap "$DOMAIN" "$LPLIST" \
    && echo "     location agent loaded (writes $DIR/.run/location.raw)" \
    || echo "     WARNING: could not load $LLABEL; weather will follow the schedule only"
else
  echo "     CoreLocationCLI not found: weather follows the schedule (brew install --cask corelocationcli to use the Mac's location)"
fi

set -a; . ./tv.env; set +a
sleep 3
echo
echo "Health check:"
curl -fsS "http://$TV_BIND:$TV_SERVER_PORT/health" || echo "(not answering yet; see $DIR/tvserver.log)"
echo
echo "URL:    http://$TV_BIND:$TV_SERVER_PORT"
echo "Token:  $TV_TOKEN"
echo "Try:    ./tv status"
