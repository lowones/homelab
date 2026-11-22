#!/usr/bin/env python3
"""Checks for the lights (lights.py, /lights, /tools/lights, the phone buttons and the BatBoard strip) and for
Check/Debug > Check cameras (/tools/cams), against stand-in relays and a stand-in go2rtc. No real Shelly, no real TV:
    python3 tests/test_lights.py   (needs the packages tvserver.py needs; node is optional)"""
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
TMP = Path(tempfile.mkdtemp(prefix="rvtv-lights-"))
os.environ.update(TV_TOKEN="t", TV_ENV_FILE=str(TMP / "none.env"), TV_TOKENS_FILE=str(TMP / "tokens.json"),
                  TV_LOCATION_FILE=str(TMP / "location.raw"), TV_ITINERARY="", SHELLY_PLUG_HOST="", RV_LIGHTS="")
sys.path.insert(0, str(ROOT))
import lights as lm

fails = []


def check(name, ok, extra=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else "  " + str(extra)))
    if not ok:
        fails.append(name)


class Relay(BaseHTTPRequestHandler):
    on = False
    wall = True
    cfg = {"id": 0, "in_mode": "flip", "initial_state": "restore_last", "auto_on": False, "auto_off": False}
    dev = {"id": "shelly1g4-e83dc1a4c394", "ver": "1.7.0", "auth_en": False}
    whole = {"wifi": {"rssi": -55}}
    requests = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        Relay.requests.append((u.path, q))
        if u.path == "/rpc/Switch.GetStatus":
            body = {"id": 0, "source": "HTTP_in", "output": Relay.on, "temperature": {"tC": 51.1}}
        elif u.path == "/rpc/Switch.Set":
            body = {"was_on": Relay.on}
            Relay.on = q.get("on") == "true"
        elif u.path == "/rpc/Input.GetStatus":
            body = {"id": 0, "state": Relay.wall}
        elif u.path == "/rpc/Switch.GetConfig":
            body = Relay.cfg
        elif u.path == "/rpc/Shelly.GetDeviceInfo":
            body = Relay.dev
        elif u.path == "/rpc/Shelly.GetStatus":
            body = Relay.whole
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


class Go2rtc(BaseHTTPRequestHandler):
    empty = set()                 # cameras that answer 200 with no picture (ONVIF off)

    def log_message(self, *a):
        pass

    def do_GET(self):
        q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        data = b"" if q.get("src", "").removesuffix("_sub") in Go2rtc.empty else b"\xff\xd8jpeg"
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(handler):
    srv = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "127.0.0.1:%d" % srv.server_address[1]


rsrv, HOST = serve(Relay)
gsrv, G2R = serve(Go2rtc)
s = socket.socket()
s.bind(("127.0.0.1", 0))
DEAD = "127.0.0.1:%d" % s.getsockname()[1]
s.close()                                   # a port nothing listens on

# ---- lights.py ---------------------------------------------------------------------------------------------------
L, probs = lm.parse("bedroom=%s, Living_Room = h2 ,bedroom=h3,bad name=h,=h,x=" % HOST)
check("parse: order kept, names lower case, labels capitalised",
      [(l["name"], l["label"]) for l in L] == [("bedroom", "Bedroom"), ("living_room", "Living Room")], L)
check("parse: a repeated name, a bad name and empty parts are reported, not used", len(probs) == 4, probs)
check("parse: nothing set means no lights and no problems", lm.parse("") == ([], []))

Relay.on, Relay.requests = False, []
check("set_on: switches on and reads the state back", lm.set_on(HOST, True) is True and Relay.on)
check("set_on: the Set call is exactly on=true for switch 0",
      [q for p, q in Relay.requests if p == "/rpc/Switch.Set"] == [{"id": "0", "on": "true"}], Relay.requests)
check("set_on: switches off", lm.set_on(HOST, False) is False and not Relay.on)
try:
    lm.is_on(DEAD, timeout=1)
    check("nothing listening: PlugError", False)
except lm.PlugError:
    check("nothing listening: PlugError", True)

Relay.requests = []
i = lm.info(HOST)
check("info: reads state, wall switch, mode, settings, firmware and Wi-Fi",
      i["on"] is False and i["wall"] is True and i["in_mode"] == "flip" and i["initial_state"] == "restore_last"
      and i["firmware"] == "1.7.0" and i["rssi"] == -55 and i["auth"] is False, i)
check("info: flip mode with the wall contact closed and the light off is fine (no warnings)", i["warnings"] == [], i["warnings"])
check("info: changes nothing", all(p != "/rpc/Switch.Set" for p, q in Relay.requests), Relay.requests)
Relay.cfg = dict(Relay.cfg, in_mode="follow", auto_off=True)
Relay.whole = {"wifi": {"rssi": -80}}
w = lm.info(HOST)["warnings"]
check("info: warns about follow mode out of step with the wall, a timer and a weak signal",
      len(w) == 3 and "follow mode" in w[0] and "timer" in w[1] and "-80" in w[2], w)
Relay.cfg = dict(Relay.cfg, in_mode="flip", auto_off=False)
Relay.whole = {"wifi": {"rssi": -55}}
d = lm.describe("Bedroom", i)
check("describe: state, wall switch, Wi-Fi, firmware and password",
      d[0] == "Bedroom: off, 51 C" and "mode flip" in d[1] and "the server" in d[1] and "-55 dBm (good)" in d[2]
      and d[-1].startswith("Password: none"), d)

# ---- the server --------------------------------------------------------------------------------------------------
spec = importlib.util.spec_from_file_location("tvserver", ROOT / "tvserver.py")
tv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tv)
logging.getLogger("uvicorn.error").setLevel(logging.ERROR)
(TMP / "tokens.json").write_text(json.dumps({
    "guest": {"token": "g"},
    "carphone": {"token": "c", "actions": ["stops"]},
    "nooff": {"token": "n", "actions": [a for a in tv.GUEST_ACTIONS if a != "power_off"]}}))


