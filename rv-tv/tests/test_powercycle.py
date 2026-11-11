#!/usr/bin/env python3
"""Checks for Tools > Power cycle TV (powercycle.py and POST /tools/powercycle): the plug requests on a stand-in plug,
the endpoint (permissions, cooldown, plug unreachable, TV not back, input switch, log) and the phone button.
No real plug, no real TV:   python3 tests/test_powercycle.py   (needs the packages tvserver.py needs; node is optional)"""
import importlib.util
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="rvtv-pc-"))
os.environ.update(TV_TOKEN="t", TV_ENV_FILE=str(TMP / "none.env"), TV_TOKENS_FILE=str(TMP / "tokens.json"),
                  TV_LOCATION_FILE=str(TMP / "location.raw"), TV_ITINERARY="", SHELLY_PLUG_HOST="")
sys.path.insert(0, str(ROOT))
import powercycle as pcm

fails = []


def check(name, ok, extra=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else "  " + str(extra)))
    if not ok:
        fails.append(name)


class Plug(BaseHTTPRequestHandler):
    on = True
    notshelly = False
    watts = 90.6
    cfg = {"id": 0, "initial_state": "on", "auto_on": False, "auto_off": False}
    dev = {"id": "shellyplugusg4-98c37700cfd8", "mac": "98C37700CFD8", "ver": "1.7.99-plugusg4prod1", "auth_en": False}
    whole = {"wifi": {"sta_ip": "192.168.1.35", "status": "got ip", "rssi": -52}}
    requests = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        Plug.requests.append((u.path, q))
        if u.path == "/rpc/Switch.GetStatus":
            body = {"hello": 1} if Plug.notshelly else {"id": 0, "source": "SHC", "output": Plug.on, "apower": Plug.watts if Plug.on else 0.0,
                                                        "voltage": 121.5, "temperature": {"tC": 43.4, "tF": 110.1}}
        elif u.path == "/rpc/Switch.GetConfig":
            body = Plug.cfg
        elif u.path == "/rpc/Shelly.GetDeviceInfo":
            body = Plug.dev
        elif u.path == "/rpc/Shelly.GetStatus":
            body = Plug.whole
        elif u.path == "/rpc/Switch.Set":
            body = {"was_on": Plug.on}
            if q.get("on") == "true":
                Plug.on = True
            elif q.get("on") == "false" and "toggle_after" not in q:
                Plug.on = False
        else:
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


srv = HTTPServer(("127.0.0.1", 0), Plug)
threading.Thread(target=srv.serve_forever, daemon=True).start()
HOST = "127.0.0.1:%d" % srv.server_address[1]
s = socket.socket()
s.bind(("127.0.0.1", 0))
DEAD = "127.0.0.1:%d" % s.getsockname()[1]
s.close()                                   # a port nothing listens on

# ---- powercycle.py against the stand-in plug ----------------------------------------------------------------------
Plug.on, Plug.requests = True, []
r = pcm.cycle(HOST, 10)
sets = [q for p, q in Plug.requests if p == "/rpc/Switch.Set"]
check("plug on: one Switch.Set, off now and back on by the plug after 10 s",
      r == {"was_on": True, "action": "cycled"} and sets == [{"id": "0", "on": "false", "toggle_after": "10"}], (r, sets))
Plug.on, Plug.requests = False, []
r = pcm.cycle(HOST, 10)
sets = [q for p, q in Plug.requests if p == "/rpc/Switch.Set"]
check("plug already off: it is turned ON (no toggle_after, which would do nothing and then switch it on)",
      r == {"was_on": False, "action": "turned_on"} and sets == [{"id": "0", "on": "true"}] and Plug.on, (r, sets))
for bad in ("", "a b", "x/../y", "1.2.3.4/rpc", "evil.com?x=", "-start", "a:b"):
    try:
        pcm.status(bad)
        check("bad host refused: %r" % bad, False)
    except pcm.PlugError:
        check("bad host refused: %r" % bad, True)
