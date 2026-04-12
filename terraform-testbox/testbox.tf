locals {
  ssh_public_key = file("~/.ssh/id_ed25519.pub")
}

resource "proxmox_virtual_environment_vm" "testbox" {
  name      = "testbox"
  node_name = "grid"
  vm_id     = 220

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
    dedicated = 2048
  }

  disk {
    datastore_id = "local-lvm"
    interface    = "scsi0"
    size         = 20
  }

  network_device {
    bridge = "vmbr0"
    model  = "virtio"
  }

  initialization {
    ip_config {
      ipv4 {
        address = "192.168.0.220/24"
        gateway = "192.168.0.1"
      }
    }
    user_account {
      username = "ubuntu"
      keys     = [local.ssh_public_key]
    }
  }
}
# Note: vi mode is handled via Ansible or manual setup
# Add to ~/.bashrc after SSH: echo 'set -o vi' >> ~/.bashrc

resource "null_resource" "testbox_setup" {
  depends_on = [proxmox_virtual_environment_vm.testbox]

  provisioner "local-exec" {
    command = "sleep 30 && ansible-playbook -i '192.168.0.220,' -u ubuntu ${path.module}/../ansible/testbox-setup.yml"
  }
}
