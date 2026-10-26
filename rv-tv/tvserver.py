#!/usr/bin/env python3
"""RV TV control service for an LG webOS TV.

Run:   python tvserver.py          start the API
       python tvserver.py pair     one-time pairing (accept the prompt on the TV)

Config comes from tv.env next to this file (KEY=VALUE lines) or the environment:
  TV_IP, TV_MAC, TV_TOKEN (required), TV_BIND, TV_SERVER_PORT, TV_WS_PORT, TV_KEYFILE
Every API endpoint except /health needs:  Authorization: Bearer <token>
(the admin TV_TOKEN, or a guest token from tokens.json). /guest and /display serve pages
that hold no secrets; they read the token from the URL fragment (#token).

Display feature: POST /display/open puts a fullscreen dashboard on the Mac's screen (which
is the TV when the TV is on HDMI_1); POST /display/close removes it.
  TV_DISPLAY_BROWSER  path to a Chrome/Brave/Edge/Firefox binary (default: first one found)
  TV_MAC_INPUT        the TV input the Mac is on (default HDMI_1)
  TV_WEATHER_URL      weather API base (default Open-Meteo, no key needed)
"""
import asyncio
import base64
import csv
import datetime
import io
import json
import logging
import math
import os
import re
import secrets
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path

import websockets
from fastapi import Depends, FastAPI, Header, HTTPException, Path as PathParam, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
try:  # newer wakeonlan releases renamed the function
    from wakeonlan import wake as send_magic_packet
except ImportError:
    from wakeonlan import send_magic_packet

BASE = Path(__file__).resolve().parent
log = logging.getLogger("uvicorn.error")


def load_env():
    f = Path(os.environ.get("TV_ENV_FILE", str(BASE / "tv.env")))
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                v = v.strip()
                if len(v) > 1 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]           # tv.env is also sourced by the shell, so URLs with & need quotes
                os.environ.setdefault(k.strip(), v)


load_env()
TV_IP = os.environ.get("TV_IP", "192.168.1.201")
TV_MAC = os.environ.get("TV_MAC", "ec:31:11:a2:e5:16")
TV_TOKEN = os.environ.get("TV_TOKEN", "")
TV_WS_PORT = int(os.environ.get("TV_WS_PORT", "3001"))
KEYFILE = Path(os.environ.get("TV_KEYFILE", str(BASE / "tv_key.txt")))
BIND = os.environ.get("TV_BIND", "127.0.0.1")
SERVER_PORT = int(os.environ.get("TV_SERVER_PORT", "8780"))
MAC_INPUT = os.environ.get("TV_MAC_INPUT", "HDMI_1")
DISPLAY_DIR = BASE / "display"
RUN_DIR = BASE / ".run"
STATE_FILE = Path(os.environ.get("TV_DISPLAY_FILE", str(BASE / "display.json")))
WEATHER_URL = os.environ.get("TV_WEATHER_URL", "https://api.open-meteo.com/v1/forecast")
GEOCODE_URL = os.environ.get("TV_GEOCODE_URL", "https://geocoding-api.open-meteo.com/v1/search")
DEFAULT_LOC = {"lat": 42.7325, "lon": -84.5555, "label": "Lansing, MI"}
# Optional: where the weather looks follows the trip schedule. Comma-separated CSV sources
# (a Google Sheets export URL per tab, or a path to a saved .csv). Column 2 is the date
# (m/d/yyyy), column 4 the city ("Moab, UT"); blank cities mean "still there".
ITIN_SOURCES = [x.strip() for x in os.environ.get("TV_ITINERARY", "").split(",") if x.strip()]
# How often the trip tabs are fetched again (seconds, at least 60). Short on purpose: column G
# holds reminders, and a note typed into the sheet should show up on the TV within minutes.
ITIN_REFRESH = max(60, int(os.environ.get("TV_ITINERARY_REFRESH", "900")))
REMINDER_COL = 6             # column G (0-based) of today's row is the reminder
CITY_COL, CAMP_COL, TERMS_COL = 3, 4, 7   # columns D (city), E (campground), H (terms confirmation) of a dated row
INFO_COL = 11                # column L (INFO): any text there also puts today's row on the reminder card
REMINDER_MAX = 300           # characters kept; the dashboard shows about three lines

# The Mac's own position, refreshed by locationagent.sh (see install-tvserver.sh).
LOCATION_FILE = Path(os.environ.get("TV_LOCATION_FILE") or Path(__file__).resolve().parent / ".run" / "location.raw")
DEVICE_MAX_AGE = 30 * 60     # seconds: an older fix is ignored
DEVICE_MAX_ACC = 5000        # meters: coarser than this is probably an IP-based guess
# Optional: the page the "Open sheet" button shows on the Mac's screen.
SHEET_URL = os.environ.get("TV_SHEET_URL", "").strip()
US_STATES = {"AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
             "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
             "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
             "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
             "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
             "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
             "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
             "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
             "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
             "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming"}

# Keep this list identical to the one used when the TV was paired. Permissions are
# fixed at pairing time, so changing it means pairing again.
PERMS = ["LAUNCH", "CONTROL_POWER", "CONTROL_INPUT_TV", "CONTROL_AUDIO",
         "READ_INSTALLED_APPS", "READ_INPUT_DEVICE_LIST", "READ_RUNNING_APPS"]


class TVUnreachable(Exception):
    pass


class TVError(Exception):
    pass


_locks = {}


def get_lock(name):
    """One asyncio.Lock per purpose, created on first use inside the running loop.
    (On Python 3.9 a Lock made at import time is tied to the wrong event loop and raises
    "attached to a different loop" as soon as two requests collide.)"""
    if name not in _locks:
        _locks[name] = asyncio.Lock()
    return _locks[name]


def load_key():
    try:
        return KEYFILE.read_text().strip() or None
    except FileNotFoundError:
        return None


def save_key(key):
    fd = os.open(str(KEYFILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key)


async def tv_connect(connect_timeout=5.0, register_timeout=15.0):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        ws = await asyncio.wait_for(
            websockets.connect(f"wss://{TV_IP}:{TV_WS_PORT}", ssl=ctx), connect_timeout)
    except (OSError, asyncio.TimeoutError, websockets.exceptions.WebSocketException) as e:
        raise TVUnreachable(str(e) or type(e).__name__)
    key = load_key()
    if not key and register_timeout < 60:
        await ws.close()
        raise TVError("not paired yet: run 'python tvserver.py pair' on the mini")
    payload = {"forcePairing": False, "pairingType": "PROMPT",
               "manifest": {"manifestVersion": 1, "permissions": PERMS}}
    if key:
        payload["client-key"] = key
    try:
        await ws.send(json.dumps({"type": "register", "id": "register_0", "payload": payload}))
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), register_timeout))
            if msg.get("type") == "registered":
                new = msg.get("payload", {}).get("client-key")
                if new and new != key:
                    save_key(new)
                return ws
            if msg.get("type") == "error":
                raise TVError(f"TV refused registration: {msg.get('error')}")
    except asyncio.TimeoutError:
        await ws.close()
        raise TVError("TV did not answer the registration (re-pair?)")
    except websockets.exceptions.WebSocketException as e:
        raise TVUnreachable(str(e) or type(e).__name__)


async def call(uri, payload=None, expect_close=False):
    async with get_lock("tv"):
        ws = await tv_connect()
        try:
            await ws.send(json.dumps(
                {"type": "request", "id": "req", "uri": uri, "payload": payload or {}}))
            while True:
                try:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), 10))
                except websockets.exceptions.ConnectionClosed:
                    if expect_close:
                        return {"returnValue": True}
                    raise TVUnreachable("TV closed the connection")
                except asyncio.TimeoutError:
                    raise TVError("TV did not answer the request")
                if msg.get("id") == "req":
                    break
        finally:
            try:
                await ws.close()
            except Exception:
                pass
    if msg.get("type") == "error":
        raise TVError(msg.get("error", "unknown TV error"))
    return msg.get("payload", {})


async def tv_probe(timeout=2.0):
    """Returns (state, detail): ('on', None), ('off', why we could not connect),
    or ('on', note) when the TV answers but registration has a problem."""
    try:
        ws = await tv_connect(connect_timeout=timeout)
    except TVUnreachable as e:
        return "off", str(e)
    except TVError as e:
        return "on", str(e)
    await ws.close()
    return "on", None


async def tv_is_up(timeout=2.0):
    state, _ = await tv_probe(timeout)
    return state == "on"


# ---- access control -------------------------------------------------------
# TV_TOKEN is the admin token (everything). Guests live in tokens.json next to this
# file, one token per person, so one can be revoked without touching the others:
#   {"kevin": {"token": "...", "actions": ["status","power_on", ...]}}
# "actions" is optional; without it a guest gets GUEST_ACTIONS. The file is re-read
# whenever it changes, so add/revoke needs no restart.
TOKENS_FILE = Path(os.environ.get("TV_TOKENS_FILE", str(BASE / "tokens.json")))
GUEST_ACTIONS = ["status", "power_on", "power_off", "input", "volume", "mute", "app"]
GUEST_INPUTS = {"HDMI_1"}                      # guests may only switch to the Mac
GUEST_APPS = {"netflix", "amazon", "youtube.leanback.v4"}
_tok_cache = {"mtime": None, "data": {}}
ALL_ACTIONS = GUEST_ACTIONS + ["display", "display_data", "stops", "admin"]
_display = {"token": None, "proc": None, "browser": None, "page": None,   # the running kiosk, if any
            "batcam": None}                                                # id of the Safari window BatCam opened


def load_tokens():
    try:
        st = TOKENS_FILE.stat()
    except FileNotFoundError:
        return {}
    if _tok_cache["mtime"] != st.st_mtime_ns:
        try:
            data = json.loads(TOKENS_FILE.read_text())
        except ValueError:
            log.error("tokens.json is not valid JSON; guest tokens disabled")
            data = {}
        _tok_cache.update(mtime=st.st_mtime_ns, data=data)
    return _tok_cache["data"]


def save_tokens(data):
    tmp = TOKENS_FILE.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, TOKENS_FILE)


class Who:
    def __init__(self, name, admin, actions):
        self.name, self.admin, self.actions = name, admin, actions


def identify(authorization):
    scheme, _, tok = authorization.partition(" ")
    if scheme.lower() != "bearer" or not tok:
        raise HTTPException(401, "bad or missing token")
    tb = tok.encode()
    if secrets.compare_digest(tb, TV_TOKEN.encode()):
        return Who("admin", True, None)
    dt = _display["token"]
    if dt and secrets.compare_digest(tb, dt.encode()):
        return Who("display", False, {"display_data"})   # the kiosk page: may read /display/data only
    for name, rec in load_tokens().items():
        if secrets.compare_digest(tb, str(rec.get("token", "")).encode()):
            return Who(name, False, set(rec.get("actions", GUEST_ACTIONS)))
    raise HTTPException(401, "bad or missing token")


def need(action):
    def dep(authorization: str = Header(default=""), stoptoken: str = Query(default="")):
        if not TV_TOKEN:
            raise HTTPException(500, "server has no TV_TOKEN configured")
        via_url = False
        if not authorization and stoptoken and action == "stops":
            authorization, via_url = "Bearer " + stoptoken, True    # the iPhone Shortcut can't always send headers
        who = identify(authorization)
        if via_url and (who.admin or who.actions != {"stops"}):
            log.warning("denied: %s sent a token in the URL that can do more than report parking", who.name)
            raise HTTPException(403, "only a --stops token may be sent in the URL")
        if action and not who.admin and action not in who.actions:
            log.warning("denied: %s tried %s", who.name, action)
            raise HTTPException(403, f"not allowed: {action}")
        if action:
            log.info("%s: %s", who.name, action)
        return who
    return dep


@asynccontextmanager
async def lifespan(_app):
    yield
    kiosk_stop()          # closing the service also closes the dashboard it opened


app = FastAPI(title="RV TV control", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


@app.exception_handler(TVUnreachable)
async def _unreachable(request, exc):
    return JSONResponse(status_code=503, content={
        "detail": "TV unreachable (off, asleep, or not on the network)", "error": str(exc)})


@app.exception_handler(TVError)
async def _tverror(request, exc):
    return JSONResponse(status_code=502, content={"detail": str(exc)})


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/status")
async def status(who: Who = Depends(need("status"))):
    state, detail = await tv_probe()
    if state == "off":
        # "off" can also mean macOS is blocking this process from the LAN
        # (Local Network privacy) or the TV is on a different network.
        log.warning("status: cannot reach TV at %s:%s: %s", TV_IP, TV_WS_PORT, detail)
        return {"power": "off", "reason": detail,
                "hint": "If the TV is actually on, check macOS Settings > Privacy & Security > Local Network for python."}
    out = {"power": "on"}
    if detail:
        out["note"] = detail
    try:
        vol = await call("ssap://audio/getVolume")
        out["volume"] = vol.get("volumeStatus", {}).get("volume", vol.get("volume"))
        out["muted"] = vol.get("volumeStatus", {}).get("muteStatus", vol.get("muted"))
        app_info = await call("ssap://com.webos.applicationManager/getForegroundAppInfo")
        out["app"] = app_info.get("appId")
    except (TVError, TVUnreachable) as e:
        out["note"] = str(e)
    return out


@app.get("/inputs")
async def inputs(who: Who = Depends(need("admin"))):
    r = await call("ssap://tv/getExternalInputList")
    return [{"id": d.get("id"), "label": d.get("label"), "connected": d.get("connected")}
            for d in r.get("devices", [])]


@app.get("/apps")
async def apps(who: Who = Depends(need("admin"))):
    r = await call("ssap://com.webos.applicationManager/listLaunchPoints")
    return [{"id": a.get("id"), "title": a.get("title")} for a in r.get("launchPoints", [])]


async def wake_tv(wait, check_first=True):
    """Send wake packets until the TV answers or `wait` seconds pass. True if it is up."""
    if check_first and await tv_is_up():
        return True
    waited = 0
    while True:
        try:
            send_magic_packet(TV_MAC)
        except OSError as e:
            raise TVError(f"could not send wake packet: {e}")
        if waited >= wait:
            return False
        for _ in range(3):
            await asyncio.sleep(1)
            waited += 1
        if await tv_is_up():
            return True


@app.post("/power/on")
async def power_on(wait: int = Query(30, ge=0, le=120), who: Who = Depends(need("power_on"))):
    if await tv_is_up():
        return {"power": "on", "note": "already on"}
    if await wake_tv(wait, check_first=False):
        return {"power": "on"}
    return JSONResponse(status_code=202, content={
        "power": "unknown", "detail": "wake packet sent but the TV is not answering yet"})


@app.post("/power/off")
async def power_off(who: Who = Depends(need("power_off"))):
    await call("ssap://system/turnOff", expect_close=True)
    return {"power": "off"}


@app.post("/input/{input_id}")
async def set_input(input_id: str, who: Who = Depends(need("input"))):
    if not who.admin and input_id not in GUEST_INPUTS:
        raise HTTPException(403, "not allowed: that input")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,32}", input_id):
        raise HTTPException(400, "bad input id")
    await call("ssap://tv/switchInput", {"inputId": input_id})
    return {"input": input_id}


@app.post("/volume/up")
async def volume_up(who: Who = Depends(need("volume"))):
    await call("ssap://audio/volumeUp")
    return {"ok": True}


@app.post("/volume/down")
async def volume_down(who: Who = Depends(need("volume"))):
    await call("ssap://audio/volumeDown")
    return {"ok": True}


@app.post("/volume/{level}")
async def set_volume(level: int = PathParam(..., ge=0, le=100), who: Who = Depends(need("volume"))):
    await call("ssap://audio/setVolume", {"volume": level})
    return {"volume": level}


@app.post("/mute/{state}")
async def mute(state: str, who: Who = Depends(need("mute"))):
    if state not in ("on", "off"):
        raise HTTPException(400, "use on or off")
    await call("ssap://audio/setMute", {"mute": state == "on"})
    return {"muted": state == "on"}


@app.post("/app/{app_id}")
async def launch_app(app_id: str, who: Who = Depends(need("app"))):
    if not who.admin and app_id not in GUEST_APPS:
        raise HTTPException(403, "not allowed: that app")
    if not re.fullmatch(r"[A-Za-z0-9_.\-]{1,80}", app_id):
        raise HTTPException(400, "bad app id")
    await call("ssap://system.launcher/launch", {"id": app_id})
    return {"launched": app_id}


# ---- display: dashboard shown on the Mac's own screen ---------------------------
CHROMIUM = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
FIREFOX = "/Applications/Firefox.app/Contents/MacOS/firefox"
_weather = {"data": None, "key": None, "next": 0.0, "error": None, "at": None}


def find_browser():
    override = os.environ.get("TV_DISPLAY_BROWSER")
    for c in ([override] if override else CHROMIUM + [FIREFOX]):
        if c and os.access(c, os.X_OK):
            return c
    return None


def browser_cmd(exe, url):
    firefox = "firefox" in exe.lower()
    prof = RUN_DIR / ("kiosk-firefox" if firefox else "kiosk-profile")
    prof.mkdir(parents=True, exist_ok=True)
    if firefox:
        return [exe, "--kiosk", "--no-remote", "--profile", str(prof), url]
    cache = RUN_DIR / "kiosk-cache"                       # thrown away on every open: never serves stale widgets
    shutil.rmtree(cache, ignore_errors=True)
    return [exe, "--kiosk", f"--user-data-dir={prof}", f"--disk-cache-dir={cache}", "--no-first-run",
            "--no-default-browser-check", "--noerrdialogs", "--disable-infobars", url]


def kiosk_running():
    p = _display["proc"]
    return p is not None and p.poll() is None


def kiosk_stop():
    p = _display["proc"]
    if p is not None and p.poll() is None:
        p.terminate()
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
    _display.update(proc=None, token=None, browser=None, page=None)

# ---- BatCam: the cameras site in Safari, fullscreen on the Mac's screen ------------
# Safari is the Mac's own browser, so its saved login (iCloud Keychain / autofill) applies; the
# kiosk browser has a separate profile and could not use it. The site does not redirect, so
# there are two pages: login, and watch (after logging in you land on /protected; press Load).
BATCAM_BASE = os.environ.get("TV_BATCAM_URL", "https://service.low-ones.com").rstrip("/")
BATCAM_PAGES = {"login": "/login", "watch": "/watch"}

# ---- Canny cameras: live video from go2rtc (runs on oracle at the house), shown in Safari ------
# go2rtc turns each camera's rotating ONVIF stream URL into a stable stream name. The Mac reaches
# it through an ssh tunnel to oracle (ssh -N -L 1984:127.0.0.1:1984 oracle-r), so from the Mac's
# browser it is http://localhost:1984. Per camera two streams are expected in go2rtc.yaml:
#   <name>      the full-size stream (used when one camera is shown fullscreen)
#   <name>_sub  the small 640x360 stream (used in the grid; far lighter on Starlink)
# TV_CAMS_LIVE lists the cameras that are set up in go2rtc; the others show as "not connected".
GO2RTC_URL = os.environ.get("TV_GO2RTC_URL", "http://localhost:1984").rstrip("/")
CAMS = [("driveway", "Driveway"), ("coop", "Coop"), ("barn", "Barn"),
        ("leanto", "Lean-To"), ("rv", "RV"), ("batcave", "BatCave")]
CAM_LABEL = dict(CAMS)
CAMS_LIVE = [c.strip() for c in os.environ.get("TV_CAMS_LIVE", "driveway").split(",") if c.strip() in CAM_LABEL]


def cam_list():
    return [{"id": c, "label": l, "live": c in CAMS_LIVE} for c, l in CAMS]

SAFARI_OPEN = """on run argv
  set theURL to item 1 of argv
  tell application "Safari"
    activate
    make new document with properties {URL:theURL}
    delay 1
    return (id of front window) as text
  end tell
end run"""
# Fullscreen needs Accessibility (System Settings > Privacy & Security > Accessibility) for the
# process that runs osascript; without it the window is sized to the screen instead.
SAFARI_FULLSCREEN = """tell application "Safari" to activate
delay 1
tell application "System Events" to tell process "Safari" to keystroke "f" using {control down, command down}"""
SAFARI_FILL = """on run argv
  set wid to (item 1 of argv) as integer
  tell application "Finder" to set b to bounds of window of desktop
  tell application "Safari" to set bounds of window id wid to b
end run"""
SAFARI_CLOSE = """on run argv
  set wid to (item 1 of argv) as integer
  tell application "Safari"
    if exists (window id wid) then close (window id wid)
  end tell
end run"""


