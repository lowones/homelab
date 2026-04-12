# Runbook — troubleshooting

## Pod not starting

```bash
kubectl get pods -A
kubectl describe pod <pod-name> -n <namespace>
kubectl logs <pod-name> -n <namespace>
kubectl logs <pod-name> -n <namespace> --previous
```

## ArgoCD not syncing

```bash
kubectl get applications -n argocd
argocd app sync <appname>
kubectl logs -n argocd deployment/argocd-server
```

## Grafana not accessible

```bash
kubectl get pods -n monitoring
kubectl get svc -n monitoring monitoring-grafana
kubectl get ipaddresspool -n metallb-system
```

## Terraform state conflict (VMs exist but not in state)

SSH to grid and manually destroy:

```bash
qm stop 210 && qm destroy 210 --purge
qm stop 211 && qm destroy 211 --purge
qm stop 212 && qm destroy 212 --purge
qm stop 213 && qm destroy 213 --purge
```

Then run `terraform apply -auto-approve` fresh.

## Node not ready

```bash
kubectl describe node <node-name>
ssh ubuntu@192.168.0.21X "sudo systemctl status kubelet"
ssh ubuntu@192.168.0.21X "sudo systemctl status containerd"
```

## Longhorn volume stuck

```bash
kubectl get pods -n longhorn-system
kubectl describe pvc <pvc-name> -n <namespace>
kubectl logs -n longhorn-system -l app=longhorn-manager
```
