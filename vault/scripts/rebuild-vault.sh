#!/bin/bash
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=============================="
echo "  Vault Full Rebuild Pipeline"
echo "=============================="

read -p "This will destroy and rebuild Vault. Continue? (yes/no): " CONFIRM
if [ "$CONFIRM" != "yes" ]; then
    echo "Aborted"
    exit 1
fi

echo ""
echo "Step 1: Backup"
bash "$SCRIPT_DIR/01-backup.sh"

echo ""
echo "Restarting and unsealing Vault after backup..."
ssh ubuntu@192.168.0.251 "sudo systemctl start vault && sleep 5 && sudo systemctl start vault-unseal"
echo "Waiting for unseal..."
sleep 20

echo ""
echo "Step 2: Destroy VM"
bash "$SCRIPT_DIR/02-destroy.sh"

echo ""
echo "Step 3: Deploy VM"
bash "$SCRIPT_DIR/03-deploy.sh"

echo ""
echo "Step 4: Initialize Vault"
bash "$SCRIPT_DIR/04-init-vault.sh"

echo ""
echo "Step 5: Restore Secrets"
bash "$SCRIPT_DIR/05-restore-secrets.sh"

echo ""
echo "=============================="
echo "  Rebuild Complete!"
echo "=============================="
echo ""
echo "Your credentials are in: ~/vault-credentials-*.txt (encrypted)"
echo "Your encryption key is: ~/.vault-unseal-key"
echo ""
echo "SAVE THESE FILES then run:"
echo "  rm ~/.vault-unseal-key ~/vault-credentials-*.txt"
