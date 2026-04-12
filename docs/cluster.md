# Kubernetes Cluster

## Version

Kubernetes v1.29.15 with containerd runtime and Flannel CNI.

## Access

```bash
# From testbox or batcomputer (after kubeconfig setup)
kubectl get nodes

# SSH to control plane
ssh ubuntu@192.168.0.210

# Run kubectl via SSH
ssh ubuntu@192.168.0.210 kubectl get pods -A
```

## Setup kubeconfig locally

```bash
mkdir -p ~/.kube
ssh ubuntu@192.168.0.210 "cat ~/.kube/config" > ~/.kube/config
kubectl get nodes
```

## Useful commands

```bash
# All pods across all namespaces
kubectl get pods -A

# Watch pods in real time
kubectl get pods -A -w

# Describe a pod
kubectl describe pod <pod-name> -n <namespace>

# Get pod logs
kubectl logs <pod-name> -n <namespace>

# Get all services
kubectl get svc -A

# Get all ingresses
kubectl get ingress -A

# Check node resources
kubectl top nodes

# Check pod resources
kubectl top pods -A
```

## Namespaces

| Namespace | Contents |
|-----------|----------|
| default | User applications (nginx, etc) |
| kube-system | Core Kubernetes components |
| metallb-system | MetalLB load balancer |
| ingress-nginx | Nginx ingress controller |
| longhorn-system | Longhorn storage |
| monitoring | Prometheus + Grafana |
| argocd | ArgoCD GitOps |
