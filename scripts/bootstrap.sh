#!/bin/bash
set -e

echo "=== Homelab Bootstrap ==="

# Install dependencies
export DEBIAN_FRONTEND=noninteractive
sudo DEBIAN_FRONTEND=noninteractive apt update -qq
sudo DEBIAN_FRONTEND=noninteractive apt install -y -o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold" git ansible

# Install Terraform
wget -O- https://apt.releases.hashicorp.com/gpg | sudo gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/hashicorp.list > /dev/null
sudo DEBIAN_FRONTEND=noninteractive apt update -qq && sudo DEBIAN_FRONTEND=noninteractive apt install -y -o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold" terraform

# Generate SSH keys
if [ ! -f ~/.ssh/id_ed25519 ]; then
  ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519
fi

if [ ! -f ~/.ssh/github_ed25519 ]; then
  ssh-keygen -t ed25519 -N "" -f ~/.ssh/github_ed25519
fi

# SSH config
cat > ~/.ssh/config << 'SSHEOF'
Host github.com
  IdentityFile ~/.ssh/github_ed25519
  User git

Host 192.168.0.*
  StrictHostKeyChecking no
  UserKnownHostsFile /dev/null
SSHEOF
chmod 600 ~/.ssh/config

# Show GitHub key and wait
echo ""
echo "=== Add this key to GitHub -> homelab repo -> Settings -> Deploy keys ==="
cat ~/.ssh/github_ed25519.pub
echo ""
read -p "Press Enter once you've added the deploy key to GitHub..."

# Clone repo
if [ ! -d ~/homelab ]; then
  git clone git@github.com:lowones/homelab.git ~/homelab
else
  echo "homelab repo already exists, pulling latest..."
  cd ~/homelab && git pull
fi

# Set up env
if [ ! -f ~/homelab/.env ]; then
  cp ~/homelab/.env.example ~/homelab/.env
  sed -i 's/^/export /' ~/homelab/.env
  echo ""
  echo "=== Fill in your Proxmox token secret ==="
  vim ~/homelab/.env
fi

# Vault password
if [ ! -f ~/.vault_pass ]; then
  echo ""
  read -s -p "Enter vault password (get from batcomputer: cat ~/.vault_pass): " vault_pass
  echo ""
  echo "$vault_pass" > ~/.vault_pass
  chmod 600 ~/.vault_pass
fi

# Vi mode
echo 'set -o vi' >> ~/.bashrc

# Source env
grep -q "^export" ~/homelab/.env || sed -i "s/^/export /" ~/homelab/.env
source ~/homelab/.env

echo ""
echo "=== Bootstrap complete ==="
echo "Run the following to build the cluster:"
echo "cd ~/homelab/terraform && terraform apply -auto-approve && cd ~/homelab/ansible && ansible-playbook -i inventory.ini all.yml"
