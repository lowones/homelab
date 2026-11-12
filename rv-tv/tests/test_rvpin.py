#!/usr/bin/env python3
"""Checks for BatBelt's "RV is here" pin: it beats the Mac's Wi-Fi fix (even a steady wrong one, and after the 6-hour
age limit), lets go on real move evidence (today's sheet city elsewhere, a phone park) or after TV_LOC_PIN_DAYS, and the
--location token and /location/rv endpoints.
Plain python, no network:   python3 tests/test_rvpin.py   (needs the same packages as tvserver.py)"""
import asyncio
import importlib.util
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="rvtv-rvpin-"))
os.environ.update(TV_TOKEN="t", TV_ENV_FILE=str(TMP / "none.env"), TV_LOCATION_FILE=str(TMP / "location.raw"),
                  TV_TOKENS_FILE=str(TMP / "tokens.json"), TV_ITINERARY="", TV_NETTEST_DIR=str(TMP / "nettest"),
                  TV_SITES_FILE=str(TMP / "sites.json"), TV_DISPLAY_FILE=str(TMP / "display.json"))
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("tvserver", ROOT / "tvserver.py")
tv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tv)
tv.RUN_DIR = TMP
tv.STOPS_FILE = TMP / "stops.json"
tv.LOCATION_HISTORY_FILE = TMP / "location_history.log"
tv.DEVICE_MAX_AGE = 1e12                     # made-up times: the 30-minute staleness rule must not fire
logging.getLogger("uvicorn.error").setLevel(logging.ERROR)

fails = []


def check(name, ok, extra=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else "  " + str(extra)))
    if not ok:
        fails.append(name)


LOC = Path(tv.LOCATION_FILE)
NOW = time.time()
H = 3600
SB = (44.547325, -124.070189)                # South Beach, OR: where the RV really is
BEND = (44.083446, -121.256594)              # the wrong place Apple's database keeps returning
NEWPORT = (44.6368, -124.0535)               # 10 km from South Beach: ordinary Wi-Fi jitter
CLOV = (45.2296, -123.9684)                  # Cloverdale, 76 km north: the next stop


def reset():
    tv._fixstate.update(stamp=None, trusted=None, held=[], loaded=False, outcome=None, raw=None, pin=None)
    tv._itin_seen.update(date=None, loc=None)
    for f in (tv._trusted_file(), tv._rejected_file(), tv._pin_file(), LOC, tv.STOPS_FILE):
        f.unlink(missing_ok=True)


def put(t, pos):
    LOC.write_text("%.6f|%.6f|40||\n" % pos)
    os.utime(LOC, (t, t))


def P(pos, at):
    return {"lat": pos[0], "lon": pos[1], "at": at}


# ---- judge_fix with a pin -------------------------------------------------------------------------------------------
pin = P(SB, NOW)
trusted = P(SB, NOW)
sheet_sb = {"lat": SB[0], "lon": SB[1]}
sheet_cl = {"lat": CLOV[0], "lon": CLOV[1]}
check("near the pin: believed", tv.judge_fix(P(NEWPORT, NOW + 600), trusted, [], None, [], pin)[0] == "accept")
check("far, nothing backing it: held", tv.judge_fix(P(BEND, NOW + 600), trusted, [], None, [], pin)[0] == "hold")
held = [(NOW + k * 600, BEND[0], BEND[1]) for k in range(1, 30)]
check("far and steady for 5 hours: still held (a steady run does not beat the pin)",
      tv.judge_fix(P(BEND, NOW + 5 * H), trusted, held, None, [], pin)[0] == "hold")
check("far, last good fix 7 hours old: still held (the age limit does not beat the pin)",
      tv.judge_fix(P(BEND, NOW + 7 * H), trusted, [], None, [], pin)[0] == "hold")
check("far, at today's sheet city elsewhere: believed (move day)",
      tv.judge_fix(P(CLOV, NOW + 600), trusted, [], sheet_cl, [], pin)[0] == "accept")
check("far, sheet city is where the pin is: held",
      tv.judge_fix(P(BEND, NOW + 600), trusted, [], sheet_sb, [], pin)[0] == "hold")
park = {"lat": BEND[0], "lon": BEND[1], "at": NOW + 600 - 1800}
check("far, at a recent phone park: believed",
      tv.judge_fix(P(BEND, NOW + 600), trusted, [], None, [park], pin)[0] == "accept")
old_pin = P(SB, NOW - 15 * 86400)
check("a 15-day-old pin is ignored: the normal rules apply",
      tv.judge_fix(P(BEND, NOW + 7 * H), trusted, [], None, [], old_pin) ==
      tv.judge_fix(P(BEND, NOW + 7 * H), trusted, [], None, []))
