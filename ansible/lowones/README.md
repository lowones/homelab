# low-ones VPS + RV Pi tunnel (Ansible)

Manages the RV Pi (`zero`, standalone, not part of matrix) reverse tunnel to the low-ones VPS, and the
VPS-side key that allows it. First step toward putting the hand-edited VPS under Ansible.

Ports on the VPS: **8675** tron, **8676** oracle, **8677** zero. Only 8677 is managed here;
tron's and oracle's hand-made tunnels are untouched (the matrix cutover is in flight).

| File | What it does |
|---|---|
| `vps-tunnel-keys.yml` | Adds the Pi's key to `~sshtunnel/.ssh/authorized_keys` on the VPS. Non-exclusive. Does not touch sshd. |
| `pi-tunnel.yml` + `roles/ssh_reverse_tunnel` | Pi side: tunnel user, key check, pinned VPS host key, systemd unit. Never restarts the tunnel. |
| `vps-sshd.yml` | sshd drop-in `10-keepalive.conf` (ClientAlive 30s x3) so a vanished tunnel's stale session frees its port in ~90s. Validated with `sshd -t`, reload only. |
| `vps-tunnel-health.yml` | VPS cron (1/min) reads the SSH banner through port 8677 and pings Healthchecks.io (`/fail` if silent). Ping URL passed with `-e`, not stored in the repo. |
| `bootstrap-pi.sh` | One-time hand bootstrap of the Pi (pin + unit, not enabled), used before Ansible can reach the Pi. |

## Security model

The Pi's key line on the VPS is `restrict,port-forwarding,permitopen="192.0.2.1:1",permitlisten="localhost:8677"`:
it can open `-R localhost:8677` and nothing else. No shell (`sshtunnel` has `/bin/true`), no other ports,
no public bind, no `-L` pivot. `permitopen` points at an RFC 5737 address because authorized_keys has no
`none` keyword (sshd rejects the whole line). The VPS host key is pinned (`StrictHostKeyChecking=yes`);
the fingerprint in `group_vars/all.yml` was verified on the VPS itself.

## Order of operations

1. **Check the key.** On the Pi: `ssh-keygen -lf /var/lib/rvtunnel/id_ed25519.pub` must print
   `SHA256:8nh5HUHv6zAkRMZm9nOJTQ6waznbOS1lUGyS3CNHFaY` (matches `rvtunnel_pubkey`).
2. **Pi user + key (already done by hand once):**
   `sudo useradd --system --create-home --home-dir /var/lib/rvtunnel --shell /usr/sbin/nologin rvtunnel`
   then `sudo -u rvtunnel ssh-keygen -t ed25519 -N "" -C rvtunnel@zero -f /var/lib/rvtunnel/id_ed25519`.
3. **VPS key first** (fail2ban's sshd jail is active; a Pi retrying with an unauthorized key can get the RV's IP banned):
   ```
   cd ansible/lowones
   ansible-playbook vps-tunnel-keys.yml --check --diff -K
   ansible-playbook vps-tunnel-keys.yml -K
   ```
4. **Pi bootstrap** (over Tailscale, on the Pi): run `bootstrap-pi.sh`.
5. **Start it** (on the Pi): `sudo systemctl enable --now rvtunnel-lowones`, then
   `journalctl -u rvtunnel-lowones -n 10 --no-pager`.
6. **Verify on the VPS:** `ss -tlnp | grep 8677` shows `127.0.0.1:8677` (and `[::1]`), never `0.0.0.0`;
   `ssh -p 8677 lowone@localhost hostname` prints `zero`.
7. **Let batcomputer jump through the VPS:** `ansible-playbook vps-admin-keys.yml --check --diff -K`, then
   without `--check` (see "Jump host for batcomputer" below). Without it ProxyJump fails with
   "administratively prohibited". Keep another VPS session open while applying: this is the key Ansible uses.
8. **Authorize batcomputer's key on the Pi** (once, by hand over Tailscale; the Pi is key-only). On batcomputer
   print the command, then paste it on the Pi:
   ```
   echo "mkdir -p ~/.ssh && chmod 700 ~/.ssh && echo '$(cat ~/.ssh/id_ed25519.pub)' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
   ```
