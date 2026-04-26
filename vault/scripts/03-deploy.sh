#!/bin/bash
set -e
echo "=== Deploying Vault VM ==="

cd ~/dev/vault
eval $(ssh-agent -s)
ssh-add ~/.ssh/id_ed25519

terraform apply -auto-approve

echo "Waiting for VM to boot..."
sleep 30

echo "Running Ansible..."
cd ~/dev/vault/ansible
ansible-playbook -i inventory.ini install_vault.yml

echo "VM deployed and Vault installed"
