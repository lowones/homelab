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
                os.environ.setdefault(k.strip(), v.strip())


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


_itin = {"rows": {}, "next": {}, "error": None}     # per source: parsed rows, when to refetch
_geo = {}                                            # city -> (lat, lon, label)
DATE_RE = re.compile(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*$")


def parse_itinerary(text):
    """CSV text -> sorted [(date, city)]. City is '' on days the sheet leaves blank."""
    rows = []
    for r in csv.reader(io.StringIO(text)):
        m = DATE_RE.match(r[1]) if len(r) > 1 else None
        if m:
            rows.append((datetime.date(int(m[3]), int(m[1]), int(m[2])), r[3].strip() if len(r) > 3 else ""))
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
                _itin["rows"][src] = parse_itinerary(read_source(src))
                _itin["next"][src] = now + 6 * 3600
                _itin["error"] = None
            except Exception as e:
                _itin["error"] = str(e) or type(e).__name__
                _itin["next"][src] = now + 300
                log.warning("itinerary fetch failed: %s", _itin["error"])
    rows = sorted((r for v in _itin["rows"].values() for r in v), key=lambda x: x[0])
    city = None
    for d, c in rows:
        if d > today:
            break
        city = c or city
    if rows and today > rows[-1][0] + datetime.timedelta(days=14):
        return None
    return city


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


async def current_location():
    loc = load_location()
    if ITIN_SOURCES and loc.get("mode", "auto") != "manual":
        try:
            found = await asyncio.get_running_loop().run_in_executor(
                None, itinerary_location, datetime.date.today())
            if found:
                return found
        except Exception as e:
            log.warning("itinerary location failed: %s", e)
    return {**loc, "source": "manual"}


def fetch_weather(loc):
    """Blocking; run in a thread. Returns a compact dict for the weather widget."""
    q = urllib.parse.urlencode({
        "latitude": loc["lat"], "longitude": loc["lon"], "timezone": "auto", "forecast_days": 3,
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
        "daily": "temperature_2m_max,temperature_2m_min,weather_code",
        "temperature_unit": "fahrenheit", "wind_speed_unit": "mph"})
    with urllib.request.urlopen(f"{WEATHER_URL}?{q}", timeout=8) as r:
        j = json.load(r)
    cur, d = j["current"], j["daily"]
    return {"temp": cur["temperature_2m"], "feels": cur["apparent_temperature"],
            "code": cur["weather_code"], "wind": cur["wind_speed_10m"],
            "days": [{"date": d["time"][i], "hi": d["temperature_2m_max"][i],
                      "lo": d["temperature_2m_min"][i], "code": d["weather_code"][i]}
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
            "updated": _weather["at"]}


@app.get("/whoami")
async def whoami(who: Who = Depends(need(None))):
    return {"name": who.name, "admin": who.admin,
            "actions": ALL_ACTIONS if who.admin else sorted(who.actions)}


@app.get("/display", response_class=FileResponse)
async def display_page():
    # Holds no secrets; the page reads its token from the URL fragment.
    return FileResponse(DISPLAY_DIR / "index.html")


@app.get("/display/data")
async def display_data(who: Who = Depends(need("display_data"))):
    return {"now": time.time(), "tv": await status(who), "weather": await get_weather()}


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
        return await _display_open(tv, page)


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
        token, url = None, SHEET_URL                         # an ordinary web page: no token
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
        if not ITIN_SOURCES:
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
</div>
<form data-act="admin" onsubmit="setPlace(event)">
 <input id="place" placeholder="Weather place, e.g. Moab, Utah" autocomplete="off">
 <button>Set</button>
 <button type="button" onclick="go('/display/location?auto=true','Weather now follows the trip schedule...')">Follow schedule</button>
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