try:
    pcm.status(DEAD, timeout=1)
    check("nothing listening: PlugError", False)
except pcm.PlugError as e:
    check("nothing listening: PlugError, no change attempted", True)
Plug.requests, Plug.notshelly = [], True
try:
    pcm.cycle(HOST, 10)
    check("an address that is not a Shelly switch is refused before any Switch.Set", False)
except pcm.PlugError:
    check("an address that is not a Shelly switch is refused before any Switch.Set", not [1 for p, q in Plug.requests if p == "/rpc/Switch.Set"], Plug.requests)
Plug.notshelly = False
try:
    pcm._get(HOST, "/missing")
    check("an HTTP error becomes a PlugError", False)
except pcm.PlugError as e:
    check("an HTTP error becomes a PlugError", "404" in str(e), e)
lg = TMP / "powercycle.log"
pcm.log_cycle(lg, "devon", "ok", 47)
pcm.log_cycle(lg, "devon", "plug_unreachable", None)
lines = lg.read_text().splitlines()
check("the cycle is logged without secrets", "by devon result ok tv_back_after 47s" in lines[0]
      and "result plug_unreachable tv_back_after -" in lines[1], lines)

# ---- Check TV plug: info(), describe(), last_cycle() --------------------------------------------------------------
Plug.on, Plug.watts, Plug.requests = True, 90.6, []
i = pcm.info(HOST)
check("info: reads status, config, device info and Wi-Fi", i["on"] is True and i["watts"] == 90.6 and i["volts"] == 121.5 and i["temp_c"] == 43.4
      and i["initial_state"] == "on" and i["auto_off"] is False and i["auth"] is False and i["firmware"].startswith("1.7.99") and i["rssi"] == -52, i)
check("info: a healthy plug has no warnings", i["warnings"] == [], i["warnings"])
check("info: changes nothing (only reads)", all(p != "/rpc/Switch.Set" for p, q in Plug.requests), Plug.requests)
Plug.cfg = dict(Plug.cfg, initial_state="off", auto_off=True)
Plug.whole = {"wifi": {"rssi": -80}}
i2 = pcm.info(HOST)
check("info: warns about initial_state, auto-off and a weak signal", len(i2["warnings"]) == 3 and any("initial_state is off" in w for w in i2["warnings"])
      and any("auto-off" in w for w in i2["warnings"]) and any("-80" in w for w in i2["warnings"]), i2["warnings"])
Plug.cfg = dict(Plug.cfg, initial_state="on", auto_off=False)
Plug.whole = {"wifi": {"rssi": -52}}
Plug.on = False
i3 = pcm.info(HOST)
check("info: an off plug says the TV has no power", i3["on"] is False and any("no power" in w for w in i3["warnings"]), i3["warnings"])
Plug.on, Plug.watts = True, 0.4
i4 = pcm.info(HOST)
check("info: 0 W with the plug on says the TV is off or elsewhere", any("under 2 W" in w for w in i4["warnings"]), i4["warnings"])
Plug.watts = 90.6
Plug.dev = {}
Plug.whole = {}
i5 = pcm.info(HOST)
check("info: the best-effort parts may be missing without failing", i5["on"] is True and i5["firmware"] is None and i5["rssi"] is None and i5["auth"] is False, i5)
Plug.dev = {"id": "x", "mac": "98C37700CFD8", "ver": "1.7.99-plugusg4prod1", "auth_en": True}
Plug.whole = {"wifi": {"rssi": -52}}
try:
    pcm.info(DEAD, timeout=1)
    check("info: an unreachable plug raises PlugError", False)
except pcm.PlugError:
    check("info: an unreachable plug raises PlugError", True)
