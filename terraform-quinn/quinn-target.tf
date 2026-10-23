# Target VM for quinn's lab: VMID 247 -> 192.168.0.247. Same specs and login model as quinn-vm (252),
# which is the controller (git over ssh, ansible). The ubuntu account is our admin login; the quinn
# account is created by the same ansible/quinn-setup.yml and the same vault file, so quinn's one
# public key works on both machines.
resource "proxmox_virtual_environment_vm" "quinn_target" {
  name      = "quinn-target"
  node_name = "grid"
  vm_id     = 247

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
        address = "192.168.0.247/24"
        gateway = "192.168.0.1"
      }
    }
    user_account {
      username = "ubuntu"
      keys     = [local.ssh_public_key]
    }
  }
}

# Separate from quinn_setup so a replace of one machine's account setup never touches the other.
resource "null_resource" "quinn_target_setup" {
  depends_on = [proxmox_virtual_environment_vm.quinn_target]

  provisioner "local-exec" {
    command = "ansible-playbook --vault-password-file ~/.vault_pass -i '192.168.0.247,' -u ubuntu -e @${path.module}/../ansible/quinn-vars.yml ${path.module}/../ansible/quinn-setup.yml"
  }
}