async def tv_off(timeout=2.0):
    return "off", "test"
tv.tv_probe = tv_off

page = tv.GUEST_PAGE
check("home has a Bedroom button above TV apps, on the power_off permission",
      'data-light="bedroom" onclick="lightTap(\'bedroom\')">Bedroom</button>' in page
      and page.index('data-light="bedroom"') < page.index("onclick=\"view('apps')\">TV apps"))
check("Tools has a Lights button that opens the lights menu", "onclick=\"view('lights')\">Lights</button>" in page and 'id="v-lights"' in page)
check("the lights menu un-hides its grid once the buttons are in (applyPerms hides empty grids at load)",
      "g.hidden=!Array.prototype.some.call(g.children" in page[page.index("async function lightsLoad"):page.index("async function lightTap")])
check("Check/Debug has Check lights and Check cameras with their result lines",
      'onclick="lightsCheck()"' in page and 'onclick="camsCheck()"' in page and 'id="tl-lights"' in page and 'id="tl-cams"' in page)
node = shutil.which("node")
if node:
    js = page[page.index("<script>") + 8:page.index("</script>")]
    f = TMP / "page.js"
    f.write_text(js)
    r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
    check("the whole page script still parses (node --check)", r.returncode == 0, r.stderr[:300])
    r = subprocess.run([node, "--check", str(ROOT / "display" / "widgets" / "tv.js")], capture_output=True, text=True)
    if "import" in r.stderr or "export" in r.stderr:
        r = subprocess.run([node, "--input-type=module", "--check"], input=(ROOT / "display" / "widgets" / "tv.js").read_text(),
                           capture_output=True, text=True)
    check("the BatBoard TV widget still parses", r.returncode == 0, r.stderr[:300])
else:
    print("skip script syntax checks (node not installed)")

from fastapi.testclient import TestClient
with TestClient(tv.app) as cl:
    H = lambda tok: {"Authorization": "Bearer " + tok}
    tv.LIGHTS_SPEC = ""
    j = cl.get("/status", headers=H("g")).json()
    check("no RV_LIGHTS: /status has an empty lights list", j.get("lights") == [], j)
    r = cl.get("/tools/lights", headers=H("t"))
    check("no RV_LIGHTS: Check lights says what to add", r.status_code == 400 and "RV_LIGHTS" in r.text, r.text)

    tv.LIGHTS_SPEC = "bedroom=%s,porch=%s" % (HOST, DEAD)
    Relay.on = True
    j = cl.get("/status", headers=H("g")).json()
    check("/status reports each light, a dead relay as null, even with the TV off",
          j["power"] == "off" and j["lights"] == [{"name": "bedroom", "label": "Bedroom", "on": True},
                                                  {"name": "porch", "label": "Porch", "on": None}], j)
    check("GET /lights needs a token", cl.get("/lights").status_code == 401)
    check("a stops-only token may not read the lights", cl.get("/lights", headers=H("c")).status_code == 403)

    Relay.requests = []
    check("a token without power_off may not switch a light, and the relay was not touched",
          cl.post("/lights/bedroom/off", headers=H("n")).status_code == 403 and not Relay.requests)
    r = cl.post("/lights/bedroom/toggle", headers=H("g"))
    check("a default guest toggles the light (like the TV plug): on -> off", r.status_code == 200 and r.json() == {
        "name": "bedroom", "label": "Bedroom", "on": False} and not Relay.on, r.text)
    r = cl.post("/lights/bedroom/on", headers=H("g"))
    check("on sets it on", r.status_code == 200 and r.json()["on"] is True and Relay.on, r.text)
    check("an unknown light: 404", cl.post("/lights/kitchen/on", headers=H("t")).status_code == 404)
    check("a bad state: 422", cl.post("/lights/bedroom/dim", headers=H("t")).status_code == 422)
    r = cl.post("/lights/porch/on", headers=H("t"))
    check("a relay that does not answer: 503 naming the light", r.status_code == 503 and "porch" in r.text, r.text)

    r = cl.get("/tools/lights", headers=H("g"))
    j = r.json()
    check("Check lights: one dead relay makes it unreachable, both are listed",
          r.status_code == 200 and j["result"] == "unreachable" and any(x.startswith("Bedroom: ON") for x in j["lines"])
          and any("Porch: did not answer" in x for x in j["lines"]), j)
    tv.LIGHTS_SPEC = "bedroom=%s" % HOST
    j = cl.get("/tools/lights", headers=H("g")).json()
    check("Check lights: a healthy relay is ok", j["result"] == "ok", j)

    tv.GO2RTC_URL, tv.CAMS_LIVE = "http://" + G2R, ["driveway", "coop"]
    Go2rtc.empty = set()
    j = cl.get("/tools/cams", headers=H("g")).json()
    check("Check cameras: all with video is ok", j["result"] == "ok" and j["bad"] == [], j)
    Go2rtc.empty = {"coop"}
    j = cl.get("/tools/cams", headers=H("g")).json()
    check("Check cameras: an empty picture is named, with the ONVIF hint",
          j["result"] == "warn" and j["bad"] == ["Coop"] and "ONVIF" in j["lines"][-1], j)
    tv.CAMS_LIVE = []
    check("Check cameras: none set up is a 400", cl.get("/tools/cams", headers=H("g")).status_code == 400)

shutil.rmtree(str(TMP), ignore_errors=True)
rsrv.shutdown()
gsrv.shutdown()
print()
print("all passed" if not fails else "%d FAILED: %s" % (len(fails), "; ".join(fails)))
sys.exit(1 if fails else 0)
