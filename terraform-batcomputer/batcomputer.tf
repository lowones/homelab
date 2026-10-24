# batcomputer (VM 101): the Ubuntu desktop VM that runs Terraform and Ansible for this repo.
#
# Left out on purpose, as they are on grid: the guest agent is not set up (no agent line), and the Ubuntu
# desktop install ISO is still attached as ide2. Declaring either made the plan change them (the provider's
# cdrom block defaults to enabled = false, which would eject the ISO), so the VM's config is not touched.
#
# It already existed (created 2025-04-29 by hand from the Ubuntu 24.04.2 desktop ISO), so this file
# describes it as it is on grid and it is imported, not created:
#   terraform import proxmox_virtual_environment_vm.batcomputer grid/101
# then `terraform plan` must show nothing (or only harmless provider defaults) before any apply.
#
# It has its own folder and state on purpose. In terraform/ (the k8s cluster) a `terraform destroy`
# for a rebuild would take this machine with it; here prevent_destroy refuses that.
resource "proxmox_virtual_environment_vm" "batcomputer" {
  name      = "batcomputer"
  vm_id     = 101
  node_name = "grid"
  on_boot   = true

  operating_system {
    type = "l26"
  }

  cpu {
    cores   = 4
    sockets = 1
    type    = "x86-64-v2-AES"
  }

  memory {
    dedicated = 2500
    floating  = 0
  }

  scsi_hardware = "virtio-scsi-single"
  boot_order    = ["scsi0", "ide2", "net0"]

  disk {
    datastore_id = "DATA"
    interface    = "scsi0"
    file_format  = "qcow2"
    size         = 100
    iothread     = true
  }

  network_device {
    bridge      = "vmbr0"
    model       = "virtio"
    mac_address = "BC:24:11:60:F4:EB"
    firewall    = true
  }

  # a change must never stop or reboot the machine that runs Terraform
  reboot_after_update = false

  lifecycle {
    prevent_destroy = true
  }
}
