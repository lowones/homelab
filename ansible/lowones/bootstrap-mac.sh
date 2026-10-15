#!/bin/bash
# Reverse tunnel for the RV Mac mini (replaces zero/Pi). Run: sudo bash bootstrap-mac.sh
# Safe to re-run: skips the user and key if they already exist. Does NOT start the tunnel.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }

VPS_HOST=service.low-ones.com
VPS_PORT=7822
VPS_FP='SHA256:paXSB5ITzgYaU1YZI3SGojr97XV8a43Fl+CNmCmKbpA'
TUN_PORT=8677
SVC=_rvtunnel
DIR=/var/db/rvtunnel
LABEL=com.lowones.rvtunnel
PLIST=/Library/LaunchDaemons/$LABEL.plist

# 1. hidden service user: no shell, no password
if ! dscl . -read /Users/$SVC >/dev/null 2>&1; then
  uid=399
  if dscl . -list /Users UniqueID | awk -v u=$uid '$2==u{f=1} END{exit !f}'; then
    echo "uid $uid is taken, edit the script"; exit 1
  fi
  dscl . -create /Users/$SVC
  dscl . -create /Users/$SVC UserShell /usr/bin/false
  dscl . -create /Users/$SVC RealName "RV tunnel"
  dscl . -create /Users/$SVC UniqueID $uid
  dscl . -create /Users/$SVC PrimaryGroupID 20
  dscl . -create /Users/$SVC NFSHomeDirectory /var/empty
  dscl . -create /Users/$SVC Password '*'
  dscl . -create /Users/$SVC IsHidden 1
  echo "[1/5] created user $SVC"
else
  echo "[1/5] user $SVC already exists"
fi

# 2. key (reused if it already exists)
install -d -o $SVC -g staff -m 700 $DIR
if [ ! -f $DIR/id_ed25519 ]; then
  (cd / && sudo -u $SVC ssh-keygen -q -t ed25519 -N '' -C "rvtunnel@mini" -f $DIR/id_ed25519)
  echo "[2/5] generated key"
else
  echo "[2/5] key already exists"
fi

# 3. pin the VPS host key by fingerprint, abort if nothing matches
scan=$(mktemp); one=$(mktemp)
ssh-keyscan -T 10 -p $VPS_PORT $VPS_HOST 2>/dev/null | grep -v '^#' > "$scan" || true
: > $DIR/known_hosts
while IFS= read -r line || [ -n "$line" ]; do
  [ -n "$line" ] || continue
  printf '%s\n' "$line" > "$one"
  fp=$(ssh-keygen -lf "$one" 2>/dev/null | awk '{print $2}') || true
  if [ "$fp" = "$VPS_FP" ]; then
    printf '%s\n' "$line" >> $DIR/known_hosts
  fi
done < "$scan"
rm -f "$scan" "$one"
if [ ! -s $DIR/known_hosts ]; then
  echo "VPS host key did not match the pinned fingerprint ($VPS_FP), aborting"
  exit 1
fi
chown $SVC:staff $DIR/known_hosts; chmod 600 $DIR/known_hosts
echo "[3/5] pinned VPS host key"

# 4. launchd daemon (written, not loaded)
cat > $PLIST <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>UserName</key><string>$SVC</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/ssh</string>
    <string>-N</string>
    <string>-F</string><string>/dev/null</string>
    <string>-i</string><string>$DIR/id_ed25519</string>
    <string>-o</string><string>IdentitiesOnly=yes</string>
    <string>-o</string><string>IdentityAgent=none</string>
    <string>-o</string><string>BatchMode=yes</string>
    <string>-o</string><string>UserKnownHostsFile=$DIR/known_hosts</string>
    <string>-o</string><string>StrictHostKeyChecking=yes</string>
    <string>-o</string><string>ExitOnForwardFailure=yes</string>
    <string>-o</string><string>ConnectTimeout=20</string>
    <string>-o</string><string>ServerAliveInterval=15</string>
    <string>-o</string><string>ServerAliveCountMax=3</string>
    <string>-R</string><string>localhost:$TUN_PORT:localhost:22</string>
    <string>-p</string><string>$VPS_PORT</string>
    <string>sshtunnel@$VPS_HOST</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>60</integer>
  <key>StandardOutPath</key><string>$DIR/rvtunnel.log</string>
  <key>StandardErrorPath</key><string>$DIR/rvtunnel.log</string>
</dict>
</plist>
EOF
chown root:wheel $PLIST; chmod 644 $PLIST
plutil -lint $PLIST
echo "[4/5] wrote $PLIST (not loaded)"

# 5. never sleep, power back on after outage
for s in "sleep 0" "disksleep 0" "womp 1" "autorestart 1" "powernap 0" "tcpkeepalive 1"; do
  pmset -a $s || echo "pmset $s not supported here, skipped"
done
echo "[5/5] power settings applied"

echo
echo "Add this public key on the VPS (step 2):"
cat $DIR/id_ed25519.pub
echo
echo "Mac sshd host key (verify on first connect):"
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