lg2 = TMP / "pc2.log"
check("last_cycle: no file gives None", pcm.last_cycle(lg2) is None)
pcm.log_cycle(lg2, "devon", "ok", 37)
check("last_cycle: a good cycle reads as a sentence", pcm.last_cycle(lg2).endswith("by devon: ok, TV back after 37s"), pcm.last_cycle(lg2))
pcm.log_cycle(lg2, "devon", "tv_not_back", None)
check("last_cycle: the newest line wins and 'not back' has no time", pcm.last_cycle(lg2).endswith("by devon: tv not back"), pcm.last_cycle(lg2))
d = pcm.describe(i, "2026-11-11 13:46:01 by devon: ok, TV back after 37s", 0)
check("describe: shows state, watts, Wi-Fi, firmware, settings, password, last cycle, ready",
      d[0].startswith("Plug: on, 90.6 W, 122 V, 43 C") and any("-52 dBm (good)" in x for x in d) and any("switches itself on" in x for x in d)
      and any("Password: none" in x for x in d) and any("TV back after 37s" in x for x in d) and d[-1] == "Power cycle ready.", d)
d = pcm.describe(i2, None, 140)
check("describe: warnings are listed last, cooldown and 'none logged' shown",
      d[-1].startswith("\u25b2") and any("none logged" in x for x in d) and any("Next power cycle allowed in 140 s." == x for x in d), d)

# ---- the endpoint -------------------------------------------------------------------------------------------------
spec = importlib.util.spec_from_file_location("tvserver", ROOT / "tvserver.py")
tv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tv)
logging.getLogger("uvicorn.error").setLevel(logging.ERROR)
tv.RUN_DIR = TMP
tv.PC_SETTLE = 0
(TMP / "tokens.json").write_text(json.dumps({
    "guest": {"token": "g"},
    "carphone": {"token": "c", "actions": ["stops"]},
    "nooff": {"token": "n", "actions": [a for a in tv.GUEST_ACTIONS if a != "power_off"]}}))

page = tv.GUEST_PAGE
check("the phone page has the button, gated on the power_off permission, and the result line",
      'id="pc-go"' in page and 'data-act="power_off"' in page and 'onclick="pcTap()"' in page and 'id="tl-pc"' in page)
check("the page script calls the new endpoint and arms with a first tap", "api('POST','/tools/powercycle')" in page and "function pcTap()" in page)
check("the phone page has the Check TV plug button on the status permission and its own result line",
      'id="pg-go"' in page and 'onclick="plugCheck()"' in page and 'id="tl-plug"' in page and "api('GET','/tools/plug')" in page)
node = shutil.which("node")
if node:
    js = page[page.index("<script>") + 8:page.index("</script>")]
    f = TMP / "page.js"
    f.write_text(js)
    r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
    check("the whole page script still parses (node --check)", r.returncode == 0, r.stderr[:300])
else:
    print("skip page script syntax check (node not installed)")

calls = {"wake": [], "call": [], "fixip": 0}
state = {"wake": [True], "call_fail": 0}


async def fake_wake(wait, check_first=True):
    calls["wake"].append((wait, check_first))
    return state["wake"].pop(0)


async def fake_call(uri, payload=None, expect_close=False):
    calls["call"].append((uri, payload))
    if state["call_fail"]:
        state["call_fail"] -= 1
        raise tv.TVError("not ready")
    return {}


async def fake_fixip(apply=True, who=None):
    calls["fixip"] += 1
    return {"result": "not_found", "message": "The TV is not on the network."}


tv.wake_tv, tv.call, tv.fixip_run = fake_wake, fake_call, fake_fixip




def reset(wake=(True,)):
    tv._pc["last"] = 0.0
    calls.update(wake=[], call=[], fixip=0)
    state.update(wake=list(wake), call_fail=0)
    Plug.on, Plug.requests = True, []


