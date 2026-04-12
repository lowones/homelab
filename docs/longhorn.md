# Longhorn Storage

Longhorn provides distributed block storage for Kubernetes persistent volumes, backed by the VM disks.

## Using persistent storage in an app

```yaml
# PersistentVolumeClaim
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: myapp-data
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: longhorn
  resources:
    requests:
      storage: 5Gi
---
# Use in a deployment
spec:
  containers:
    - name: myapp
      volumeMounts:
        - name: data
          mountPath: /data
  volumes:
    - name: data
      persistentVolumeClaim:
        claimName: myapp-data
```

## Useful commands

```bash
# List all PVCs
kubectl get pvc -A

# List all PVs
kubectl get pv

# Describe a PVC
kubectl describe pvc myapp-data

# Check Longhorn pods
kubectl get pods -n longhorn-system
```

## Storage classes

| Class | Access mode | Use case |
|-------|-------------|---------|
| longhorn | ReadWriteOnce | Single pod storage |
| longhorn (RWX) | ReadWriteMany | Shared storage |
