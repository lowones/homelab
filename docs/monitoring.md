# Monitoring

## Access

Grafana URL: http://192.168.0.201
Username: admin
Password: stored in Ansible Vault (vault_grafana_password)

## Key dashboards

Navigate to Dashboards -> Browse -> Kubernetes:

| Dashboard | What it shows |
|-----------|---------------|
| Kubernetes / Compute Resources / Cluster | Overall CPU and memory |
| Kubernetes / Compute Resources / Node | Per node breakdown |
| Kubernetes / Compute Resources / Pod | Per pod resources |
| Kubernetes / Networking | Network traffic |
| Node Exporter / Nodes | Host level metrics |

## Prometheus

Prometheus scrapes metrics from all pods and nodes automatically.

```bash
# Port-forward Prometheus UI
kubectl port-forward svc/monitoring-kube-prometheus-prometheus 9090:9090 -n monitoring

# Then open http://localhost:9090
```

## Useful PromQL queries

```promql
# CPU usage by node
sum(rate(node_cpu_seconds_total{mode!="idle"}[5m])) by (node)

# Memory usage by pod
sum(container_memory_usage_bytes{namespace!=""}) by (pod)

# Pod restart count
sum(kube_pod_container_status_restarts_total) by (pod)

# Disk usage
node_filesystem_avail_bytes / node_filesystem_size_bytes
```
