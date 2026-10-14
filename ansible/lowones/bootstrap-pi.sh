#!/usr/bin/env bash
# One-time HAND bootstrap of the RV Pi's tunnel, so Ansible (pi-tunnel.yml) can adopt it later.
# Run ON THE PI as the normal user (needs sudo). Does NOT enable or start the service.
# Prereq: user 'rvtunnel' and /var/lib/rvtunnel/id_ed25519 already exist (see README.md step 2).
# The unit below must stay byte-identical to roles/ssh_reverse_tunnel/templates/rvtunnel.service.j2
# rendered with group_vars/all.yml, otherwise the first Ansible run reports a change.
set -euo pipefail

FP='SHA256:paXSB5ITzgYaU1YZI3SGojr97XV8a43Fl+CNmCmKbpA'   # lowones_hostkey_fingerprint
HK=$(mktemp)
ssh-keyscan -t ed25519 -p 7822 service.low-ones.com 2>/dev/null > "$HK"
ssh-keygen -lf "$HK" | grep -qF "$FP" || { echo "HOST KEY MISMATCH - stopping, nothing installed"; exit 1; }
echo "low-ones host key matches the verified fingerprint"

sudo install -d -m 0755 /etc/rvtunnel
sudo install -m 0644 "$HK" /etc/rvtunnel/known_hosts
sudo tee /etc/systemd/system/rvtunnel-lowones.service >/dev/null <<'UNIT'
# Managed by Ansible (ansible/lowones, role ssh_reverse_tunnel). Do not hand-edit.
[Unit]
Description=Reverse SSH tunnel to low-ones (localhost:8677 -> local sshd)
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=rvtunnel
NoNewPrivileges=yes
PrivateTmp=yes
ExecStart=/usr/bin/ssh -NT \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -o ExitOnForwardFailure=yes \
  -o ConnectTimeout=20 \
  -o BatchMode=yes \
  -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile=/etc/rvtunnel/known_hosts \
  -i /var/lib/rvtunnel/id_ed25519 \
  -R localhost:8677:localhost:22 \
  -p 7822 sshtunnel@service.low-ones.com
Restart=always
RestartSec=60

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
echo "Installed, NOT enabled. Authorize the key on the VPS first (vps-tunnel-keys.yml)."
