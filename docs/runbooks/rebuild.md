# Runbook — full destroy and rebuild

## From batcomputer

```bash
# Destroy K8s cluster
source ~/dev/homelab/.env
cd ~/dev/homelab/terraform && terraform destroy -auto-approve

# Rebuild K8s cluster
terraform apply -auto-approve && \
cd ~/dev/homelab/ansible && \
ansible-playbook -i inventory.ini all.yml && \
ssh ubuntu@192.168.0.210 kubectl get nodes
```

## From testbox

```bash
source ~/homelab/.env
cd ~/homelab/terraform && terraform destroy -auto-approve && \
terraform apply -auto-approve && \
cd ~/homelab/ansible && \
ansible-playbook -i inventory.ini all.yml && \
ssh ubuntu@192.168.0.210 kubectl get nodes
```

## Destroy/rebuild testbox (from batcomputer)

```bash
cd ~/dev/homelab/terraform-testbox && terraform destroy -auto-approve && terraform apply -auto-approve
```

## Manual VM cleanup (if Terraform state is lost)

SSH to grid and run:

```bash
qm stop 210 && qm destroy 210 --purge
qm stop 211 && qm destroy 211 --purge
qm stop 212 && qm destroy 212 --purge
qm stop 213 && qm destroy 213 --purge
```
