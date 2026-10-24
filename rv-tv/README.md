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
  15 min (`TV_ITINERARY_REFRESH=<seconds>`, minimum 60). Setting a place by hand pins it;
  `./tv place auto` or "Follow schedule" goes back to the sheet.
- **Reminder** (same trip tabs, nothing to configure): today's row can put a red "Reminder" card at the
  top right, cut off after three lines. Two columns fire it. **Column G (REMINDER):** shown unless the
  cell is only a date that is already past (`m/d/yyyy`, `m/d/yy` or `yyyy-mm-dd`; the year must be in
  the cell, so format the column to show it). Anything else in the cell shows: text, a date plus text,
  a bare `6/25`. **Column L (INFO):** any text there shows too, even when G is a past date. When both
  fire, G is the first line and INFO the second. **A date in G is looked up:** the row for that date in
  the trip tab for its year (the 2027 tab must be in `TV_ITINERARY`) is read, and if its city (column D)
  has text the card shows the city, with the date and the campground (column E) under it, in green when
  column H (terms confirmation) has any text and red when it does not (a check mark or `!` is added so
  the two do not rely on color). No row or no city: the date shows as plain text. Past dates stay hidden.
  Only today's row counts, so a reminder is written on
  the day it should appear. When the sheet cannot be fetched the last good copy keeps being used.
  Adding the next year's tab to `TV_ITINERARY` covers it.
- **Private trip sheet (service account):** to stop sharing the sheet by link, make a Google service account, share the
  sheet with its email as Viewer, put the downloaded JSON key somewhere only the mini can read it (for example
  `rv-tv/.run/google-sa.json`, mode 600, never in git or `tv.env`) and add `TV_GOOGLE_KEY=<that path>` to `tv.env`, then
  `bash install-tvserver.sh`. The server signs a short login with the key (using the Mac's `openssl`), gets a one-hour
  token, and sends it only to Google hosts when it reads the trip tabs and the campgrounds tab. Without `TV_GOOGLE_KEY`
  nothing changes. If Google refuses (sheet not shared with the account, key revoked) the dashboard keeps the last good
  copy and `itinerary_error` in `/display/state` says why. Tests: `tests/test_google_auth.py`.
- **Moon phase** at the right of the weather row (name, icon, percent lit). Worked out on the server
  from the date, so it needs no internet. `TV_MOON=off` hides it.
- **Open sheet** button: `TV_SHEET_URL=<sheet link>` in `tv.env` shows that page fullscreen on the Mac
  instead of the dashboard (same permission as the dashboard; `./tv sheet`). The kiosk browser has its
  own profile: sign in to Google there once (screen-share to the mini) if the sheet is not public.
  Quit with the "Close screen" button, or Cmd+Q on the Mac.
- CLI: `./tv display open|close|state`, `./tv sheet`, `./tv place "City, State"|auto`.

- **Map pins for post offices, libraries and Costco:** blue envelope, purple book, green cart. The server asks OpenStreetMap
  (Overpass) once for everything near the Mac (cached 24 h, last good answer kept when the link drops); the map shows only the
  nearest `TV_POI_COUNT` (default 5) of each kind that are inside the picture, no labels. `TV_POI=off` hides them,
  `TV_OVERPASS_URL` takes one URL or a comma-separated list (default: overpass.openstreetmap.fr, then
  overpass-api.de); a server that fails or times out is skipped for the next one. Highways use the same
  list. `/display/data` has a `layer_errors` object saying why a layer is empty or stale. Needs internet;
  with none, the pins just don't appear.

## Canny cameras (live video on the TV)
Phone menu: **BatCam > Canny (live)** has one button per camera (Driveway, Coop, Barn, Lean-To, RV,
BatCave) and **All cameras** (a 2-wide grid). A camera opens fullscreen in Safari on the Mac; Home /
Close screen / Show dashboard remove it. BatCave shows as "not connected" until it is on the LAN.
- Video comes from **go2rtc on oracle** (house LAN). It asks each camera over ONVIF for its current
  stream address (the cameras rotate it every few minutes), so nothing is hardcoded. Config example:
  `cams/go2rtc.yaml.example`. Streams are `NAME` (full size, single view) and `NAME_sub` (640x360, grid).
- The Mac reaches it through an ssh tunnel (`ssh -N -L 1984:127.0.0.1:1984 oracle-r`, kept up by the launchd
  agent below), so go2rtc is `http://localhost:1984` on the Mac. `TV_GO2RTC_URL` overrides that.
- `TV_CAMS_LIVE=driveway,coop` in `tv.env` lists the cameras that are set up in go2rtc (default
  `driveway`); the rest show "not connected". The page shows a red banner when the tunnel is down.
- Endpoints: `POST /display/cams?cam=driveway|coop|barn|leanto|rv|batcave|all` (`display` permission),
  `GET /display/cams?src=...` (the page itself, no secrets).

Keeping it running (survives reboots and drops):
- **oracle:** go2rtc is a systemd service installed by Ansible: `cd ansible/cams && ansible-playbook cams.yml -K`
  (see `ansible/cams/README.md`; cameras and their logins are configured there, not in this folder).
- **mini:** `bash install-cams-tunnel.sh` installs a launchd agent that holds the ssh tunnel to oracle
  (`TV_CAMS_SSH_HOST`, default `oracle-r`), starts it at login and reconnects within about 15 s.
  `--print` shows the plist, `--remove` takes it out. Log: `.run/camtunnel.log`.

## Network test for video calls (`nettest.py`)
`python3 nettest.py` (or `./tv nettest`) runs a 10-minute test from the machine it is on and writes a
report with a graph per target plus health numbers (call-quality score, round trip, jitter, loss, longest
dropout, a verdict of Good / Marginal / Poor). Standard library only. Runs live in `.run/nettest/<date-time>/`
(`result.json`, `report.html`, `raw/` = what ping and iperf3 printed). `python3 nettest.py report [DIR]` redraws a
report, `latest` prints the newest folder, `--duration 60` shortens the test, Ctrl-C stops early and still saves.
- **Ping** (1/s) to every target, **TCP connect timing** (1/s) to Meet, Zoom and Teams (they ignore ping), and
  **iperf3 UDP** (1.5 Mbit/s both ways, like a video call) to servers listed in `TV_NETTEST_IPERF`.
- iperf3 needs `brew install iperf3` on the mini and a server on the far end (`iperf3 -s`; oracle already has
  one on 5201). Without it the test still runs on ping and TCP and says so.
- Config in `tv.env`: `TV_NETTEST_IPERF=vps=host,oracle=host`, `TV_NETTEST_PING`, `TV_NETTEST_TCP`,
  `TV_NETTEST_SECONDS`, `TV_NETTEST_KBPS` (formats at the top of `nettest.py`; flags override).
- How the verdict is decided is printed at the bottom of every report. Thresholds are constants at the top of
  `nettest.py` (`GOOD`, `MARGINAL`, `OUTAGE_*`). When most targets drop the same second, the report blames the
  link (dish, Wi-Fi); when only one does, it blames that path.
- **Place and time log:** every run records when it ran and where, from the mini's own location fix (the same
  `.run/location.raw` the phone menu uses; only used if under 6 hours old, otherwise the run is logged with no
  place and says so). `result.json` has `place` and `geo` (lat, lon to 4 places, accuracy in m). One line per run
  also goes to `.run/nettest/history.jsonl` (verdict, score, numbers, and the run as 10-second points), and older
  runs are added the first time it syncs. `python3 nettest.py history` lists them.
