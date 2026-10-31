#!/usr/bin/env python3
"""Checks for the location filter (a far-away Wi-Fi fix is held back unless something backs it up), the daily wind and
humidity columns of the forecast, nettest tagging with the believed position, and the times in the log.
Plain python, no network:   python3 tests/test_locfilter.py   (needs the same packages as tvserver.py)"""
import importlib.util
import io
import json
import logging
import os
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="rvtv-locfilter-"))
os.environ.update(TV_TOKEN="t", TV_ENV_FILE=str(TMP / "none.env"), TV_LOCATION_FILE=str(TMP / "location.raw"),
                  TV_TOKENS_FILE=str(TMP / "tokens.json"), TV_ITINERARY="", TV_NETTEST_DIR=str(TMP / "nettest"),
                  TV_SITES_FILE=str(TMP / "sites.json"))
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("tvserver", ROOT / "tvserver.py")
tv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tv)
tv.RUN_DIR = TMP
tv.STOPS_FILE = TMP / "stops.json"
tv.LOCATION_HISTORY_FILE = TMP / "location_history.log"
import nettest as nt

nt.LOCATION_FILE = Path(tv.LOCATION_FILE)

fails = []


def check(name, ok, extra=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else "  " + str(extra)))
    if not ok:
        fails.append(name)


LOC = Path(tv.LOCATION_FILE)
NOW = time.time()
H = 3600
STEP = 600                                   # the agent runs every 10 minutes
SB = (44.547325, -124.070189)                # South Beach, OR: where the RV really was
BEND = (44.083446, -121.256594)              # the wrong place Apple's database kept returning
CLOV = (45.2296, -123.9684)                  # Cloverdale, 76 km north of South Beach
logging.getLogger("uvicorn.error").setLevel(logging.ERROR)    # keep the 'held back' warnings out of the test output
tv.DEVICE_MAX_AGE = 1e12                     # the replays use made-up times, so the 30-minute rule must not fire


def sheet(city, at):
    return {"lat": at[0], "lon": at[1], "label": city, "source": "itinerary"}


SHEET_SB = sheet("South Beach, OR", SB)


def reset():
    tv._fixstate.update(stamp=None, trusted=None, held=[], loaded=False, outcome=None)
    tv._itin_seen.update(date=None, loc=None)
    for f in (tv._trusted_file(), tv._rejected_file(), tv.LOCATION_HISTORY_FILE, LOC, tv.STOPS_FILE):
        f.unlink(missing_ok=True)


def put(t, pos, city="", region="", acc=40):
    """The location agent writes a fix at time t."""
    LOC.write_text("%.6f|%.6f|%d|%s|%s\n" % (pos[0], pos[1], acc, city, region))
    os.utime(LOC, (t, t))


def fix(t, pos, sh=None):
    put(t, pos, "South Beach" if pos == SB else "Bend" if pos == BEND else "Cloverdale" if pos == CLOV else "", "OR")
    return tv.device_location(sh)


def where(d):
    return d["label"] if d else None


# ---- judge_fix on its own ---------------------------------------------------------------------------------------
T0 = {"lat": SB[0], "lon": SB[1], "at": NOW}
F = lambda pos, at=NOW + STEP: {"lat": pos[0], "lon": pos[1], "at": at}     # noqa: E731
sheet_sb = {"lat": SB[0], "lon": SB[1]}
sheet_cl = {"lat": CLOV[0], "lon": CLOV[1]}
check("no good fix yet: believed", tv.judge_fix(F(BEND), None, [], None, [])[0] == "accept")
check("a few km away: believed", tv.judge_fix(F((SB[0] + 0.02, SB[1])), T0, [], sheet_sb, [])[0] == "accept")
check("a far fix with no backing: held", tv.judge_fix(F(BEND), T0, [], None, [])[0] == "hold")
check("a far fix at today's sheet city: believed", tv.judge_fix(F(CLOV), T0, [], sheet_cl, [])[0] == "accept")
check("a far fix far from the sheet city: held", tv.judge_fix(F(BEND), T0, [], sheet_cl, [])[0] == "hold")
park = {"lat": BEND[0], "lon": BEND[1], "at": NOW + STEP - 1800}
check("a far fix at a recent phone park: believed", tv.judge_fix(F(BEND), T0, [], sheet_sb, [park])[0] == "accept")
old_park = dict(park, at=NOW + STEP - 4 * H)
check("a 4-hour-old park does not back it up", tv.judge_fix(F(BEND), T0, [], sheet_sb, [old_park])[0] == "hold")
far_park = dict(park, lat=BEND[0] + 0.05)
check("a park 5 km off does not back it up", tv.judge_fix(F(BEND), T0, [], sheet_sb, [far_park])[0] == "hold")
check("a last good fix over 6 hours old vetoes nothing",
      tv.judge_fix(F(BEND, NOW + 7 * H), T0, [], sheet_sb, [])[0] == "accept")
