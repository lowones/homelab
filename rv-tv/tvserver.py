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
import csv
import datetime
import io
import json
import logging
import os
import re
import secrets
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path

import websockets
from fastapi import Depends, FastAPI, Header, HTTPException, Path as PathParam, Query
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
ALL_ACTIONS = GUEST_ACTIONS + ["display", "display_data", "admin"]
_display = {"token": None, "proc": None, "browser": None, "page": None}   # the running kiosk, if any


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
    def dep(authorization: str = Header(default="")):
        if not TV_TOKEN:
            raise HTTPException(500, "server has no TV_TOKEN configured")
        who = identify(authorization)
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
    return [exe, "--kiosk", f"--user-data-dir={prof}", "--no-first-run",
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


_itin = {"rows": {}, "next": {}, "stops": {}, "error": None}     # per source: parsed rows, when to refetch
_geo = {}                                            # city -> (lat, lon, label)
DATE_RE = re.compile(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*$")


def parse_itinerary(text):
    """CSV text -> sorted [(date, city, row)]. City is '' on days the sheet leaves blank;
    row is the 1-based sheet row."""
    rows = []
    for n, r in enumerate(csv.reader(io.StringIO(text)), 1):
        m = DATE_RE.match(r[1]) if len(r) > 1 else None
        if m:
            rows.append((datetime.date(int(m[3]), int(m[1]), int(m[2])), r[3].strip() if len(r) > 3 else "", n))
    return sorted(rows, key=lambda x: x[0])


def read_source(src):
    if src.startswith(("http://", "https://")):
        req = urllib.request.Request(src, headers={"User-Agent": "rv-tv"})
        with urllib.request.urlopen(req, timeout=10) as r:
            text = r.read().decode("utf-8", "replace")
        if text.lstrip()[:1] == "<":       # an HTML page: Google wants a sign-in
            raise ValueError("got a web page instead of CSV; share the sheet as 'anyone with the link can view'")
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
                _itin["next"][src] = now + 6 * 3600
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
        "latitude": loc["lat"], "longitude": loc["lon"], "timezone": "auto", "forecast_days": 3,
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
        "daily": "temperature_2m_max,temperature_2m_min,weather_code,sunrise,sunset",
        "temperature_unit": "fahrenheit", "wind_speed_unit": "mph"})
    with urllib.request.urlopen(f"{WEATHER_URL}?{q}", timeout=8) as r:
        j = json.load(r)
    cur, d = j["current"], j["daily"]
    return {"temp": cur["temperature_2m"], "feels": cur["apparent_temperature"],
            "code": cur["weather_code"], "wind": cur["wind_speed_10m"],
            "days": [{"date": d["time"][i], "hi": d["temperature_2m_max"][i],
                      "lo": d["temperature_2m_min"][i], "code": d["weather_code"][i],
                      "rise": (d.get("sunrise") or [None] * len(d["time"]))[i],
                      "set": (d.get("sunset") or [None] * len(d["time"]))[i]}
                     for i in range(len(d["time"]))]}


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
        if len(data) > 300:
            data = dict(list(data.items())[-300:])
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
    return FileResponse(DISPLAY_DIR / "index.html")


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
    out["nav"] = await _soft(get_nav(), "nav")
    out["tides"] = await _soft(get_tides(), "tides")
    out["map"] = get_map()
    return out


@app.get("/display/state")
async def display_state(who: Who = Depends(need(None))):
    return {"open": kiosk_running(), "page": _display["page"], "browser": _display["browser"],
            "location": await current_location(), "itinerary": bool(ITIN_SOURCES),
            "itinerary_error": _itin["error"], "sheet": bool(SHEET_URL)}


