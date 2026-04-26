#!/bin/bash
set -e
echo "=== Vault Backup ==="

BACKUP_DIR="${HOME}/vault-backups"
mkdir -p "$BACKUP_DIR"

# Stop vault and backup data directory
ssh ubuntu@192.168.0.251 "sudo systemctl stop vault && sudo tar -czf /tmp/vault-backup-$(date +%Y%m%d-%H%M).tar.gz /opt/vault/data && sudo chmod 644 /tmp/vault-backup-*.tar.gz"

# Copy backup to batcomputer
scp ubuntu@192.168.0.251:/tmp/vault-backup-*.tar.gz "$BACKUP_DIR/"

echo "Backup saved to $BACKUP_DIR"
ls -la "$BACKUP_DIR/"