v, why = tv.judge_fix(F(BEND), T0, [], sheet_sb, [])
check("the reason says the sheet puts the RV at the last good fix", "sheet says the RV is there" in why, why)

# ---- the real pattern: South Beach all day, Bend fixes in runs of one, two, three and many --------------------------
reset()
t = NOW - 30 * H
seen = []
for k in range(6):
    seen.append(where(fix(t, SB, SHEET_SB)))
    t += STEP
check("good fixes show South Beach", set(seen) == {"South Beach, OR"}, seen)
for run in (1, 2, 3):
    seen = []
    for k in range(run):
        d = fix(t, BEND, SHEET_SB)
        seen.append((where(d), d.get("held")))
        t += STEP
    check("a run of %d Bend fix(es) still shows South Beach" % run,
          all(w == "South Beach, OR" for w, _ in seen) and seen[-1][1] == run, seen)
    d = fix(t, SB, SHEET_SB)
    t += STEP
    check("the next good fix clears the hold (run of %d)" % run, where(d) == "South Beach, OR" and "held" not in d, d)
rej = tv._rejected_file().read_text().splitlines()
check("every held fix is in the rejected log", len(rej) == 6, rej)
check("a rejected line carries time, coordinates, accuracy and the reason",
      re.fullmatch(r"\d+\|44\.083446\|-121\.256594\|40\|.+", rej[0]) is not None, rej[0])
trusted = json.loads(tv._trusted_file().read_text())
check("the believed fix is on disk for restarts and nettest", round(trusted["lat"], 4) == round(SB[0], 4)
      and trusted["city"] == "South Beach", trusted)

# a long run is held while the sheet says we are here, until the last good fix is 6 hours old
good_at = json.loads(tv._trusted_file().read_text())["at"]
accepted_at, accepted_t = None, None
for k in range(1, 60):
    t += STEP
    d = fix(t, BEND, SHEET_SB)
    if where(d) == "Bend, OR":
        accepted_at, accepted_t = k, t
        break
check("20 straight Bend fixes (over 3 hours) are still held when the sheet says South Beach",
      accepted_at is not None and accepted_at > 20, accepted_at)
check("only the age limit on the last good fix lets Bend through: the first fix more than 6 hours after it",
      accepted_t is not None and accepted_t - good_at > 6 * H and accepted_t - STEP - good_at <= 6 * H,
      (accepted_t, good_at))

# ---- no sheet: a steady run is believed after two hours, an unsteady one never ----------------------------------------
reset()
t = NOW - 30 * H
fix(t, SB)
first = None
for k in range(1, 30):
    t += STEP
    d = fix(t, BEND)
    if where(d) == "Bend, OR":
        first = k
        break
check("without a sheet a steady run is believed once it has lasted 2 hours (13th fix)", first == 13, first)

reset()
t = NOW - 30 * H
fix(t, SB)
seen = set()
for k in range(30):                                                    # 5 hours: inside the 6-hour age limit
    t += STEP
    seen.add(where(fix(t, BEND if k % 2 == 0 else (44.9, -122.0))))      # two far places taking turns
check("fixes that keep jumping around are not believed (5 hours of them)", seen == {"South Beach, OR"}, seen)

# ---- a bad fix on the morning of a move day (sheet already shows the destination) is still held ------------------
reset()
t = NOW - 30 * H
fix(t, SB, sheet("Cloverdale, OR", CLOV))
d = fix(t + STEP, BEND, sheet("Cloverdale, OR", CLOV))
check("move-day morning: a Bend fix is held", where(d) == "South Beach, OR" and d.get("held") == 1, d)
d = fix(t + 2 * STEP, CLOV, sheet("Cloverdale, OR", CLOV))
check("move-day arrival at the sheet city is believed at once", where(d) == "Cloverdale, OR" and "held" not in d, d)

# ---- a real drive, 15 km a step, far from the sheet: every step is believed ----------------------------------------
reset()
t = NOW - 30 * H
fix(t, SB, sheet("Cloverdale, OR", CLOV))
lat, ok = SB[0], True
for k in range(1, 12):
    lat += 0.135                                                    # about 15 km north per step
    t += STEP
    put(t, (lat, SB[1]), "Underway", "OR")
    d = tv.device_location(sheet("Cloverdale, OR", CLOV))
    ok = ok and d["label"] == "Underway, OR" and "held" not in d
