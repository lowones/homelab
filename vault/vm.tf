resource "proxmox_virtual_environment_file" "vault_userdata" {
  content_type = "snippets"
  datastore_id = "DATA"
  node_name    = "grid"

  source_raw {
    data      = <<-USERDATA
      #cloud-config
      hostname: vault
      manage_etc_hosts: true
      package_update: true
      packages:
        - vim
        - curl
        - wget
        - unzip
        - gpg
        - qemu-guest-agent
      runcmd:
        - systemctl enable qemu-guest-agent
        - systemctl start qemu-guest-agent
        - echo 'set -o vi' >> /etc/bash.bashrc
      write_files:
        - path: /home/ubuntu/.ssh/authorized_keys
          owner: ubuntu:ubuntu
          permissions: '0600'
          defer: true
          content: |
            ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPBIo3wtbTU2WCXdK3f8w8+SUxIfwClwrfxQ1QTk5JX+ lowone@lowone-Standard-PC-i440FX-PIIX-1996
    USERDATA
    file_name = "vault-user-data.yaml"
  }
}

resource "proxmox_virtual_environment_vm" "vault" {
  name      = "vault"
  vm_id     = 251
  node_name = "grid"
  on_boot   = true

  clone {
    vm_id = 9000
    full  = true
  }

  agent { enabled = true }

  cpu {
    cores = 2
    type  = "host"
  }

  memory { dedicated = 2048 }

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
    datastore_id      = "local-lvm"
    user_data_file_id = proxmox_virtual_environment_file.vault_userdata.id

    ip_config {
      ipv4 {
        address = "192.168.0.251/24"
        gateway = "192.168.0.1"
      }
    }
  }

  purge_on_destroy    = true
  reboot_after_update = true
}

output "vault_ip" {
  value = "192.168.0.251"
}
