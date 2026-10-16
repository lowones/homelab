# rv-tv

Control service for the RV's LG webOS TV (QNED75B), run on the Mac mini (`neverland`).
FastAPI over Tailscale, bearer-token auth, a phone-friendly guest page, Wake-on-LAN.

## Layout
- `tvserver.py` the service (`python tvserver.py pair` to pair; `token add|revoke|list|link` for guests)
- `tv` command-line wrapper (`./tv status`, `./tv on`, `./tv input HDMI_1`)
- `install-tvserver.sh` venv, `tv.env`, launchd agent `com.lowone.tvserver` (prod, port 8780)
- `run-dev.sh` dev instance on port 8781 (`bash run-dev.sh mock` = fake TV)
- `tests/smoke.sh` access-rule checks against a fake TV; `tests/mocktv.py` the fake TV
- `migrate-from-tvtest.sh` one-time move of prod from `~/dev/tvtest`

Never committed (see `.gitignore`): `tv.env`, `tv_key.txt`, `tokens.json`, `.venv`.
Treat the token and guest links like passwords.

## Workflow
1. `git switch -c my-feature`
2. Edit, then `bash tests/smoke.sh` (must print ALL PASSED)
3. `bash run-dev.sh mock` to click through the guest page against the fake TV, or
   `bash run-dev.sh` to try it on the real TV (dev token printed at start)
4. Merge, then on the mini: `git pull && launchctl kickstart -k gui/$(id -u)/com.lowone.tvserver`
5. Release: `git tag rv-tv-vN`. Roll back: `git checkout rv-tv-vN -- rv-tv && kickstart` as above.

## Facts
TV `192.168.1.201` (DHCP lease, not reserved), MAC `ec:31:11:a2:e5:16`. HDMI_1 = the mini.
Mini `pmset displaysleep` must be 0 or the TV loses signal and powers itself off.
Guests: On/Off, Netflix, Prime, YouTube, Mac input, volume (see `GUEST_*` in `tvserver.py`).
