#!/bin/bash
set -e
echo "=== Restoring Secrets to Vault ==="

export VAULT_ADDR='http://192.168.0.251:8200'

CREDS_FILE=$(ls -t ${HOME}/vault-credentials-*.txt.enc ${HOME}/vault-credentials-*.txt 2>/dev/null | head -1)

if [[ "$CREDS_FILE" == *.enc ]]; then
    ROOT_TOKEN=$(openssl enc -aes-256-cbc -pbkdf2 -d -pass file:${HOME}/.vault-unseal-key \
        -in "$CREDS_FILE" | grep 'Root Token' | awk '{print $NF}')
else
    ROOT_TOKEN=$(grep 'Root Token' "$CREDS_FILE" | awk '{print $NF}')
fi

export VAULT_TOKEN="${VAULT_TOKEN:-$ROOT_TOKEN}"

vault secrets enable -path=homelab kv-v2 2>/dev/null || true

# Check for local secrets file
SECRETS_FILE="${HOME}/.homelab-secrets"
if [ -f "$SECRETS_FILE" ]; then
    echo "Loading secrets from $SECRETS_FILE"
    source "$SECRETS_FILE"
else
    echo "No secrets file found - prompting for values"
    read -s -p "Proxmox API token secret: " PROXMOX_SECRET
    echo ""
fi

vault kv put homelab/proxmox \
    api_token_id='terraform@pam!t2' \
    api_token_secret="$PROXMOX_SECRET"

vault kv put homelab/nautobot \
    api_token='0123456789abcdef0123456789abcdef01234567' \
    url='http://192.168.0.250:8080'

vault kv put homelab/nautobot-app \
    db_password='nautobotpassword' \
    secret_key='super-secret-nautobot-key-change-me-in-prod' \
    superuser_password='admin' \
    url='http://192.168.0.250:8080'

vault kv put homelab/ssh \
    private_key="$(cat ~/.ssh/id_ed25519)" \
    public_key="$(cat ~/.ssh/id_ed25519.pub)"

if [ -f ~/.kube/config-cka-a ]; then
    vault kv put homelab/kubernetes/cluster-a \
        kubeconfig="$(cat ~/.kube/config-cka-a)"
fi

echo "Secrets restored"
vault kv list homelab/
