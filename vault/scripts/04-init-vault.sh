#!/bin/bash
set -e
echo "=== Initializing Vault ==="

VAULT_VM="ubuntu@192.168.0.251"
VAULT_ADDR="http://192.168.0.251:8200"
OUTPUT_FILE="${HOME}/vault-credentials-$(date +%Y%m%d-%H%M).txt"
ENCRYPT_KEY="${HOME}/.vault-unseal-key"

# Generate encryption key
openssl rand -base64 32 > "$ENCRYPT_KEY"
chmod 600 "$ENCRYPT_KEY"
echo "Encryption key generated: $ENCRYPT_KEY"

# Wait for Vault to be ready
echo "Waiting for Vault..."
for i in {1..30}; do
    if curl -sf http://192.168.0.251:8200/v1/sys/health > /dev/null 2>&1; then
        break
    fi
    sleep 2
done

# Initialize Vault
echo "Initializing Vault..."
INIT_OUTPUT=$(ssh "$VAULT_VM" "export VAULT_ADDR='http://127.0.0.1:8200' && vault operator init")

echo "$INIT_OUTPUT"

# Parse unseal keys and root token
KEY1=$(echo "$INIT_OUTPUT" | grep 'Unseal Key 1' | awk '{print $NF}')
KEY2=$(echo "$INIT_OUTPUT" | grep 'Unseal Key 2' | awk '{print $NF}')
KEY3=$(echo "$INIT_OUTPUT" | grep 'Unseal Key 3' | awk '{print $NF}')
KEY4=$(echo "$INIT_OUTPUT" | grep 'Unseal Key 4' | awk '{print $NF}')
KEY5=$(echo "$INIT_OUTPUT" | grep 'Unseal Key 5' | awk '{print $NF}')
ROOT_TOKEN=$(echo "$INIT_OUTPUT" | grep 'Initial Root Token' | awk '{print $NF}')

# Save to encrypted output file
cat > /tmp/vault-creds-plain.txt << CREDS
Vault Credentials - $(date)
===========================
Unseal Key 1: $KEY1
Unseal Key 2: $KEY2
Unseal Key 3: $KEY3
Unseal Key 4: $KEY4
Unseal Key 5: $KEY5
Root Token: $ROOT_TOKEN
CREDS

openssl enc -aes-256-cbc -pbkdf2 -pass file:"$ENCRYPT_KEY" -in /tmp/vault-creds-plain.txt -out "$OUTPUT_FILE"
rm -f /tmp/vault-creds-plain.txt
chmod 600 "$OUTPUT_FILE"
echo "Credentials saved to: $OUTPUT_FILE"
echo "Encryption key: $ENCRYPT_KEY"
echo ""
echo "To decrypt: openssl enc -aes-256-cbc -pbkdf2 -d -pass file:${HOME}/.vault-unseal-key -in $OUTPUT_FILE"

# Encrypt unseal keys for auto-unseal service
echo -n "$KEY1" | openssl enc -aes-256-cbc -pbkdf2 -pass file:"$ENCRYPT_KEY" -base64 > /tmp/unseal-1.enc
echo -n "$KEY2" | openssl enc -aes-256-cbc -pbkdf2 -pass file:"$ENCRYPT_KEY" -base64 > /tmp/unseal-2.enc
echo -n "$KEY3" | openssl enc -aes-256-cbc -pbkdf2 -pass file:"$ENCRYPT_KEY" -base64 > /tmp/unseal-3.enc

# Copy encryption key and encrypted unseal keys to vault VM
scp "$ENCRYPT_KEY" "$VAULT_VM:/tmp/.unseal-key"
scp /tmp/unseal-1.enc /tmp/unseal-2.enc /tmp/unseal-3.enc "$VAULT_VM:/tmp/"

# Move files into place on vault VM
ssh "$VAULT_VM" "
sudo mv /tmp/.unseal-key /etc/vault.d/
sudo mv /tmp/unseal-1.enc /tmp/unseal-2.enc /tmp/unseal-3.enc /etc/vault.d/
sudo chmod 600 /etc/vault.d/.unseal-key /etc/vault.d/unseal-*.enc
sudo chown vault:vault /etc/vault.d/.unseal-key /etc/vault.d/unseal-*.enc
"

# Create unseal script on vault VM
ssh "$VAULT_VM" "sudo tee /usr/local/bin/vault-unseal.sh > /dev/null << 'SCRIPT'
#!/bin/bash
export VAULT_ADDR='http://127.0.0.1:8200'
sleep 5
SEALED=\$(vault status 2>/dev/null | grep 'Sealed' | awk '{print \$2}')
if [ \"\$SEALED\" = \"true\" ]; then
    logger \"Vault is sealed - attempting unseal\"
    KEY1=\$(openssl enc -aes-256-cbc -pbkdf2 -d -pass file:/etc/vault.d/.unseal-key -base64 -in /etc/vault.d/unseal-1.enc 2>/dev/null)
    KEY2=\$(openssl enc -aes-256-cbc -pbkdf2 -d -pass file:/etc/vault.d/.unseal-key -base64 -in /etc/vault.d/unseal-2.enc 2>/dev/null)
    KEY3=\$(openssl enc -aes-256-cbc -pbkdf2 -d -pass file:/etc/vault.d/.unseal-key -base64 -in /etc/vault.d/unseal-3.enc 2>/dev/null)
    vault operator unseal \"\$KEY1\"
    vault operator unseal \"\$KEY2\"
    vault operator unseal \"\$KEY3\"
    logger \"Vault unseal complete\"
else
    logger \"Vault already unsealed\"
fi
SCRIPT
sudo chmod 700 /usr/local/bin/vault-unseal.sh"

# Create and enable systemd service
ssh "$VAULT_VM" "sudo tee /etc/systemd/system/vault-unseal.service > /dev/null << 'SERVICE'
[Unit]
Description=Vault Auto Unseal
After=vault.service
Requires=vault.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/vault-unseal.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
SERVICE
sudo systemctl daemon-reload
sudo systemctl enable vault-unseal"

# Unseal vault
echo "Unsealing Vault..."
ssh "$VAULT_VM" "
export VAULT_ADDR='http://127.0.0.1:8200'
vault operator unseal $KEY1
vault operator unseal $KEY2
vault operator unseal $KEY3
vault status
"

echo ""
echo "=== Vault initialized and unsealed ==="
echo "Root token stored in: $OUTPUT_FILE"
echo "To decrypt credentials:"
echo "  openssl enc -aes-256-cbc -pbkdf2 -d -pass file:${HOME}/.vault-unseal-key -in $OUTPUT_FILE"
echo ""
echo "IMPORTANT: After saving credentials, delete sensitive files:"
echo "  rm ${HOME}/.vault-unseal-key $OUTPUT_FILE"
