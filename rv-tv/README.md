# rv-tv

Control service for the RV's LG webOS TV (QNED75B), run on the Mac mini (`neverland`).
FastAPI over Tailscale, bearer-token auth, a phone-friendly guest page, Wake-on-LAN.

## Layout
- `tvserver.py` the service (`python tvserver.py pair` to pair; `token add|revoke|list|link` for guests)
- `tv` command-line wrapper (`./tv status`, `./tv on`, `./tv input HDMI_1`)
- `install-tvserver.sh` venv, `tv.env`, launchd agent `com.lowone.tvserver` (prod, port 8780)
- `display/` the dashboard shown on the TV (`index.html`, `display.css`, `display.js`, `layout.json`, `widgets/*.js`)
- `run-dev.sh` dev instance on port 8781 (`bash run-dev.sh mock` = fake TV)
- `tests/smoke.sh` access-rule checks against a fake TV; `tests/mocktv.py` the fake TV
- `migrate-from-tvtest.sh` one-time move of prod from `~/dev/tvtest`

Never committed (see `.gitignore`): `tv.env`, `tv_key.txt`, `tokens.json`, `display.json`, `.venv`, `.run/`.
Treat the token and guest links like passwords.

## Dashboard on the TV
One tap on the control page (`/guest#<token>`, buttons only shown if your token may use them) runs
`POST /display/open`: wake the TV, switch it to `TV_MAC_INPUT` (HDMI_1), and open a fullscreen
browser window on the Mac pointed at `/display`. `POST /display/close` closes it again.
- Needs Google Chrome, Brave, Edge or Firefox on the Mac (kiosk mode). Set `TV_DISPLAY_BROWSER` in
  `tv.env` to pick one. The window uses its own profile in `.run/`, so your normal browser is untouched.
- The Mac must be logged in and unlocked, or the window cannot appear.
- Who may press it: admin only by default. Give a guest the button with
  `python tvserver.py token add NAME --display`.
- The page reads a short-lived token that exists only while the dashboard is open; it can read
  `/display/data` and nothing else. Restarting the service closes the dashboard.
- The caller never supplies a URL or command: the page and browser are fixed in the service.
- Preview from any device: open `/display#<admin token>`.
- Weather (Open-Meteo, no key) is cached 10 min and fetched by the Mac. Set the place with
  `./tv place "Moab, Utah"` or the box on the control page (admin). Default is Lansing, MI.
- Add a widget: drop `display/widgets/<id>.js` exporting `{init, render(el, data), tick(el)}` and
  list `{"id": "<id>", "span": N}` in `display/layout.json` (12-column grid). `data` is the JSON from
  `/display/data` (`tv`, `weather`, `now`); add fields there in `tvserver.py` for new data sources.
- **Weather follows the trip schedule** (optional). Share the sheet as "anyone with the link can
  view" and put the CSV export of each tab in `tv.env`, comma-separated:
  `TV_ITINERARY=https://docs.google.com/spreadsheets/d/<ID>/export?format=csv&gid=<GID>,...`
  (a path to a saved `.csv` also works). Today's city is the last one named on or before today
  (column 4; blank rows mean "still there"; ignored 14 days after the last dated row). Refetched every
  6 h. Setting a place by hand pins it; `./tv place auto` or "Follow schedule" goes back to the sheet.
- **Open sheet** button: `TV_SHEET_URL=<sheet link>` in `tv.env` shows that page fullscreen on the Mac
  instead of the dashboard (same permission as the dashboard; `./tv sheet`). The kiosk browser has its
  own profile: sign in to Google there once (screen-share to the mini) if the sheet is not public.
  Quit with the "Close screen" button, or Cmd+Q on the Mac.
- CLI: `./tv display open|close|state`, `./tv sheet`, `./tv place "City, State"|auto`.

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
Guests: On/Off, Netflix, Prime, YouTube, Mac input, volume (see `GUEST_*` in `tvserver.py`); the dashboard button is opt-in.
The mini runs the macOS Command Line Tools Python 3.9: keep the code 3.9-compatible.

## Gotchas
- Re-running `install-tvserver.sh` restarts the service (a few seconds of downtime). It waits for the
  old copy to unload before loading the new one; if launchd still refuses, run it in Terminal on
  the Mac itself, not over SSH (the per-user launchd domain needs a desktop login).
- Verified 2026-10-15: after `sudo reboot` the service answers `./tv status` within about a minute
  with no one touching the Mac (Local Network approval persists).
- Release tags: `rv-tv-v1` = guest tokens + guest page, repo layout.