check("a drive (15 km per fix) is followed fix by fix", ok, d)

# ---- a phone park (true GPS) backs up a far fix ---------------------------------------------------------------------
reset()
t = NOW - 30 * H
fix(t, SB, SHEET_SB)
d = fix(t + STEP, BEND, SHEET_SB)
check("park test: Bend is held first", where(d) == "South Beach, OR", d)
tv._stops_write({"pending": None, "stops": [], "seen": [], "last_at": 0.0, "last_upload": None, "last_error": None,
                 "parks": [{"lat": BEND[0], "lon": BEND[1], "at": t + STEP - 600, "rv": False}]})
d = fix(t + 2 * STEP, BEND, SHEET_SB)
check("then a phone park at that spot makes it believed", where(d) == "Bend, OR", d)

# ---- first run after an upgrade: the believed fix comes from the history log -----------------------------------------
reset()
t = NOW - 6 * H
hist = []
for k in range(12):
    pos = BEND if k in (1, 4, 7, 9, 10) else SB
    hist.append("%d|%.6f|%.6f|60" % (t + k * STEP, pos[0], pos[1]))
tv.LOCATION_HISTORY_FILE.write_text("\n".join(hist) + "\n")
d = fix(t + 12 * STEP, BEND, SHEET_SB)
check("no saved fix: 7 South Beach vs 5 Bend history entries, Bend is held", d is not None and abs(d["lat"] - 44.55) < 0.01, d)
check("the town name comes from the sheet when the restored fix has none", where(d) == "South Beach, OR", d)
reset()
hist = []
for k in range(12):                                   # Bend has repeated for hours: 9 of the last 12
    pos = SB if k in (0, 1, 2) else BEND
    hist.append("%d|%.6f|%.6f|60" % (t + k * STEP, pos[0], pos[1]))
tv.LOCATION_HISTORY_FILE.write_text("\n".join(hist) + "\n")
d = fix(t + 12 * STEP, BEND, SHEET_SB)
check("no saved fix, Bend outnumbers South Beach in the history, but the sheet says South Beach: Bend is held",
      d is not None and abs(d["lat"] - 44.55) < 0.01, d)
reset()
tv.LOCATION_HISTORY_FILE.write_text("\n".join("%d|%.6f|%.6f|60" % (t + k * STEP, BEND[0], BEND[1]) for k in range(12)) + "\n")
d = fix(t + 12 * STEP, BEND)
check("really in Bend (all history says so): believed", where(d) == "Bend, OR", d)
reset()
d = fix(t, BEND)
check("no history at all: the first fix is believed", where(d) == "Bend, OR", d)

# ---- a restart keeps the last good fix --------------------------------------------------------------------------------
reset()
t = NOW - 30 * H
fix(t, SB, SHEET_SB)
tv._fixstate.update(stamp=None, trusted=None, held=[], loaded=False, outcome=None)      # the server restarts
d = fix(t + STEP, BEND, SHEET_SB)
check("after a restart a Bend fix is still held", where(d) == "South Beach, OR", d)

# ---- other readers see the believed position, not the raw one ----------------------------------------------------------
reset()
t = NOW - 30 * H
fix(t, SB, SHEET_SB)
fix(t + STEP, BEND, SHEET_SB)
raw_now = LOC.read_text().split("|")
fx = tv._mac_fix()
check("the park matcher's 'where the Mac is' is South Beach while Bend is held back",
      fx is not None and abs(fx["lat"] - SB[0]) < 1e-6, fx)
tv.LOCATION_HISTORY_FILE.write_text("%d|%.6f|%.6f|40\n%d|%.6f|%.6f|40\n" % (t + 2, SB[0], SB[1], t + STEP + 2, BEND[0], BEND[1]))
at = tv._mac_fix_at(t + STEP + 30)
check("a history entry that was held back is skipped when looking up where the Mac was",
      at is not None and abs(at["lat"] - SB[0]) < 1e-6, at)
check("a history entry that was not held back is still used",
      abs(tv._mac_fix_at(t + 30)["lat"] - SB[0]) < 1e-6)

