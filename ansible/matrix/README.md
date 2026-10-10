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
- `/matrix/etc/matrix.conf` from `group_vars/all.yml`
- `matrix-monitor.service` (from the repo's `deploy/tron/`) plus a CPU/memory-limit drop-in and the logrotate rule: installed, not enabled
- Oracle's own ssh key (`~/.ssh/id_ed25519_matrix`, public key printed at the end) and `low-ones`/`cabin`/`driveway` aliases in `~/.ssh/config`
- optional: public keys from `~/.ansible-matrix/authorized_keys` on the controller added to `lowone`'s `authorized_keys`
- verifies: `import lowlib` on the hub's Python, byte-compiles the monitor, `convert`/`rsync` work, and **fails if a matrix service is already active**

Not in phase 1 (do these next, deliberately):

- `/matrix/etc/matrix.env` (the three healthcheck UUIDs), because a second hub must not ping the same checks
- `tunnel-lowones.service` (only one host can hold port 8675)
- the Pis' and the low-ones container's `known_hosts` entries for Oracle
- authorizing Oracle's new key on the Pis and low-ones

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
