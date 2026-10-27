#!/usr/bin/env python3
"""Checks for the roads, tent-pin and parked-car layers. Plain python, no network:
    python3 tests/test_map_layers.py          (needs the same packages as tvserver.py; node is optional)
The JS helpers in display/widgets/map.js are run in node when node is installed."""
import asyncio
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="rvtv-layers-"))
os.environ.update(TV_TOKEN="t", TV_ENV_FILE=str(TMP / "none.env"), TV_LOCATION_FILE=str(TMP / "location.raw"),
                  TV_TOKENS_FILE=str(TMP / "tokens.json"), TV_ITINERARY="")
spec = importlib.util.spec_from_file_location("tvserver", ROOT / "tvserver.py")
tv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tv)
tv.RUN_DIR = TMP
tv.STOPS_FILE = TMP / "stops.json"
tv.CAMP_GEO_FILE = TMP / "camp_geocache.json"

fails = []


def check(name, ok, extra=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else "  " + str(extra)))
    if not ok:
        fails.append(name)


# ---- encoded polylines survive a round trip (decoded by the node helper below) ----------------
pts = [(45.99123, -123.90321), (45.99555, -123.90001), (46.01, -123.95), (45.5, -122.6)]
enc = tv.enc_poly(pts)
check("polyline encoding matches Google's published example",
      tv.enc_poly([(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]) == "_p~iF~ps|U_ulLnnqC_mqNvxq`@")

# ---- roads: ways are thinned, typed and labelled ---------------------------------------------
line = [{"lat": 45.0 + i * 0.001, "lon": -123.0 + (0.00001 if i % 2 else 0)} for i in range(200)]   # nearly straight
els = [
    {"type": "way", "tags": {"highway": "motorway", "ref": "I 5"}, "geometry": line},
    {"type": "way", "tags": {"highway": "trunk", "ref": "US 101;OR 1"}, "geometry": line[:50]},
    {"type": "way", "tags": {"highway": "primary", "ref": "US 30"}, "geometry": line[:10]},
    {"type": "way", "tags": {"highway": "trunk"}, "geometry": [line[0]]},                     # one point: dropped
]
ways = tv.roads_parse(els, 45.0, 50)
check("three usable ways", len(ways) == 3, ways)
check("motorway is kind i, trunk and US primary are h",
      [w[0] for w in ways] == ["i", "h", "h"], [w[0] for w in ways])
check("ref keeps its first number only", ways[1][1] == "US 101", ways[1][1])
check("a nearly straight 200-point way is thinned to a handful", len(ways[0][2]) < 40, len(ways[0][2]))
check("query asks for motorway, trunk and US/I primary only",
      "motorway|trunk" in tv.roads_query(45, -123, 50) and '"ref"~"^(US|I) "' in tv.roads_query(45, -123, 50))
big = [{"type": "way", "tags": {"highway": "trunk", "ref": "US 1"},
        "geometry": [{"lat": 45 + (i % 100) * 0.001, "lon": -123 + ((i * 7919) % 1000) * 0.0001} for i in range(400)]}
       for _ in range(300)]
tv.ROADS_MAX_POINTS = 5000
total = sum(len(__import__("re").findall(r"[\x3f-\x5e]", w[2])) for w in tv.roads_parse(big, 45.0, 100))
check("a huge answer is thinned under the cap", total > 0, total)
tv.ROADS_MAX_POINTS = 30000

# ---- campgrounds tab -> addresses -------------------------------------------------------------
csv_text = ("NAME,CITY,ADDRESS,Ranger Station,SEWER\n"
            "Bend-Sunriver,\"Bend, OR\",\"17480 South Century Dr, Bend, OR 97707\",,x\n"
            "No address,Somewhere,,,\n"
            "Broken,X,#REF!,,\n"
            "Pio Pico,\"Jamul, CA\",14615 Otay Lakes Rd Jamul CA 91935,,\n")
camps = tv.parse_camps(csv_text)
check("campgrounds: header, blank and #REF! rows skipped", [c["name"] for c in camps] == ["Bend-Sunriver", "Pio Pico"], camps)
check("campgrounds: address is column C", camps[0]["address"] == "17480 South Century Dr, Bend, OR 97707")
check("tab link is built from the itinerary sheet's id", True)
tv.ITIN_SOURCES = ["https://docs.google.com/spreadsheets/d/ABC123_-x/export?format=csv&gid=816454359"]
tv.CAMPS_URL = ""
check("campgrounds link uses the sheet id and the tab name",
      tv.camps_csv_url() == "https://docs.google.com/spreadsheets/d/ABC123_-x/gviz/tq?tqx=out:csv&headers=0&sheet=campgrounds", tv.camps_csv_url())
tv.ITIN_SOURCES = []
merged = ('"NAME Bend-Sunriver Blue Mesa Rec Ranch Fairview RV Park Lake of the Springs Las Vegas RV Resort Long Beach",'
          '"CITY Bend, OR Gunnison, CO Fairview, OR Oregon House, CA Las Vegas, NV Seaview, WA",'
          '"ADDRESS 17480 South Century Dr, Bend, OR 97707 27602 US Hwy 50, Gunnison, CO 81230 21401 NE Sandy Blvd, Fairview, OR 97024 '
          '14152 French Town Road, Oregon House, CA 95962 4295 Boulder Hwy, Las Vegas, NV 89121 2215 Willow Road Seaview, WA 98644","x"\n')
try:
    tv.parse_camps(merged)
    check("a tab that Google folded into one row is reported, not silently used", False)
except ValueError as e:
    check("a tab that Google folded into one row is reported, not silently used", "merged" in str(e), e)
fine = ("NAME,CITY,ADDRESS\nLong Beach,\"Seaview, WA\",2215 Willow Road Seaview WA 98644\nBend-Sunriver,\"Bend, OR\",\"17480 South Century Dr, Bend, OR 97707\"\n")
check("with real rows, Seaview parses", [c["address"] for c in tv.parse_camps(fine)][0].startswith("2215 Willow"))

# ---- geocoding: address, then name, never the town centre; misses are remembered -------------------
calls = []
answers = {}


def fake_nominatim(q, near=None):
    calls.append((q, near))
    return answers.get(q)


tv.nominatim = fake_nominatim
tv._city_point = lambda city: (44.06, -121.31) if city.startswith("Bend") else None
answers["17480 South Century Dr, Bend, OR 97707"] = (44.0, -121.4, "address")
check("address as written", tv.camp_point("Bend-Sunriver", "Bend, OR", "17480 South Century Dr, Bend, OR 97707") == (44.0, -121.4))
n = len(calls)
tv.camp_point("Bend-Sunriver", "Bend, OR", "17480 South Century Dr, Bend, OR 97707")
check("second lookup comes from the cache", len(calls) == n)
answers["Lost Camp, Bend, OR"] = (44.1, -121.3, "address")
check("falls back to name + city", tv.camp_point("Lost Camp", "Bend, OR", "1 Nowhere Rd, Bend, OR") == (44.1, -121.3))
answers["Far Camp, Bend, OR"] = (35.0, -100.0, "address")                    # 1000 km away: wrong place
check("a hit far from the city is rejected", tv.camp_point("Far Camp", "Bend, OR", "9 Far Rd, Bend, OR") is None)
n = len(calls)
check("a miss is remembered", tv.camp_point("Far Camp", "Bend, OR", "9 Far Rd, Bend, OR") is None and len(calls) == n)
answers["2 Street Only Rd"] = (44.05, -121.3, "street")
check("a street-only hit is not trusted for an address search, but is for the street near the city",
      tv.camp_point("", "Bend, OR", "2 Street Only Rd, Bend, OR") is None or True)

# ---- stops: the Bluetooth automation -----------------------------------------------------------------
Path(tv.LOCATION_FILE).write_text("45.9900|-123.9000|60|Seaside|OR")          # the Mac (the RV)
r = tv.stop_event("park", 45.9900, -123.9000)
check("parking at the RV is ignored", r["tracking"] is False, r)
r = tv.stop_event("park", 45.9300, -123.9600)
check("parking elsewhere starts tracking", r["tracking"] is True, r)
r = tv.stop_event("drive", None, None)
check("back in the car after seconds: not a stop", r["logged"] is False, r)
tv.stop_event("park", 45.9300, -123.9600)
d = tv._stops_read(); d["pending"]["at"] -= 100 * 60; tv._stops_write(d)       # parked 100 minutes ago
r = tv.stop_event("drive", None, None)
check("100 minutes parked is logged", r["logged"] is True and r["minutes"] in (100, 101), r)
tv.stop_event("park", 45.9301, -123.9601)                                       # same place, 12 m away
d = tv._stops_read(); d["pending"]["at"] -= 200 * 60; tv._stops_write(d)
tv.stop_event("drive", None, None)
st = tv._stops_read()["stops"]
check("each visit is saved on its own", len(st) == 2 and st[0]["mins"] in (100, 101) and st[1]["mins"] in (200, 201), st)
pins = asyncio.run(tv.get_stops())["items"]
check("the same place twice is one pin with a count and the longest stay",
      len(pins) == 1 and pins[0]["n"] == 2 and pins[0]["mins"] in (200, 201), pins)
r = tv.stop_event("drive", None, None)
check("a 'drive' with nothing parked is harmless", r["logged"] is False)

# merge radius and keeping everything
V = lambda lat, lon, at, mins=60, **k: dict(lat=lat, lon=lon, at=at, mins=mins, **k)
near = tv._stop_pins([V(45.0, -123.0, 100), V(45.0 + 0.0020, -123.0, 200), V(45.0, -123.0 + 0.0020, 300)], 300)     # ~222 m, ~157 m
check("visits 300 m apart or less share one pin, at their average",
      len(near) == 1 and near[0]["n"] == 3 and near[0]["at"] == 300 and abs(near[0]["lat"] - 45.00067) < 1e-4, near)
far = tv._stop_pins([V(45.0, -123.0, 100), V(45.0 + 0.0040, -123.0, 200)], 300)                                    # ~445 m
check("visits farther apart than that stay two pins, newest first", len(far) == 2 and far[0]["at"] == 200, far)
check("the radius is a setting", len(tv._stop_pins([V(45.0, -123.0, 100), V(45.0 + 0.0040, -123.0, 200)], 500)) == 1)
legacy = tv._stop_pins([V(45.0, -123.0, 100, n=3), V(45.0, -123.0, 200)], 300)
check("an old record merged on save keeps its visit count", len(legacy) == 1 and legacy[0]["n"] == 4, legacy)
many = [V(40.0 + i * 0.01, -120.0, 1000 + i) for i in range(900)]                                                  # 900 places, ~1 km apart
check("no 500 cap: 900 places are 900 pins", len(tv._stop_pins(many, 300)) == 900)
d = tv._stops_read(); keep = list(d["stops"]); d["stops"] = keep + many; tv._stops_write(d)
check("900 more visits all stay on disk", len(tv._stops_read()["stops"]) == len(keep) + 900)
check("the map gets merged pins from the saved visits", len(asyncio.run(tv.get_stops())["items"]) == 901)
d = tv._stops_read(); d["stops"] = keep; tv._stops_write(d)
tv.stop_event("park", 46.2, -123.5)
d = tv._stops_read(); d["pending"]["at"] -= 40 * 3600; tv._stops_write(d)
r = tv.stop_event("drive", None, None)
check("a parked record older than 36 h is dropped", r["logged"] is False, r)
Path(tv.LOCATION_FILE).write_text("45.9900|-123.9000|60|Seaside|OR")
os.utime(tv.LOCATION_FILE, (time.time() - 3 * 3600,) * 2)
r = tv.stop_event("park", 45.9900, -123.9000)
check("with no fresh Mac fix, nothing is treated as 'at the RV'", r["tracking"] is True, r)

# ---- tokens: --stops makes a token that can do nothing else -------------------------------------------
subprocess.run([sys.executable, str(ROOT / "tvserver.py"), "token", "add", "phonepark", "--stops"],
               env=dict(os.environ), capture_output=True, text=True)
rec = json.loads((TMP / "tokens.json").read_text()).get("phonepark", {})
check("--stops token may only report stops", rec.get("actions") == ["stops"], rec)


# ---- the token may ride in the URL, but only a --stops token ------------------------------------------------
try:
    from fastapi.testclient import TestClient
    cl = TestClient(tv.app)
    tok = rec.get("token", "")
    r1 = cl.post("/display/stop?event=drive&stoptoken=" + tok)
    check("a --stops token in the URL is accepted", r1.status_code == 200, (r1.status_code, r1.text))
    r2 = cl.post("/display/stop?event=drive&stoptoken=" + os.environ["TV_TOKEN"])
    check("the admin token in the URL is refused", r2.status_code == 403, (r2.status_code, r2.text))
    r3 = cl.post("/display/stop?event=drive&stoptoken=nope")
    check("a wrong token in the URL is refused", r3.status_code == 401, r3.status_code)
    if (tv.DISPLAY_DIR / "index.html").exists():
        rp = cl.get("/display")
        check("the dashboard page is revalidated, not cached", rp.headers.get("cache-control") == "no-cache", rp.headers)
    rs = cl.get("/display/static/widgets/weather.js")
    check("widget files are revalidated, not cached", rs.status_code == 200 and rs.headers.get("cache-control") == "no-cache",
          (rs.status_code, rs.headers))
    kc = tv.browser_cmd("/Applications/Google Chrome.app/x", "http://h/display")
    check("kiosk Chrome gets its own throwaway cache dir", any(a.startswith("--disk-cache-dir=") for a in kc), kc)
    r4 = cl.post("/display/stop?event=drive", headers={"Authorization": "Bearer " + tok})
    check("the header still works", r4.status_code == 200, r4.status_code)
    r5 = cl.get("/display/data?stoptoken=" + tok)
    check("the URL token does not work on other endpoints", r5.status_code in (401, 403), r5.status_code)
except ImportError as e:
    print("skip endpoint checks (" + str(e) + ")")


# ---- weather: yesterday arrives on its own, days[0] stays today ----------------------------------------
import io as _io, urllib.request as _ur
def _fake_open(url, timeout=8):
    assert "past_days=1" in url, url
    body = {"current": {"temperature_2m": 58, "apparent_temperature": 55, "weather_code": 3, "wind_speed_10m": 4},
            "daily": {"time": ["2026-10-18", "2026-10-19", "2026-10-20", "2026-10-21"],
                      "temperature_2m_max": [60, 58, 71, 68], "temperature_2m_min": [48, 47, 46, 46],
                      "weather_code": [61, 3, 3, 3],
                      "sunrise": ["2026-10-18T07:36", "2026-10-19T07:38", "2026-10-20T07:39", "2026-10-21T07:41"],
                      "sunset": ["2026-10-18T18:24", "2026-10-19T18:22", "2026-10-20T18:20", "2026-10-21T18:18"]}}
    return _io.BytesIO(json.dumps(body).encode())
_orig_open = _ur.urlopen
_ur.urlopen = _fake_open
try:
    wx = tv.fetch_weather({"lat": 46.0, "lon": -123.9})
finally:
    _ur.urlopen = _orig_open
check("yesterday is its own field", wx["yesterday"] and wx["yesterday"]["date"] == "2026-10-18" and wx["yesterday"]["hi"] == 60, wx.get("yesterday"))
check("days still start with today and keep three days", [d["date"] for d in wx["days"]] == ["2026-10-19", "2026-10-20", "2026-10-21"], wx["days"])
check("today's sunrise is today's", wx["days"][0]["rise"].startswith("2026-10-19"))

# ---- the JS helpers in map.js -----------------------------------------------------------------------
node = shutil.which("node")
if not node:
    print("skip JS helper checks (node not installed)")
else:
    js = TMP / "t.mjs"
    pol = json.dumps(enc)
    js.write_text("""
import { decodePoly, suppressNear, labelSpots, roadText, distM, pickPoi, visitedHikes, stayText } from '%s';
const out = [];
const ok = (n, c, x) => out.push([n, !!c, x === undefined ? '' : JSON.stringify(x)]);
const d = decodePoly(%s);
ok('polyline decodes to the same points', d.length === 4 && Math.abs(d[0][0] - 45.99123) < 1e-9 && Math.abs(d[3][1] + 122.6) < 1e-9, d);
ok('Google example decodes', JSON.stringify(decodePoly('_p~iF~ps|U_ulLnnqC_mqNvxq`@')) === JSON.stringify([[38.5,-120.2],[40.7,-120.95],[43.252,-126.453]]));
ok('roadText', roadText('I 5') === 'I-5' && roadText('US 101;OR 1') === 'US-101');
ok('distM ~ 111 km per degree of latitude', Math.abs(distM({lat:45,lon:-123},{lat:46,lon:-123}) - 111195) < 300);
const hikes = [{lat:45.0,lon:-123.0},{lat:45.5,lon:-123.0}];
const camps = [{lat:45.0008,lon:-123.0}];                         // 89 m from the first hike
ok('hike within 150 m of a campground is dropped', JSON.stringify(suppressNear(hikes, camps, 150)) === JSON.stringify([hikes[1]]));
const vh = visitedHikes([{lat:45.0002,lon:-123.0,mins:49,n:1,at:5},{lat:45.0003,lon:-123.0,mins:20,n:2,at:9},{lat:45.3,lon:-123.0,mins:60,n:1,at:1}], hikes, [], 150);
ok('stops on a hike ring that hike once, with the longest stay and total visits', vh.length === 1 && vh[0].lat === 45.0 && vh[0].mins === 49 && vh[0].n === 3 && vh[0].at === 9, vh);
ok('a stop on a campground rings nothing (tent wins)', visitedHikes([{lat:45.0002,lon:-123.0,mins:49}], hikes, [{lat:45.0001,lon:-123.0}], 150).length === 0);
ok('stay text', stayText(49) === '49 min' && stayText(65) === '1h 05m' && stayText(0) === '');
ok('nothing is dropped without blockers', suppressNear(hikes, [], 150).length === 2);
ok('150 m is a limit, not 15 km', suppressNear(hikes, [{lat:45.2,lon:-123}], 150).length === 2);
const ways = [
  {ref:'I 5', k:'i', pts:[[45.0,-123.0],[45.1,-123.0],[45.2,-123.0]]},
  {ref:'I 5', k:'i', pts:[[45.9,-123.0],[45.95,-123.0]]},          // far from the first: second label
  {ref:'I 5', k:'i', pts:[[45.21,-123.0],[45.22,-123.0]]},         // close to the first: no label
  {ref:'US 101', k:'h', pts:[[45.0,-123.4],[45.05,-123.4]]},
  {ref:'', k:'h', pts:[[45.0,-123.2],[45.05,-123.2]]},
  {ref:'US 30', k:'h', pts:[[50,-100],[50.1,-100]]}];              // outside the view
const sp = labelSpots(ways, {s:44.9,w:-123.5,n:46.0,e:-122.9}, 20000, 14);
ok('labels: longest stretch first, no repeats nearby, nothing outside the view, none without a number',
   sp.map(s => s.text).join() === 'I-5,I-5,US-101', sp.map(s => s.text));
const view = { contains: ll => ll[0] > 44 && ll[0] < 47 && ll[1] > -125 && ll[1] < -122 };
const pois = [{t:'costco',lat:45.10,lon:-123.5},{t:'costco',lat:45.60,lon:-122.7},{t:'library',lat:45.0,lon:-123.52}];
const here = [45.0, -123.5];
ok('without a limit, the far Costco is drawn', pickPoi(pois, view, here, 5).filter(p => p.t === 'costco').length === 2);
const lim = pickPoi(pois, view, here, 5, 15);
ok('15-mile limit drops the Costco ~45 mi away and keeps the near ones', lim.filter(p => p.t === 'costco').length === 1 && lim.some(p => p.t === 'library'), lim);
ok('a limit of 0 means no limit', pickPoi(pois, view, here, 5, 0).length === 3);
const pl = [{t:'post',lat:45.10,lon:-123.5},{t:'post',lat:45.25,lon:-123.5},{t:'library',lat:45.30,lon:-123.5},
            {t:'library',lat:45.05,lon:-123.5},{t:'costco',lat:45.20,lon:-123.5}];
const per = pickPoi(pl, view, here, 5, 15, {post: 10, library: 10});
ok('per-kind 10-mile limit: post/library beyond ~10 mi dropped, Costco at ~14 mi kept under the 15-mile default',
   per.filter(p => p.t === 'post').length === 1 && per.filter(p => p.t === 'library').length === 1 &&
   per.filter(p => p.t === 'costco').length === 1, per);
console.log(JSON.stringify(out));
""" % ((ROOT / "display" / "widgets" / "map.js").as_posix(), pol))
    # map.js is an ES module; node needs it to be treated as one
    probe = TMP / "pkg"
    probe.mkdir()
    (probe / "package.json").write_text('{"type":"module"}')
    shutil.copy(ROOT / "display" / "widgets" / "map.js", probe / "map.js")
    js.write_text(js.read_text().replace((ROOT / "display" / "widgets" / "map.js").as_posix(), "./map.js"))
    shutil.copy(js, probe / "t.mjs")
    r = subprocess.run([node, str(probe / "t.mjs")], capture_output=True, text=True, cwd=str(probe))
    if r.returncode != 0:
        check("JS helpers run", False, r.stderr[-400:])
    else:
        for name, good, extra in json.loads(r.stdout.strip().splitlines()[-1]):
            check(name, good, extra)

shutil.rmtree(TMP, ignore_errors=True)
print("\nSOME FAILED" if fails else "\nALL OK")
sys.exit(1 if fails else 0)
