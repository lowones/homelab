# Runbook — add a new app via ArgoCD

## 1. Create app manifests

```bash
mkdir -p ~/k8s-apps/apps/myapp
```

Create `apps/myapp/deployment.yml`, `apps/myapp/service.yml`, `apps/myapp/ingress.yml` as needed.

## 2. Create ArgoCD application manifest

```bash
cat > ~/k8s-apps/argocd/myapp.yml << 'EOF'
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: myapp
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://github.com/lowones/k8s-apps
    targetRevision: HEAD
    path: apps/myapp
  destination:
    server: https://kubernetes.default.svc
    namespace: default
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