- **Compare runs:** `python3 nettest.py compare` writes `.run/nettest/compare.html` (`--out FILE` to put it
  elsewhere). One line per run on one graph, x = time since that run started, y = round trip (median, with the
  95th as a band when three or fewer runs are drawn), jitter or packets lost. Tick runs in the table to add them
  (up to 8, one colour each), click headings to sort, "Show short runs" lists runs under 5 minutes (hidden and not
  drawn by default; the 60 s test run is one). It warns when the drawn runs did not test the same targets, since
  that makes a difference look like a place effect. Each row has a map link from its coordinates.
- A target is only called weaker than the rest when at least 3 pings were lost (`MIN_LOST`); 1 lost ping in 60 is
  1.7 % but is not evidence of a bad path.
- **Jitter** comes from the iperf3 UDP stream when there is one. A target without its own UDP stream (Cloudflare,
  Meet, ...) uses the link's UDP jitter, marked "link udp" in the table. With no UDP at all it is the typical
  (median) second-to-second change of the ping or TCP time, and that can cost a target one level, never two: a
  single stalled second used to read as 60 ms of jitter and a Poor verdict.
- **Rescore:** `python3 nettest.py rescore [DIR...]` judges saved runs again under the current rules (default all):
  verdicts, jitter and the explanatory notes change, the measurements do not. The first `result.json` is kept as
  `result.orig.json`; reports and `history.jsonl` are redrawn.
- **From the phone menu:** the BatCam button is now BatCam | Tools (both need the `display` permission). Tools >
  Network test has Start 10 min, Quick 1 min and Stop, a progress bar while it runs, then the verdict, score and
  numbers of the last run, **Full report** (the report page with its graphs) and **Compare places**. The server
  starts `nettest.py` in the background (one test at a time; output in `.run/nettest/last.log`) with `/opt/homebrew/bin`
  added to its PATH, since launchd's PATH has no `iperf3`. Endpoints, all `display`: `POST /tools/nettest/start?seconds=`,
  `POST /tools/nettest/stop`, `GET /tools/nettest/status`, `GET /tools/nettest/report?run=latest|<id>`,
  `GET /tools/nettest/compare`. Reports hold the RV's coordinates, so they are only served to a valid token and shown
  in a sandboxed frame. After pulling: `bash install-tvserver.sh` to restart the service.
- Checks: `python3 tests/test_nettest.py` (about 40 s; uses stand-in `tests/fakeping.py` and `fakeiperf.py`, and a
  real iperf3 over loopback when one is installed; the fixtures in `tests/fixtures/` are real iperf3 3.22 output).

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