v, why = tv.judge_fix(P(BEND, NOW + 600), trusted, [], None, [], pin)
check("the reason names the phone's RV location", "set on the phone" in why, why)

# ---- the whole path: pin, then the Mac's fixes -----------------------------------------------------------------------
reset()
put(NOW - 2 * H, BEND)
check("before any pin, a first wrong fix is believed", tv.device_location()["lat"] == round(BEND[0], 2))
tv.set_rv_pin(SB[0], SB[1], 8, "South Beach", "Oregon", "test")
d = tv.device_location()
check("after RV is here: the dashboard shows the pin", d and d["source"] == "phone" and d["label"] == "South Beach, OR", d)
put(NOW - H, BEND)
d = tv.device_location()
check("a wrong Mac fix is held back and the pin stays", d["source"] == "phone" and d.get("held") == 1, d)
put(NOW - H + 600, NEWPORT)
d = tv.device_location()
check("a nearby Mac fix is believed, but the pin is still what shows", d["source"] == "phone" and "held" not in d, d)
LOC.unlink()
check("no Mac fix at all: the pin still shows", (tv.device_location() or {}).get("source") == "phone")
check("the pin survives a restart", tv._load_pin() is not None and tv._load_pin()["by"] == "test")
tv._fixstate.update(loaded=False, pin=None, trusted=None)
put(NOW - H + 1200, BEND)
check("...and still holds back the wrong fix after it", tv.device_location()["source"] == "phone")
put(NOW - H + 1800, CLOV)
d = tv.device_location({"lat": CLOV[0], "lon": CLOV[1], "label": "Cloverdale, OR", "source": "itinerary"})
check("move day: the Mac's fix at the new sheet city is believed", d["source"] == "device" and d["lat"] == round(CLOV[0], 2), d)
check("...and the pin is cleared, file and all", tv._fixstate["pin"] is None and not tv._pin_file().exists())

# ---- current_location: a pin is not overridden by a Set location near the sheet city ---------------------------------
reset()
tv.set_rv_pin(CLOV[0], CLOV[1], 8, "Cloverdale", "OR", "test")
put(NOW, CLOV)
tv.ITIN_SOURCES = []
r = asyncio.run(tv.current_location())
check("current_location reports the phone pin", r["source"] == "phone", r)

# ---- token and endpoints ----------------------------------------------------------------------------------------------
reset()
out = subprocess.run([sys.executable, str(ROOT / "tvserver.py"), "token", "add", "batbelt", "--display", "--location"],
                     env=dict(os.environ), capture_output=True, text=True)
rec = json.loads((TMP / "tokens.json").read_text())["batbelt"]
check("token add --display --location: display, stops and location on top of the guest actions",
      {"display", "stops", "location", "power_on"} <= set(rec["actions"]), rec.get("actions"))
subprocess.run([sys.executable, str(ROOT / "tvserver.py"), "token", "add", "friend"],
               env=dict(os.environ), capture_output=True, text=True)
friend = json.loads((TMP / "tokens.json").read_text())["friend"]["token"]
try:
    from fastapi.testclient import TestClient
except Exception:           # TestClient needs httpx, which tvserver.py itself does not
    TestClient = None
if TestClient:
    cl = TestClient(tv.app)
    auth = {"Authorization": "Bearer " + rec["token"]}
    r = cl.post("/location/rv?lat=%f&lon=%f&acc=6&city=South%%20Beach&region=Oregon" % SB, headers=auth)
    check("POST /location/rv sets the pin", r.status_code == 200 and r.json()["pin"]["by"] == "batbelt", r.text)
    check("...and the dashboard follows it", r.json()["shown"]["source"] == "phone", r.text)
    r = cl.get("/location/rv", headers=auth)
    check("GET /location/rv reports the pin", r.status_code == 200 and r.json()["pin"]["lat"] == SB[0], r.text)
    r = cl.post("/location/rv?lat=1&lon=1", headers={"Authorization": "Bearer " + friend})
    check("a plain guest token may not set it (403)", r.status_code == 403, r.status_code)
    tv.save_state(mode="manual", lat=1.0, lon=1.0, label="Somewhere")
    r = cl.post("/location/rv?lat=%f&lon=%f" % SB, headers=auth)
    check("RV is here turns a manual pin back to following the location",
          tv.load_location()["mode"] == "auto" and r.json()["shown"]["source"] == "phone", r.text)
else:
    print("skip endpoint checks (pip install httpx to run them)")

print("\nall passed" if not fails else "\n%d FAILED" % len(fails))
sys.exit(1 if fails else 0)