def _osa(script, *args, timeout=20):
    try:
        r = subprocess.run(["osascript", "-e", script, *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise RuntimeError("osascript not found: BatCam only works on the Mac")
    except subprocess.TimeoutExpired:
        raise RuntimeError("Safari did not answer in %d s: is there a permission prompt on the Mac's screen?" % timeout)
    if r.returncode != 0:
        err = (r.stderr or "").strip()
        if "-1743" in err or "not authorized" in err.lower():
            err = "not allowed to control Safari: System Settings > Privacy & Security > Automation, allow it. (%s)" % err[:120]
        raise RuntimeError(err[:300] or "osascript failed")
    return (r.stdout or "").strip()


def batcam_close():
    """Closes the Safari window BatCam opened. A no-op (False) when none is open. Blocking."""
    wid = _display.get("batcam")
    if not wid:
        return False
    _display["batcam"] = None
    try:
        _osa(SAFARI_CLOSE, wid, timeout=10)
    except RuntimeError as e:
        log.info("could not close the BatCam window: %s", e)
    return True


def _safari_show(url):
    """Opens url in a new Safari window and makes it fullscreen. Remembers the window so
    batcam_close() (Home, Close screen, Show dashboard) removes it. Blocking."""
    batcam_close()                                  # one window at a time
    wid = _osa(SAFARI_OPEN, url, timeout=30)
    if not wid.isdigit():
        raise RuntimeError("Safari gave no window id (%r)" % wid[:80])
    _display["batcam"] = wid
    fullscreen = False
    try:
        _osa(SAFARI_FULLSCREEN, timeout=15)
        fullscreen = True
    except RuntimeError as e:
        log.info("Safari fullscreen refused (%s); sizing the window to the screen instead", e)
        try:
            _osa(SAFARI_FILL, wid, timeout=10)
        except RuntimeError as e2:
            log.info("could not size the Safari window: %s", e2)
    return fullscreen


def batcam_open(page):
    """Opens the login or watch page in a new Safari window, fullscreen. Blocking."""
    url = BATCAM_BASE + BATCAM_PAGES[page]
    fullscreen = _safari_show(url)
    return {"batcam": "open", "page": page, "url": url, "fullscreen": fullscreen}


def cams_open(cam):
    """Opens one Canny camera (or cam='all', a 2-wide grid) from go2rtc in Safari. Blocking."""
    url = "http://%s:%d/display/cams?src=%s" % (BIND, SERVER_PORT, urllib.parse.quote(cam))
    fullscreen = _safari_show(url)
    return {"cams": "open", "cam": cam, "url": url, "fullscreen": fullscreen}


# JavaScript run inside the Safari window by AppleScript's "do JavaScript". Single quotes only.
BATCAM_JS = {
    "top": "(function(){window.scrollTo({top:0,behavior:'smooth'});return 'ok top';})()",
    "bottom": "(function(){window.scrollTo({top:document.documentElement.scrollHeight,behavior:'smooth'});return 'ok bottom';})()",
}
# PAST / NEW: click the left (25%) or right (75%) of the camera image that is mostly in view. The
# page listens for a click on the <img id="...Image"> and compares clientX with the image's middle.
BATCAM_STEP_JS = ("(function(side){"
                  "var imgs=[].slice.call(document.querySelectorAll('img.image')).filter(function(i){return /Image$/.test(i.id);});"
                  "if(!imgs.length)return 'no-camera';"
                  "var vh=window.innerHeight,best=null,bv=0;"
                  "imgs.forEach(function(i){var r=i.getBoundingClientRect();var v=Math.min(r.bottom,vh)-Math.max(r.top,0);if(v>bv){bv=v;best=i;}});"
                  "if(!best)return 'no-camera-in-view';"
                  "var r=best.getBoundingClientRect();"
                  "var x=r.left+r.width*(side==='past'?0.25:0.75);"
                  "var y=(Math.max(r.top,0)+Math.min(r.bottom,vh))/2;"
                  "best.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,view:window,clientX:x,clientY:y}));"
                  "return 'ok '+best.id+' '+side;"
                  "})('__SIDE__')")
SAFARI_JS = """on run argv
  set wid to (item 1 of argv) as integer
  set js to item 2 of argv
  tell application "Safari" to return do JavaScript js in current tab of window id wid
end run"""


def batcam_do(action):
    """Scroll or click inside the BatCam Safari window. Blocking.
    LookupError: no BatCam window. RuntimeError: Safari refused (message is for the phone)."""
    wid = _display.get("batcam")
    if not wid:
        raise LookupError("BatCam is not open: press Load first")
    js = BATCAM_JS.get(action) or BATCAM_STEP_JS.replace("__SIDE__", action)
    try:
        return _osa(SAFARI_JS, wid, js, timeout=15)
    except RuntimeError as e:
        low = str(e).lower()
        if "apple events" in low and "javascript" in low:
            raise RuntimeError("Safari is blocking scripts. On the Mac: Safari > Settings > Advanced > Show features for "
                               "web developers, then Develop > Allow JavaScript from Apple Events.")
        if "can't get window" in low or "-1728" in low:
            _display["batcam"] = None
            raise RuntimeError("The BatCam window is gone: press Load.")
        raise


def load_location():
    """The saved (manual) place plus the mode: 'auto' follows the itinerary when one is set."""
    try:
        return {**DEFAULT_LOC, **json.loads(STATE_FILE.read_text())}
    except (FileNotFoundError, ValueError):
        return dict(DEFAULT_LOC)


def save_state(**changes):
    cur = load_location()
    cur.update(changes)
    fd = os.open(str(STATE_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cur, f)
    return cur


_itin = {"rows": {}, "next": {}, "stops": {}, "reminders": {}, "infos": {}, "details": {}, "error": None}   # per source: parsed rows, when to refetch
_geo = {}                                            # city -> (lat, lon, label)
DATE_RE = re.compile(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*$")
# a reminder cell that is nothing but a date: m/d/yyyy, m/d/yy or yyyy-mm-dd
NOTE_DATE_RES = (re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$"), re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2})$"),
                 re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$"))


def parse_itinerary(text):
    """CSV text -> sorted [(date, city, row)]. City is '' on days the sheet leaves blank;
    row is the 1-based sheet row."""
    rows = []
    for n, r in enumerate(csv.reader(io.StringIO(text)), 1):
        m = DATE_RE.match(r[1]) if len(r) > 1 else None
        if m:
            rows.append((datetime.date(int(m[3]), int(m[1]), int(m[2])), r[3].strip() if len(r) > 3 else "", n))
    return sorted(rows, key=lambda x: x[0])


def _column_notes(text, col):
    """CSV text -> {date: text} from one column of every dated row. Whitespace (including line
    breaks inside the cell) is collapsed to single spaces; empty cells are left out."""
    out = {}
    for r in csv.reader(io.StringIO(text)):
        m = DATE_RE.match(r[1]) if len(r) > 1 else None
        if not m or len(r) <= col:
            continue
        note = " ".join(r[col].split())
        if note:
            d = datetime.date(int(m[3]), int(m[1]), int(m[2]))
            out[d] = (out[d] + " / " + note) if d in out else note      # two rows, same date: keep both
    return out


def parse_reminders(text):
    """CSV text -> {date: reminder text} from column G (REMINDER) of every dated row."""
    return _column_notes(text, REMINDER_COL)


def parse_infos(text):
    """CSV text -> {date: info text} from column L (INFO) of every dated row."""
    return _column_notes(text, INFO_COL)


def parse_details(text):
    """CSV text -> {date: {"city", "camp", "confirmed"}} from columns D, E and H of every dated row.
    `confirmed` is True when H has any text. Two rows with the same date are merged (first city and
    campground that are not empty win, any H text confirms)."""
    out = {}
    for r in csv.reader(io.StringIO(text)):
        m = DATE_RE.match(r[1]) if len(r) > 1 else None
        if not m:
            continue
        def cell(i):
            return " ".join(r[i].split()) if len(r) > i else ""
        d = datetime.date(int(m[3]), int(m[1]), int(m[2]))
        cur = out.setdefault(d, {"city": "", "camp": "", "confirmed": False})
        cur["city"] = cur["city"] or cell(CITY_COL)
        cur["camp"] = cur["camp"] or cell(CAMP_COL)
        cur["confirmed"] = cur["confirmed"] or bool(cell(TERMS_COL))
    return out


def note_date(note):
    """The date when the whole cell is one date (m/d/yyyy, m/d/yy or yyyy-mm-dd), else None."""
    note = note.strip()
    for i, rx in enumerate(NOTE_DATE_RES):
        m = rx.match(note)
        if not m:
            continue
        a, b, c = (int(x) for x in m.groups())
        y, mo, d = (a, b, c) if i == 2 else (c + 2000 if i == 1 else c, a, b)
        try:
            return datetime.date(y, mo, d)
        except ValueError:
            return None
    return None


def note_is_past_date(note, today):
    """True only when the whole cell is a date (m/d/yyyy, m/d/yy or yyyy-mm-dd) before `today`.
    The sheet's reminders point at the day a reservation was made; once that day is behind us the
    reminder is done. Anything else in the cell (text, a date plus text, a bare m/d with no year,
    an impossible date) is not treated as a date, so it still shows."""
    d = note_date(note)
    return d is not None and d < today


# ---- Google service account (TV_GOOGLE_KEY) ------------------------------------------------------
# With the trip sheet no longer shared by link, the mini reads it as a Google service account: it signs a
# short JWT with the account's private key (using the system `openssl`, so no extra Python packages),
# trades it for a one-hour access token, and sends that token as a Bearer header, but only to Google
# hosts (also after a redirect). Nothing happens unless TV_GOOGLE_KEY points at the downloaded JSON key.
GOOGLE_KEY_FILE = os.environ.get("TV_GOOGLE_KEY", "").strip()
GOOGLE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
GOOGLE_HOST_SUFFIXES = (".google.com", ".googleusercontent.com", ".googleapis.com")
_gtoken = {"token": None, "exp": 0.0}
_gtoken_lock = threading.Lock()


def google_host(url):
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return any(host == suf[1:] or host.endswith(suf) for suf in GOOGLE_HOST_SUFFIXES)


def _b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def google_service_account():
    """The parsed key file, or None when TV_GOOGLE_KEY is not set."""
    if not GOOGLE_KEY_FILE:
        return None
    try:
        info = json.loads(Path(GOOGLE_KEY_FILE).read_text())
        return {"email": info["client_email"], "key": info["private_key"],
                "uri": info.get("token_uri") or "https://oauth2.googleapis.com/token"}
    except Exception as e:
        raise ValueError("cannot read the Google key %s: %s" % (GOOGLE_KEY_FILE, type(e).__name__)) from None


def google_token():
    """Blocking. A valid access token for the service account (cached for most of its hour), or None
    when no key is configured."""
    sa = google_service_account()
    if sa is None:
        return None
    with _gtoken_lock:
        now = time.time()
        if _gtoken["token"] and now < _gtoken["exp"] - 300:
            return _gtoken["token"]
        head = _b64u(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        claims = _b64u(json.dumps({"iss": sa["email"], "scope": GOOGLE_SCOPE, "aud": sa["uri"],
                                   "iat": int(now), "exp": int(now) + 3600}).encode())
        signing_input = (head + "." + claims).encode()
        openssl = shutil.which("openssl") or "/usr/bin/openssl"
        with tempfile.NamedTemporaryFile("w", suffix=".pem") as kf:          # created 0600, removed on close
            kf.write(sa["key"])
            kf.flush()
            out = subprocess.run([openssl, "dgst", "-sha256", "-sign", kf.name], input=signing_input,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
        if out.returncode != 0 or not out.stdout:
            raise ValueError("openssl could not sign the Google login: " + out.stderr.decode("utf-8", "replace").strip()[:150])
        assertion = head + "." + claims + "." + _b64u(out.stdout)
        body = urllib.parse.urlencode({"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                       "assertion": assertion}).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(sa["uri"], data=body, headers={"User-Agent": "rv-tv"}),
                                        timeout=15) as r:
                got = json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            raise ValueError("Google refused the service account login (HTTP %d): is the key still valid?" % e.code) from None
        if not got.get("access_token"):
            raise ValueError("Google sent no access token")
        _gtoken["token"] = got["access_token"]
        _gtoken["exp"] = now + float(got.get("expires_in", 3600))
        log.info("google service account token ok (%s)", sa["email"])
        return _gtoken["token"]


class _GoogleOnlyRedirect(urllib.request.HTTPRedirectHandler):
    """Keeps the Bearer header only while the redirect stays on a Google host."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and not google_host(newurl):
            new.headers.pop("Authorization", None)
            new.unredirected_hdrs.pop("Authorization", None)
        return new


def read_source(src):
    if src.startswith(("http://", "https://")):
        headers = {"User-Agent": "rv-tv"}
        token = google_token() if google_host(src) else None
        if token:
            headers["Authorization"] = "Bearer " + token
        opener = urllib.request.build_opener(_GoogleOnlyRedirect)
        try:
            with opener.open(urllib.request.Request(src, headers=headers), timeout=10) as r:
                text = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if token and e.code in (401, 403, 404):
                sa = google_service_account()
                raise ValueError("Google answered %d for the sheet: share it with %s (Viewer)" % (e.code, sa["email"])) from None
            raise
        if text.lstrip()[:1] == "<":       # an HTML page: Google wants a sign-in
            raise ValueError("got a web page instead of CSV; " + (
                "share the sheet with the service account" if token else
                "share the sheet as 'anyone with the link can view', or set TV_GOOGLE_KEY"))
        return text
    return Path(src).read_text()


def itinerary_city(today):
    """Blocking. The city for `today`: the last city named on or before it, up to 14 days
    after the final dated row (so a stale sheet does not pin the weather forever)."""
    now = time.time()
    for src in ITIN_SOURCES:
        if now >= _itin["next"].get(src, 0):
            try:
                text = read_source(src)
                _itin["rows"][src] = parse_itinerary(text)
                _itin["stops"][src] = parse_stops(text)
                _itin["reminders"][src] = parse_reminders(text)
                _itin["infos"][src] = parse_infos(text)
                _itin["details"][src] = parse_details(text)
                _itin["next"][src] = now + ITIN_REFRESH
                _itin["error"] = None
            except Exception as e:
                _itin["error"] = str(e) or type(e).__name__
                _itin["next"][src] = now + 300
                log.warning("itinerary fetch failed: %s", _itin["error"])
    rows = sorted((r for v in _itin["rows"].values() for r in v), key=lambda x: x[0])
    city = None
    for d, c, _row in rows:
        if d > today:
            break
        city = c or city
    if rows and today > rows[-1][0] + datetime.timedelta(days=14):
        return None
    return city


def sheet_target(today):
    """Blocking. TV_SHEET_URL aimed at today's row, so the kiosk (which is not signed in to
    Google, so no on-open script runs) still opens on the right tab at the right place.
    Falls back to the plain link when the schedule has no row for today."""
    itinerary_city(today)                       # refreshes the cached sources
    base = SHEET_URL.split("#")[0].split("?")[0]
    for src in ITIN_SOURCES:
        g = re.search(r"[?&]gid=(\d+)", src)
        row = next((r for d, _c, r in _itin["rows"].get(src, []) if d == today), None)
        if g and row:
            return f"{base}?gid={g[1]}#gid={g[1]}&range=A{row}"
    return SHEET_URL


def place_for(d):
    """The sheet row for date `d` (looked up in every trip tab; the date carries its year, so only the
    tab for that year has it): {"city", "camp", "confirmed"} or None when no tab has a row with a city."""
    found = None
    for det in _itin.get("details", {}).values():
        row = det.get(d)
        if not row:
            continue
        if found is None:
            found = dict(row)
        else:
            found["city"] = found["city"] or row["city"]
            found["camp"] = found["camp"] or row["camp"]
            found["confirmed"] = found["confirmed"] or row["confirmed"]
    return found if found and found["city"] else None


def reminder_today(today):
    """Blocking. What today's row puts on the reminder card, or None. Two things can fire it:
    column G (REMINDER), unless the cell is just a date that is already past, and column L (INFO),
    whenever it has any text. G comes first, INFO on a second line. The same text on two tabs shows
    once; different text on two tabs is joined with " / ". If a refresh fails the last good copy of
    the sheet keeps being used.
    A G cell that is only a date is looked up in the trip tab for its year: when that row has a city
    (column D), the card shows the city (and under it, smaller, the date and the campground from column E),
    tone "ok" (green) if column H has text, "bad" (red) if not. Without such a row the date shows as
    plain text. `lines` is in the result only when a place is shown."""
    itinerary_city(today)                       # refreshes the cached sources
    g_parts, i_parts = [], []
    for src in ITIN_SOURCES:
        note = _itin["reminders"].get(src, {}).get(today)
        if note and not note_is_past_date(note, today) and note not in g_parts:
            g_parts.append(note)
        info = _itin["infos"].get(src, {}).get(today)
        if info and info not in i_parts:
            i_parts.append(info)
    plain, places = [], []
    for note in g_parts:
        d = note_date(note)
        pl = place_for(d) if d else None
        if pl:
            places.append((note, pl))
        else:
            plain.append(note)
    lines = []
    if plain:
        lines.append({"t": " / ".join(plain)})
    for note, pl in places:
        lines.append({"t": pl["city"], "sub": " \u00b7 ".join(x for x in (note, pl["camp"]) if x),
                      "tone": "ok" if pl["confirmed"] else "bad", "flat": f"{note} \u00b7 {pl['city']}" + (f" \u00b7 {pl['camp']}" if pl["camp"] else "")})
    if i_parts:
        lines.append({"t": " / ".join(i_parts)})
    if not lines:
        return None
    text = "\n".join(ln.get("flat") or ln["t"] for ln in lines)
    if len(text) > REMINDER_MAX:
        text = text[:REMINDER_MAX - 1].rstrip() + "\u2026"
    out = {"text": text, "date": today.isoformat()}
    if places:
        out["lines"] = [{k: (v[:120] if isinstance(v, str) else v) for k, v in ln.items() if v and k != "flat"} for ln in lines]
    return out


async def get_reminder():
    if not ITIN_SOURCES:
        return None
    return await asyncio.get_running_loop().run_in_executor(None, reminder_today, datetime.date.today())


def itinerary_location(today):
    """Blocking. Location for today from the schedule, or None."""
    city = itinerary_city(today)
    if not city:
        return None
    if city not in _geo:
        found = geocode(city)
        if not found:
            return None
        _geo[city] = found
    lat, lon, label = _geo[city]
    return {"lat": lat, "lon": lon, "label": city, "source": "itinerary"}


def _km(a, b):
    """Great-circle distance in km between two {'lat','lon'} dicts."""
    import math
    p1, p2 = math.radians(a["lat"]), math.radians(b["lat"])
    h = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(b["lon"] - a["lon"]) / 2) ** 2)
    return 6371 * 2 * math.asin(math.sqrt(h))


def device_location():
    """The Mac's own fix from LOCATION_FILE ('lat|lon|accuracy_m|city|state'), or None when the
    file is missing, unreadable, stale (file age) or too coarse. Coordinates are rounded to
    0.01 degree (~1 km) so Wi-Fi jitter does not bust the weather cache."""
    try:
        age = time.time() - LOCATION_FILE.stat().st_mtime
        parts = LOCATION_FILE.read_text().strip().split("|") + [""] * 5
        lat, lon, acc = float(parts[0]), float(parts[1]), float(parts[2])
    except (OSError, ValueError):
        return None
    if age > DEVICE_MAX_AGE or not (-90 <= lat <= 90 and -180 <= lon <= 180) or not (0 <= acc <= DEVICE_MAX_ACC):
        return None

    def clean(s):
        s = s.strip()
        return "" if s.lower() in ("(null)", "null", "nil") else s
    city, region = clean(parts[3]), clean(parts[4])
    abbr = {v.lower(): k for k, v in US_STATES.items()}.get(region.lower(), region)
    label = ", ".join(x for x in (city, abbr) if x) or f"{lat:.2f}, {lon:.2f}"
    return {"lat": round(lat, 2), "lon": round(lon, 2), "label": label, "source": "device",
            "accuracy": round(acc)}


async def current_location():
    """manual pin > device fix > trip schedule > saved default."""
    loc = load_location()
    if loc.get("mode", "auto") != "manual":
        itin = None
        if ITIN_SOURCES:
            try:
                itin = await asyncio.get_running_loop().run_in_executor(
                    None, itinerary_location, datetime.date.today())
            except Exception as e:
                log.warning("itinerary location failed: %s", e)
        dev = device_location()
        if dev:
            if itin and _km(dev, itin) > 50:          # the sheet and the Mac disagree: say so
                dev["schedule"] = itin["label"]
            return dev
        if itin:
            return itin
    return {**loc, "source": "manual"}


def fetch_weather(loc):
    """Blocking; run in a thread. Returns a compact dict for the weather widget."""
    q = urllib.parse.urlencode({
        "latitude": loc["lat"], "longitude": loc["lon"], "timezone": "auto", "forecast_days": 3, "past_days": 1,
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
        "daily": "temperature_2m_max,temperature_2m_min,weather_code,sunrise,sunset",
        "temperature_unit": "fahrenheit", "wind_speed_unit": "mph"})
    with urllib.request.urlopen(f"{WEATHER_URL}?{q}", timeout=8) as r:
        j = json.load(r)
    cur, d = j["current"], j["daily"]
    rows = [{"date": d["time"][i], "hi": d["temperature_2m_max"][i],
             "lo": d["temperature_2m_min"][i], "code": d["weather_code"][i],
             "rise": (d.get("sunrise") or [None] * len(d["time"]))[i],
             "set": (d.get("sunset") or [None] * len(d["time"]))[i]}
            for i in range(len(d["time"]))]
    # past_days=1 puts yesterday first. It is sent on its own so that days[0] stays today
    # (sunrise/sunset and the widget's "Today" row depend on that).
    yesterday = None
    if len(rows) > 1 and rows[0]["date"] < rows[1]["date"] and rows[0]["hi"] is not None and rows[0]["lo"] is not None:
        yesterday, rows = rows[0], rows[1:]
    elif len(rows) > 3:
        rows = rows[-3:]
    return {"temp": cur["temperature_2m"], "feels": cur["apparent_temperature"],
            "code": cur["weather_code"], "wind": cur["wind_speed_10m"],
            "yesterday": yesterday, "days": rows}


async def get_weather():
    """Cached 10 min; after a failure retry in 60 s and serve the last good data meanwhile."""
    loc = await current_location()
    key = (loc["lat"], loc["lon"])
    now = time.time()
    if now >= _weather["next"] or _weather["key"] != key:
        try:
            data = await asyncio.get_running_loop().run_in_executor(None, fetch_weather, loc)
            _weather.update(data=data, key=key, next=now + 600, error=None, at=now)
        except Exception as e:  # network down, bad JSON, API change...
            _weather["error"] = str(e) or type(e).__name__
            _weather["next"] = now + 60
            if _weather["key"] != key:
                _weather["data"] = None
            log.warning("weather fetch failed: %s", _weather["error"])
    return {"location": loc["label"], "source": loc["source"], "data": _weather["data"], "error": _weather["error"],
            "updated": _weather["at"], "schedule": loc.get("schedule")}


# ---- second, fixed weather place (Dundee, MI 48131 by default) ------------------------------
# TV_WEATHER_EXTRA="Label|lat|lon" or "off". Same Open-Meteo call as the main weather, cached
# 10 min; if it fails the widget just leaves the block out.
def _parse_extra(raw):
    raw = (raw or "").strip()
    if raw.lower() in ("off", "0", "no", "false", "none"):
        return None
    try:
        label, lat, lon = [x.strip() for x in raw.split("|")]
        lat, lon = float(lat), float(lon)
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("out of range")
        return {"label": label or "%.2f, %.2f" % (lat, lon), "lat": lat, "lon": lon}
    except Exception:
        log.warning("TV_WEATHER_EXTRA ignored (want 'Label|lat|lon' or 'off'): %r", raw)
        return None


WEATHER_EXTRA = _parse_extra(os.environ.get("TV_WEATHER_EXTRA", "Dundee, MI|41.9570|-83.6561"))
_weather_extra = {"data": None, "next": 0.0, "at": None}


async def get_weather_extra():
    if not WEATHER_EXTRA:
        return None
    now = time.time()
    if now >= _weather_extra["next"]:
        try:
            full = await asyncio.get_running_loop().run_in_executor(None, fetch_weather, WEATHER_EXTRA)
            today = (full.get("days") or [{}])[0]
            _weather_extra.update(
                data={"temp": full["temp"], "code": full["code"], "hi": today.get("hi"), "lo": today.get("lo")},
                next=now + 600, at=now)
        except Exception as e:
            _weather_extra["next"] = now + 60
            log.warning("extra weather fetch failed: %s", str(e) or type(e).__name__)
    if not _weather_extra["data"]:
        return None
    return {"label": WEATHER_EXTRA["label"], "data": _weather_extra["data"], "updated": _weather_extra["at"]}


# ---- moon phase -------------------------------------------------------------------------------
# Computed here from the date (Meeus, "Astronomical Algorithms" ch. 48, with the main correction
# terms), so it needs no network and works during a Starlink outage. TV_MOON=off hides it.
MOON_ON = os.environ.get("TV_MOON", "on").strip().lower() not in ("off", "0", "no", "false")
MOON_ICONS = {"New Moon": "\U0001F311", "Waxing Crescent": "\U0001F312", "First Quarter": "\U0001F313",
              "Waxing Gibbous": "\U0001F314", "Full Moon": "\U0001F315", "Waning Gibbous": "\U0001F316",
              "Last Quarter": "\U0001F317", "Waning Crescent": "\U0001F318"}
MOON_EXACT_DEG = 12.2        # about one day either side of new, quarter and full keeps the principal name


def moon_info(ts=None):
    """{'name', 'icon', 'percent', 'waxing'} for the moment `ts` (unix seconds; default now).
    Illumination is good to about 1 percent."""
    ts = time.time() if ts is None else ts
    T = (ts / 86400.0 + 2440587.5 - 2451545.0) / 36525.0
    rad = math.radians
    D = (297.8501921 + 445267.1114034 * T - 0.0018819 * T ** 2 + T ** 3 / 545868 - T ** 4 / 113065000) % 360
    M = (357.5291092 + 35999.0502909 * T - 0.0001536 * T ** 2 + T ** 3 / 24490000) % 360
    Mp = (134.9633964 + 477198.8675055 * T + 0.0087414 * T ** 2 + T ** 3 / 69699 - T ** 4 / 14712000) % 360
    i = (180 - D - 6.289 * math.sin(rad(Mp)) + 2.100 * math.sin(rad(M)) - 1.274 * math.sin(rad(2 * D - Mp))
         - 0.658 * math.sin(rad(2 * D)) - 0.214 * math.sin(rad(2 * Mp)) - 0.110 * math.sin(rad(D)))
    illum = (1 + math.cos(rad(i))) / 2
    elong = (180 - i) % 360                    # 0 new, 90 first quarter, 180 full, 270 last quarter
    name = None
    for centre, label in ((0, "New Moon"), (90, "First Quarter"), (180, "Full Moon"), (270, "Last Quarter")):
        if abs((elong - centre + 180) % 360 - 180) <= MOON_EXACT_DEG:
            name = label
    if not name:
        name = ("Waxing Crescent" if elong < 90 else "Waxing Gibbous" if elong < 180
                else "Waning Gibbous" if elong < 270 else "Waning Crescent")
    return {"name": name, "icon": MOON_ICONS[name], "percent": int(round(illum * 100)), "waxing": elong < 180}


# ==== NAV: next stop, route, tides (map dashboard) ===================================
# Everything here fetches in a background thread and serves the last good answer, so a slow
# or dead Starlink link never holds up /display/data. All of it is optional: TV_NAV=off
# turns it off and the dashboard falls back to the weather location alone.
import threading

NAV_ON = os.environ.get("TV_NAV", "on").strip().lower() not in ("off", "0", "no", "false")
NOMINATIM_URL = os.environ.get("TV_NOMINATIM_URL", "https://nominatim.openstreetmap.org/search")
OSRM_URL = os.environ.get("TV_OSRM_URL", "https://router.project-osrm.org/route/v1/driving")
TIDE_DATA_URL = os.environ.get("TV_TIDES_URL", "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter")
TIDE_STATIONS_URL = os.environ.get(
    "TV_TIDE_STATIONS_URL",
    "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations.json?type=tidepredictions")
TIDE_MAX_KM = float(os.environ.get("TV_TIDE_MAX_KM", "60"))       # farther than this: no tide row
ROUTE_CROP_KM = float(os.environ.get("TV_ROUTE_CROP_KM", "130"))  # keep route points this close to the Mac
HTTP_UA = os.environ.get("TV_HTTP_UA", "rv-tv-dashboard/1.0 (personal use)")
GEOCACHE_FILE = RUN_DIR / "nav_geocache.json"
TIDE_STATIONS_FILE = RUN_DIR / "tide_stations.json"


def http_json(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": HTTP_UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except Exception as e:
        # The mini's Python 3.9 (LibreSSL) fails the TLS handshake with some hosts (OSRM);
        # system curl handles them. Only retry on TLS errors.
        import ssl, subprocess
        reason = getattr(e, "reason", e)
        if not isinstance(reason, ssl.SSLError) and "SSL" not in str(e):
            raise
        out = subprocess.run(["curl", "-sS", "-f", "-m", str(timeout), "-A", HTTP_UA,
                              "-H", "Accept: application/json", url],
                             capture_output=True, timeout=timeout + 5)
        if out.returncode != 0:
            raise RuntimeError("curl: " + out.stderr.decode("utf-8", "replace").strip()[:200])
        return json.loads(out.stdout)


class Partial(Exception):
    """Raised by a background job whose result is usable but degraded (e.g. a straight line
    because the routing service is down); the value is kept and the job retries sooner."""
    def __init__(self, value, message):
        super().__init__(message)
        self.value = value


class BgCache:
    """get(key, fn) returns (value, error, fetched_at) straight away and, when the entry is
    stale, refreshes it in a daemon thread. Until the first fetch ends the value is None."""
    def __init__(self, name, ok_ttl, fail_ttl):
        self.name, self.ok_ttl, self.fail_ttl = name, ok_ttl, fail_ttl
        self._lock = threading.Lock()
        self._items = {}

    def get(self, key, fn):
        now = time.time()
        with self._lock:
            e = self._items.get(key)
            if e is None:
                if len(self._items) >= 40:                    # the RV moves; forget old places
                    del self._items[min(self._items, key=lambda k: self._items[k]["touched"])]
                e = self._items[key] = {"value": None, "error": None, "at": None, "next": 0.0,
                                        "busy": False, "touched": now}
            e["touched"] = now
            start = now >= e["next"] and not e["busy"]
            if start:
                e["busy"] = True
            snap = (e["value"], e["error"], e["at"])
        if start:
            threading.Thread(target=self._run, args=(key, fn), daemon=True, name="bg-" + self.name).start()
        return snap

    def _run(self, key, fn):
        value = error = None
        state = "fail"
        try:
            value, state = fn(), "ok"
        except Partial as p:
            value, error, state = p.value, str(p), "partial"
        except Exception as ex:
            error = str(ex) or type(ex).__name__
            log.warning("%s refresh failed: %s", self.name, error)
        now = time.time()
        with self._lock:
            e = self._items.get(key)
            if e is None:
                return
            e["busy"] = False
            if state == "ok":
                e.update(value=value, error=None, at=now, next=now + self.ok_ttl)
            elif state == "partial":
                e.update(value=value, error=error, at=now, next=now + self.fail_ttl)
            else:
                e.update(error=error, next=now + self.fail_ttl)


_route_bg = BgCache("route", ok_ttl=6 * 3600, fail_ttl=90)
_tide_bg = BgCache("tides", ok_ttl=3 * 3600, fail_ttl=120)


# ---- next stop (column K of the trip sheet) ----------------------------------------
def parse_stops(text):
    """CSV text -> [{'date', 'city', 'address', 'row'}] sorted by date. Column 4 is the city,
    column 11 (K) the street address of that stop. Rows with a blank city are 'still there'."""
    stops = []
    for n, r in enumerate(csv.reader(io.StringIO(text)), 1):
        m = DATE_RE.match(r[1]) if len(r) > 1 else None
        city = r[3].strip() if len(r) > 3 else ""
        if m and city:
            stops.append({"date": datetime.date(int(m[3]), int(m[1]), int(m[2])), "city": city,
                          "address": r[10].strip() if len(r) > 10 else "", "row": n})
    return sorted(stops, key=lambda s: (s["date"], s["row"]))


def pick_target(today, move_day):
    """Blocking. Which stop the map should aim at.
    Move day (the Mac is far from where the sheet says today): today's own stop.
    Otherwise: the first later stop in a different city than today's."""
    today_city = itinerary_city(today)                  # also refreshes the cached sheet
    stops = sorted((s for v in _itin.get("stops", {}).values() for s in v),
                   key=lambda s: (s["date"], s["row"]))
    same = lambda a, b: a.strip().lower() == b.strip().lower()
    if move_day and today_city:
        mine = [s for s in stops if s["date"] <= today and same(s["city"], today_city)]
        return mine[-1] if mine else None
    later = [s for s in stops if s["date"] > today and not (today_city and same(s["city"], today_city))]
    return later[0] if later else None


_geo_lock = threading.Lock()
_nom_lock = threading.Lock()
_nom_last = [0.0]


def _geocache_read():
    try:
        return json.loads(GEOCACHE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _geocache_put(key, val):
    with _geo_lock:
        data = _geocache_read()
        data[key] = val
        if len(data) > 1500:
            data = dict(list(data.items())[-1500:])
        try:
            RUN_DIR.mkdir(parents=True, exist_ok=True)
            tmp = GEOCACHE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            os.replace(tmp, GEOCACHE_FILE)
        except OSError:
            pass


def nominatim(query, near=None):
    """OpenStreetMap search, at most one request a second (their usage policy). With near=(lat, lon)
    the search is limited to a box about 90 km wide around it, which finds country roads and
    campgrounds that a free-text 'street, town, state' query misses. Returns (lat, lon, kind)
    where kind is 'address' (a house or business) or 'street' (only the road was found), or None."""
    with _nom_lock:
        wait = 1.1 - (time.time() - _nom_last[0])
        if wait > 0:
            time.sleep(wait)
        _nom_last[0] = time.time()
    params = {"q": query, "format": "jsonv2", "limit": 1}
    if near:
        d = 0.4                                          # degrees, about 44 km each way
        params.update(viewbox=f"{near[1] - d},{near[0] + d},{near[1] + d},{near[0] - d}", bounded=1)
    res = http_json(f"{NOMINATIM_URL}?{urllib.parse.urlencode(params)}")
    if not res:
        return None
    kind = "street" if res[0].get("addresstype") in ("road", "highway", "street") else "address"
    return float(res[0]["lat"]), float(res[0]["lon"]), kind


def _city_point(city):
    if city in _geo:
        return _geo[city][0], _geo[city][1]
    found = geocode(city)                               # Open-Meteo, same lookup the weather uses
    if found:
        _geo[city] = found
        return found[0], found[1]
    got = nominatim(city)
    return (got[0], got[1]) if got else None


def resolve_stop(stop):
    """Blocking. {'lat','lon','precision'}: precision is 'address', 'street' or 'city', the best the
    lookups could do. Tries the address as written, then the street alone inside a box around the
    city (campgrounds and rural roads often are not filed under the town the sheet names), then
    the city centre. A hit more than 80 km from the city is treated as the wrong place."""
    errs = []

    def attempt(key, fn):
        hit = _geocache_read().get(key)
        if hit:
            return hit
        try:
            got = fn()
        except Exception as e:
            errs.append(str(e) or type(e).__name__)
            return None
        if got:
            _geocache_put(key, list(got))
            return list(got)
        return None

    try:
        cpt = _city_point(stop["city"])
    except Exception as e:
        cpt, _ = None, errs.append(str(e) or type(e).__name__)
    addr = stop["address"]
    if addr:
        def as_written():
            got = nominatim(addr)
            if got and cpt and _km({"lat": got[0], "lon": got[1]}, {"lat": cpt[0], "lon": cpt[1]}) > 80:
                return None
            return got

        hit = attempt("addr:" + addr.lower(), as_written)
        if not hit and cpt:
            street = addr.split(",")[0].strip()
            for q in dict.fromkeys([street, re.sub(r"^\d+[a-zA-Z]?\s+", "", street)]):
                hit = attempt("box:%s@%.1f,%.1f" % (q.lower(), cpt[0], cpt[1]), lambda q=q: nominatim(q, near=cpt))
                if hit:
                    break
        if hit:
            return {"lat": hit[0], "lon": hit[1], "precision": hit[2]}
    if cpt:
        return {"lat": cpt[0], "lon": cpt[1], "precision": "city"}
    raise LookupError("could not place " + (addr or stop["city"]) + (" (" + "; ".join(errs) + ")" if errs else ""))


def osrm_route(a, b):
    """Car route a -> b, both (lat, lon). OSRM's public demo server ignores RV height and length
    limits, so treat the miles and minutes as a rough guide."""
    url = (f"{OSRM_URL}/{a[1]:.5f},{a[0]:.5f};{b[1]:.5f},{b[0]:.5f}"
           "?overview=full&geometries=geojson&steps=false")
    j = http_json(url, timeout=20)
    if j.get("code") != "Ok" or not j.get("routes"):
        raise ValueError("routing: " + str(j.get("message") or j.get("code")))
    r = j["routes"][0]
    return {"coords": [[c[1], c[0]] for c in r["geometry"]["coordinates"]],
            "miles": r["distance"] / 1609.344, "minutes": r["duration"] / 60.0}


def crop_route(coords, center, radius_km):
    """Keep only the parts of the route within radius_km of center (plus one point either side,
    so lines still leave the screen). A route that leaves and re-enters comes back as several
    separate segments."""
    pts = [{"lat": c[0], "lon": c[1]} for c in coords]
    near = [_km(center, p) <= radius_km for p in pts]
    n = len(pts)
    segs, cur = [], []
    for i in range(n):
        if near[i] or (i > 0 and near[i - 1]) or (i < n - 1 and near[i + 1]):
            cur.append([round(coords[i][0], 5), round(coords[i][1], 5)])
        elif cur:
            segs.append(cur)
            cur = []
    if cur:
        segs.append(cur)
    return [s for s in segs if len(s) > 1]


def route_job(stop, lat, lon):
    pt = resolve_stop(stop)
    a, b = (lat, lon), (pt["lat"], pt["lon"])
    here = {"lat": lat, "lon": lon}
    try:
        r = osrm_route(a, b)
        return {"point": pt, "route": {
            "segments": crop_route(r["coords"], here, ROUTE_CROP_KM),
            "miles": round(r["miles"], 1), "minutes": round(r["minutes"]), "straight": False}}
    except Exception as e:
        line_mi = round(_km(here, {"lat": b[0], "lon": b[1]}) * 0.621371, 1)
        raise Partial({"point": pt, "route": {
            "segments": [[[a[0], a[1]], [b[0], b[1]]]], "miles": line_mi, "minutes": None,
            "straight": True}}, "route service: " + (str(e) or type(e).__name__))


async def get_nav():
    """Where the Mac is, where it is headed, and the route between them (None when TV_NAV=off)."""
    if not NAV_ON:
        return None
    loc = await current_location()
    loop = asyncio.get_running_loop()
    move_day = bool(loc.get("schedule"))                # the Mac and today's sheet row disagree by > 50 km
    stop = None
    if ITIN_SOURCES:
        stop = await loop.run_in_executor(None, pick_target, datetime.date.today(), move_day)
    out = {"here": {"lat": loc["lat"], "lon": loc["lon"], "label": loc["label"], "source": loc["source"]},
           "move_day": move_day, "target": None, "route": None, "error": None}
    if stop:
        target = {"label": stop["city"], "address": stop["address"], "date": stop["date"].isoformat()}
        key = (round(loc["lat"], 2), round(loc["lon"], 2), stop["city"].lower(), stop["address"].lower())
        value, error, _at = _route_bg.get(key, lambda: route_job(stop, loc["lat"], loc["lon"]))
        if value:
            target.update(lat=value["point"]["lat"], lon=value["point"]["lon"],
                          precision=value["point"]["precision"])
            out["route"] = value["route"]
        out["error"] = error
        out["target"] = target
    return out


# ---- tides: nearest NOAA prediction station ----------------------------------------
def tide_stations():
    try:
        if time.time() - TIDE_STATIONS_FILE.stat().st_mtime < 30 * 86400:
            return json.loads(TIDE_STATIONS_FILE.read_text())
    except (OSError, ValueError):
        pass
    try:
        j = http_json(TIDE_STATIONS_URL, timeout=30)
        out = []
        for s in j.get("stations", []):
            lat, lon = s.get("lat"), s.get("lng", s.get("lon"))
            if lat is not None and lon is not None and s.get("id"):
                out.append({"id": str(s["id"]), "name": s.get("name") or str(s["id"]),
                            "lat": float(lat), "lon": float(lon)})
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        TIDE_STATIONS_FILE.write_text(json.dumps(out, separators=(",", ":")))
        return out
    except Exception:
        try:                                            # an old list beats none
            return json.loads(TIDE_STATIONS_FILE.read_text())
        except (OSError, ValueError):
            raise


def tides_job(lat, lon, today):
    here = {"lat": lat, "lon": lon}
    best, best_km = None, None
    for s in tide_stations():
        d = _km(here, s)
        if best_km is None or d < best_km:
            best, best_km = s, d
    if best is None or best_km > TIDE_MAX_KM:
        return {"station": None}                        # inland: the widget hides the row
    q = urllib.parse.urlencode({
        "product": "predictions", "application": "rv-tv", "datum": "MLLW", "station": best["id"],
        "begin_date": today.strftime("%Y%m%d"),
        "end_date": (today + datetime.timedelta(days=1)).strftime("%Y%m%d"),
        "time_zone": "lst_ldt", "interval": "hilo", "units": "english", "format": "json"})
    j = http_json(f"{TIDE_DATA_URL}?{q}", timeout=15)
    preds = j.get("predictions")
    if not preds:
        raise ValueError("tides: " + str((j.get("error") or {}).get("message", "no predictions")))
    return {"station": best["name"], "id": best["id"], "miles": round(best_km * 0.621371),
            "events": [{"t": p["t"], "v": round(float(p["v"]), 1), "type": p["type"]} for p in preds]}


async def get_tides():
    if not NAV_ON:
        return None
    loc = await current_location()
    today = datetime.date.today()
    key = (round(loc["lat"], 1), round(loc["lon"], 1), today.isoformat())
    value, error, at = _tide_bg.get(key, lambda: tides_job(loc["lat"], loc["lon"], today))
    if value is None:
        return {"station": None, "error": error} if error else None
    return {**value, "error": error, "updated": at}


# ---- map pins: post offices, libraries, Costco (OpenStreetMap via Overpass) --------------
# The server asks for every one near the Mac once (cached a day, background thread, last good
# answer kept when Starlink drops). The map page then keeps only the nearest few of each kind
# that are actually inside the picture. TV_POI=off turns it off.
POI_ON = os.environ.get("TV_POI", "on").strip().lower() not in ("off", "0", "no", "false")
POI_COUNT = max(1, min(20, int(os.environ.get("TV_POI_COUNT", "5") or 5)))     # per kind, on screen
POI_MILES = max(0.0, float(os.environ.get("TV_POI_MILES", "15") or 15))        # farther than this from the RV: not drawn (0 = no limit)
POI_LOCAL_MILES = max(0.0, float(os.environ.get("TV_POI_LOCAL_MILES", "10") or 10))   # post offices and libraries: tighter limit
# TV_OVERPASS_URL is one URL or a comma-separated list, tried in order: the free public servers
# go down now and then (502, or no answer), so a second one takes over. The server that answered
# last is tried first next time, so a dead one does not cost a timeout on every refresh.
OVERPASS_URLS = [u.strip() for u in os.environ.get(
    "TV_OVERPASS_URL", "https://overpass.openstreetmap.fr/api/interpreter,https://overpass-api.de/api/interpreter"
).split(",") if u.strip()]
OVERPASS_URL = OVERPASS_URLS[0]                      # kept for older callers
_overpass_last_ok = {"i": 0}
_layer_errors = {}                                   # layer -> last refresh error, shown in /display/data
POI_KINDS = ("post", "library", "costco")
POI_KEEP = 40                       # per kind, nearest to the Mac, sent to the page
_poi_bg = BgCache("poi", ok_ttl=24 * 3600, fail_ttl=300)


def overpass_json(query, timeout):
    """Ask each Overpass server in turn (`timeout` seconds each) and return the first good answer.
    A good answer is a JSON object with 'elements'; a 200 that only carries a 'remark' about a
    runtime error or timeout counts as a failure. Raises RuntimeError naming every server that failed."""
    urls = OVERPASS_URLS
    n = len(urls)
    start = _overpass_last_ok["i"] % n
    errs = []
    for k in range(n):
        i = (start + k) % n
        host = urllib.parse.urlparse(urls[i]).netloc or urls[i]
        try:
            j = http_json(urls[i] + "?" + urllib.parse.urlencode({"data": query}), timeout=timeout)
            if not isinstance(j, dict) or "elements" not in j:
                raise ValueError("unexpected answer")
            if not j["elements"] and j.get("remark"):
                raise ValueError(str(j["remark"])[:120])
        except Exception as e:
            errs.append("%s: %s" % (host, (str(e) or type(e).__name__)[:120]))
            log.warning("overpass %s failed: %s", host, errs[-1])
            continue
        _overpass_last_ok["i"] = i
        return j
    raise RuntimeError("overpass: all %d server%s failed (%s)" % (n, "" if n == 1 else "s", "; ".join(errs)))


def poi_query(lat, lon, miles):
    """Overpass QL for the box the map can show (shorter side `miles`, longer side up to ~1.3x,
    plus a margin because the cache key is rounded to 0.1 degree)."""
    import math
    half_mi = miles * 0.8 + 8
    dlat = half_mi / 69.0
    dlon = half_mi / (69.0 * max(0.2, math.cos(math.radians(lat))))
    box = "%.4f,%.4f,%.4f,%.4f" % (lat - dlat, lon - dlon, lat + dlat, lon + dlon)
    return ('[out:json][timeout:25];('
            'nwr["amenity"="post_office"](%(b)s);'
            'nwr["amenity"="library"](%(b)s);'
            'nwr["shop"]["brand"="Costco"](%(b)s);'
            'nwr["shop"]["brand:wikidata"="Q715583"](%(b)s);'
            'nwr["shop"]["name"~"^Costco",i](%(b)s););out center tags qt;' % {"b": box})


def poi_kind(tags):
    """'post' | 'library' | 'costco' | None for one OSM element's tags."""
    if tags.get("access") in ("private", "no"):
        return None
    if tags.get("amenity") == "post_office":
        return "post"
    if tags.get("amenity") == "library":
        return "library"
    if tags.get("shop") and (tags.get("brand") == "Costco" or tags.get("brand:wikidata") == "Q715583"
                             or tags.get("name", "").lower().startswith("costco")):
        return "costco"
    return None


def poi_parse(elements, lat, lon):
    """Overpass elements -> [{'t', 'lat', 'lon'}], nearest first per kind, near-duplicates merged
    (a building way and the node inside it are the same place)."""
    here = {"lat": lat, "lon": lon}
    per = {k: [] for k in POI_KINDS}
    for el in elements:
        kind = poi_kind(el.get("tags") or {})
        c = el if "lat" in el else el.get("center") or {}
        if kind is None or "lat" not in c or "lon" not in c:
            continue
        p = {"lat": float(c["lat"]), "lon": float(c["lon"])}
        if any(_km(p, q) < 0.08 for q in per[kind]):
            continue
        p["d"] = _km(here, p)
        per[kind].append(p)
    out = []
    for kind in POI_KINDS:
        for p in sorted(per[kind], key=lambda q: q["d"])[:POI_KEEP]:
            out.append({"t": kind, "lat": round(p["lat"], 5), "lon": round(p["lon"], 5)})
    return out


def poi_job(lat, lon, miles):
    j = overpass_json(poi_query(lat, lon, miles), timeout=25)
    return poi_parse(j["elements"], lat, lon)


async def get_poi():
    if not (NAV_ON and POI_ON):
        return None
    loc = await current_location()
    miles = get_map()["miles"]
    bucket = 25 if miles <= 25 else 50 if miles <= 50 else 100 if miles <= 100 else 250
    key = (round(loc["lat"], 1), round(loc["lon"], 1), bucket)
    value, error, at = _poi_bg.get(key, lambda: poi_job(loc["lat"], loc["lon"], bucket))
    _layer_errors["poi"] = error
    if value is None:
        return None
    # max_miles is the default limit; max_miles_by_kind overrides it per kind (0 = no limit for that kind)
    return {"items": value, "count": POI_COUNT, "max_miles": POI_MILES,
            "max_miles_by_kind": {"post": POI_LOCAL_MILES, "library": POI_LOCAL_MILES}}


# ---- map layers: highways, campground pins, parked-car stops ------------------------------
# Roads: interstates and other motorways, trunk roads and US-numbered primary roads from
# OpenStreetMap (Overpass), simplified and sent as encoded polylines. TV_ROADS=off turns it off.
# Tent pins: every address in column K of the trip tabs plus column C of the "campgrounds" tab,
# geocoded in the background (one Nominatim request a second, cached on disk). TV_CAMPS=off.
# Stops: the iPhone reports "car parked" / "driving again" (POST /display/stop) when it leaves or
# rejoins the car's Bluetooth; a stay of TV_STOP_MINUTES or more becomes a white pin
# (every visit is kept; visits within TV_STOP_MERGE_M of each other share one pin on the map).
ROADS_ON = os.environ.get("TV_ROADS", "on").strip().lower() not in ("off", "0", "no", "false")
CAMPS_ON = os.environ.get("TV_CAMPS", "on").strip().lower() not in ("off", "0", "no", "false")
CAMPS_TAB = os.environ.get("TV_CAMPS_TAB", "campgrounds").strip() or "campgrounds"
CAMPS_URL = os.environ.get("TV_CAMPS_URL", "").strip()          # a CSV link; default: the campgrounds tab by name
STOP_MIN_MIN = float(os.environ.get("TV_STOP_MINUTES", "90"))     # parked at least this long to count
STOP_HOME_M = float(os.environ.get("TV_STOP_HOME_M", "300"))      # parking this close to the Mac does not count
STOP_MERGE_M = float(os.environ.get("TV_STOP_MERGE_M", "300"))    # visits this close share one pin on the map
STOP_KEEP = 20000                                                 # visits kept on disk (about 2 MB); far past a year of driving
PARK_LOG_KEEP = 300                                               # car park spots kept for Tools > Set location
STOP_PINS_MAX = 2000                                              # pins sent to the map, newest first
STOPS_FILE = RUN_DIR / "stops.json"
LOCATION_HISTORY_FILE = Path(os.environ.get("TV_LOCATION_HISTORY") or LOCATION_FILE.with_name("location_history.log"))
STOP_MAX_AGE_DAYS = 60      # an uploaded event older than this is dropped
STOP_FUTURE_SLACK = 300     # seconds: a phone clock this far ahead is clamped to now
CAMP_GEO_FILE = RUN_DIR / "camp_geocache.json"
ROADS_MAX_POINTS = 30000
_roads_bg = BgCache("roads", ok_ttl=24 * 3600, fail_ttl=300)


def roads_query(lat, lon, miles):
    """Overpass QL for the box the map can show. Wider than the post-office box: the water-edge
    shift can slide the view by a third of its size."""
    import math
    half_mi = miles * 0.9 + 8
    dlat = half_mi / 69.0
    dlon = half_mi / (69.0 * max(0.2, math.cos(math.radians(lat))))
    box = "%.4f,%.4f,%.4f,%.4f" % (lat - dlat, lon - dlon, lat + dlat, lon + dlon)
    return ('[out:json][timeout:40];('
            'way["highway"~"^(motorway|trunk)$"](%(b)s);'
            'way["highway"="primary"]["ref"~"^(US|I) "](%(b)s););out tags geom qt;' % {"b": box})


def _dp(pts, tol, k):
    """Douglas-Peucker, iterative. pts are (lat, lon); k = cos(latitude) so lon degrees match lat degrees."""
    import math
    n = len(pts)
    if n < 3:
        return list(pts)
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay, bx, by = pts[a][1] * k, pts[a][0], pts[b][1] * k, pts[b][0]
        dx, dy = bx - ax, by - ay
        l2 = dx * dx + dy * dy
        best, idx = -1.0, -1
        for i in range(a + 1, b):
            px, py = pts[i][1] * k, pts[i][0]
            if l2 == 0:
                d = math.hypot(px - ax, py - ay)
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
                d = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
            if d > best:
                best, idx = d, i
        if best > tol:
            keep[idx] = True
            stack.append((a, idx))
            stack.append((idx, b))
    return [p for p, kp in zip(pts, keep) if kp]


def enc_poly(pts):
    """Google's encoded polyline format at 1e-5 degrees (the page decodes it)."""
    out, plat, plon = [], 0, 0
    for la, lo in pts:
        a, b = int(round(la * 1e5)), int(round(lo * 1e5))
        for v in (a - plat, b - plon):
            v = ~(v << 1) if v < 0 else (v << 1)
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1f)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        plat, plon = a, b
    return "".join(out)


def roads_parse(elements, lat, miles):
    """Overpass ways -> [[kind, ref, encoded_polyline], ...]. kind 'i' = interstate / other motorway,
    'h' = trunk or US-numbered primary road. Geometry is thinned to about one screen pixel at the
    widest view this data serves, and thinned further if the total gets large."""
    import math
    k = math.cos(math.radians(lat))
    raw = []
    for el in elements:
        geom = el.get("geometry") or []
        if len(geom) < 2:
            continue
        tags = el.get("tags") or {}
        ref = (tags.get("ref") or "").split(";")[0].strip()
        kind = "i" if tags.get("highway") == "motorway" or ref.startswith("I ") else "h"
        raw.append((kind, ref, [(g["lat"], g["lon"]) for g in geom]))
    tol = max(0.00004, miles * 1609.344 / 900.0 / 111320.0 * 0.8)
    for _ in range(8):
        thin = [(kd, rf, _dp(pts, tol, k)) for kd, rf, pts in raw]
        if sum(len(t[2]) for t in thin) <= ROADS_MAX_POINTS:
            break
        tol *= 1.7
    return [[kd, rf, enc_poly(pts)] for kd, rf, pts in thin if len(pts) >= 2]


def roads_job(lat, lon, miles):
    j = overpass_json(roads_query(lat, lon, miles), timeout=45)
    return roads_parse(j["elements"], lat, miles)


async def get_roads():
    if not (NAV_ON and ROADS_ON):
        return None
    loc = await current_location()
    miles = get_map()["miles"]
    bucket = 25 if miles <= 25 else 50 if miles <= 50 else 100 if miles <= 100 else 250
    key = (round(loc["lat"], 1), round(loc["lon"], 1), bucket)
    value, error, at = _roads_bg.get(key, lambda: roads_job(loc["lat"], loc["lon"], bucket))
    _layer_errors["roads"] = error
    if value is None:
        return None
    return {"v": int(at or 0), "ways": value}


# -- tent pins ----------------------------------------------------------------------------
def camps_csv_url():
    if CAMPS_URL:
        return CAMPS_URL
    for src in ITIN_SOURCES + [SHEET_URL]:
        m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", src or "")
        if m:
            return ("https://docs.google.com/spreadsheets/d/%s/gviz/tq?tqx=out:csv&headers=0&sheet=%s"
                    % (m[1], urllib.parse.quote(CAMPS_TAB)))      # headers=0: do not let Google fold the top rows into one header
    return None


def parse_camps(text):
    """CSV of the campgrounds tab -> [{'name', 'city', 'address'}]. Column A name, B city, C address."""
    out = []
    for r in csv.reader(io.StringIO(text)):
        if len(r) < 3 or r[0].strip().lower() == "name":
            continue
        addr = r[2].strip()
        if len(addr) > 160 or len(r[0]) > 160:          # Google folded many rows into one cell: unusable
            raise ValueError("the campgrounds tab came back merged into one row; "
                             "set TV_CAMPS_URL to the tab's CSV export link (File > Share > Publish, or .../export?format=csv&gid=<tab id>)")
        if addr and not addr.startswith("#"):
            out.append({"name": r[0].strip(), "city": r[1].strip(), "address": addr})
    return out


_cg_lock = threading.Lock()


def _cg_read():
    try:
        return json.loads(CAMP_GEO_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _cg_put(key, val):
    with _cg_lock:
        data = _cg_read()
        data[key] = val
        if len(data) > 3000:
            data = dict(list(data.items())[-3000:])
        try:
            RUN_DIR.mkdir(parents=True, exist_ok=True)
            tmp = CAMP_GEO_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            os.replace(tmp, CAMP_GEO_FILE)
        except OSError:
            pass


def camp_point(name, city, address):
    """Blocking. (lat, lon) for one campground / stop address, or None. Tries the address as written,
    then the campground by name and city, then the street alone near the city. Never falls back to the
    middle of a town: a tent pin in the wrong place is worse than none. Misses are remembered for a
    week so a bad address does not cost three lookups on every refresh."""
    key = "c:" + address.lower()
    hit = _cg_read().get(key)
    if hit:
        if hit[0] is not None:
            return hit[0], hit[1]
        if time.time() - hit[3] < 7 * 86400:
            return None
    try:
        cpt = _city_point(city) if city else None
    except Exception:
        cpt = None

    def sane(got):
        return got and not (cpt and _km({"lat": got[0], "lon": got[1]}, {"lat": cpt[0], "lon": cpt[1]}) > 80)

    got = None
    try:
        g = nominatim(address)
        if sane(g) and g[2] == "address":
            got = g
        if not got and name:
            g = nominatim("%s, %s" % (name, city) if city else name)
            if sane(g):
                got = g
        if not got and cpt:
            street = address.split(",")[0].strip()
            g = nominatim(street, near=cpt)
            if sane(g):
                got = g
    except Exception as e:
        log.warning("campground lookup failed for %s: %s", address, e)
        return None                                    # network trouble: do not cache a miss
    if got:
        _cg_put(key, [got[0], got[1], got[2], time.time()])
        return got[0], got[1]
    _cg_put(key, [None, None, "miss", time.time()])
    return None


_camps = {"items": [], "v": 0, "next": 0.0, "busy": False, "error": None}


def _camps_wanted():
    """Blocking. [(name, city, address)] without repeats: the campgrounds tab, then the trip stops."""
    seen, out = set(), []

    def add(name, city, addr):
        k = re.sub(r"\s+", " ", addr.strip().lower())
        if addr.strip() and k not in seen:
            seen.add(k)
            out.append((name, city, addr.strip()))

    url = camps_csv_url()
    if url:
        try:
            for c in parse_camps(read_source(url)):
                add(c["name"], c["city"], c["address"])
        except Exception as e:
            _camps["error"] = "campgrounds tab: " + (str(e) or type(e).__name__)
            log.warning("%s", _camps["error"])
    try:
        itinerary_city(datetime.date.today())            # makes sure the trip tabs are loaded
    except Exception as e:
        log.warning("trip tabs for pins: %s", e)
    for v in _itin.get("stops", {}).values():
        for st in v:
            add("", st["city"], st["address"])
    return out


def _camps_worker():
    now = time.time()
    try:
        _camps["error"] = None
        items = []
        for n, (name, city, addr) in enumerate(_camps_wanted(), 1):
            pt = camp_point(name, city, addr)
            if pt and not any(_km({"lat": pt[0], "lon": pt[1]}, {"lat": q["lat"], "lon": q["lon"]}) < 0.08 for q in items):
                items.append({"lat": round(pt[0], 5), "lon": round(pt[1], 5)})
            if n % 10 == 0:                              # pins appear while the slow part is still running
                _camps.update(items=list(items), v=_camps["v"] + 1)
        _camps.update(items=list(items), v=_camps["v"] + 1, next=now + 6 * 3600)
    except Exception as e:
        _camps["error"] = str(e) or type(e).__name__
        _camps["next"] = now + 300
        log.warning("campground pins failed: %s", _camps["error"])
    finally:
        _camps["busy"] = False


async def get_camps():
    if not (NAV_ON and CAMPS_ON):
        return None
    if time.time() >= _camps["next"] and not _camps["busy"]:
        _camps["busy"] = True
        threading.Thread(target=_camps_worker, daemon=True, name="bg-camps").start()
    return {"v": _camps["v"], "items": _camps["items"], "error": _camps["error"]}


# -- parked-car stops -----------------------------------------------------------------------
# The phone saves each "car parked" / "driving again" as a small file (event, time, place) and uploads the
# files whenever it has a connection, maybe hours later. So every event carries its OWN time (`at`), and
# everything below judges a stay by event time, never by when the upload happened to arrive.
_stops_lock = threading.Lock()


def _stops_read():
    try:
        d = json.loads(STOPS_FILE.read_text())
        return {"pending": d.get("pending"), "stops": list(d.get("stops") or []), "seen": list(d.get("seen") or []),
                "parks": list(d.get("parks") or []),
                "last_at": float(d.get("last_at") or 0), "last_upload": d.get("last_upload"),
                "last_error": d.get("last_error")}
    except (OSError, ValueError, AttributeError, TypeError):
        return {"pending": None, "stops": [], "seen": [], "parks": [], "last_at": 0.0, "last_upload": None,
                "last_error": None}


def _stops_write(d):
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STOPS_FILE.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(d, f)
    os.replace(tmp, STOPS_FILE)


def _mac_fix():
    """The Mac's exact position from the location agent, or None when it is missing or stale."""
    try:
        age = time.time() - LOCATION_FILE.stat().st_mtime
        parts = LOCATION_FILE.read_text().strip().split("|")
        lat, lon = float(parts[0]), float(parts[1])
    except (OSError, ValueError, IndexError):
        return None
    return {"lat": lat, "lon": lon} if age <= DEVICE_MAX_AGE else None


def _mac_fix_at(t):
    """Where the Mac was around time t. The location agent logs a fix every 10 minutes
    (location_history.log): the nearest one within 45 minutes wins. Without one, a recent event falls
    back to the current fix (the old behaviour); an older one gets None, so it is never mistaken
    for 'parked at the RV'."""
    best = None
    try:
        lines = LOCATION_HISTORY_FILE.read_text().splitlines()[-3000:]
    except OSError:
        lines = []
    for ln in lines:
        parts = ln.split("|")
        try:
            ts, la, lo = float(parts[0]), float(parts[1]), float(parts[2])
        except (ValueError, IndexError):
            continue
        gap = abs(ts - t)
        if gap <= 45 * 60 and (best is None or gap < best[0]):
            best = (gap, la, lo)
    if best:
        return {"lat": best[1], "lon": best[2]}
    return _mac_fix() if time.time() - t <= 3 * 3600 else None


def parse_event_time(v):
    """Epoch seconds for the phone's time stamp, or None. Accepts ISO 8601 with an offset
    ('2026-10-24T14:03:11-07:00', '...Z', '...-0700'), the same without one (read as the Mac's local
    time) and plain epoch seconds or milliseconds."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        t = float(v)
        return t / 1000.0 if t > 1e11 else t
    t = str(v).strip()
    if not t:
        return None
    if re.fullmatch(r"\d{9,13}(\.\d+)?", t):
        n = float(t)
        return n / 1000.0 if n > 1e11 else n
    t = re.sub(r"[zZ]$", "+00:00", t)
    t = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", t)
    t = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], t, count=1)
    try:
        dt = datetime.datetime.fromisoformat(t.replace("/", "-"))
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M"):
            try:
                dt = datetime.datetime.strptime(t, fmt)
                break
            except ValueError:
                dt = None
        if dt is None:
            return None
    try:
        return dt.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def _num(v):
    """A latitude/longitude from a number or text ('45,99' with a decimal comma too), else None."""
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        pass
    try:
        t = str(v).strip()
        return float(t.replace(",", ".")) if t.count(",") == 1 and "." not in t else None
    except ValueError:
        return None


def parse_stop_lines(text):
    """The phone's plain-text form, one event per line: 'park|2026-10-24T14:03:11-07:00|45.93|-123.96' or
    'drive|2026-10-24T16:10:00-07:00'. No quotes to mangle (iOS turns typed quotes into curly ones).
    Returns a list of event dicts, or None when a line does not look like one."""
    events = []
    for ln in text.splitlines():
        ln = ln.strip().strip("\ufeff")
        if not ln:
            continue
        parts = [x.strip() for x in ln.split("|")]
        if parts[0].lower() not in ("park", "drive") or len(parts) > 4:
            return None
        e = {"event": parts[0], "at": parts[1] if len(parts) > 1 else None}
        if len(parts) > 2:
            e["lat"] = parts[2]
        if len(parts) > 3:
            e["lon"] = parts[3]
        events.append(e)
    return events or None


def _stop_prepare(raw, now):
    """One uploaded event -> (event dict, None) or (None, why it cannot be used). Bad data is a
    permanent problem: retrying the same file will never fix it, so the caller drops it."""
    if not isinstance(raw, dict):
        return None, "not an event"
    ev = str(raw.get("event") or "").strip().lower()
    if ev not in ("park", "drive"):
        return None, "event must be park or drive"
    at, note = parse_event_time(raw.get("at")), None
    stamped = at is not None            # only a time the phone gave can be a re-send; an arrival time cannot
    if at is None:
        at, note = now, ("no usable time, used the arrival time" if raw.get("at") not in (None, "") else None)
    if at > now + STOP_FUTURE_SLACK:
        at, note, stamped = now, "time was in the future, used the arrival time", False
    if at < now - STOP_MAX_AGE_DAYS * 86400:
        return None, "older than %d days" % STOP_MAX_AGE_DAYS
    lat, lon = _num(raw.get("lat")), _num(raw.get("lon"))
    if ev == "park" and (lat is None or lon is None or not (-90 <= lat <= 90) or not (-180 <= lon <= 180)):
        return None, "park needs a valid lat and lon"
    return {"event": ev, "at": at, "lat": lat, "lon": lon, "note": note, "stamped": stamped}, None


def _stop_apply(d, ev):
    """One event in time order against the saved state. Returns the result for the phone."""
    at, pend = ev["at"], d["pending"]
    if ev["event"] == "park":
        here = _mac_fix_at(at)
        at_home = bool(here) and _km(here, {"lat": ev["lat"], "lon": ev["lon"]}) * 1000 < STOP_HOME_M
        d["pending"] = None if at_home else {"lat": round(ev["lat"], 5), "lon": round(ev["lon"], 5), "at": at}
        d["parks"].append({"lat": round(ev["lat"], 5), "lon": round(ev["lon"], 5), "at": at, "rv": at_home})
        d["parks"] = d["parks"][-PARK_LOG_KEEP:]                # every park, even at the RV: Set location offers these
        return {"event": "park", "tracking": not at_home, "note": "at the RV, ignored" if at_home else None}
    if pend and at - pend["at"] > 36 * 3600:        # a park the drive never answered
        pend = d["pending"] = None
    if not pend:
        return {"event": "drive", "logged": False, "note": "no parked car on record"}
    mins = (at - pend["at"]) / 60.0
    d["pending"] = None
    logged = mins >= STOP_MIN_MIN
    if logged:                                       # every visit is kept; the map merges nearby ones (_stop_pins)
        d["stops"].append({"lat": pend["lat"], "lon": pend["lon"], "at": pend["at"], "mins": round(mins)})
        d["stops"] = d["stops"][-STOP_KEEP:]
    return {"event": "drive", "logged": logged, "minutes": round(mins)}


def stops_ingest(events, now=None):
    """Blocking. Takes uploaded events (dicts with event, at, lat, lon) in any order and applies them
    in time order. Safe to send twice: an event already seen is acknowledged and ignored, and one older
    than the newest event already applied is skipped. Every result carries ok=True when the phone may
    delete its copy (applied, duplicate, stale, or permanently unusable)."""
    now = time.time() if now is None else now
    prepared, results = [], [None] * len(events)
    for i, raw in enumerate(events):
        ev, why = _stop_prepare(raw, now)
        if ev is None:
            log.warning("stops: dropped an event (%s): %.120r", why, raw)
            results[i] = {"ok": True, "dropped": why}
        else:
            prepared.append((ev["at"], i, ev))
    prepared.sort(key=lambda x: (x[0], x[1]))
    with _stops_lock:
        d = _stops_read()
        seen = d["seen"]
        for _at, i, ev in prepared:
            key = "%s|%d" % (ev["event"], int(ev["at"]))
            if ev["stamped"] and key in seen:
                results[i] = {"ok": True, "event": ev["event"], "duplicate": True}
            elif ev["stamped"] and ev["at"] < d["last_at"]:
                results[i] = {"ok": True, "event": ev["event"], "skipped": "older than an event already applied"}
            else:
                res = _stop_apply(d, ev)
                res["ok"] = True
                if ev["note"]:
                    res["time_note"] = ev["note"]
                if ev["stamped"]:
                    seen.append(key)
                    d["last_at"] = max(d["last_at"], ev["at"])
                results[i] = res
        d["seen"] = seen[-400:]
        applied = sum(1 for r in results if r and "duplicate" not in r and "skipped" not in r and "dropped" not in r)
        d["last_upload"] = {"at": now, "received": len(events), "applied": applied,
                            "newest": max([e[0] for e in prepared], default=None)}
        d["last_error"] = None
        _stops_write(d)
    return results


def stop_event(event, lat, lon, at=None):
    """One event, as the old shortcuts send it (no `at`: now). Kept for them and for the tests."""
    return stops_ingest([{"event": event, "lat": lat, "lon": lon, "at": at}])[0]


def stops_note_error(msg):
    with _stops_lock:
        d = _stops_read()
        d["last_error"] = {"at": time.time(), "msg": msg[:200]}
        _stops_write(d)


def _stop_pins(stops, merge_m):
    """Saved visits -> map pins. Visits within merge_m of a pin's first visit join it: the pin sits at their
    average, shows the newest visit time and the longest stay, and counts the visits. Old records that
    were merged on save carry their own count in 'n'. Newest pin first."""
    pins = []
    for s_ in sorted(stops, key=lambda v: v["at"]):
        n = s_.get("n", 1)
        for p in pins:
            if _km(p["anchor"], s_) * 1000 <= merge_m:
                p["sum_lat"] += s_["lat"] * n
                p["sum_lon"] += s_["lon"] * n
                p["n"] += n
                p["at"] = max(p["at"], s_["at"])
                p["mins"] = max(p["mins"] or 0, s_.get("mins") or 0)
                break
        else:
            pins.append({"anchor": s_, "sum_lat": s_["lat"] * n, "sum_lon": s_["lon"] * n, "n": n,
                         "at": s_["at"], "mins": s_.get("mins") or 0})
    pins.sort(key=lambda p: -p["at"])
    return [{"lat": round(p["sum_lat"] / p["n"], 5), "lon": round(p["sum_lon"] / p["n"], 5),
             "at": p["at"], "mins": p["mins"], "n": p["n"]} for p in pins[:STOP_PINS_MAX]]


_stop_pins_cache = {"key": None, "items": []}


async def get_stops():
    try:
        st = STOPS_FILE.stat()
        key = (st.st_mtime_ns, st.st_size, STOP_MERGE_M)
    except OSError:
        return {"items": []}
    if _stop_pins_cache["key"] != key:               # the file changes a few times a day; /display/data is polled
        _stop_pins_cache.update(key=key, items=_stop_pins(_stops_read()["stops"], STOP_MERGE_M))
    return {"items": _stop_pins_cache["items"]}


@app.post("/display/stop")
async def stop_endpoint(request: Request, event: str = Query(None, pattern="^(park|drive)$"),
                        lat: float = Query(None, ge=-90, le=90), lon: float = Query(None, ge=-180, le=180),
                        at: str = Query(None), who: Who = Depends(need("stops"))):
    """The phone's parking automation. One event in the query (event=park&lat=..&lon=..[&at=<time>]) or in
    the body: JSON (one object {"event","at","lat","lon"} or a list of them) or plain lines like
    park|2026-10-24T14:03:11-07:00|45.93|-123.96 (one event per line), whatever the content type.
    The reply has "ok": true when the phone may delete its copy. Needs a token with the 'stops' action
    (tvserver.py token add NAME --stops)."""
    raw = await request.body()
    if len(raw) > 200000:
        raise HTTPException(413, "too much data")
    loop = asyncio.get_running_loop()
    if raw.strip():
        try:
            text = raw.decode("utf-8-sig")
            try:
                body = json.loads(text)
            except ValueError:
                body = parse_stop_lines(text)          # 'park|time|lat|lon' lines
                if body is None:
                    raise ValueError("neither JSON nor park|time|lat|lon lines")
                if len(body) == 1:
                    body = body[0]
        except (ValueError, UnicodeDecodeError):
            await loop.run_in_executor(None, stops_note_error, "the body was not JSON or park|time|lat|lon lines (%d bytes)" % len(raw))
            raise HTTPException(400, "the body is not JSON or park|time|lat|lon lines")
        events = body if isinstance(body, list) else [body]
        if len(events) > 500:
            raise HTTPException(413, "at most 500 events at a time")
        results = await loop.run_in_executor(None, stops_ingest, events)
        if isinstance(body, list):
            return {"ok": True, "received": len(events), "results": results}
        return results[0]
    if event is None:
        raise HTTPException(400, "send event=park|drive in the query or a JSON body")
    if event == "park" and (lat is None or lon is None):
        raise HTTPException(400, "park needs lat and lon")
    return (await loop.run_in_executor(None, stops_ingest, [{"event": event, "lat": lat, "lon": lon, "at": at}]))[0]


@app.get("/tools/stops/status")
async def stops_status(who: Who = Depends(need("display"))):
    """What the Tools page shows about the phone's stop log."""
    d = _stops_read()
    p = d["pending"]
    return {"last_upload": d["last_upload"], "last_error": d["last_error"], "last_event_at": d["last_at"] or None,
            "parked_since": p["at"] if p else None, "stops": len(d["stops"]),
            "pins": len(_stop_pins(d["stops"], STOP_MERGE_M)), "now": time.time()}


async def _soft(coro, what):
    """Run an optional part of /display/data; a failure there must not blank the weather or TV."""
    try:
        return await coro
    except Exception as e:
        log.warning("%s failed: %s", what, e)
        return None


# ==== end NAV =========================================================================


@app.get("/whoami")
async def whoami(who: Who = Depends(need(None))):
    return {"name": who.name, "admin": who.admin,
            "actions": ALL_ACTIONS if who.admin else sorted(who.actions)}


@app.get("/display", response_class=FileResponse)
async def display_page():
    # Holds no secrets; the page reads its token from the URL fragment.
    return FileResponse(DISPLAY_DIR / "index.html", headers={"Cache-Control": "no-cache"})


# ---- map zoom: how many miles the dashboard map spans (shorter side). A setting, like the weather
# place: default from TV_MAP_MILES (35), changed by POST /display/map, kept in .run/map.json.
MAP_MILES_MIN, MAP_MILES_MAX = 5.0, 500.0


def _clean_miles(v, fallback):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return fallback
    if not (MAP_MILES_MIN <= v <= MAP_MILES_MAX):
        return fallback
    return int(v) if v == int(v) else round(v, 1)


MAP_MILES_DEFAULT = _clean_miles(os.environ.get("TV_MAP_MILES", "35"), 35)
MAP_FILE = STATE_FILE.parent / "map.json"


def get_map():
    try:
        miles = _clean_miles(json.loads(MAP_FILE.read_text()).get("miles"), MAP_MILES_DEFAULT)
    except (FileNotFoundError, ValueError, AttributeError):
        miles = MAP_MILES_DEFAULT
    return {"miles": miles, "default": MAP_MILES_DEFAULT}


@app.post("/display/map")
async def set_map(miles: float = Query(None, ge=5, le=500), reset: bool = Query(False),
                  who: Who = Depends(need("display"))):
    """How wide the dashboard map is, in miles: /display/map?miles=25, or ?reset=true for the default."""
    if reset:
        try:
            MAP_FILE.unlink()
        except FileNotFoundError:
            pass
    elif miles is None:
        raise HTTPException(400, "give miles=<%d-%d> or reset=true" % (MAP_MILES_MIN, MAP_MILES_MAX))
    else:
        fd = os.open(str(MAP_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"miles": _clean_miles(miles, MAP_MILES_DEFAULT)}, f)
    return get_map()


@app.get("/display/data")
async def display_data(who: Who = Depends(need("display_data"))):
    out = {"now": time.time(), "tv": await status(who), "weather": await get_weather()}
    out["weather_extra"] = await _soft(get_weather_extra(), "weather_extra")
    out["moon"] = moon_info() if MOON_ON else None
    out["reminder"] = await _soft(get_reminder(), "reminder")
    out["nav"] = await _soft(get_nav(), "nav")
    out["tides"] = await _soft(get_tides(), "tides")
    out["map"] = get_map()
    out["poi"] = await _soft(get_poi(), "poi")
    out["roads"] = await _soft(get_roads(), "roads")
    out["camps"] = await _soft(get_camps(), "camps")
    out["stops"] = await _soft(get_stops(), "stops")
    # why a layer is empty or stale, so a failure shows here and not only in the log
    errs = dict(_layer_errors)
    errs["camps"] = (out["camps"] or {}).get("error")
    out["layer_errors"] = {k: v for k, v in errs.items() if v}
    return out


@app.get("/display/state")
async def display_state(who: Who = Depends(need(None))):
    return {"open": kiosk_running(), "page": _display["page"], "browser": _display["browser"],
            "location": await current_location(), "itinerary": bool(ITIN_SOURCES),
            "itinerary_error": _itin["error"], "sheet": bool(SHEET_URL),
            "batcam": bool(_display.get("batcam")), "cams": cam_list(), "map": get_map()}


@app.post("/display/open")
async def display_open(tv: bool = Query(True), page: str = Query("dashboard", pattern="^(dashboard|sheet)$"),
                       who: Who = Depends(need("display"))):
    """Wake the TV, switch it to the Mac's input, and show the dashboard (or, with
    page=sheet, the configured Google Sheet) fullscreen on the Mac's screen."""
    if page == "sheet" and not SHEET_URL:
        raise HTTPException(404, "no sheet configured: add TV_SHEET_URL=<link> to tv.env and restart")
    async with get_lock("display"):
        await asyncio.get_running_loop().run_in_executor(None, batcam_close)   # BatCam window out of the way
        out = await _display_open(tv, page)
    if page == "dashboard" and COPY_ON_OPEN and isinstance(out, dict):
        try:                                      # best effort: never let the clipboard break opening
            out["copied"] = (await copy_destination())["copied"]
        except Exception as e:
            out["copied"] = None
            log.info("destination not copied on open: %s", getattr(e, "detail", e))
    if page == "dashboard" and TEXT_TO and isinstance(out, dict):
        try:                                      # best effort too
            res = await text_destination()
            out["texted"] = res.get("sent") or res.get("skipped")
        except Exception as e:
            out["texted"] = "failed: %s" % e
            log.warning("destination not texted on open: %s", e)
    return out


async def _display_open(tv, page="dashboard"):
    exe = find_browser()
    if not exe:
        raise HTTPException(500, "no kiosk-capable browser found: install Google Chrome, Brave, "
                                 "Edge or Firefox, or set TV_DISPLAY_BROWSER to its binary")
    out = {}
    if tv:
        try:
            if await wake_tv(30):
                out["tv"] = "on"
                await call("ssap://tv/switchInput", {"inputId": MAC_INPUT})
                out["input"] = MAC_INPUT
            else:
                out["tv"] = "not answering (dashboard opened anyway)"
        except (TVError, TVUnreachable) as e:
            out["tv"] = f"problem: {e} (dashboard opened anyway)"
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, kiosk_stop)            # replace any running copy
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    if page == "sheet":
        token = None                                         # an ordinary web page: no token
        try:
            url = await loop.run_in_executor(None, sheet_target, datetime.date.today())
        except Exception as e:
            log.warning("sheet jump failed, opening the plain link: %s", e)
            url = SHEET_URL
    else:
        token = secrets.token_urlsafe(18)                    # fresh, valid only until close
        url = f"http://{BIND}:{SERVER_PORT}/display#{token}"
    logf = open(RUN_DIR / "kiosk.log", "ab")
    proc = subprocess.Popen(browser_cmd(exe, url), stdin=subprocess.DEVNULL,
                            stdout=logf, stderr=logf)
    logf.close()
    _display.update(proc=proc, token=token, browser=os.path.basename(exe), page=page)
    await asyncio.sleep(1.5)
    if proc.poll() is not None:                              # browser quit straight away
        code = proc.returncode
        _display.update(proc=None, token=None, browser=None, page=None)
        raise TVError(f"the browser exited at once (code {code}); see {RUN_DIR / 'kiosk.log'}")
    out.update(display="open", page=page, browser=os.path.basename(exe))
    return out


@app.post("/display/close")
async def display_close(who: Who = Depends(need("display"))):
    async with get_lock("display"):
        was = kiosk_running()
        await asyncio.get_running_loop().run_in_executor(None, kiosk_stop)
        cam = await asyncio.get_running_loop().run_in_executor(None, batcam_close)
    return {"display": "closed", "was_open": was, "batcam_closed": cam}


@app.post("/display/batcam")
async def batcam_open_endpoint(page: str = Query("watch", pattern="^(login|watch)$"), tv: bool = Query(True),
                               who: Who = Depends(need("display"))):
    """Wake the TV, switch it to the Mac, and open the cameras site (page=watch) or its login
    page (page=login) in Safari, fullscreen. The dashboard keeps running underneath."""
    out = {}
    async with get_lock("display"):
        if tv:
            try:
                if await wake_tv(30):
                    out["tv"] = "on"
                    await call("ssap://tv/switchInput", {"inputId": MAC_INPUT})
                    out["input"] = MAC_INPUT
                else:
                    out["tv"] = "not answering (Safari opened anyway)"
            except (TVError, TVUnreachable) as e:
                out["tv"] = f"problem: {e} (Safari opened anyway)"
        try:
            out.update(await asyncio.get_running_loop().run_in_executor(None, batcam_open, page))
        except RuntimeError as e:
            raise HTTPException(500, "could not open Safari: %s" % e)
    return out


@app.post("/display/batcam/close")
async def batcam_close_endpoint(who: Who = Depends(need("display"))):
    """Closes the Safari window BatCam opened (the dashboard underneath stays)."""
    async with get_lock("display"):
        was = await asyncio.get_running_loop().run_in_executor(None, batcam_close)
    return {"batcam": "closed", "was_open": was}


@app.post("/display/batcam/do")
async def batcam_do_endpoint(action: str = Query(..., pattern="^(top|bottom|past|new)$"),
                             who: Who = Depends(need("display"))):
    """top / bottom scroll the BatCam window; past / new click the left / right side of the
    camera image on screen (older / newer frame). Result text comes from the page."""
    async with get_lock("display"):
        try:
            res = await asyncio.get_running_loop().run_in_executor(None, batcam_do, action)
        except LookupError as e:
            raise HTTPException(409, str(e))
        except RuntimeError as e:
            raise HTTPException(500, str(e))
    return {"action": action, "result": res}


# ---- Tools > Network test: runs nettest.py in the background and serves its results to the phone menu ----
_nettest = {"proc": None, "seconds": 0, "started": 0.0}
NETTEST_RUN_RE = r"^(latest|\d{8}-\d{6})$"


def nettest_mod():
    """nettest.py next to this file (standard library only), imported on first use."""
    if str(BASE) not in sys.path:
        sys.path.insert(0, str(BASE))
    import nettest
    return nettest


def nettest_running():
    """{'pid','started','duration_s','id'} while a test runs (ours, or one started from the shell), else None."""
    nt = nettest_mod()
    info = nt.running_info() or {}
    p = _nettest["proc"]
    if p is not None and p.poll() is None:
        return {"pid": p.pid, "started": info.get("started") or _nettest["started"],
                "duration_s": info.get("duration_s") or _nettest["seconds"], "id": info.get("id")}
    if info:
        return {"pid": int(info["pid"]), "started": info.get("started") or time.time(),
                "duration_s": info.get("duration_s") or 0, "id": info.get("id")}
    return None


def nettest_last():
    """The newest finished run, boiled down for the phone page, or None."""
    nt = nettest_mod()
    d = nt.latest_dir()
    if not d:
        return None
    try:
        r = json.loads((d / "result.json").read_text())
        o = r.get("overall", {})
    except (OSError, ValueError):
        return None
    keys = ("rtt_avg", "rtt_p95", "jitter_ms", "loss_pct", "longest_lost", "usable", "of")
    return {"id": r.get("id"), "started": r.get("started"), "place": r.get("place"), "partial": bool(r.get("partial")),
            "site": (r.get("site") or {}).get("name"),
            "duration_s": r.get("duration_s"), "sent": r.get("sent"), "verdict": o.get("verdict"), "rank": o.get("rank"),
            "mos": o.get("mos"), "summary": o.get("summary"), "numbers": {k: o.get(k) for k in keys},
            "udp": any(t.get("udp") for t in r.get("targets", [])), "notes": list(r.get("notes", []))[:3]}


def nettest_error():
    """Why the last test we started did not run, or None (exit 0 and 3 both leave a saved result)."""
    p = _nettest["proc"]
    if p is None or p.poll() in (None, 0, 3):
        return None
    try:
        tail = (nettest_mod().RUN_DIR / "last.log").read_text(errors="replace").strip().splitlines()
        return (tail[-1] if tail else "") or "the test stopped (exit %s)" % p.returncode
    except OSError:
        return "the test stopped (exit %s)" % p.returncode


@app.post("/tools/nettest/start")
async def nettest_start(seconds: int = Query(600, ge=10, le=1800), who: Who = Depends(need("display"))):
    """Starts a network test in the background (one at a time). It reads tv.env like ./tv nettest does."""
    nt = nettest_mod()
    async with get_lock("nettest"):
        if nettest_running():
            raise HTTPException(409, "A test is already running.")
        env = dict(os.environ)
        env["PATH"] = ":".join(["/opt/homebrew/bin", "/usr/local/bin", env.get("PATH", "")])   # launchd's PATH has no brew
        nt.RUN_DIR.mkdir(parents=True, exist_ok=True)
        with open(str(nt.RUN_DIR / "last.log"), "wb") as logf:
            try:
                proc = subprocess.Popen([sys.executable, str(BASE / "nettest.py"), "run", "--duration", str(seconds)],
                                        stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT, env=env,
                                        cwd=str(BASE), start_new_session=True)
            except OSError as e:
                raise HTTPException(500, "could not start the test: %s" % e)
        _nettest.update(proc=proc, seconds=seconds, started=time.time())
    return {"started": True, "seconds": seconds}


@app.post("/tools/nettest/stop")
async def nettest_stop(who: Who = Depends(need("display"))):
    """Ends the running test early; what was measured so far is still saved as a short run."""
    run = nettest_running()
    if not run:
        return {"stopped": False}
    try:
        cmd = subprocess.run(["ps", "-p", str(run["pid"]), "-o", "command="], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        cmd = ""
    if "nettest.py" not in cmd:
        raise HTTPException(409, "the running process is not the network test")
    os.kill(run["pid"], signal.SIGTERM)
    return {"stopped": True}


@app.get("/tools/nettest/status")
async def nettest_status(who: Who = Depends(need("display"))):
    run = nettest_running()
    out = {"running": bool(run), "error": None if run else nettest_error(), "last": nettest_last()}
    if run:
        dur = int(run["duration_s"] or 0)
        out.update(elapsed_s=round(max(0.0, min(time.time() - float(run["started"]), dur + 45 if dur else 1e9)), 1), duration_s=dur, id=run["id"])
    return out


@app.get("/tools/nettest/report", response_class=HTMLResponse)
async def nettest_report(run: str = Query("latest", pattern=NETTEST_RUN_RE), who: Who = Depends(need("display"))):
    """The saved report page of a run. It shows where the RV was, so it is only served to a valid token."""
    nt = nettest_mod()
    d = nt.latest_dir() if run == "latest" else nt.RUN_DIR / run
    f = (d / "report.html") if d else None
    if not f or not f.exists():
        raise HTTPException(404, "No report yet. Run a test first.")
    return HTMLResponse(f.read_text(), headers={"Cache-Control": "no-store"})


@app.get("/tools/nettest/compare", response_class=HTMLResponse)
async def nettest_compare(who: Who = Depends(need("display"))):
    """compare.html made fresh from every saved run."""
    nt = nettest_mod()

    def build():
        path, _n = nt.write_compare()
        return path.read_text()
    page = await asyncio.get_running_loop().run_in_executor(None, build)
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


# ---- Tools > Network test > Set location: name the campground the dish is at and tag the runs done there ----------
SITE_PARK_RANGE_M = float(os.environ.get("TV_SITE_PARK_RANGE_M", "2000"))     # car spots this close to the Mac are offered
SITE_PARKS_MAX = 5
_site_lock = threading.Lock()


def site_candidates():
    """Blocking. What the Set location screen needs: the Mac's fix, the car's recent park spots near it (one per
    place, nearest first), saved sites near it, the site the Mac is on now, and today's campground from the sheet."""
    nt = nettest_mod()
    fix = nt.location_fix()
    out = {"mac": None, "parks": [], "sites": [], "current": None, "sheet": None, "now": time.time()}
    if fix and fix["lat"] is not None:
        out["mac"] = {"lat": fix["lat"], "lon": fix["lon"], "accuracy_m": fix["accuracy_m"], "age_s": fix["age_s"],
                      "place": fix["place"]}
        out["current"] = nt.site_tag_for_fix(fix)
        kept = []
        for p in sorted(_stops_read()["parks"], key=lambda x: -x["at"]):                     # newest first
            if out["now"] - p["at"] > nt.SITE_LOOKBACK_S:
                break
            d = nt.dist_m(fix["lat"], fix["lon"], p["lat"], p["lon"])
            if d <= SITE_PARK_RANGE_M and not any(nt.dist_m(k["lat"], k["lon"], p["lat"], p["lon"]) < 30 for k in kept):
                kept.append({"lat": p["lat"], "lon": p["lon"], "at": p["at"], "dist_m": int(round(d)), "at_rv": bool(p.get("rv"))})
        out["parks"] = sorted(kept, key=lambda k: (k["dist_m"], -k["at"]))[:SITE_PARKS_MAX]
        for st in nt.read_sites()["sites"]:
            d = nt.dist_m(fix["lat"], fix["lon"], st["lat"], st["lon"])
            if d <= SITE_PARK_RANGE_M:
                out["sites"].append({"id": st["id"], "name": st["name"], "lat": st["lat"], "lon": st["lon"], "dist_m": int(round(d))})
        out["sites"].sort(key=lambda x: x["dist_m"])
    try:
        d0 = datetime.date.today()
        itinerary_city(d0)                                   # makes sure the trip tabs are loaded
        row = place_for(d0)
        if row:
            out["sheet"] = {"city": row["city"], "camp": row["camp"]}
    except Exception as e:
        log.warning("set location: today's campground unavailable: %s", e)
    return out


@app.get("/tools/site/candidates")
async def site_candidates_endpoint(who: Who = Depends(need("display"))):
    return await asyncio.get_running_loop().run_in_executor(None, site_candidates)


@app.get("/tools/site/preview")
async def site_preview_endpoint(lat: float = Query(..., ge=-90, le=90), lon: float = Query(..., ge=-180, le=180),
                                who: Who = Depends(need("display"))):
    """The runs a Set location at this point could tag, each with whether it is ticked by default and why."""
    nt = nettest_mod()
    return {"runs": await asyncio.get_running_loop().run_in_executor(None, nt.retag_candidates, lat, lon)}


@app.post("/tools/site/set")
async def site_set_endpoint(request: Request, who: Who = Depends(need("display"))):
    """Body: {name, lat, lon, ids?: [run ids], source?}. Saves the site (or reuses the saved one within
    TV_SITE_MATCH_M), makes it the newest Set location and tags the runs (ids, or the suggested ones)."""
    try:
        body = await request.json()
        name, lat, lon = str(body["name"]), float(body["lat"]), float(body["lon"])
        ids = body.get("ids")
        if ids is not None and not (isinstance(ids, list) and all(isinstance(i, str) for i in ids)):
            raise ValueError("ids must be a list of run ids")
        source = re.sub(r"[^a-z]", "", str(body.get("source") or "manual").lower())[:12] or "manual"
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, "send {name, lat, lon} as JSON (ids and source are optional)")
    nt = nettest_mod()

    def run():
        with _site_lock:
            return nt.set_site(name, lat, lon, ids=ids, source=source)
    try:
        return await asyncio.get_running_loop().run_in_executor(None, run)
    except ValueError as e:
        raise HTTPException(400, str(e))


CAM_NAME_RE = "^(all|%s)$" % "|".join(c for c, _ in CAMS)


@app.get("/display/cams", response_class=HTMLResponse)
async def cams_page(src: str = Query("all", pattern=CAM_NAME_RE)):
    """The camera page Safari shows: one camera filling the screen, or src=all for a 2-wide grid.
    Holds no secrets (the video itself comes from go2rtc, reachable only from the Mac)."""
    html = (DISPLAY_DIR / "cams.html").read_text()
    cfg = {"g2r": GO2RTC_URL, "cams": cam_list(), "src": src}
    return html.replace("__CFG__", json.dumps(cfg).replace("<", "\\u003c"))


@app.post("/display/cams")
async def cams_open_endpoint(cam: str = Query("all", pattern=CAM_NAME_RE), tv: bool = Query(True),
                             who: Who = Depends(need("display"))):
    """Wake the TV, switch it to the Mac, and show one Canny camera (cam=driveway ...) or all of
    them in a 2-wide grid (cam=all) fullscreen in Safari. Close screen / Home removes it."""
    if cam == "all":
        if not CAMS_LIVE:
            raise HTTPException(409, "no cameras are set up yet (TV_CAMS_LIVE is empty)")
    elif cam not in CAMS_LIVE:
        raise HTTPException(409, "%s is not connected yet" % CAM_LABEL[cam])
    out = {}
    async with get_lock("display"):
        if tv:
            try:
                if await wake_tv(30):
                    out["tv"] = "on"
                    await call("ssap://tv/switchInput", {"inputId": MAC_INPUT})
                    out["input"] = MAC_INPUT
                else:
                    out["tv"] = "not answering (Safari opened anyway)"
            except (TVError, TVUnreachable) as e:
                out["tv"] = f"problem: {e} (Safari opened anyway)"
        try:
            out.update(await asyncio.get_running_loop().run_in_executor(None, cams_open, cam))
        except RuntimeError as e:
            raise HTTPException(500, "could not open Safari: %s" % e)
    return out


def geocode(place):
    """'Moab, Utah' -> (lat, lon, 'Moab, Utah'). Blocking; run in a thread."""
    name, _, hint = place.partition(",")
    hint = hint.strip().lower()
    us = hint.upper() in US_STATES                      # "CA" means California, not Canada
    if us:
        hint = US_STATES[hint.upper()].lower()
    q = urllib.parse.urlencode({"name": name.strip(), "count": 10, "language": "en", "format": "json"})
    with urllib.request.urlopen(f"{GEOCODE_URL}?{q}", timeout=8) as r:
        results = json.load(r).get("results") or []
    if not results:
        return None
    def matches(x):
        fields = [str(x.get(k, "")).lower() for k in ("admin1", "country", "country_code")]
        return any(hint and (hint == f or hint in f) for f in fields)
    best = next((x for x in results if matches(x) and (not us or x.get("country_code") == "US")),
                None) or next((x for x in results if matches(x)), results[0])
    label = ", ".join(v for v in (best.get("name"), best.get("admin1") or best.get("country")) if v)
    return best["latitude"], best["longitude"], label


@app.post("/display/location")
async def set_location(place: str = Query("", max_length=80),
                       lat: float = Query(None, ge=-90, le=90), lon: float = Query(None, ge=-180, le=180),
                       label: str = Query("", max_length=60), auto: bool = Query(False),
                       who: Who = Depends(need("admin"))):
    """Where the weather tile looks. The RV moves, so this is a setting, not a constant.
    Give either place=\"Moab, Utah\" (looked up for you) or lat= and lon=, which pins the
    weather there. auto=true goes back to following the itinerary."""
    if auto:
        if not ITIN_SOURCES and not LOCATION_FILE.exists():
            raise HTTPException(400, "no itinerary configured: add TV_ITINERARY=<csv url> to tv.env")
        save_state(mode="auto")
        _weather["next"] = 0.0
        return await current_location()
    if place.strip():
        try:
            found = await asyncio.get_running_loop().run_in_executor(None, geocode, place.strip())
        except Exception as e:
            raise TVError(f"place lookup failed (is the Mac online?): {e}")
        if not found:
            raise HTTPException(404, f"no place found for {place!r}; try 'City, State'")
        lat, lon, label = found[0], found[1], label or found[2]
    elif lat is None or lon is None:
        raise HTTPException(400, "give place=... or both lat= and lon=")
    loc = {"lat": lat, "lon": lon, "label": label.strip() or f"{lat:.2f}, {lon:.2f}"}
    save_state(**loc, mode="manual")
    _weather["next"] = 0.0
    return loc


COPY_ON_OPEN = os.environ.get("TV_COPY_ON_OPEN", "on").strip().lower() not in ("off", "0", "no", "false")


async def copy_destination():
    """Puts the destination address (sheet column K of the target: today's row on a move day,
    otherwise the next stop) on the Mac's clipboard with pbcopy, so Universal Clipboard can hand
    it to a phone signed in to the same Apple ID. Returns {"copied", "label", "date"}."""
    nav = await get_nav()
    t = (nav or {}).get("target") or {}
    text = t.get("address") or t.get("label")
    if not text:
        raise HTTPException(404, "no destination found in the itinerary")
    import shutil as _sh
    if not _sh.which("pbcopy"):
        raise HTTPException(501, "pbcopy not found: copying only works on the Mac")

    def run():
        subprocess.run(["pbcopy"], input=text.encode("utf-8"), timeout=5, check=True,
                       env={**os.environ, "LC_ALL": "en_US.UTF-8"})
    try:
        await asyncio.get_running_loop().run_in_executor(None, run)
    except Exception as e:
        raise HTTPException(500, "could not use the clipboard: %s" % e)
    return {"copied": text, "label": t.get("label"), "date": t.get("date")}


# ---- text the destination to friends (iMessage through the Messages app on the Mac) ----------
TEXT_TO = [x.strip() for x in os.environ.get("TV_TEXT_TO", "").split(",") if x.strip()]
TEXT_MODE = os.environ.get("TV_TEXT_MODE", "move").strip().lower()
try:
    TEXT_MIN_GAP = float(os.environ.get("TV_TEXT_MIN_GAP", "300"))
except ValueError:
    TEXT_MIN_GAP = 300.0
TEXT_FILE = STATE_FILE.parent / "text_sent.json"
IMESSAGE_SCRIPT = """on run argv
  set theTo to item 1 of argv
  set theMsg to item 2 of argv
  tell application "Messages"
    set svc to 1st service whose service type = iMessage
    try
      set theBuddy to buddy theTo of svc
    on error
      set theBuddy to participant theTo of svc
    end try
    send theMsg to theBuddy
  end tell
end run"""


def text_state():
    try:
        return json.loads(TEXT_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def text_save(**kv):
    fd = os.open(str(TEXT_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(kv, f)


def text_body(nav):
    t = nav["target"]
    when = ""
    if t.get("date"):
        try:
            d = datetime.date.fromisoformat(t["date"])
            when = " (%s %d)" % (d.strftime("%a %b"), d.day)
        except ValueError:
            pass
    addr = t.get("address") or t.get("label")
    if nav.get("move_day"):
        lines = ["Move day: heading to %s" % (t.get("label") or addr)]
    else:
        lines = ["Next stop%s: %s" % (when, t.get("label") or addr)]
    if t.get("address"):
        lines.append(t["address"])
    r = nav.get("route") or {}
    if r.get("miles") and not r.get("straight"):
        mins = r.get("minutes")
        frm = (nav.get("here") or {}).get("label")
        lines.append("%s mi%s%s" % (int(round(r["miles"])), (", about %dh %02dm" % (mins // 60, mins % 60)) if mins else "",
                                    (" from " + frm) if frm else ""))
    lines.append("https://maps.apple.com/?dirflg=d&daddr=" + urllib.parse.quote(addr))
    return "\n".join(lines)


def send_imessage(to, body):
    try:
        r = subprocess.run(["osascript", "-e", IMESSAGE_SCRIPT, to, body], capture_output=True, text=True, timeout=45)
    except FileNotFoundError:
        raise RuntimeError("osascript not found: texting only works on the Mac")
    except subprocess.TimeoutExpired:
        raise RuntimeError("Messages did not answer in 45 s: is there a permission prompt on the Mac's screen?")
    if r.returncode != 0:
        err = (r.stderr or "").strip()
        if "-1743" in err or "not authorized" in err.lower():
            err = "not allowed to control Messages: System Settings > Privacy & Security > Automation, allow it. (%s)" % err[:120]
        raise RuntimeError(err[:300] or "osascript failed")


async def text_destination(force=False):
    """Texts the destination to TV_TEXT_TO. Returns {"sent": [...]} or {"skipped": reason}."""
    if not TEXT_TO:
        return {"skipped": "TV_TEXT_TO is not set"}
    if TEXT_MODE not in ("move", "always") and not force:
        return {"skipped": "texting is off (TV_TEXT_MODE=%s)" % TEXT_MODE}
    nav = await get_nav()
    t = (nav or {}).get("target") or {}
    if not (t.get("address") or t.get("label")):
        return {"skipped": "no destination in the itinerary"}
    st, now = text_state(), time.time()
    key = "%s|%s" % (datetime.date.today().isoformat(), t.get("address") or t.get("label"))
    if force:
        if now - st.get("at", 0) < 30:
            return {"skipped": "texted %d s ago" % (now - st["at"])}
    elif TEXT_MODE == "move":
        if not nav.get("move_day"):
            return {"skipped": "not a move day"}
        if st.get("key") == key:
            return {"skipped": "already texted today"}
    elif now - st.get("at", 0) < TEXT_MIN_GAP:
        return {"skipped": "texted %d s ago (minimum gap %d s)" % (now - st["at"], TEXT_MIN_GAP)}
    body = text_body(nav)
    loop = asyncio.get_running_loop()
    for to in TEXT_TO:
        await loop.run_in_executor(None, send_imessage, to, body)
    text_save(key=key, at=now)
    log.info("texted destination %r to %d recipient(s)", t.get("address") or t.get("label"), len(TEXT_TO))
    return {"sent": TEXT_TO, "text": body}


@app.post("/display/text-address")
async def text_address(who: Who = Depends(need("display"))):
    """Texts the destination (address + maps link) to TV_TEXT_TO right now."""
    if not TEXT_TO:
        raise HTTPException(400, "no recipient: add TV_TEXT_TO=+1... to tv.env and restart")
    try:
        res = await text_destination(force=True)
    except RuntimeError as e:
        raise HTTPException(500, "could not send the text: %s" % e)
    log.info("%s: text-address %s", who.name, "sent" if "sent" in res else res)
    return res


@app.post("/display/copy-address")
async def copy_address(who: Who = Depends(need("display"))):
    """Copies the destination address to the Mac's clipboard (see copy_destination)."""
    res = await copy_destination()
    log.info("%s: copied destination %r", who.name, res["copied"])
    return res


# ---- Tools > Prep for move: make the Mac safe for a power cut ---------------------------------------
# The mini is unplugged with the Mac still running, so everything that could be writing to disk is
# stopped first: the TV goes off, the destination is texted, then prepmove.py (its own one-shot launchd
# job, because it has to outlive this service) stops the RV TV agents, quits every app, texts "ready"
# with the apps that would not quit and the Apple Maps link, and syncs. Tailscale, the reverse tunnel and
# sshd stay up. TV_PREPMOVE=off disables it (run-dev.sh does, so a dev copy can never quit your apps).
TEXT_SELF = [x.strip() for x in os.environ.get("TV_TEXT_SELF", "").split(",") if x.strip()]
IS_MAC = sys.platform == "darwin"
PREP_ON = os.environ.get("TV_PREPMOVE", "on").strip().lower() not in ("off", "0", "no", "false")
PREP_KEEP = [x.strip() for x in os.environ.get("TV_PREP_KEEP", "").split(",") if x.strip()]   # more app names to leave open
PREP_JOB = "com.lowone.prepmove"
_prep = {"running": False, "started": 0.0, "task": None}


def prep_report():
    try:
        return json.loads((RUN_DIR / "prepmove-report.json").read_text())
    except (OSError, ValueError):
        return None


def prep_spawn(plan):
    """Blocking. Writes the plan and starts prepmove.py as a one-shot launchd job named com.lowone.prepmove;
    where launchd refuses (not a login session), a detached process does the same work."""
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    plan_file = RUN_DIR / "prepmove-plan.json"
    fd = os.open(str(plan_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(plan, f)
    args = [sys.executable, str(BASE / "prepmove.py"), "--plan", str(plan_file)]
    logf = str(RUN_DIR / "prepmove.log")
    plist = RUN_DIR / (PREP_JOB + ".plist")
    plist.write_text("""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>%s</string>
<key>ProgramArguments</key><array>%s</array>
<key>WorkingDirectory</key><string>%s</string>
<key>RunAtLoad</key><true/>
<key>StandardOutPath</key><string>%s</string>
<key>StandardErrorPath</key><string>%s</string>
</dict></plist>
""" % (PREP_JOB, "".join("<string>%s</string>" % a.replace("&", "&amp;").replace("<", "&lt;") for a in args),
       BASE, logf, logf))
    dom = "gui/%d" % os.getuid()
    subprocess.run(["launchctl", "bootout", "%s/%s" % (dom, PREP_JOB)], capture_output=True, timeout=15)
    r = subprocess.run(["launchctl", "bootstrap", dom, str(plist)], capture_output=True, text=True, timeout=15)
    if r.returncode == 0:
        return "launchd"
    log.warning("prepmove: launchctl bootstrap failed (%s); starting it detached instead", (r.stderr or "").strip()[:100])
    with open(logf, "ab") as lf:
        subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=lf, stderr=lf, cwd=str(BASE), start_new_session=True)
    return "detached"


async def prep_flow(dry_run):
    """The part that needs this service (the TV, the nav data, the friend text); the rest is prepmove.py."""
    loop = asyncio.get_running_loop()
    warnings = []
    try:
        friend = None
        if not dry_run:
            try:
                friend = await text_destination()       # the usual rule: once a day per destination, TV_TEXT_MODE applies
            except Exception as e:
                warnings.append("The text to your friend failed: %s" % str(e)[:100])
        body = None
        try:
            nav = await get_nav()
            t = (nav or {}).get("target") or {}
            if t.get("address") or t.get("label"):
                body = text_body(nav)
        except Exception as e:
            warnings.append("No destination in the text (%s)." % str(e)[:80])
        if not dry_run:
            try:
                await call("ssap://system/turnOff", expect_close=True)
            except Exception as e:
                warnings.append("TV did not confirm power off (%s)." % (str(e)[:60] or "unreachable"))
            await loop.run_in_executor(None, kiosk_stop)
            if friend and friend.get("sent"):
                await asyncio.sleep(10)                  # let iMessage hand the text over before apps start closing
        plan = {"dry_run": dry_run, "self_to": TEXT_SELF, "destination_text": body, "warnings": warnings,
                "imessage_script": IMESSAGE_SCRIPT, "keep_extra": PREP_KEEP}
        how = await loop.run_in_executor(None, prep_spawn, plan)
        log.info("prepmove: %s started (%s)%s", "dry run" if dry_run else "run", how,
                 "; friend text: %s" % (friend if friend else "none"))
    except Exception:
        log.exception("prepmove failed before the helper started")
    finally:
        _prep["running"] = False


@app.post("/tools/prepmove/start")
async def prepmove_start(dry_run: bool = Query(False), who: Who = Depends(need("display"))):
    """Starts the prep. dry_run=1 only lists what would be quit and stopped. The reply comes first; for a real run
    this service stops about a minute later, so the phone page cannot show the end: the ready text does."""
    if not PREP_ON:
        raise HTTPException(501, "Prep for move is switched off here (TV_PREPMOVE=off).")
    if not IS_MAC:
        raise HTTPException(501, "Prep for move only works on the Mac.")
    if _prep["running"] and time.time() - _prep["started"] < 300:
        raise HTTPException(409, "Already prepping.")
    _prep.update(running=True, started=time.time())
    _prep["task"] = asyncio.get_running_loop().create_task(prep_flow(dry_run))      # kept so it is not collected
    log.info("%s: prepmove %s", who.name, "dry run" if dry_run else "START")
    return {"started": True, "dry_run": dry_run, "text_to_you": bool(TEXT_SELF), "text_to_friend": bool(TEXT_TO),
            "tv_off": not dry_run}


@app.get("/tools/prepmove/status")
async def prepmove_status(who: Who = Depends(need("display"))):
    return {"enabled": PREP_ON and IS_MAC, "running": _prep["running"], "report": prep_report(),
            "text_to_you": bool(TEXT_SELF), "now": time.time()}


class FreshStatic(StaticFiles):
    """Display files are revalidated on every load (cheap: the ETag answers 304), so a changed widget
    reaches the kiosk at once instead of whenever Chrome decides its cached copy has expired."""
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


if DISPLAY_DIR.is_dir():
    app.mount("/display/static", FreshStatic(directory=str(DISPLAY_DIR)), name="display-static")


GUEST_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RV TV</title>
<style>
:root{color-scheme:light dark;--bg:#f4f4f6;--fg:#111;--card:#fff;--line:#d0d0d6;--acc:#2a5bd7}
@media (prefers-color-scheme:dark){:root{--bg:#111;--fg:#eee;--card:#1e1e22;--line:#3a3a42;--acc:#6b93ff}}
*{box-sizing:border-box}
body{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:17px system-ui,sans-serif;max-width:480px;margin-inline:auto}
h1{font-size:20px;margin:4px 0 12px}
#st{padding:10px 12px;border:1px solid var(--line);border-radius:10px;background:var(--card);margin-bottom:12px;min-height:44px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px}
button{padding:18px 8px;font:inherit;font-weight:600;border-radius:12px;border:1px solid var(--line);background:var(--card);color:var(--fg)}
button:active{background:var(--acc);color:#fff}
button.on{border-color:var(--acc)}
button:disabled{opacity:.5}
[hidden]{display:none!important}
form{display:flex;gap:8px;margin-bottom:10px}
input{flex:1;min-width:0;padding:12px;font:inherit;border-radius:12px;border:1px solid var(--line);background:var(--card);color:var(--fg)}
form button{padding:12px 14px}
.note{font-size:14px;opacity:.7;margin:0 0 10px}
.wide{grid-column:1/-1}
.bb{grid-column:1/-1;padding:10px 14px 4px;border-radius:12px;border:1px solid var(--acc);background:var(--card);color:var(--fg);cursor:pointer;user-select:none;-webkit-user-select:none;-webkit-tap-highlight-color:transparent}
.bb.down{background:var(--acc);color:#fff}
.bb-h{display:flex;justify-content:space-between;align-items:baseline;font-size:14px;font-weight:600}
.bb-h span+span{font-weight:500;opacity:.8}
.bb-s{margin-top:2px;cursor:default}
.bb input{display:block;width:100%;flex:none;height:28px;margin:0;padding:0;border:0;border-radius:0;background:transparent;-webkit-appearance:none;appearance:none}
.bb input::-webkit-slider-runnable-track{height:4px;border-radius:2px;background:var(--line)}
.bb input::-webkit-slider-thumb{-webkit-appearance:none;box-sizing:border-box;width:24px;height:24px;margin-top:-10px;border-radius:50%;border:2px solid var(--card);background:var(--acc)}
.bb input::-moz-range-track{height:4px;border-radius:2px;background:var(--line)}
.bb input::-moz-range-thumb{box-sizing:border-box;width:24px;height:24px;border-radius:50%;border:2px solid var(--card);background:var(--acc)}
.bb-ticks{position:relative;height:28px;margin:0 12px}
.bb-ticks i{position:absolute;top:0;width:1px;height:5px;margin-left:-.5px;background:var(--fg);opacity:.45}
.bb-ticks i.m{height:9px;opacity:.8}
.res{border:1px solid var(--line);border-radius:12px;background:var(--card);padding:12px 14px;margin-bottom:10px}
.res .v{font-size:20px;font-weight:700}
.res .sub{font-size:14px;opacity:.8;margin-top:2px}
.tiles{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:10px}
.tiles div{border:1px solid var(--line);border-radius:10px;padding:8px 10px;font-size:13px}
.tiles b{display:block;font-size:19px}
.res ul{margin:10px 0 0;padding-left:18px;font-size:14px;opacity:.85}
.bar{height:8px;border-radius:4px;background:var(--line);overflow:hidden;margin-top:8px}
.bar i{display:block;height:100%;width:0;background:var(--acc);transition:width .6s linear}
#rep{width:100%;height:calc(100vh - 140px);min-height:460px;border:1px solid var(--line);border-radius:12px;background:transparent}
.bb-ticks b{position:absolute;top:12px;transform:translateX(-50%);font-size:11px;font-weight:500;opacity:.75}
#site-map{height:46vh;min-height:300px;border:1px solid var(--line);border-radius:12px;margin:0 0 10px;background:#222}
.sp{width:26px;height:26px;border-radius:50%;border:2px solid #fff;background:#2a5bd7;color:#fff;font:700 13px/22px system-ui,sans-serif;text-align:center;box-shadow:0 1px 4px #0009}
.sp.car{background:#d67a00}.sp.mac{background:#555}.sp.site{background:#1a9c55}.sp.drop{background:#9b30d0}
.sp.sel{outline:3px solid #fff;outline-offset:0;transform:scale(1.3)}
.sptip{font:600 12px system-ui,sans-serif;padding:1px 6px;border-radius:6px}
.prow{padding:10px 12px;margin-bottom:6px;border:1px solid var(--line);border-radius:10px;background:var(--card);cursor:pointer;font-size:15px}
.prow.sel{border-color:var(--acc);box-shadow:inset 0 0 0 1px var(--acc)}
.prow small{display:block;opacity:.7;font-size:13px}
.runrow{display:flex;gap:10px;align-items:flex-start;padding:8px 0;font-size:14px;border-top:1px solid var(--line)}
.runrow input{flex:none;width:20px;height:20px;margin-top:2px}
.runrow small{display:block;opacity:.7}
#site-name{width:100%;flex:none;margin:6px 0 10px}
</style></head><body>
<h1 id="title">RV TV</h1>
<div id="st">Checking TV...</div>
<div id="tvctl">
<div class="grid">
 <button class="on" data-act="power_on" onclick="go('/power/on?wait=30','Waking the TV (up to 30 s)...')">Power on</button>
 <button data-act="power_off" onclick="go('/power/off','Turning off...')">Power off</button>
</div>
<div class="grid">
 <button data-act="volume" onclick="go('/volume/down','')">Vol &minus;</button>
 <button data-act="volume" onclick="go('/volume/up','')">Vol +</button>
</div>
<div class="grid">
 <button data-act="mute" onclick="go('/mute/on','')">Mute</button>
 <button data-act="mute" onclick="go('/mute/off','')">Unmute</button>
</div>
</div>
<div id="v-main">
<div class="grid">
 <button data-act="app" onclick="go('/app/netflix','Opening Netflix...')">Netflix</button>
 <button data-act="app" onclick="go('/app/amazon','Opening Prime Video...')">Prime Video</button>
 <button data-act="app" onclick="go('/app/youtube.leanback.v4','Opening YouTube...')">YouTube</button>
 <button data-act="input" onclick="go('/input/HDMI_1','Switching to the Mac...')">Mac screen</button>
</div>
<div class="grid">
 <div class="bb" id="bb" role="button" tabindex="0" data-act="display" onclick="batboard()"
      onkeydown="if(event.key==='Enter')batboard()" onpointerdown="this.classList.add('down')"
      onpointerup="this.classList.remove('down')" onpointerleave="this.classList.remove('down')"
      onpointercancel="this.classList.remove('down')">
  <div class="bb-h"><span>BatBoard</span><span id="bbv">35 miles</span></div>
  <div class="bb-s" onclick="event.stopPropagation()" onpointerdown="event.stopPropagation()">
   <input type="range" id="bbr" min="10" max="100" step="5" value="35" aria-label="Map width in miles">
   <div class="bb-ticks" id="bbt"></div>
  </div>
 </div>
 <button data-act="display" onclick="go('/display/open?page=sheet','Opening the sheet on the Mac (up to 40 s)...')">SNOW</button>
 <button data-act="display" onclick="go('/display/close','Closing the screen...')">Close screen</button>
 <button data-act="display" onclick="view('cam')">BatCam</button>
 <button data-act="display" onclick="view('tools')">Tools</button>
</div>
<form data-act="admin" onsubmit="setPlace(event)">
 <input id="place" placeholder="Weather place, e.g. Moab, Utah" autocomplete="off">
 <button>Set</button>
 <button type="button" onclick="go('/display/location?auto=true','Weather now follows the Mac location, then the trip schedule...')">Follow location</button>
</form>
</div>
<div id="v-cam" hidden>
<div class="grid">
 <button data-act="display" onclick="go('/display/batcam?page=login','Opening the login page in Safari (up to 40 s)...')">Login</button>
 <button class="on" data-act="display" onclick="go('/display/batcam?page=watch','Opening the cameras in Safari (up to 40 s)...')">Load</button>
</div>
<div class="grid">
 <button class="wide" data-act="display" onclick="view('canny')">Canny (live)</button>
</div>
<div class="grid">
 <button data-act="display" onclick="cam('top')">Top</button>
 <button data-act="display" onclick="cam('bottom')">Bottom</button>
</div>
<div class="grid">
 <button data-act="display" onclick="cam('past')">PAST</button>
 <button data-act="display" onclick="cam('new')">NEW</button>
</div>
<div class="grid">
 <button class="wide" data-act="display" onclick="home()">Home</button>
</div>
</div>
<div id="v-canny" hidden>
<div class="grid">
 <button class="wide on" data-act="display" onclick="camGo('all','all cameras')">All cameras</button>
</div>
<div class="grid" id="canny-btns"></div>
<div class="grid">
 <button data-act="display" onclick="view('cam')">Back</button>
 <button data-act="display" onclick="home()">Home</button>
</div>
</div>
<div id="v-tools" hidden>
<div class="grid">
 <button class="wide" data-act="display" onclick="view('nettest')">Network test</button>
</div>
<div class="grid">
 <button id="pm-go" class="wide" data-act="display" onclick="pmTap()">Prep for move</button>
</div>
<div class="grid">
 <button class="wide" data-act="display" onclick="pmRun(true)">Test the prep (changes nothing)</button>
</div>
<div class="res" id="tl-info">
 <div class="sub" id="tl-stops"></div>
 <div class="sub" id="tl-prep"></div>
</div>
<div class="grid">
 <button class="wide" data-act="display" onclick="home()">Home</button>
</div>
</div>
<div id="v-nettest" hidden>
<div class="grid">
 <button id="nt-go10" class="on" data-act="display" onclick="ntStart(600)">Start 10 min</button>
 <button id="nt-go1" data-act="display" onclick="ntStart(60)">Quick 1 min</button>
</div>
<div class="grid">
 <button id="nt-stop" class="wide" data-act="display" onclick="ntStop()" disabled>Stop</button>
</div>
<div class="res" id="nt-prog" hidden>
 <div id="nt-ptxt">Testing...</div>
 <div class="bar"><i id="nt-bar"></i></div>
 <div class="sub">Uses about 3 Mbit/s while it runs. Streaming or calls on the RV network will show up in the numbers.</div>
</div>
<div class="res" id="nt-res" hidden>
 <div class="v" id="nt-verdict"></div>
 <div class="sub" id="nt-sum"></div>
 <div class="sub" id="nt-when"></div>
 <div class="tiles">
  <div>Call quality<b id="nt-score"></b></div>
  <div>Round trip<b id="nt-rtt"></b></div>
  <div>Slowest 5%<b id="nt-p95"></b></div>
  <div>Jitter<b id="nt-jit"></b></div>
  <div>Packet loss<b id="nt-loss"></b></div>
  <div>Longest dropout<b id="nt-drop"></b></div>
 </div>
 <ul id="nt-notes"></ul>
</div>
<div class="grid">
 <button id="nt-rep" data-act="display" onclick="ntShow('report')" disabled>Full report</button>
 <button data-act="display" onclick="ntShow('compare')">Compare places</button>
</div>
<div class="grid">
 <button class="wide" data-act="display" onclick="siteOpen()">Set location</button>
</div>
<div class="note" id="nt-site"></div>
<div class="grid">
 <button data-act="display" onclick="view('tools')">Back</button>
 <button data-act="display" onclick="home()">Home</button>
</div>
</div>
<div id="v-site" hidden>
<div class="res"><div id="site-sum">Looking...</div><div class="sub" id="site-sub"></div></div>
<div id="site-map"></div>
<div class="note">Tap a pin or a row to choose where the dish is. Tap empty map to drop your own pin. Pinch or use + and - to zoom.</div>
<div id="site-pins"></div>
<label class="note" for="site-name">Name for this campground</label>
<input id="site-name" type="text" maxlength="60" autocomplete="off" placeholder="Campground name">
<div class="res" id="site-runs-box">
 <div id="site-runs-h"><b>Tests it will tag</b></div>
 <div id="site-runs"></div>
</div>
<div class="grid">
 <button id="site-save" class="on" data-act="display" onclick="siteSave()" disabled>Save location</button>
 <button data-act="display" onclick="view('nettest')">Cancel</button>
</div>
</div>
<div id="v-report" hidden>
<div class="grid">
 <button class="wide" data-act="display" onclick="view('nettest')">Back</button>
</div>
<iframe id="rep" sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox" title="Network test"></iframe>
</div>
<script>
var T=location.hash.slice(1);
try{if(T){localStorage.setItem('tvtok',T)}else{T=localStorage.getItem('tvtok')||''}}catch(e){}
var st=document.getElementById('st'), ME=null;
function show(t){st.textContent=t}
async function api(method,path){
  var r=await fetch(path,{method:method,headers:{Authorization:'Bearer '+T}});
  var j={};try{j=await r.json()}catch(e){}
  return {ok:r.ok,status:r.status,j:j};
}
function can(a){return ME&&(ME.admin||ME.actions.indexOf(a)>=0)}
function applyPerms(){
  document.querySelectorAll('[data-act]').forEach(function(e){e.hidden=!can(e.dataset.act)});
  document.querySelectorAll('.grid').forEach(function(g){
    g.hidden=!Array.prototype.some.call(g.children,function(c){return !c.hidden})});
}
async function go(path,msg){
  if(msg)show(msg);
  try{
    var r=await api('POST',path);
    if(r.status==401){show('This link is not valid any more. Ask Devon for a new one.');return}
    if(r.status==403){show('Not allowed for your link.');return}
    if(r.status==503){show('TV is off or not reachable. Press Power on.');return}
    if(r.status==202){show('Wake signal sent, the TV is slow. Try again in a few seconds.');return}
    if(!r.ok){show('Something went wrong: '+(r.j.detail||r.status));return}
  }catch(e){show('Cannot reach the TV box. Are you on the RV Wi-Fi?');return}
  setTimeout(refresh,1500);
}
var BB={touched:false};
function bbShow(){document.getElementById('bbv').textContent=document.getElementById('bbr').value+' miles'}
function bbInit(){
  var h='';
  for(var m=10;m<=100;m+=5){
    var x=((m-10)/90*100).toFixed(3);
    h+='<i'+(m%10===0?' class="m"':'')+' style="left:'+x+'%"></i>';
    if(m%20===10||m===100)h+='<b style="left:'+x+'%">'+m+'</b>';
  }
  document.getElementById('bbt').innerHTML=h;
  document.getElementById('bbr').oninput=function(){BB.touched=true;bbShow()};
}
async function batboard(){
  var m=document.getElementById('bbr').value;
  show('Map set to '+m+' miles...');
  try{
    var r=await api('POST','/display/map?miles='+encodeURIComponent(m));
    if(r.status==401){show('This link is not valid any more. Ask Devon for a new one.');return}
    if(r.status==403){show('Not allowed for your link.');return}
    if(!r.ok){show('Could not set the map width: '+(r.j.detail||r.status));return}
  }catch(e){show('Cannot reach the TV box. Are you on the RV Wi-Fi?');return}
  go('/display/open','Map '+m+' miles. Waking the TV and opening the dashboard (up to 40 s)...');
}
var CUR='main', ntTimer=null;
function view(v){
  var titles={main:'RV TV',cam:'BatCam',canny:'Canny',tools:'Tools',nettest:'Network test',site:'Set location',report:'Report'};
  Object.keys(titles).forEach(function(n){document.getElementById('v-'+n).hidden=(n!==v)});
  document.getElementById('title').textContent=titles[v];
  document.title=titles[v];
  CUR=v;
  document.getElementById('tvctl').hidden=(v==='report'||v==='site');   // the report and the map get the whole screen
  if(ntTimer){clearInterval(ntTimer);ntTimer=null}
  if(v==='cam')show('BatCam: press Login if the cameras say 401, then Load.');
  else if(v==='canny'){show('Canny: pick a camera.');loadCams()}
  else if(v==='tools'){show('Tools');toolsInfo()}
  else if(v==='nettest'){ntRefresh();ntTimer=setInterval(ntRefresh,3000)}
  else if(v==='report'||v==='site'){}
  else refresh();
}
function ago(t,now){
  var s=Math.max(0,Math.round((now||Date.now()/1000)-t));
  if(s<90)return 'just now';
  if(s<5400)return Math.round(s/60)+' min ago';
  if(s<172800)return Math.round(s/3600)+' h ago';
  return Math.round(s/86400)+' days ago';
}
async function toolsInfo(){
  var a=document.getElementById('tl-stops'),b=document.getElementById('tl-prep');
  a.textContent='';b.textContent='';
  try{
    var r=await api('GET','/tools/stops/status');
    if(r.ok){
      var j=r.j,t=[];
      t.push(j.last_upload?'Stop log: last upload '+ago(j.last_upload.at,j.now)+' ('+j.last_upload.applied+' new)':'Stop log: nothing uploaded yet');
      if(j.parked_since)t.push('car parked since '+ago(j.parked_since,j.now));
      if(j.last_error)t.push('last problem: '+j.last_error.msg+' ('+ago(j.last_error.at,j.now)+')');
      a.textContent=t.join('. ')+'.';
    }
    var q=await api('GET','/tools/prepmove/status');
    if(q.ok){
      var p=q.j,rep=p.report,u=[];
      if(!p.enabled)u.push('Prep for move is not available on this copy.');
      else{
        if(rep){
          u.push((rep.dry_run?'Last test ':'Last prep ')+ago(rep.at,p.now)+': '+(rep.dry_run?'would quit ':'quit ')+(rep.quit.length)+' app(s)'
            +(rep.refused.length?', would not quit: '+rep.refused.join(', '):'')+'.');
          if(rep.dry_run&&rep.quit.length)u.push('Would quit: '+rep.quit.join(', ')+'.');
        }
        if(!p.text_to_you)u.push('No TV_TEXT_SELF in tv.env, so the ready text will not reach you.');
      }
      b.textContent=u.join(' ');
    }
  }catch(e){}
}
var PM={armed:false,timer:null};
function pmReset(){
  PM.armed=false;if(PM.timer){clearTimeout(PM.timer);PM.timer=null}
  var b=document.getElementById('pm-go');b.textContent='Prep for move';b.classList.remove('on');
}
function pmTap(){
  if(!PM.armed){
    PM.armed=true;
    var b=document.getElementById('pm-go');b.textContent='Tap again to confirm (10 s)';b.classList.add('on');
    PM.timer=setTimeout(function(){pmReset();show('Prep cancelled.')},10000);
    show('This turns the TV off, quits every app and stops the RV TV service on the mini.');
    return;
  }
  pmReset();pmRun(false);
}
async function pmRun(dry){
  show(dry?'Testing the prep...':'Starting the prep...');
  try{
    var r=await api('POST','/tools/prepmove/start'+(dry?'?dry_run=1':''));
    if(r.status==401){show('This link is not valid any more. Ask Devon for a new one.');return}
    if(r.status==403){show('Not allowed for your link.');return}
    if(r.status==409){show('Already prepping.');return}
    if(!r.ok){show('Could not start: '+(r.j.detail||r.status));return}
    if(dry){show('Testing. Results show here in about 30 seconds.');setTimeout(toolsInfo,20000);setTimeout(toolsInfo,40000)}
    else show('Prepping. This page goes quiet in about a minute'+(r.j.text_to_you?', then you get a text when the mini is ready.':'. No TV_TEXT_SELF is set, so no text will come: wait 2 minutes before cutting power.'));
  }catch(e){show('Cannot reach the TV box. Are you on the RV Wi-Fi?')}
}
function ntClock(s){s=Math.max(0,Math.round(s));return Math.floor(s/60)+':'+('0'+(s%60)).slice(-2)}
var NTICON=['✓','▲','✕'];
function ntSet(id,t){document.getElementById(id).textContent=t}
function ntNum(v,u,d){return (v===null||v===undefined)?'-':Number(v).toFixed(d||0)+(u||'')}
function ntRender(j){
  document.getElementById('nt-go10').disabled=j.running;
  document.getElementById('nt-go1').disabled=j.running;
  document.getElementById('nt-stop').disabled=!j.running;
  var pr=document.getElementById('nt-prog');pr.hidden=!j.running;
  if(j.running){
    var f=j.duration_s?Math.min(1,j.elapsed_s/j.duration_s):0;
    document.getElementById('nt-bar').style.width=(f*100).toFixed(1)+'%';
    ntSet('nt-ptxt','Testing '+ntClock(j.elapsed_s)+(j.duration_s?' of '+ntClock(j.duration_s):''));
  }
  var L=j.last,res=document.getElementById('nt-res');
  res.hidden=!L;
  document.getElementById('nt-rep').disabled=!L;
  if(L){
    var rk=(L.rank===0||L.rank===1||L.rank===2)?L.rank:2;
    ntSet('nt-verdict',NTICON[rk]+' '+(L.verdict||'').charAt(0)+(L.verdict||'').slice(1).toLowerCase());
    ntSet('nt-sum',L.summary||'');
    var d=L.started?new Date(L.started*1000).toLocaleString([],{weekday:'short',hour:'numeric',minute:'2-digit'}):'';
    var len=L.sent>=60?Math.round(L.sent/60*10)/10+' min':(L.sent||0)+' s';
    ntSet('nt-when',[L.site||L.place||'place unknown',d,len+(L.partial?' (stopped early)':'')+(L.udp?'':', ping only')].join(' · '));
    var n=L.numbers||{};
    ntSet('nt-score',ntNum(L.mos,' / 5',1));ntSet('nt-rtt',ntNum(n.rtt_avg,' ms'));ntSet('nt-p95',ntNum(n.rtt_p95,' ms'));
    ntSet('nt-jit',ntNum(n.jitter_ms,' ms',1));ntSet('nt-loss',ntNum(n.loss_pct,' %',1));ntSet('nt-drop',ntNum(n.longest_lost,' s'));
    var ul=document.getElementById('nt-notes');ul.textContent='';
    (L.notes||[]).forEach(function(t){var li=document.createElement('li');li.textContent=t;ul.appendChild(li)});
  }
  if(j.running)show('Network test running...');
  else if(j.error)show('The test did not run: '+j.error);
  else show(L?'Last test is below.':'No test yet. Press Start.');
}
async function ntRefresh(){
  try{
    var r=await api('GET','/tools/nettest/status');
    if(r.status==401){show('This link is not valid any more. Ask Devon for a new one.');return}
    if(r.status==403){show('Not allowed for your link.');return}
    if(!r.ok){show('Status unavailable ('+(r.j.detail||r.status)+')');return}
    ntRender(r.j);
  }catch(e){show('Cannot reach the TV box. Are you on the RV Wi-Fi?')}
}
async function ntStart(sec){
  show('Starting the '+(sec>=600?'10 minute':'1 minute')+' test...');
  try{
    var r=await api('POST','/tools/nettest/start?seconds='+sec);
    if(r.status==409){show('A test is already running.')}
    else if(!r.ok){show('Could not start: '+(r.j.detail||r.status));return}
  }catch(e){show('Cannot reach the TV box. Are you on the RV Wi-Fi?');return}
  ntRefresh();
}
async function ntStop(){
  show('Stopping...');
  try{await api('POST','/tools/nettest/stop')}catch(e){show('Cannot reach the TV box.');return}
  setTimeout(ntRefresh,1500);
}
var SITE={map:null,layer:null,pins:[],sel:-1,cand:null,nameTouched:false,runsFor:'',seq:0};
async function apiJson(method,path,body){
  var r=await fetch(path,{method:method,headers:{Authorization:'Bearer '+T,'Content-Type':'application/json'},body:JSON.stringify(body)});
  var j={};try{j=await r.json()}catch(e){}
  return {ok:r.ok,status:r.status,j:j};
}
function siteLeaflet(){
  return new Promise(function(ok,bad){
    if(window.L){ok();return}
    var c=document.createElement('link');c.rel='stylesheet';c.href='/display/static/vendor/leaflet.css';document.head.appendChild(c);
    var s=document.createElement('script');s.src='/display/static/vendor/leaflet.js';s.onload=ok;s.onerror=bad;document.head.appendChild(s);
  });
}
function siteMapInit(){
  if(SITE.map){SITE.map.invalidateSize();return}
  var m=L.map('site-map',{zoomControl:true,attributionControl:true,tap:true});
  m.attributionControl.setPrefix(false);
  L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',{maxZoom:19,maxNativeZoom:18,attribution:'Esri'}).addTo(m);
  L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}',{maxZoom:19,maxNativeZoom:18}).addTo(m);
  SITE.layer=L.layerGroup().addTo(m);
  m.setView([44.5,-123],6);
  m.on('click',function(e){siteDrop(e.latlng.lat,e.latlng.lng)});
  SITE.map=m;
}
function siteWhen(t){return new Date(t*1000).toLocaleString([],{weekday:'short',hour:'numeric',minute:'2-digit'})}
function siteDist(m){return m<1000?Math.round(m)+' m':(m/1000).toFixed(1)+' km'}
function siteBuildPins(c){
  var pins=[],n=0;
  (c.sites||[]).forEach(function(x){pins.push({kind:'site',id:x.id,lat:x.lat,lon:x.lon,tag:x.name,title:x.name,sub:'Saved site, '+siteDist(x.dist_m)+' from the Mac',name:x.name})});
  (c.parks||[]).forEach(function(x){n++;pins.push({kind:'car',lat:x.lat,lon:x.lon,tag:n+' · '+siteWhen(x.at),title:'Car stop '+n,sub:'Parked '+siteWhen(x.at)+', '+siteDist(x.dist_m)+' from the Mac',mark:String(n)})});
  if(c.mac)pins.push({kind:'mac',lat:c.mac.lat,lon:c.mac.lon,tag:'Mac',dir:'bottom',title:'Where the Mac is',sub:(c.mac.place||'')+(c.mac.accuracy_m!=null?' (±'+c.mac.accuracy_m+' m)':'')+(c.mac.age_s>1800?', position is '+Math.round(c.mac.age_s/3600)+' h old':''),mark:'M'});
  return pins;
}
function siteDraw(){
  SITE.layer.clearLayers();
  SITE.pins.forEach(function(p,i){
    var cls='sp '+p.kind+(i===SITE.sel?' sel':'');
    var ic=L.divIcon({className:'',html:'<div class="'+cls+'"></div>',iconSize:[26,26],iconAnchor:[13,13]});
    var mk=L.marker([p.lat,p.lon],{icon:ic,zIndexOffset:i===SITE.sel?1000:0}).addTo(SITE.layer);
    var el=mk.getElement();
    if(el&&el.firstChild)el.firstChild.textContent=p.mark||(p.kind==='site'?'S':'+');
    var tip=document.createElement('span');tip.textContent=p.tag;
    var dn=p.dir==='bottom';mk.bindTooltip(tip,{permanent:true,direction:dn?'bottom':'top',offset:[0,dn?16:-16],className:'sptip'});
    mk.on('click',function(ev){L.DomEvent.stopPropagation(ev);siteSelect(i)});
  });
}
function siteFit(){
  var sel=SITE.pins[SITE.sel];if(!sel)return;
  var b=SITE.pins.filter(function(p){return SITE.map.distance([p.lat,p.lon],[sel.lat,sel.lon])<=600}).map(function(p){return [p.lat,p.lon]});
  if(b.length<2)SITE.map.setView([sel.lat,sel.lon],17);
  else SITE.map.fitBounds(b,{padding:[60,60],maxZoom:18});
}
function siteRows(){
  var box=document.getElementById('site-pins');box.textContent='';
  SITE.pins.forEach(function(p,i){
    var d=document.createElement('div');d.className='prow'+(i===SITE.sel?' sel':'');d.setAttribute('role','button');d.tabIndex=0;
    d.appendChild(document.createTextNode(p.title));
    var sm=document.createElement('small');sm.textContent=p.sub||'';d.appendChild(sm);
    d.onclick=function(){siteSelect(i)};
    box.appendChild(d);
  });
}
function siteDrop(lat,lon){
  SITE.pins=SITE.pins.filter(function(p){return p.kind!=='drop'});
  SITE.pins.push({kind:'drop',lat:lat,lon:lon,tag:'Dropped pin',title:'Dropped pin',sub:lat.toFixed(5)+', '+lon.toFixed(5),mark:'*'});
  siteSelect(SITE.pins.length-1);
}
function siteSelect(i){
  SITE.sel=i;
  var p=SITE.pins[i];
  var nm=document.getElementById('site-name');
  if(p.kind==='site'){nm.value=p.name;nm.disabled=true;SITE.nameTouched=false}
  else{
    nm.disabled=false;
    if(!SITE.nameTouched){
      var sh=SITE.cand&&SITE.cand.sheet;
      nm.value=(sh&&sh.camp)||(SITE.cand&&SITE.cand.mac&&SITE.cand.mac.place)||'';
    }
  }
  siteDraw();siteRows();siteRuns();
  document.getElementById('site-save').disabled=false;
}
async function siteRuns(){
  var p=SITE.pins[SITE.sel];if(!p)return;
  var key=p.lat+','+p.lon,my=++SITE.seq;
  var box=document.getElementById('site-runs');box.textContent='Checking earlier tests...';
  var r;
  try{r=await api('GET','/tools/site/preview?lat='+encodeURIComponent(p.lat)+'&lon='+encodeURIComponent(p.lon))}
  catch(e){box.textContent='Could not list earlier tests.';return}
  if(my!==SITE.seq)return;
  box.textContent='';
  if(!r.ok){box.textContent='Could not list earlier tests ('+(r.j.detail||r.status)+').';return}
  var runs=(r.j.runs||[]).slice().sort(function(a,b){return b.started-a.started});
  if(!runs.length){box.textContent='No earlier tests since your last Set location. Tests from now on tag themselves here.';return}
  runs.forEach(function(x){
    var row=document.createElement('label');row.className='runrow';
    var cb=document.createElement('input');cb.type='checkbox';cb.checked=!!x.suggest;cb.dataset.id=x.id;row.appendChild(cb);
    var tx=document.createElement('div');
    tx.appendChild(document.createTextNode(siteWhen(x.started)+' · '+(x.place||'place unknown')+(x.short?' (short)':'')));
    var sm=document.createElement('small');sm.textContent=x.reason||'';tx.appendChild(sm);
    row.appendChild(tx);box.appendChild(row);
  });
}
async function siteOpen(){
  view('site');show('Looking up where the Mac and the car are...');
  document.getElementById('site-save').disabled=true;
  document.getElementById('site-runs').textContent='';
  document.getElementById('site-pins').textContent='';
  SITE.pins=[];SITE.sel=-1;SITE.nameTouched=false;
  try{await siteLeaflet()}catch(e){show('The map did not load. Is the Mac online?');return}
  siteMapInit();
  var nm=document.getElementById('site-name');
  nm.oninput=function(){SITE.nameTouched=true};
  var r;
  try{r=await api('GET','/tools/site/candidates')}
  catch(e){show('Cannot reach the TV box. Are you on the RV Wi-Fi?');return}
  if(r.status==401){show('This link is not valid any more. Ask Devon for a new one.');return}
  if(r.status==403){show('Not allowed for your link.');return}
  if(!r.ok){show('Could not look up positions ('+(r.j.detail||r.status)+')');return}
  var c=r.j;SITE.cand=c;
  document.getElementById('site-sum').textContent=c.current?('Tests here are tagged '+c.current.name+'.'):'No saved site here yet.';
  var sub=[];
  if(c.mac&&c.mac.age_s>1800)sub.push('The position from the Mac is '+Math.round(c.mac.age_s/3600)+' h old, so check the pin.');
  if(c.sheet&&(c.sheet.camp||c.sheet.city))sub.push('Sheet for today: '+[c.sheet.camp,c.sheet.city].filter(Boolean).join(', ')+'.');
  document.getElementById('site-sub').textContent=sub.join(' ');
  SITE.pins=siteBuildPins(c);
  if(!SITE.pins.length){
    SITE.map.setView([44.5,-123],6);SITE.map.invalidateSize();siteRows();
    show('No position from the Mac or the car yet. Zoom the map and tap where the dish is.');return;
  }
  var pick=SITE.pins.findIndex(function(p){return c.current&&p.kind==='site'&&p.id===c.current.id});
  if(pick<0)pick=SITE.pins.findIndex(function(p){return p.kind==='car'});
  if(pick<0)pick=SITE.pins.findIndex(function(p){return p.kind==='mac'});
  SITE.map.invalidateSize();
  siteSelect(pick);siteFit();
  show('Choose where the dish is, check the name, and save.');
}
async function siteSave(){
  var p=SITE.pins[SITE.sel];if(!p)return;
  var name=document.getElementById('site-name').value.trim();
  if(!name){show('Type a name for this campground first.');return}
  var ids=[];document.querySelectorAll('#site-runs input[type=checkbox]').forEach(function(c){if(c.checked)ids.push(c.dataset.id)});
  var b=document.getElementById('site-save');b.disabled=true;show('Saving...');
  var r;
  try{r=await apiJson('POST','/tools/site/set',{name:name,lat:p.lat,lon:p.lon,ids:ids,source:p.kind})}
  catch(e){b.disabled=false;show('Cannot reach the TV box. Are you on the RV Wi-Fi?');return}
  if(!r.ok){b.disabled=false;show('Could not save: '+(r.j.detail||r.status));return}
  var n=r.j.retagged;
  view('nettest');
  var msg='Saved '+r.j.site.name+'. '+n+' earlier test'+(n===1?'':'s')+' tagged. Tests from now on here tag themselves.';
  document.getElementById('nt-site').textContent=msg;
  show(msg);
}
async function ntShow(kind){
  view('report');show('Loading...');
  var f=document.getElementById('rep');f.srcdoc='';
  try{
    var r=await fetch('/tools/nettest/'+kind,{headers:{Authorization:'Bearer '+T}});
    var t=await r.text();
    if(!r.ok){var m='';try{m=JSON.parse(t).detail}catch(e){}show(m||('Could not load ('+r.status+')'));return}
    f.srcdoc=t;show(kind==='report'?'Report. Touch the graphs for the numbers.':'Compare places. Tick runs in the table to show them.');
  }catch(e){show('Cannot reach the TV box. Are you on the RV Wi-Fi?')}
}
async function loadCams(){
  var box=document.getElementById('canny-btns');
  try{
    var d=await api('GET','/display/state');
    if(!d.ok||!d.j.cams)return;
    box.innerHTML='';
    d.j.cams.forEach(function(c){
      var b=document.createElement('button');
      b.dataset.act='display';
      b.textContent=c.live?c.label:c.label+' (not connected)';
      if(!c.live)b.style.opacity='.5';
      b.onclick=function(){camGo(c.id,c.label)};
      box.appendChild(b);
    });
    applyPerms();
  }catch(e){show('Cannot reach the TV box. Are you on the RV Wi-Fi?')}
}
function camGo(id,label){
  go('/display/cams?cam='+encodeURIComponent(id),'Opening '+label+' (up to 40 s)...');
}
var CAMSAY={top:'Top',bottom:'Bottom',past:'PAST',new:'NEW'};
async function cam(action){
  show(CAMSAY[action]+'...');
  try{
    var r=await api('POST','/display/batcam/do?action='+action);
    if(r.status==401){show('This link is not valid any more. Ask Devon for a new one.');return}
    if(r.status==403){show('Not allowed for your link.');return}
    if(!r.ok){show(r.j.detail||('Could not do that ('+r.status+')'));return}
    var t=r.j.result||'';
    if(t.indexOf('no-camera')==0){show('The cameras page is not showing. Press Load first.');return}
    show(CAMSAY[action]+(t.indexOf('ok ')==0?'':' ('+t+')'));
  }catch(e){show('Cannot reach the TV box. Are you on the RV Wi-Fi?')}
}
async function home(){
  view('main');
  try{await api('POST','/display/batcam/close')}catch(e){}
  setTimeout(refresh,800);
}
async function setPlace(ev){
  ev.preventDefault();
  var p=document.getElementById('place').value.trim();
  if(!p)return;
  show('Looking up '+p+'...');
  try{
    var r=await api('POST','/display/location?place='+encodeURIComponent(p));
    if(!r.ok){show(r.j.detail||('Could not set the place ('+r.status+')'));return}
    show('Weather place set to '+r.j.label);
    document.getElementById('place').value='';
  }catch(e){show('Cannot reach the TV box.')}
}
async function refresh(){
  try{
    if(!ME){
      var w=await api('GET','/whoami');
      if(w.status==401){show('This link is not valid any more. Ask Devon for a new one.');return}
      if(!w.ok){show('Status unavailable');return}
      ME=w.j;applyPerms();
    }
    var r=await api('GET','/status');
    if(r.status==401){show('This link is not valid any more. Ask Devon for a new one.');return}
    if(!r.ok){show('Status unavailable');return}
    var j=r.j, line;
    if(j.power!='on'){line='TV is off'}
    else{line='TV is on'+(j.volume!=null?' - volume '+j.volume:'')+(j.muted?' (muted)':'')}
    if(can('display')){
      var d=await api('GET','/display/state');
      if(d.ok&&d.j.open)line+=d.j.page==='sheet'?' - sheet open on the Mac':' - dashboard open';
      if(d.ok&&d.j.batcam)line+=' - BatCam open on the Mac';
      if(d.ok&&d.j.map&&!BB.touched){document.getElementById('bbr').value=d.j.map.miles;bbShow()}
    }
    if(CUR!=='nettest'&&CUR!=='report')show(line);
  }catch(e){show('Cannot reach the TV box. Are you on the RV Wi-Fi?')}
}
bbInit();refresh();setInterval(refresh,15000);
</script></body></html>"""


@app.get("/guest", response_class=HTMLResponse)
async def guest_page():
    # The page holds no secrets; the token comes from the URL fragment (#token),
    # which browsers never send to the server or put in logs.
    return GUEST_PAGE


async def pair():
    print(f"Pairing with {TV_IP}. Watch the TV and accept the prompt with the remote.")
    ws = await tv_connect(connect_timeout=10, register_timeout=120)
    await ws.close()
    print(f"Paired. Key saved to {KEYFILE}")


def base_url():
    return os.environ.get("TV_BASE_URL", f"http://{BIND}:{SERVER_PORT}").rstrip("/")


def token_cmd(args):
    cmd = args[0] if args else "list"
    data = load_tokens()
    if cmd == "add" and len(args) >= 2:
        name = args[1]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name):
            sys.exit("name: letters, digits, - and _ only")
        if name in data:
            sys.exit(f"{name} already exists (revoke it first to rotate)")
        rec = {"token": secrets.token_urlsafe(24)}
        acts = [a for a in GUEST_ACTIONS if not ("--no-off" in args and a == "power_off")]
        if "--display" in args:
            acts.append("display")      # may open and close the dashboard on the Mac
        if "--stops" in args:
            acts = ["stops"]            # the phone's parking automation: nothing else
        if acts != GUEST_ACTIONS:
            rec["actions"] = acts
        data[name] = rec
        save_tokens(data)
        if "--stops" in args:
            print(f"Added {name} (may only report parking). Token for the Shortcut:\n  {rec['token']}")
        else:
            print(f"Added {name}. Send them this link:\n  {base_url()}/guest#{rec['token']}")
    elif cmd == "revoke" and len(args) >= 2:
        if data.pop(args[1], None) is None:
            sys.exit("no such guest")
        save_tokens(data)
        print(f"Revoked {args[1]}.")
    elif cmd == "list":
        for n, r in data.items():
            acts = r.get("actions")
            print(f"{n}: {'custom ' + ','.join(acts) if acts else 'default guest'}")
        if not data:
            print("(no guests)")
    elif cmd == "link" and len(args) >= 2 and args[1] in data:
        print(f"{base_url()}/guest#{data[args[1]]['token']}")
    else:
        sys.exit("usage: tvserver.py token add NAME [--no-off] [--display] [--stops] | revoke NAME | list | link NAME")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "pair":
        asyncio.run(pair())
    elif len(sys.argv) > 1 and sys.argv[1] == "token":
        token_cmd(sys.argv[2:])
    else:
        if not TV_TOKEN:
            sys.exit("TV_TOKEN is not set (put it in tv.env). Refusing to start without auth.")
        import uvicorn
        uvicorn.run(app, host=BIND, port=SERVER_PORT, log_level="info")
