#!/usr/bin/env python3
"""Checks for the Set location source: when the Mac's fix is more than 50 km from today's sheet city and the newest
Set location (from the network tool) is within 50 km of it, the Set location is "here".
Plain python, no network:   python3 tests/test_setloc.py   (needs the same packages as tvserver.py)"""
import asyncio
import importlib.util
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="rvtv-setloc-"))
os.environ.update(TV_TOKEN="t", TV_ENV_FILE=str(TMP / "none.env"), TV_LOCATION_FILE=str(TMP / "location.raw"),
                  TV_TOKENS_FILE=str(TMP / "tokens.json"), TV_ITINERARY="", TV_NETTEST_DIR=str(TMP / "nettest"),
                  TV_SITES_FILE=str(TMP / "sites.json"), TV_LOCATION_STATE=str(TMP / "state.json"))
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("tvserver", ROOT / "tvserver.py")
tv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tv)
tv.RUN_DIR = TMP
tv.STOPS_FILE = TMP / "stops.json"
tv.LOCATION_HISTORY_FILE = TMP / "location_history.log"
import nettest as nt

nt.LOCATION_FILE = Path(tv.LOCATION_FILE)
logging.getLogger("uvicorn.error").setLevel(logging.ERROR)
tv.DEVICE_MAX_AGE = 1e12

fails = []


def check(name, ok, extra=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else "  " + str(extra)))
    if not ok:
        fails.append(name)


LOC = Path(tv.LOCATION_FILE)
NOW = time.time()
H = 3600
SB = (44.547325, -124.070189)                # South Beach, OR
FLO = (43.98262, -124.09984)                 # Florence, OR, 62 km south
BEND = (44.083446, -121.256594)


def put(pos, city="", region="OR"):
    LOC.write_text("%.6f|%.6f|40|%s|%s\n" % (pos[0], pos[1], city, region))
    os.utime(LOC, (time.time(), time.time()))


def reset(itin_pos=None):
    tv._fixstate.update(stamp=None, trusted=None, held=[], loaded=False, outcome=None)
    tv._itin_seen.update(date=None, loc=None)
    for f in (tv._trusted_file(), tv._rejected_file(), tv.LOCATION_HISTORY_FILE, LOC, tv.STOPS_FILE,
              tv._override_file(), nt.SITES_FILE):
        Path(f).unlink(missing_ok=True)
    tv.save_location({"mode": "auto", "lat": 42.7, "lon": -84.5, "label": "Lansing, MI"}) \
        if hasattr(tv, "save_location") else None
    state = {"city": None}
    tv.ITIN_SOURCES = ["x"]
    tv.itinerary_location = lambda day: state["city"]
    return state


def set_site(name, pos, ago_h=0.0):
    d = nt.read_sites()
    sid = "s%d" % (len(d["sites"]) + 1)
    d["sites"].append({"id": sid, "name": name, "lat": pos[0], "lon": pos[1], "set_at": NOW, "source": "phone"})
    d["anchors"].append({"at": time.time() - ago_h * H, "site_id": sid, "retagged": 0})
    nt.write_sites(d)


def where():
    return asyncio.run(tv.current_location())


def city(name, pos):
    return {"lat": pos[0], "lon": pos[1], "label": name, "source": "itinerary"}


# ---- the Mac agrees with the sheet: the Mac is used, the Set location is not looked at ---------------------------------
st = reset()
st["city"] = city("South Beach, OR", SB)
set_site("Florence, OR", FLO)
put(SB, "South Beach")
r = where()
check("Mac within 50 km of the sheet: the Mac is here", r["source"] == "device" and r["label"] == "South Beach, OR", r)
check("... and no override is left for nettest", not tv._override_file().exists())

# ---- the Mac is off (Bend), the Set location is at the sheet city: the Set location wins ------------------------------
st = reset()
st["city"] = city("South Beach, OR", SB)
set_site("South Beach, OR", SB)
put(BEND, "Bend")
r = where()
check("Mac in Bend, sheet South Beach, Set location South Beach: Set location is here",
      r["source"] == "setloc" and r["label"] == "South Beach, OR" and "schedule" not in r, r)
