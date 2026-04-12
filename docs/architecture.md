# Architecture

## Overview

A fully automated Kubernetes homelab running on Proxmox, managed with Terraform and Ansible, with GitOps deployment via ArgoCD.

## Physical Infrastructure

| Host | IP | Role | RAM | Storage |
|------|----|------|-----|---------|
| grid | 192.168.0.122 | Proxmox hypervisor | 46GB | NVMe + HDD |
| batcomputer | 192.168.0.101 | Management VM | 2.5GB | 100GB |
| testbox | 192.168.0.220 | Test/deploy VM | 2GB | 20GB |

## Kubernetes Cluster

| VM | IP | Role | CPU | RAM | Disk |
|----|----|------|-----|-----|------|
| k8s-control | 192.168.0.210 | Control plane | 2 | 4GB | 50GB |
| k8s-worker-1 | 192.168.0.211 | Worker | 2 | 8GB | 50GB |
| k8s-worker-2 | 192.168.0.212 | Worker | 2 | 8GB | 50GB |
| k8s-worker-3 | 192.168.0.213 | Worker | 2 | 8GB | 50GB |

## Installed Services

| Service | IP | Purpose |
|---------|----|---------|
| MetalLB | 192.168.0.200-220 | LoadBalancer IPs for bare metal |
| Nginx Ingress | 192.168.0.200 | HTTP routing into cluster |
| Longhorn | internal | Distributed persistent storage |
| Prometheus | internal | Metrics collection |
| Grafana | 192.168.0.201 | Monitoring dashboards |
| ArgoCD | 192.168.0.202 | GitOps app deployment |

## Network Layout

```
Internet
    |
Router (192.168.0.1)
    |
Proxmox (192.168.0.122) — vmbr0 bridge
    |
    +-- batcomputer (.101)
    +-- testbox (.220)
    +-- k8s-control (.210)
    +-- k8s-worker-1 (.211)
    +-- k8s-worker-2 (.212)
    +-- k8s-worker-3 (.213)
```

## GitHub Repos

| Repo | Purpose |
|------|---------|
| lowones/homelab | Infrastructure — Terraform + Ansible |
| lowones/k8s-apps | Applications — deployed via ArgoCD |

## Secrets

| Secret | Location |
|--------|----------|
| Proxmox API token | Ansible Vault + .env |
| SSH keys | ~/.ssh/ (never committed) |
| Vault password | ~/.vault_pass (never committed) |
| Grafana password | Ansible Vault |
| ArgoCD password | Ansible Vault |
