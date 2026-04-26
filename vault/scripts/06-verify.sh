#!/bin/bash
echo "=== Verifying New Vault ==="

export VAULT_ADDR='http://192.168.0.252:8200'

CREDS_FILE=$(ls -t ${HOME}/vault-credentials-*.txt.enc 2>/dev/null | head -1)
ROOT_TOKEN=$(openssl enc -aes-256-cbc -pbkdf2 -d -pass file:${HOME}/.vault-unseal-key \
  -in "$CREDS_FILE" | grep 'Root Token' | awk '{print $NF}')
export VAULT_TOKEN="$ROOT_TOKEN"

echo "Vault status:"
vault status

echo ""
echo "Secrets:"
vault kv list homelab/

echo ""
echo "Testing reboot unseal..."
ssh ubuntu@192.168.0.252 "sudo reboot" || true
sleep 40
vault status

if vault status | grep -q "Sealed.*false"; then
    echo "✅ Auto-unseal working"
else
    echo "❌ Auto-unseal failed"
    exit 1
fi
