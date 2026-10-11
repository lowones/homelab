# matrix: camera pipeline, deployed from batcomputer

Ansible for the matrix camera system (Pi nodes → hub → low-ones VPS). The app
code lives in the private `matrix_matrix` repo; this directory only says how to
get a host ready to run it. Same layout as `../tabcolor/`.

## Phase 1: the hub (Oracle)

Prepares `oracle` (192.168.0.240) to take over from Tron. It installs and
configures everything **but does not enable or start any matrix service**, so
it cannot compete with Tron, the live hub.

What `roles/matrix_hub` does:

- `matrix` group, `lowone` added to it, `/matrix` tree (`bin etc log deploy stash outbound update inbound cabin driveway`, `lowone:matrix`, setgid)
- apt: `rsync imagemagick python3-venv git openssh-client`; checks that `convert` works (links it to `magick` if only ImageMagick 7's `magick` exists)
- `/matrix/.venv` with `requests` and `psutil`
- rsyncs `bin/` and `deploy/` from the controller's `matrix_matrix` checkout (no GitHub access needed on Oracle); `lowlib.py` from `vendor/lowlib.py`
- `/matrix/etc/matrix.conf` from `group_vars/all.yml` (with `DRY_RUN=1` until cutover)
- `matrix-monitor.service` (from the repo's `deploy/tron/`) plus a CPU/memory-limit drop-in and the logrotate rule: installed, not enabled
- Oracle's own ssh key (`~/.ssh/id_ed25519_matrix`, public key printed at the end) and `low-ones`/`cabin`/`driveway` aliases in `~/.ssh/config`
- optional: public keys from `~/.ansible-matrix/authorized_keys` on the controller added to `lowone`'s `authorized_keys`
- verifies: `import lowlib` on the hub's Python, byte-compiles the monitor, `convert`/`rsync` work, and **fails if a matrix service is already active**

Not in phase 1 (do these next, deliberately):

- `/matrix/etc/matrix.env` (the three healthcheck UUIDs), because a second hub must not ping the same checks
- `tunnel-lowones.service` (only one host can hold port 8675)
- the Pis' and the low-ones container's `known_hosts` entries for Oracle
- authorizing Oracle's new key on the Pis and low-ones

## Phase 1b: shadow run (dry-run monitor fed from Tron)

Runs Oracle's monitor for real, but in **dry-run mode**, on images pulled from
Tron, so you can compare it with the live hub before cutover. `DRY_RUN=1` in
`matrix.conf` makes the monitor resize and stash as normal but never rsync, ssh
to low-ones or ping healthchecks. As a second guard the play refuses to run if
`matrix.env` (the healthcheck UUIDs) exists, and the tunnel to low-ones stays off.

One-time, on **Tron**: authorize Oracle's key, read-only and locked to `/matrix`.
Oracle's public key is printed at the end of a normal run (or `cat
~/.ssh/id_ed25519_matrix.pub` on Oracle). `which rrsync` must find it.

    cp ~/.ssh/authorized_keys ~/.ssh/authorized_keys.bak-$(date +%F)
    echo 'from="192.168.0.240",restrict,command="/usr/bin/rrsync -ro /matrix" <paste the public key>' >> ~/.ssh/authorized_keys

Then from the controller:

    ansible-playbook hub.yml -K -e matrix_hub_shadow=true

This also scans Tron's ed25519 host key and prints its fingerprint; compare with
`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` on Tron, or pass
`-e matrix_hub_shadow_hostkey_fingerprint=SHA256:...` to make the play check it.
It installs `matrix-shadow-feed.timer` (every 30 s: rsync each `<node>/<node>.jpg`
from Tron, read-only), then starts the timer and `matrix-monitor`. The feed does
not touch the update triggers, because Tron only pushes on its 5-minute stale
poll. Watch the monitor with `tail -f /matrix/log/monitor.log` (the unit logs to
that file, not the journal): expect about one `DRY RUN: would rsync ...` per
camera every 5 minutes, the same as Tron's real pushes.

End the shadow run (the play refuses to run without `matrix_hub_shadow=true`
while the monitor is up):

    sudo systemctl disable --now matrix-monitor matrix-shadow-feed.timer

Remove the restricted line from Tron's `authorized_keys` when you are done.

Cutover is a later, separate step: `matrix_hub_dry_run=false`, `matrix.env`,
the tunnel, and the `known_hosts`/authorized_keys changes on the Pis and low-ones.

## Needs on the controller (batcomputer)

- `ansible-core` 2.16+ with the `ansible.posix` collection (`ansible-galaxy collection list | grep posix`; the `ansible` package includes it)
- `rsync`
- a `matrix_matrix` checkout at `~/dev/matrix_matrix` (or `-e matrix_src=...`) containing `bin/image_update_monitor.py`, `bin/matrix_config.py`, `deploy/tron/matrix-monitor.service`, `deploy/tron/logrotate-matrix-monitor` and `vendor/lowlib.py`. The play stops before touching Oracle if any is missing.

## Run

    cd ~/dev/homelab/ansible/matrix
    ansible-playbook hub.yml --check --diff -K     # dry run; -K asks for lowone's sudo password
    ansible-playbook hub.yml -K
    ansible-playbook hub.yml -K                    # again: expect changed=0

`--check` works on a fresh host; some verification steps are skipped there
because nothing is installed yet. The target needs `python3-apt` (default on
Debian) for the apt module's check mode.

## Notes

- Public repo: no secrets, no host keys, no `lowlib.py` here. Keep it that way.
- `matrix.conf` is written without comments because the config parser's
  tolerance for them is unverified. Compare it with Tron's before cutover.
