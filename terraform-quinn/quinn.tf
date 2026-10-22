locals {
  ssh_public_key = file("~/.ssh/id_ed25519.pub")
}

# Small VM for quinn: VMID 252 -> 192.168.0.252. The ubuntu account is the admin login (our key);
# the quinn account itself is created by ansible/quinn-setup.yml so the password hash and quinn's
# key stay in an ansible-vault file instead of Terraform state.
resource "proxmox_virtual_environment_vm" "quinn" {
  name      = "quinn-vm"
  node_name = "grid"
  vm_id     = 252

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
        address = "192.168.0.252/24"
        gateway = "192.168.0.1"
      }
    }
    user_account {
      username = "ubuntu"
      keys     = [local.ssh_public_key]
    }
  }
}

resource "null_resource" "quinn_setup" {
  depends_on = [proxmox_virtual_environment_vm.quinn]

  provisioner "local-exec" {
    command = "ansible-playbook --vault-password-file ~/.vault_pass -i '192.168.0.252,' -u ubuntu -e @${path.module}/../ansible/quinn-vars.yml ${path.module}/../ansible/quinn-setup.yml"
  }
}
