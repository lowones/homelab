#!/usr/bin/env python3
"""RV TV control service for an LG webOS TV.

Run:   python tvserver.py          start the API
       python tvserver.py pair     one-time pairing (accept the prompt on the TV)

Config comes from tv.env next to this file (KEY=VALUE lines) or the environment:
  TV_IP, TV_MAC, TV_TOKEN (required), TV_BIND, TV_SERVER_PORT, TV_WS_PORT, TV_KEYFILE
Every endpoint except /health needs:  Authorization: Bearer <TV_TOKEN>
"""
import asyncio
import json
import logging
import os
import re
import secrets
import ssl
import sys
import time
from pathlib import Path

import websockets
from fastapi import Depends, FastAPI, Header, HTTPException, Path as PathParam, Query
from fastapi.responses import HTMLResponse, JSONResponse
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

# Keep this list identical to the one used when the TV was paired. Permissions are
# fixed at pairing time, so changing it means pairing again.
PERMS = ["LAUNCH", "CONTROL_POWER", "CONTROL_INPUT_TV", "CONTROL_AUDIO",
         "READ_INSTALLED_APPS", "READ_INPUT_DEVICE_LIST", "READ_RUNNING_APPS"]


class TVUnreachable(Exception):
    pass


class TVError(Exception):
    pass


LOCK = asyncio.Lock()


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
    async with LOCK:
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
    for name, rec in load_tokens().items():
        if secrets.compare_digest(tb, str(rec.get("token", "")).encode()):
            return Who(name, False, set(rec.get("actions", GUEST_ACTIONS)))
    raise HTTPException(401, "bad or missing token")


def need(action):
    def dep(authorization: str = Header(default="")):
        if not TV_TOKEN:
            raise HTTPException(500, "server has no TV_TOKEN configured")
        who = identify(authorization)
        if not who.admin and action not in who.actions:
            log.warning("denied: %s tried %s", who.name, action)
            raise HTTPException(403, f"not allowed: {action}")
        log.info("%s: %s", who.name, action)
        return who
    return dep


app = FastAPI(title="RV TV control", docs_url=None, redoc_url=None, openapi_url=None)


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


@app.post("/power/on")
async def power_on(wait: int = Query(30, ge=0, le=120), who: Who = Depends(need("power_on"))):
    if await tv_is_up():
        return {"power": "on", "note": "already on"}
    waited = 0
    while True:
        try:
            send_magic_packet(TV_MAC)
        except OSError as e:
            raise TVError(f"could not send wake packet: {e}")
        if waited >= wait:
            break
        for _ in range(3):
            await asyncio.sleep(1)
            waited += 1
        if await tv_is_up():
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
</style></head><body>
<h1>RV TV</h1>
<div id="st">Checking TV...</div>
<div class="grid">
 <button class="on" onclick="go('/power/on?wait=30','Waking the TV (up to 30 s)...')">Power on</button>
 <button onclick="go('/power/off','Turning off...')">Power off</button>
</div>
<div class="grid">
 <button onclick="go('/app/netflix','Opening Netflix...')">Netflix</button>
 <button onclick="go('/app/amazon','Opening Prime Video...')">Prime Video</button>
 <button onclick="go('/app/youtube.leanback.v4','Opening YouTube...')">YouTube</button>
 <button onclick="go('/input/HDMI_1','Switching to the Mac...')">Mac screen</button>
</div>
<div class="grid">
 <button onclick="go('/volume/down','')">Vol &minus;</button>
 <button onclick="go('/volume/up','')">Vol +</button>
 <button onclick="go('/mute/on','')">Mute</button>
 <button onclick="go('/mute/off','')">Unmute</button>
</div>
<script>
var T=location.hash.slice(1);
try{if(T){localStorage.setItem('tvtok',T)}else{T=localStorage.getItem('tvtok')||''}}catch(e){}
var st=document.getElementById('st');
function show(t){st.textContent=t}
async function api(method,path){
  var r=await fetch(path,{method:method,headers:{Authorization:'Bearer '+T}});
  var j={};try{j=await r.json()}catch(e){}
  return {ok:r.ok,status:r.status,j:j};
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
async function refresh(){
  try{
    var r=await api('GET','/status');
    if(r.status==401){show('This link is not valid any more. Ask Devon for a new one.');return}
    if(!r.ok){show('Status unavailable');return}
    var j=r.j;
    if(j.power!='on'){show('TV is off');return}
    show('TV is on'+(j.volume!=null?' - volume '+j.volume:'')+(j.muted?' (muted)':''));
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
        if "--no-off" in args:
            rec["actions"] = [a for a in GUEST_ACTIONS if a != "power_off"]
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
        sys.exit("usage: tvserver.py token add NAME [--no-off] | revoke NAME | list | link NAME")


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