check("... so it is not a move day", not r.get("schedule"))
check("... weather and the route get its coordinates", abs(r["lat"] - 44.55) < 0.01 and abs(r["lon"] + 124.07) < 0.01, r)
ov = json.loads(tv._override_file().read_text())
check("... and nettest is told the position and name", ov["name"] == "South Beach, OR" and abs(ov["lat"] - SB[0]) < 1e-6, ov)
fx = nt.location_fix()
check("... so a test run is tagged with the Set location, not Bend", fx and fx["place"] == "South Beach, OR"
      and abs(fx["lat"] - 44.5473) < 0.001, fx)

# ---- move day: sheet rolled to Florence, Mac still back at South Beach ---------------------------------------------------
st = reset()
st["city"] = city("Florence, OR", FLO)
set_site("South Beach, OR", SB)
put(SB, "South Beach")
r = where()
check("move day before arriving: the old Set location is not near the sheet city, so MOVE DAY as before",
      r["source"] == "device" and r.get("schedule") == "Florence, OR", r)
check("... and no override", not tv._override_file().exists())
set_site("Florence, OR", FLO)
r = where()
check("after tapping Set location in Florence: Florence is here, no MOVE DAY",
      r["source"] == "setloc" and r["label"] == "Florence, OR" and "schedule" not in r, r)
fx = nt.location_fix()
check("... and tests are tagged Florence", fx and fx["place"] == "Florence, OR", fx)

# ---- the Set location goes stale ------------------------------------------------------------------------------------------
st = reset()
st["city"] = city("Florence, OR", FLO)
set_site("Florence, OR", FLO, ago_h=49)
put(BEND, "Bend")
r = where()
check("a Set location older than 48 h is ignored", r["source"] == "device" and r.get("schedule") == "Florence, OR", r)
set_site("Florence, OR", FLO, ago_h=47)
r = where()
check("... one under 48 h counts", r["source"] == "setloc", r)

# ---- the newest anchor decides, not the file order of sites ------------------------------------------------------------
st = reset()
st["city"] = city("Florence, OR", FLO)
set_site("Florence, OR", FLO, ago_h=30)
set_site("South Beach, OR", SB, ago_h=1)
put(BEND, "Bend")
r = where()
check("only the newest Set location is considered (it is South Beach, 62 km from the sheet): MOVE DAY",
      r["source"] == "device" and r.get("schedule") == "Florence, OR", r)

# ---- other cases leave things alone ----------------------------------------------------------------------------------------
st = reset()
st["city"] = None
set_site("South Beach, OR", SB)
put(BEND, "Bend")
r = where()
check("no sheet city today: the Set location is not used", r["source"] == "device", r)
st = reset()
st["city"] = city("South Beach, OR", SB)
check("no sites file at all, Mac off: MOVE DAY as before", (put(BEND, "Bend"), where())[1].get("schedule") == "South Beach, OR")
st = reset()
st["city"] = city("South Beach, OR", SB)
set_site("South Beach, OR", SB)
put(BEND, "Bend")
tv._note_override(None)
nt.SITES_FILE.write_text("{not json")
r = where()
check("a broken sites file does not break the location", r["source"] == "device", r)
st = reset()
st["city"] = city("South Beach, OR", SB)
set_site("South Beach, OR", SB)
put(BEND, "Bend")
where()
os.utime(tv._override_file(), None)
d = json.loads(tv._override_file().read_text())
d["at"] = time.time() - 2000
tv._override_file().write_text(json.dumps(d))
fx = nt.location_fix()
check("nettest ignores an override file older than 30 minutes", fx and fx["place"] != "South Beach, OR", fx)

# ---- display ----------------------------------------------------------------------------------------------------------------
js = (ROOT / "display" / "widgets" / "location.js").read_text()
check("the location widget has words for the new source", "setloc: 'From your Set location'" in js)

print()
print("all passed" if not fails else "%d FAILED: %s" % (len(fails), "; ".join(fails)))
sys.exit(1 if fails else 0)
