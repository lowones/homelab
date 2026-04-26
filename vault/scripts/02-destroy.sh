#!/bin/bash
set -e
echo "=== Destroying Vault VM ==="

cd ~/dev/vault
terraform destroy -auto-approve

echo "VM destroyed"
