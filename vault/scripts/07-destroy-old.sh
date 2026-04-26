#!/bin/bash
set -e
echo "=== Destroying OLD Vault VM ==="

cd ~/dev/vault

# Point Terraform at new vault
sed -i 's|http://192.168.0.251:8200|http://192.168.0.252:8200|g' main.tf terraform.tfvars 2>/dev/null || true

# Update vault token in tfvars
CREDS_FILE=$(ls -t ${HOME}/vault-credentials-*.txt.enc 2>/dev/null | head -1)
ROOT_TOKEN=$(openssl enc -aes-256-cbc -pbkdf2 -d -pass file:${HOME}/.vault-unseal-key \
  -in "$CREDS_FILE" | grep 'Root Token' | awk '{print $NF}')

sed -i "s|vault_token.*=.*|vault_token = \"$ROOT_TOKEN\"|" terraform.tfvars

terraform destroy -auto-approve

# Rename new vault terraform project to replace old
cp -r ~/dev/vault-new/* ~/dev/vault/
rm -rf ~/dev/vault-new

echo "Old Vault VM destroyed"
echo "New Vault is now the primary at 192.168.0.252"
echo ""
echo "Update your VAULT_ADDR to http://192.168.0.252:8200"
