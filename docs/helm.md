# Helm

## Installed releases

```bash
helm list -A
```

| Release | Namespace | Chart | Purpose |
|---------|-----------|-------|---------|
| metallb | metallb-system | metallb-0.15.3 | LoadBalancer IPs |
| ingress-nginx | ingress-nginx | ingress-nginx-4.15.1 | HTTP routing |
| longhorn | longhorn-system | longhorn-1.11.1 | Storage |
| monitoring | monitoring | kube-prometheus-stack-83.4.0 | Prometheus + Grafana |
| argocd | argocd | argo-cd-9.5.0 | GitOps |

## Common commands

```bash
# List all releases
helm list -A

# See values used for a release
helm get values monitoring -n monitoring

# See all available values for a chart
helm show values prometheus-community/kube-prometheus-stack | less

# Upgrade a release with new values
helm upgrade monitoring prometheus-community/kube-prometheus-stack \
  -n monitoring \
  --reuse-values \
  --set grafana.adminPassword=newpassword

# Roll back a release
helm rollback monitoring 1 -n monitoring

# See release history
helm history monitoring -n monitoring

# Uninstall a release
helm uninstall monitoring -n monitoring
```

## Adding a new chart

```bash
# Add a repo
helm repo add bitnami https://charts.bitnami.com/bitnami
helm repo update

# Search for charts
helm search repo bitnami/postgresql

# Install
helm upgrade --install postgresql bitnami/postgresql \
  --namespace postgresql \
  --create-namespace \
  --set auth.postgresPassword=mypassword
```
