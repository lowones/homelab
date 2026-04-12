# Bootstrap — new machine setup

Run this on any fresh Ubuntu VM to set up the homelab tools and clone the repo.

## One command

```bash
curl -s https://raw.githubusercontent.com/lowones/homelab/main/scripts/bootstrap.sh -o /tmp/bootstrap.sh && bash /tmp/bootstrap.sh
```

## What it does

1. Installs git, ansible, terraform, kubectl, helm
2. Generates SSH keys (id_ed25519 for VMs, github_ed25519 for GitHub)
3. Configures ~/.ssh/config
4. Prompts you to add the GitHub deploy key
5. Clones the homelab repo
6. Prompts for vault password and generates .env from vault
7. Adds vi mode and env sourcing to ~/.bashrc

## Manual steps required

1. Add `~/.ssh/github_ed25519.pub` as a deploy key to the homelab repo on GitHub
2. Enter the vault password when prompted (get from batcomputer: `cat ~/.vault_pass`)

## After bootstrap

```bash
source ~/homelab/.env
cd ~/homelab/terraform && terraform init && terraform apply -auto-approve
cd ~/homelab/ansible && ansible-playbook -i inventory.ini all.yml
ssh ubuntu@192.168.0.210 kubectl get nodes
```
