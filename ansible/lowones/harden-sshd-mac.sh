#!/bin/bash
# Lock down sshd on the RV Mac mini: key-only, one allowed user, no forwarding.
# Run from your normal account: sudo bash harden-sshd-mac.sh
# KEEP ONE SSH SESSION OPEN while you run this, and test a second login before closing it.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
ADMIN=${SUDO_USER:?run with sudo from your normal account}
AK=/Users/$ADMIN/.ssh/authorized_keys
F=/etc/ssh/sshd_config.d/10-hardening.conf

[ -s "$AK" ] || { echo "no $AK for $ADMIN, refusing: you would be locked out"; exit 1; }
echo "keys currently authorized for $ADMIN:"
ssh-keygen -lf "$AK"
grep -q '^Include /etc/ssh/sshd_config.d' /etc/ssh/sshd_config || { echo "sshd_config has no Include for sshd_config.d, aborting"; exit 1; }

[ -f $F ] && cp $F $F.bak
cat > $F <<EOF
PasswordAuthentication no
KbdInteractiveAuthentication no
AuthenticationMethods publickey
PubkeyAuthentication yes
PermitRootLogin no
AllowUsers $ADMIN
AllowTcpForwarding no
AllowAgentForwarding no
X11Forwarding no
MaxAuthTries 3
LoginGraceTime 20
ClientAliveInterval 30
ClientAliveCountMax 3
EOF

if ! sshd -t; then
  rm -f $F; [ -f $F.bak ] && mv $F.bak $F
  echo "config invalid, change reverted"; exit 1
fi

echo; echo "effective settings:"
sshd -T | grep -Ei '^(passwordauthentication|kbdinteractiveauthentication|authenticationmethods|allowusers|allowtcpforwarding|permitrootlogin) '
echo; echo "Applies to NEW connections. Test a key login from a second terminal before closing this one."
