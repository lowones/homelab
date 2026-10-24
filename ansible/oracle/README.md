# oracle (Ansible)

Tailscale and the iperf3 server on oracle (192.168.0.240, Debian 13). The RV network test
(`rv-tv/nettest.py`) measures UDP loss and jitter against `oracle:5201` over the tailnet.

Run from batcomputer, on the LAN:

    cd ~/dev/homelab/ansible/oracle
    ansible-playbook tailscale-iperf.yml --check --diff -K    # a first run should show no changes
    ansible-playbook tailscale-iperf.yml -K

- Both pieces were installed by hand first, so this file records the setup. It has not been run against oracle yet.
- The playbook never stores a Tailscale login. If oracle is not logged in, it stops and prints the `tailscale up` command.
- Tailscale DNS is off (`--accept-dns=false`) because oracle runs its own resolver.
- Port 5201 is open to the whole tailnet. To limit it, add a tag for oracle and an ACL rule in the Tailscale admin console that allows only the RV mini (neverland) to reach `oracle:5201`. This has not been applied.
- The DDNS scripts are in `../../oracle/`.