reset()
t = NOW - 600
put(t, SB, "South Beach", "OR")
tv.DEVICE_MAX_AGE = 1800
tv.device_location(SHEET_SB)
put(NOW - 60, BEND, "Bend", "OR")
tv.device_location(SHEET_SB)
nt.LOCATION_FILE = LOC
f = nt.location_fix()
check("nettest tags a run with the believed place", f is not None and f["place"] == "South Beach, OR", f)
check("nettest's age is the agent's last write, so a held fix does not make it look old",
      f is not None and f["age_s"] < 120, f)
tv._trusted_file().unlink()
f = nt.location_fix()
check("nettest falls back to the raw file when tvserver has not judged anything", f is not None and f["place"] == "Bend, OR", f)
tv.DEVICE_MAX_AGE = 1e12

# ---- a stale file is still ignored (the old 30-minute rule) ------------------------------------------------------------
reset()
put(NOW - 2 * H, SB, "South Beach", "OR")
tv.DEVICE_MAX_AGE = 1800
check("a 2-hour-old file is ignored as before", tv.device_location(SHEET_SB) is None)
put(NOW - 60, SB, "South Beach", "OR")
check("a fresh one is used", where(tv.device_location(SHEET_SB)) == "South Beach, OR")
tv.DEVICE_MAX_AGE = 1e12
reset()
LOC.write_text("999|999|10|x|y\n")
check("a fix outside the globe is ignored", tv.device_location() is None)
LOC.write_text("44.5|-124.0|9000|x|y\n")
check("a fix coarser than 5 km is ignored", tv.device_location() is None)

# ---- the forecast carries wind and humidity per day ---------------------------------------------------------------------


class Reply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def forecast(extra):
    body = {"current": {"temperature_2m": 54.3, "apparent_temperature": 50.8, "weather_code": 2, "wind_speed_10m": 8.4},
            "daily": {"time": ["2026-10-30", "2026-10-31", "2026-11-01", "2026-11-02"],
                      "temperature_2m_max": [58, 61, 60, 49], "temperature_2m_min": [44, 47, 45, 38],
                      "weather_code": [2, 61, 3, 71]}}
    body["daily"].update(extra)
    urls = []
    real = tv.urllib.request.urlopen
    tv.urllib.request.urlopen = lambda url, timeout=0: (urls.append(url), Reply(json.dumps(body).encode()))[1]
    try:
        out = tv.fetch_weather({"lat": 44.5, "lon": -124.0})
    finally:
        tv.urllib.request.urlopen = real
    return out, urls[0]


out, url = forecast({"wind_speed_10m_max": [12.4, 22.6, 9.5, 31.0], "relative_humidity_2m_mean": [71.2, 88.0, 64.5, None]})
check("the request asks for the day's top wind and mean humidity",
      "wind_speed_10m_max" in url and "relative_humidity_2m_mean" in url, url)
check("each forecast day has a rounded wind and humidity",
      [(d["wind"], d["hum"]) for d in out["days"]] == [(23, 88), (10, 64), (31, None)], out["days"])
check("yesterday has them too", out["yesterday"]["wind"] == 12 and out["yesterday"]["hum"] == 71, out["yesterday"])
out, _ = forecast({})
check("a forecast without them still works (no columns)",
      all(d["wind"] is None and d["hum"] is None for d in out["days"]) and len(out["days"]) == 3, out["days"])

# ---- the log has times -------------------------------------------------------------------------------------------------
cfg = tv.log_config_with_times()
stream = io.StringIO()
import logging.config

logging.config.dictConfig(cfg)
logging.getLogger("uvicorn").handlers[0].stream = stream           # uvicorn.error propagates to this one
logging.getLogger("uvicorn.error").info("devon: power_on")
acc = logging.getLogger("uvicorn.access")
acc.handlers[0].stream = stream
acc.info('%s - "%s %s HTTP/%s" %d', "100.74.199.18:49360", "POST", "/power/on", "1.1", 202)
lines = stream.getvalue().splitlines()
stamp = r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d[+-]\d{4}"
check("an app log line starts with a date, time and UTC offset",
      len(lines) == 2 and re.match(stamp + r" INFO:\s+devon: power_on$", lines[0]) is not None, lines)
check("an access log line does too", re.match(stamp + r" INFO:\s+100\.74\.199\.18:49360 - \"POST /power/on HTTP/1\.1\" 202", lines[1]) is not None, lines)
import uvicorn.config as uc

check("uvicorn's own default config is left alone", "asctime" not in uc.LOGGING_CONFIG["formatters"]["default"]["fmt"])

print()
print("FAILED: " + ", ".join(fails) if fails else "all passed")
sys.exit(1 if fails else 0)
