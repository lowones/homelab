# ArgoCD

## Access

URL: https://192.168.0.202
Username: admin
Password: stored in Ansible Vault (vault_argocd_password)

## Concepts

- **Application** — a deployment managed by ArgoCD pointing to a git repo path
- **Sync** — ArgoCD pulling from git and applying to the cluster
- **Auto-sync** — automatically deploys git changes within ~3 minutes
- **Self-heal** — automatically fixes manual changes to revert to git state
- **Prune** — deletes resources removed from git

## Adding a new app

1. Add manifests to `k8s-apps/apps/<appname>/`
2. Add an ArgoCD Application manifest to `k8s-apps/argocd/<appname>.yml`:

```yaml
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
```

3. Push to git — ArgoCD will detect and deploy automatically

## CLI usage

```bash
# Login
argocd login 192.168.0.202 --username admin --password <password> --insecure

# List apps
argocd app list

# Sync an app manually
argocd app sync nginx

# Get app status
argocd app get nginx

# Roll back an app
argocd app rollback nginx 1

# Delete an app
argocd app delete nginx
```

## Checking sync status

```bash
kubectl get applications -n argocd
```