9. **Adopt with Ansible** from batcomputer (goes through the tunnel). First connect once by hand to
   record the Pi's host key under the alias the inventory uses, and compare it with
   `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` on the Pi:
   ```
   ssh -o ProxyJump=lowone@service.low-ones.com:7822 -o HostKeyAlias=zero -p 8677 lowone@localhost hostname
   ansible-playbook pi-tunnel.yml --check --diff
   ```
   Expect exactly one change: the "Tunnel key" task, because `openssh_keypair` sets the `.pub` file's mode to
   match the private key (0644 -> 0600). It does not regenerate the key; check the fingerprint is unchanged
   before and after (`sudo ssh-keygen -lf /var/lib/rvtunnel/id_ed25519.pub`).

## Caveats

- **The role never restarts the tunnel** (it would cut its own connection). If the unit changes, restart it
  over Tailscale: `sudo systemctl restart rvtunnel-lowones`.
- The tunnel exposes the Pi's sshd on the VPS loopback. Make sure the Pi is key-only
  (`sudo sshd -T | grep -E "passwordauthentication|permitrootlogin"`); Raspberry Pi OS defaults to passwords on.
- `RestartSec=60`, same as oracle's unit, to stay under fail2ban's radar during outages.
- If the Pi is rebuilt it gets a new key: update `rvtunnel_pubkey` and re-run `vps-tunnel-keys.yml`.
  The role asserts the Pi's key matches the var, so a mismatch fails loudly instead of silently.
- Next phases (not started): rest of the VPS under Ansible (sshd drop-ins incl. post-quantum KEX and
  `AcceptEnv`, firewalld, fail2ban, Caddy, matrix-watchdog, cron), then restrict tron's/oracle's keys
  (tron's matrix tunnel also binds `172.17.0.1:8675`, so its `permitlisten` must allow that too).

## Jump host for batcomputer (`vps-admin-keys.yml`)

batcomputer's Ansible key on the VPS (user `lowone`, comment `batcomputer-ansible`) was `restrict,pty`,
so ProxyJump through the VPS to the Pi's tunnel port failed with "administratively prohibited".
This play rewrites that one line to
`restrict,pty,port-forwarding,permitopen="localhost:8677",permitlisten="192.0.2.1:1"`:
it may forward to the Pi's port and nothing else, and still cannot open listeners (`permitlisten`
points at RFC 5737 TEST-NET-1 for the same reason as `permitopen` above). `lowone` already has sudo
and a shell on the VPS, so this adds no real capability. Run it before `pi-tunnel.yml`.
The target string must stay `localhost`, matching the inventory's jump target.

## Stale session / "remote port forwarding failed" (seen Oct 14)

Symptom (Pi): `journalctl -u rvtunnel-lowones` shows `Error: remote port forwarding failed for listen port 8677`
every ~60s. Cause: the old session died without a FIN (Starlink blip) and the VPS still holds
`127.0.0.1:8677`, because `ClientAliveInterval` was 0. With `ExitOnForwardFailure=yes` every retry then fails.

- Permanent fix: `ansible-playbook vps-sshd.yml -K` (verify: `sudo sshd -T | grep clientalive` -> 30 / 3).
- Manual unblock, if it ever recurs: on the VPS `sudo ss -tlnp | grep :8677`, check the PID is an `sshtunnel`
  session (`ps -o pid,user,etime,cmd -p <pid>`), then `sudo kill <pid>`.
- The unit has `ConnectTimeout=20`: during an outage a connect attempt gives up in 20s instead of hanging ~2 min,
  so recovery after the network returns is ~1 min. Changing the unit means a manual restart over Tailscale
  (`sudo systemctl restart rvtunnel-lowones`); the role never restarts the tunnel itself.
- Alarm: `ansible-playbook vps-tunnel-health.yml -K -e rvtunnel_hc_url=https://hc-ping.com/<uuid>`.
- Test the recovery on purpose (the Pi stays reachable over Tailscale): on the Pi
  `sudo iptables -I OUTPUT -p tcp --dport 7822 -j DROP`, wait ~3 min, then
  `sudo iptables -D OUTPUT -p tcp --dport 7822 -j DROP`; the tunnel should return without help.