@app.post("/display/open")
async def display_open(tv: bool = Query(True), page: str = Query("dashboard", pattern="^(dashboard|sheet)$"),
                       who: Who = Depends(need("display"))):
    """Wake the TV, switch it to the Mac's input, and show the dashboard (or, with
    page=sheet, the configured Google Sheet) fullscreen on the Mac's screen."""
    if page == "sheet" and not SHEET_URL:
        raise HTTPException(404, "no sheet configured: add TV_SHEET_URL=<link> to tv.env and restart")
    async with get_lock("display"):
        out = await _display_open(tv, page)
    if page == "dashboard" and COPY_ON_OPEN and isinstance(out, dict):
        try:                                      # best effort: never let the clipboard break opening
            out["copied"] = (await copy_destination())["copied"]
        except Exception as e:
            out["copied"] = None
            log.info("destination not copied on open: %s", getattr(e, "detail", e))
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
    return {"display": "closed", "was_open": was}


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


@app.post("/display/copy-address")
async def copy_address(who: Who = Depends(need("display"))):
    """Copies the destination address to the Mac's clipboard (see copy_destination)."""
    res = await copy_destination()
    log.info("%s: copied destination %r", who.name, res["copied"])
    return res


if DISPLAY_DIR.is_dir():
    app.mount("/display/static", StaticFiles(directory=str(DISPLAY_DIR)), name="display-static")


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
</style></head><body>
<h1>RV TV</h1>
<div id="st">Checking TV...</div>
<div class="grid">
 <button class="on" data-act="power_on" onclick="go('/power/on?wait=30','Waking the TV (up to 30 s)...')">Power on</button>
 <button data-act="power_off" onclick="go('/power/off','Turning off...')">Power off</button>
</div>
<div class="grid">
 <button data-act="app" onclick="go('/app/netflix','Opening Netflix...')">Netflix</button>
 <button data-act="app" onclick="go('/app/amazon','Opening Prime Video...')">Prime Video</button>
 <button data-act="app" onclick="go('/app/youtube.leanback.v4','Opening YouTube...')">YouTube</button>
 <button data-act="input" onclick="go('/input/HDMI_1','Switching to the Mac...')">Mac screen</button>
</div>
<div class="grid">
 <button data-act="volume" onclick="go('/volume/down','')">Vol &minus;</button>
 <button data-act="volume" onclick="go('/volume/up','')">Vol +</button>
 <button data-act="mute" onclick="go('/mute/on','')">Mute</button>
 <button data-act="mute" onclick="go('/mute/off','')">Unmute</button>
</div>
<div class="grid">
 <button class="on" data-act="display" onclick="go('/display/open','Waking the TV and opening the dashboard (up to 40 s)...')">Show dashboard</button>
 <button data-act="display" onclick="go('/display/open?page=sheet','Opening the sheet on the Mac (up to 40 s)...')">Open sheet</button>
 <button data-act="display" onclick="go('/display/close','Closing the screen...')">Close screen</button>
 <button data-act="display" onclick="copyDest()">Copy destination</button>
</div>
<form data-act="admin" onsubmit="setPlace(event)">
 <input id="place" placeholder="Weather place, e.g. Moab, Utah" autocomplete="off">
 <button>Set</button>
 <button type="button" onclick="go('/display/location?auto=true','Weather now follows the Mac location, then the trip schedule...')">Follow location</button>
</form>
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
async function copyDest(){
  show('Copying the destination on the Mac...');
  try{
    var r=await api('POST','/display/copy-address');
    if(!r.ok){show(r.j.detail||('Could not copy ('+r.status+')'));return}
    show('Copied on the Mac: '+r.j.copied+'. Paste it here in a few seconds.');
  }catch(e){show('Cannot reach the TV box. Are you on the RV Wi-Fi?')}
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
    }
    show(line);
  }catch(e){show('Cannot reach the TV box. Are you on the RV Wi-Fi?')}
}
refresh();setInterval(refresh,15000);
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
        if acts != GUEST_ACTIONS:
            rec["actions"] = acts
        data[name] = rec
        save_tokens(data)
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
        sys.exit("usage: tvserver.py token add NAME [--no-off] [--display] | revoke NAME | list | link NAME")


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
