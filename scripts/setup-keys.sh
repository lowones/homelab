#!/bin/bash
set -e

echo "=== Homelab Key Setup ==="

if [ ! -f ~/.ssh/id_ed25519 ]; then
  echo "Generating SSH key for K8s VMs..."
  ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519
else
  echo "K8s VM SSH key already exists"
fi

if [ ! -f ~/.ssh/github_ed25519 ]; then
  echo "Generating SSH key for GitHub..."
  ssh-keygen -t ed25519 -N "" -f ~/.ssh/github_ed25519
  echo ""
  echo "Add this public key to GitHub -> Settings -> Deploy keys:"
  cat ~/.ssh/github_ed25519.pub
else
  echo "GitHub SSH key already exists"
fi

if ! grep -q "github_ed25519" ~/.ssh/config 2>/dev/null; then
  cat >> ~/.ssh/config << 'SSHEOF'

Host github.com
  IdentityFile ~/.ssh/github_ed25519
  User git

Host 192.168.0.*
  StrictHostKeyChecking no
  UserKnownHostsFile /dev/null
SSHEOF
  echo "SSH config updated"
fi

if [ ! -f ~/.vault_pass ]; then
  echo "$(openssl rand -base64 32)" > ~/.vault_pass
  chmod 600 ~/.vault_pass
  echo "Vault password generated at ~/.vault_pass"
else
  echo "Vault password already exists"
fi

echo ""
echo "=== Setup complete ==="
echo "1. Fill in .env with your Proxmox token secret"
echo "2. Add GitHub public key to repo deploy keys"
echo "3. Run: source .env"