from fastapi.testclient import TestClient
with TestClient(tv.app) as cl:
    H = lambda tok: {"Authorization": "Bearer " + tok}
    reset()
    check("no token: 401", cl.post("/tools/powercycle").status_code == 401)
    check("a stops-only token (the car phone): 403", cl.post("/tools/powercycle", headers=H("c")).status_code == 403)
    check("a token without power_off: 403 and the plug was not touched",
          cl.post("/tools/powercycle", headers=H("n")).status_code == 403 and not Plug.requests)
    tv.PLUG_HOST = ""
    r = cl.post("/tools/powercycle", headers=H("t"))
    check("no SHELLY_PLUG_HOST: 400 that says what to add", r.status_code == 400 and "SHELLY_PLUG_HOST" in r.text, (r.status_code, r.text))

    tv.PLUG_HOST = HOST
    r = cl.post("/tools/powercycle", headers=H("g"))
    j = r.json()
    check("a default guest token may cycle: 200, ok, plug was on", r.status_code == 200 and j["result"] == "ok" and j["plug_was_on"] is True, (r.status_code, r.text))
    check("the plug was told to cut and restore itself", [q for p, q in Plug.requests if p == "/rpc/Switch.Set"] == [{"id": "0", "on": "false", "toggle_after": "10"}], Plug.requests)
    check("wake packets wait up to 120 s and do not check first (the TV is going down)", calls["wake"] == [(120, False)], calls["wake"])
    check("then the Mac input is selected", calls["call"] == [("ssap://tv/switchInput", {"inputId": tv.MAC_INPUT})], calls["call"])
    check("Check TV address was not needed", calls["fixip"] == 0)
    check("the cycle is logged with who asked", "by guest result ok" in (TMP / "powercycle.log").read_text().splitlines()[-1])

    r = cl.post("/tools/powercycle", headers=H("t"))
    check("a second tap within 5 minutes: 429 with the wait, plug untouched", r.status_code == 429 and "Wait" in r.text and len([1 for p, q in Plug.requests if p == "/rpc/Switch.Set"]) == 1, (r.status_code, r.text))

    reset(wake=(False, True))
    r = cl.post("/tools/powercycle", headers=H("t"))
    j = r.json()
    check("TV slow: runs Check TV address once, waits again (45 s), and is ok when it answers",
          j["result"] == "ok" and calls["fixip"] == 1 and calls["wake"] == [(120, False), (45, False)], (j, calls))

    reset(wake=(False, False))
    r = cl.post("/tools/powercycle", headers=H("t"))
    j = r.json()
    check("TV never back: 200, tv_not_back, says to look at the TV, no input switch",
          r.status_code == 200 and j["result"] == "tv_not_back" and "Look at the TV" in j["message"] and not calls["call"], (r.status_code, j))
    check("... and it is logged as not back", "result tv_not_back tv_back_after -" in (TMP / "powercycle.log").read_text().splitlines()[-1])

    reset()
    Plug.on = False
    r = cl.post("/tools/powercycle", headers=H("t"))
    j = r.json()
    check("plug was off: it is turned on, the TV is woken, and the message says so",
          j["result"] == "ok" and j["plug_was_on"] is False and "was off" in j["message"]
          and [q for p, q in Plug.requests if p == "/rpc/Switch.Set"] == [{"id": "0", "on": "true"}], (j, Plug.requests))

    reset()
    state["call_fail"] = 1
    j = cl.post("/tools/powercycle", headers=H("t")).json()
    check("the input switch is tried twice: one failure is not reported", j["result"] == "ok" and "Could not switch" not in j["message"] and len(calls["call"]) == 2, (j, calls))
    reset()
    state["call_fail"] = 2
    j = cl.post("/tools/powercycle", headers=H("t")).json()
    check("two input failures are reported, the TV is still ok", j["result"] == "ok" and "Could not switch" in j["message"], j)

    reset()
    tv.PLUG_HOST = DEAD
    r = cl.post("/tools/powercycle", headers=H("t"))
    j = r.json()
    check("plug unreachable: 200, plug_unreachable, nothing changed, no waiting",
          r.status_code == 200 and j["result"] == "plug_unreachable" and "Nothing was changed" in j["message"] and not calls["wake"], (r.status_code, j))
    check("... and the cooldown is NOT started (the power was never cut)", tv._pc["last"] == 0.0)

    # Check TV plug (read-only)
    reset()
    tv.PLUG_HOST = ""
    r = cl.get("/tools/plug", headers=H("t"))
    check("plug check, no SHELLY_PLUG_HOST: 400", r.status_code == 400 and "SHELLY_PLUG_HOST" in r.text, (r.status_code, r.text))
    tv.PLUG_HOST = HOST
    check("plug check, no token: 401", cl.get("/tools/plug").status_code == 401)
    check("plug check, car-phone token: 403", cl.get("/tools/plug", headers=H("c")).status_code == 403)
    Plug.cfg = {"id": 0, "initial_state": "on", "auto_on": False, "auto_off": False}
    Plug.whole = {"wifi": {"rssi": -52}}
    Plug.requests = []
    pcm.log_cycle(TMP / "powercycle.log", "guest", "ok", 37)
    r = cl.get("/tools/plug", headers=H("g"))
    j = r.json()
    check("plug check, default guest: 200, ok, reachable, with the lines for the phone",
          r.status_code == 200 and j["result"] == "ok" and j["reachable"] is True and j["watts"] == 90.6 and j["lines"][0].startswith("Plug: on, 90.6 W"), (r.status_code, r.text))
    check("plug check changes nothing: no Switch.Set, cooldown not started", all(p != "/rpc/Switch.Set" for p, q in Plug.requests) and tv._pc["last"] == 0.0)
    check("plug check shows the last power cycle from the log", j["last_cycle"] and "by guest" in j["last_cycle"], j["last_cycle"])
    Plug.cfg = dict(Plug.cfg, initial_state="restore_last")
    j = cl.get("/tools/plug", headers=H("t")).json()
    check("plug check with a risky setting: result warn and the warning is in the lines",
          j["result"] == "warn" and any("initial_state is restore_last" in x for x in j["lines"]), j)
    Plug.cfg = dict(Plug.cfg, initial_state="on")
    tv._pc["last"] = tv.time.time() - 100
    j = cl.get("/tools/plug", headers=H("t")).json()
    check("plug check shows the cooldown still running", 190 <= j["cooldown_left"] <= 201 and any("Next power cycle allowed in" in x for x in j["lines"]), j)
    tv._pc["last"] = 0.0
    tv.PLUG_HOST = DEAD
    r = cl.get("/tools/plug", headers=H("t"))
    j = r.json()
    check("plug check, plug unreachable: 200, plug_unreachable, says to look at the TV",
          r.status_code == 200 and j["result"] == "plug_unreachable" and j["reachable"] is False and "look at it" in j["warnings"][0], (r.status_code, j))

    reset()
    tv.PLUG_HOST = HOST
    slow = threading.Event()
    gate = threading.Event()
    first = {}

    async def gated_wake(wait, check_first=True):
        slow.set()
        while not gate.is_set():
            await tv.asyncio.sleep(0.02)
        return True

    tv.wake_tv = gated_wake
    th = threading.Thread(target=lambda: first.update(r=cl.post("/tools/powercycle", headers=H("t"))))
    th.start()
    entered = slow.wait(10)
    r2 = cl.post("/tools/powercycle", headers=H("t"))
    gate.set()
    th.join(15)
    check("a second request while one is running: 409", entered and r2.status_code == 409, (entered, r2.status_code))
    check("... and the first one still finishes normally", first.get("r") is not None and first["r"].status_code == 200 and first["r"].json()["result"] == "ok", first)

shutil.rmtree(str(TMP), ignore_errors=True)
srv.shutdown()
print()
print("all passed" if not fails else "%d FAILED: %s" % (len(fails), "; ".join(fails)))
sys.exit(1 if fails else 0)
