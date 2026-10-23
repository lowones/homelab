# lowone admin account on both quinn VMs (key-only, passwordless sudo). Runs after each VM's quinn
# setup so it never races the sshd restart that playbook does. Separate resources, so a replace of
# one never touches the other or quinn's account.
resource "null_resource" "lowone_account_quinn" {
  depends_on = [null_resource.quinn_setup]

  provisioner "local-exec" {
    command = "ansible-playbook --vault-password-file ~/.vault_pass -i '192.168.0.252,' -u ubuntu ${path.module}/../ansible/lowone-account.yml"
  }
}

resource "null_resource" "lowone_account_target" {
  depends_on = [null_resource.quinn_target_setup]

  provisioner "local-exec" {
    command = "ansible-playbook --vault-password-file ~/.vault_pass -i '192.168.0.247,' -u ubuntu ${path.module}/../ansible/lowone-account.yml"
  }
}
