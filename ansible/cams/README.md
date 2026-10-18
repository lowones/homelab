# cams: go2rtc on oracle

Video relay for the Canny Cams that the RV TV dashboard (`rv-tv/`) shows. go2rtc runs as a systemd
service on oracle (starts at boot, restarts if it dies); its API stays on `127.0.0.1:1984` (it has no
login) and the Mac mini reaches it through the ssh tunnel from `rv-tv/install-cams-tunnel.sh`.

```
cd ansible/cams
ansible-playbook cams.yml -K          # -K: sudo password on oracle
ansible-playbook cams.yml -K --check --diff   # dry run (config diff is hidden: it holds logins)
```

First run: stop a go2rtc started by hand first (Ctrl-C in its terminal), and compare the binary it was
using with the pinned one if you like: `sha256sum ~/go2rtc/go2rtc` should print the value of
`go2rtc_sha256` in `roles/go2rtc/defaults/main.yml`. The play refuses to continue if port 1984 is busy.

## Adding or changing a camera
Edit `go2rtc_cameras` in `group_vars/all.yml`, run the playbook, and add the name to `TV_CAMS_LIVE` in the
mini's `rv-tv/tv.env` (restart tvserver with `bash install-tvserver.sh`).

## Passwords
The cameras still have the factory `admin`/`admin` Onvif login (it is the vendor default, not a secret).
When a camera's password is changed, put it in Vault (`homelab/` KV, e.g. `homelab/cams/<name>`) and set
`password:` on that camera in `group_vars/all.yml` with a `community.hashi_vault.hashi_vault` lookup
(an example is in that file). Needs `ansible-galaxy collection install community.hashi_vault` on
batcomputer and `VAULT_ADDR`/`VAULT_TOKEN` in the environment.

## Notes
- Binary: pinned release + sha256 (`go2rtc_version`, `go2rtc_sha256`). Update both to upgrade.
- Config `/etc/go2rtc/go2rtc.yaml` is root:go2rtc 0640 and is rewritten by the play: do not hand-edit it.
- Logs: `journalctl -u go2rtc -f` on oracle.
