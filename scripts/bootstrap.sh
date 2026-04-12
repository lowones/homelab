#!/bin/bash
set -e

echo "=== Homelab Bootstrap ==="

# Disable needrestart prompts
sudo sed -i "s/#\$nrconf{restart} = 'i';/\$nrconf{restart} = 'a';/" /etc/needrestart/needrestart.conf 2>/dev/null || true

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

# Accept GitHub host key
ssh-keyscan github.com >> ~/.ssh/known_hosts 2>/dev/null

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

# Vault password
if [ ! -f ~/.vault_pass ]; then
  echo ""
  read -s -p "Enter vault password (get from batcomputer: cat ~/.vault_pass): " vault_pass
  echo ""
  echo "$vault_pass" > ~/.vault_pass
  chmod 600 ~/.vault_pass
fi

# Generate .env from vault
if [ ! -f ~/homelab/.env ]; then
  echo "Generating .env from vault..."
  VAULT="ansible-vault view ~/homelab/ansible/group_vars/all/vault.yml --vault-password-file ~/.vault_pass"
  API_URL=$(eval $VAULT | grep vault_proxmox_api_url | awk '{print $2}' | tr -d '"')
  TOKEN_ID=$(eval $VAULT | grep vault_proxmox_token_id | awk '{print $2}' | tr -d '"')
  TOKEN_SECRET=$(eval $VAULT | grep vault_proxmox_token_secret | awk '{print $2}' | tr -d '"')

  cat > ~/homelab/.env << ENVEOF
export PROXMOX_API_URL="${API_URL}"
export PROXMOX_API_TOKEN_ID="${TOKEN_ID}"
export PROXMOX_API_TOKEN_SECRET="${TOKEN_SECRET}"
export TF_VAR_proxmox_api_url=\$PROXMOX_API_URL
export TF_VAR_proxmox_api_token_id=\$PROXMOX_API_TOKEN_ID
export TF_VAR_proxmox_api_token_secret=\$PROXMOX_API_TOKEN_SECRET
ENVEOF
fi

# Vi mode
grep -q "set -o vi" ~/.bashrc || echo 'set -o vi' >> ~/.bashrc

# Auto source env on login
grep -q "source ~/homelab/.env" ~/.bashrc || echo 'source ~/homelab/.env' >> ~/.bashrc

# Source for current session
source ~/homelab/.env

echo ""
echo "=== Bootstrap complete ==="
echo "Run the following to build the cluster:"
echo "source ~/homelab/.env && cd ~/homelab/terraform && terraform init && terraform apply -auto-approve && cd ~/homelab/ansible && ansible-playbook -i inventory.ini all.yml && ssh ubuntu@192.168.0.210 kubectl get nodes"

# Install kubectl
if ! command -v kubectl &> /dev/null; then
  curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
  chmod +x kubectl
  sudo mv kubectl /usr/local/bin/
fi

# Install Helm
if ! command -v helm &> /dev/null; then
  curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
fi
