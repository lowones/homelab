# Homelab

Kubernetes homelab on Proxmox, fully automated with Terraform and Ansible.

## Stack
- **Proxmox** - Hypervisor (grid, 192.168.0.122)
- **Terraform** - VM provisioning
- **Ansible** - Kubernetes configuration
- **ArgoCD** - GitOps app deployment (192.168.0.202)
- **MetalLB** - Load balancer (192.168.0.200-220)
- **Nginx Ingress** - HTTP routing (192.168.0.200)
- **Longhorn** - Distributed storage
- **Prometheus/Grafana** - Monitoring (192.168.0.201)

## First time setup
```bash
git clone git@github.com:lowones/homelab.git
cd homelab
scripts/setup-keys.sh
cp .env.example .env
# Fill in PROXMOX_API_TOKEN_SECRET in .env
source .env
```

## One-liners
**Destroy:**
```bash
source .env && cd terraform && terraform destroy -auto-approve
```

**Build:**
```bash
source .env && cd terraform && terraform apply -auto-approve && cd ../ansible && ansible-playbook -i inventory.ini all.yml
```

**Destroy + rebuild:**
```bash
source .env && cd terraform && terraform destroy -auto-approve && terraform apply -auto-approve && cd ../ansible && ansible-playbook -i inventory.ini all.yml && ssh ubuntu@192.168.0.210 kubectl get nodes
```

## Secrets
| Secret | Location |
|--------|----------|
| Proxmox token | `.env` (gitignored) |
| SSH keys | `~/.ssh/` (never committed) |
| Ansible secrets | `ansible/group_vars/all/vault.yml` (encrypted) |
| Vault password | `~/.vault_pass` (gitignored) |
| Grafana password | auto-generated, retrieve via kubectl |
| ArgoCD password | auto-generated, retrieve via kubectl |
