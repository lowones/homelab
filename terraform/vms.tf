locals {
  ssh_public_key = file("~/.ssh/id_ed25519.pub")
}

resource "proxmox_virtual_environment_vm" "k8s_control" {
  name      = "k8s-control"
  node_name = "grid"
  vm_id     = 210

  agent {
    enabled = false
  }

  clone {
    vm_id = 9000
    full  = true
  }

  cpu {
    cores = 2
    type  = "host"
  }

  memory {
    dedicated = 4096
  }

  disk {
    datastore_id = "local-lvm"
    interface    = "scsi0"
    size         = 50
  }

  network_device {
    bridge = "vmbr0"
    model  = "virtio"
  }

  initialization {
    ip_config {
      ipv4 {
        address = "192.168.0.210/24"
        gateway = "192.168.0.1"
      }
    }
    user_account {
      username = "ubuntu"
      keys     = [local.ssh_public_key]
    }
  }
}

resource "proxmox_virtual_environment_vm" "k8s_worker_1" {
  name      = "k8s-worker-1"
  node_name = "grid"
  vm_id     = 211

  agent {
    enabled = false
  }

  clone {
    vm_id = 9000
    full  = true
  }

  cpu {
    cores = 2
    type  = "host"
  }

  memory {
    dedicated = 8192
  }

  disk {
    datastore_id = "local-lvm"
    interface    = "scsi0"
    size         = 50
  }

  network_device {
    bridge = "vmbr0"
    model  = "virtio"
  }

  initialization {
    ip_config {
      ipv4 {
        address = "192.168.0.211/24"
        gateway = "192.168.0.1"
      }
    }
    user_account {
      username = "ubuntu"
      keys     = [local.ssh_public_key]
    }
  }
}

resource "proxmox_virtual_environment_vm" "k8s_worker_2" {
  name      = "k8s-worker-2"
  node_name = "grid"
  vm_id     = 212

  agent {
    enabled = false
  }

  clone {
    vm_id = 9000
    full  = true
  }

  cpu {
    cores = 2
    type  = "host"
  }

  memory {
    dedicated = 8192
  }

  disk {
    datastore_id = "local-lvm"
    interface    = "scsi0"
    size         = 50
  }

  network_device {
    bridge = "vmbr0"
    model  = "virtio"
  }

  initialization {
    ip_config {
      ipv4 {
        address = "192.168.0.212/24"
        gateway = "192.168.0.1"
      }
    }
    user_account {
      username = "ubuntu"
      keys     = [local.ssh_public_key]
    }
  }
}

resource "proxmox_virtual_environment_vm" "k8s_worker_3" {
  name      = "k8s-worker-3"
  node_name = "grid"
  vm_id     = 213

  agent {
    enabled = false
  }

  clone {
    vm_id = 9000
    full  = true
  }

  cpu {
    cores = 2
    type  = "host"
  }

  memory {
    dedicated = 8192
  }

  disk {
    datastore_id = "local-lvm"
    interface    = "scsi0"
    size         = 50
  }

  network_device {
    bridge = "vmbr0"
    model  = "virtio"
  }

  initialization {
    ip_config {
      ipv4 {
        address = "192.168.0.213/24"
        gateway = "192.168.0.1"
      }
    }
    user_account {
      username = "ubuntu"
      keys     = [local.ssh_public_key]
    }
  }
}
