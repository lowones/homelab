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
    requests = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        Plug.requests.append((u.path, q))
        if u.path == "/rpc/Switch.GetStatus":
            body = {"hello": 1} if Plug.notshelly else {"id": 0, "source": "SHC", "output": Plug.on, "apower": 0.0}
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
