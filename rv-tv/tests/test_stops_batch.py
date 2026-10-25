#!/usr/bin/env python3
"""Checks for the offline stop log: events carry their own time, uploads can arrive late, twice, or out of order.
    python3 tests/test_stops_batch.py        (needs the packages tvserver.py needs; no network)
Covers: the time parser, event-time minutes, ordering, duplicates, stale replays, bad data, the Mac's position
at the time of the event (location history), the upload endpoint, and the location agent's history file."""
import datetime
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="rvtv-stops-"))
os.environ.update(TV_TOKEN="t", TV_ENV_FILE=str(TMP / "none.env"), TV_LOCATION_FILE=str(TMP / "location.raw"),
                  TV_TOKENS_FILE=str(TMP / "tokens.json"), TV_ITINERARY="", TV_RUN_DIR=str(TMP))
spec = importlib.util.spec_from_file_location("tvserver", ROOT / "tvserver.py")
tv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tv)
tv.RUN_DIR = TMP
tv.STOPS_FILE = TMP / "stops.json"
tv.LOCATION_HISTORY_FILE = TMP / "location_history.log"

fails = []
LOC = Path(tv.LOCATION_FILE)
H = 3600.0
NOW = time.time()
TZ = datetime.timezone(datetime.timedelta(hours=-7))


def check(name, ok, extra=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else "  " + str(extra)))
    if not ok:
        fails.append(name)


def iso(t):
    return datetime.datetime.fromtimestamp(t, TZ).isoformat(timespec="seconds")


def ev(kind, t, lat=None, lon=None):
    d = {"event": kind, "at": iso(t)}
    if lat is not None:
        d.update(lat=lat, lon=lon)
    return d


def reset():
    for f in (tv.STOPS_FILE, tv.LOCATION_HISTORY_FILE, LOC):
        if f.exists():
            f.unlink()


# ---- the time parser ------------------------------------------------------------------------------------------
want = datetime.datetime(2026, 10, 24, 21, 3, 11, tzinfo=datetime.timezone.utc).timestamp()
check("ISO time with an offset", tv.parse_event_time("2026-10-24T14:03:11-07:00") == want)
check("ISO time ending in Z", tv.parse_event_time("2026-10-24T21:03:11Z") == want)
check("offset without a colon", tv.parse_event_time("2026-10-24T14:03:11-0700") == want)
check("fractional seconds of any length", abs(tv.parse_event_time("2026-10-24T14:03:11.5-07:00") - want - 0.5) < 1e-6)
check("epoch seconds, as text or number", tv.parse_event_time("1790000000") == 1790000000.0 and tv.parse_event_time(1790000000) == 1790000000.0)
check("epoch milliseconds", tv.parse_event_time(1790000000123) == 1790000000.123)
loc_naive = datetime.datetime(2026, 10, 24, 14, 3, 11).timestamp()
check("no offset: read as the Mac's local time", tv.parse_event_time("2026-10-24T14:03:11") == loc_naive)
check("garbage and empty give None", tv.parse_event_time("later") is None and tv.parse_event_time("") is None
      and tv.parse_event_time(None) is None and tv.parse_event_time(True) is None)

# ---- a hike with no signal: both events arrive hours later, with their own times ------------------------------
reset()
r = tv.stops_ingest([ev("park", NOW - 4 * H, 45.93, -123.96), ev("drive", NOW - 1 * H)], now=NOW)
check("both accepted", all(x["ok"] for x in r), r)
check("the stay is measured by event time (180 min), not arrival (0 min)", r[1].get("logged") is True and r[1].get("minutes") == 180, r)
st = tv._stops_read()
check("one pin saved with the park time", len(st["stops"]) == 1 and abs(st["stops"][0]["at"] - (NOW - 4 * H)) < 2, st)
check("the upload is recorded", st["last_upload"] and st["last_upload"]["applied"] == 2 and st["last_error"] is None, st["last_upload"])

# ---- sending the same files again changes nothing -------------------------------------------------------------
r2 = tv.stops_ingest([ev("park", NOW - 4 * H, 45.93, -123.96), ev("drive", NOW - 1 * H)], now=NOW + 60)
check("a re-send is acknowledged as a duplicate", all(x["ok"] and x.get("duplicate") for x in r2), r2)
st2 = tv._stops_read()
check("a re-send adds no visit and no pin", len(st2["stops"]) == 1 and st2["stops"][0].get("n", 1) == 1, st2["stops"])

# ---- order inside one upload does not matter -------------------------------------------------------------------
reset()
r = tv.stops_ingest([ev("drive", NOW - 1 * H), ev("park", NOW - 3 * H, 45.50, -123.50)], now=NOW)
check("a drive listed before its park is still matched", r[0].get("logged") is True and r[0].get("minutes") == 120, r)

# ---- an old event arriving after newer ones is skipped ---------------------------------------------------------
r = tv.stops_ingest([ev("park", NOW - 10 * H, 45.10, -123.10)], now=NOW)
check("an event older than one already applied is skipped, and acknowledged",
      r[0]["ok"] and "skipped" in r[0] and tv._stops_read()["pending"] is None, r)

# ---- uploads in several pieces, in time order -------------------------------------------------------------------
reset()
a = tv.stops_ingest([ev("park", NOW - 5 * H, 45.20, -123.20)], now=NOW)
b = tv.stops_ingest([ev("drive", NOW - 2 * H)], now=NOW)
check("park and drive uploaded separately still make a stop", a[0].get("tracking") is True and b[0].get("logged") and b[0]["minutes"] == 180, (a, b))

# ---- bad data is dropped (and acknowledged, so the phone stops resending it) ------------------------------------
reset()
r = tv.stops_ingest([{"event": "dance", "at": iso(NOW)}, {"event": "park", "at": iso(NOW)}, "text",
                     {"event": "park", "at": iso(NOW), "lat": "95", "lon": "0"},
                     {"event": "park", "at": iso(NOW - 70 * 86400), "lat": 45, "lon": -123}], now=NOW)
check("unusable events are dropped with a reason and ok=True", all(x["ok"] and x.get("dropped") for x in r), r)
check("nothing was applied", tv._stops_read()["pending"] is None)
r = tv.stops_ingest([{"event": "park", "at": iso(NOW), "lat": "45,93", "lon": "-123,96"}], now=NOW)
check("a decimal comma from a phone in another locale is read", r[0].get("tracking") is True, r)

# ---- a time in the future or missing falls back to arrival ------------------------------------------------------
reset()
r = tv.stops_ingest([{"event": "park", "at": iso(NOW + 3 * H), "lat": 45.3, "lon": -123.3}], now=NOW)
check("a clock far ahead is clamped to arrival, with a note", r[0].get("time_note") and abs(tv._stops_read()["pending"]["at"] - NOW) < 2, r)
r = tv.stops_ingest([{"event": "drive"}], now=NOW + 5)
check("no time at all means arrival time, without a complaint", r[0]["ok"] and "time_note" not in r[0], r)

# ---- the 36 h rule uses event time --------------------------------------------------------------------------------
reset()
tv.stops_ingest([ev("park", NOW - 80 * H, 45.4, -123.4)], now=NOW)
r = tv.stops_ingest([ev("drive", NOW - 1 * H)], now=NOW)
check("a park never answered for over 36 h is dropped", r[0].get("logged") is False, r)
reset()
tv.stops_ingest([ev("park", NOW - 50 * H, 45.4, -123.4)], now=NOW)
r = tv.stops_ingest([ev("drive", NOW - 45 * H)], now=NOW)
check("a long-ago hike uploaded late is still a stop", r[0].get("logged") is True and r[0]["minutes"] == 300, r)

# ---- where the Mac WAS decides 'parked at the RV' -------------------------------------------------------------------
reset()
A, B = (45.99, -123.90), (46.50, -123.00)             # the old campsite and the new one, ~90 km apart
tv.LOCATION_HISTORY_FILE.write_text("%d|%f|%f|60\n" % (NOW - 4 * H, A[0], A[1]))
LOC.write_text("%f|%f|60|Elsewhere|OR" % B)             # right now the Mac is at B, with a fresh fix
r = tv.stops_ingest([ev("park", NOW - 4 * H, A[0], A[1])], now=NOW)
check("parking beside the Mac's position at THAT time is ignored, though the Mac has moved since",
      r[0].get("tracking") is False, r)
reset()
LOC.write_text("%f|%f|60|Elsewhere|OR" % B)
r = tv.stops_ingest([ev("park", NOW - 6 * H, A[0], A[1])], now=NOW)
check("an old event with no history is never treated as 'at the RV'", r[0].get("tracking") is True, r)
reset()
LOC.write_text("%f|%f|60|Seaside|OR" % A)
r = tv.stops_ingest([ev("park", NOW - 600, A[0], A[1])], now=NOW)
check("a recent event with no history uses the current fix (the old behaviour)", r[0].get("tracking") is False, r)
reset()
tv.LOCATION_HISTORY_FILE.write_text("%d|%f|%f|60\n" % (NOW - 4 * H, A[0], A[1]))
r = tv.stops_ingest([ev("park", NOW - 4 * H + 3 * 3600, A[0], A[1])], now=NOW)
check("a history fix more than 45 min from the event is not used", r[0].get("tracking") is True, r)

# ---- the endpoint ----------------------------------------------------------------------------------------------------
reset()
subprocess.run([sys.executable, str(ROOT / "tvserver.py"), "token", "add", "carphone", "--stops"],
               env=dict(os.environ), capture_output=True, text=True)
tok = json.loads((TMP / "tokens.json").read_text())["carphone"]["token"]
try:
    from fastapi.testclient import TestClient
    cl = TestClient(tv.app)
    url = "/display/stop?stoptoken=" + tok
    one = json.dumps(ev("park", NOW - 3 * H, 45.61, -123.61))
    r = cl.post(url, content=one + "\n", headers={"Content-Type": "text/plain"})
    check("one event as a text/plain body, trailing newline and all", r.status_code == 200 and r.json().get("ok") is True
          and r.json().get("tracking") is True, (r.status_code, r.text))
    r = cl.post(url, content=("﻿" + json.dumps(ev("drive", NOW - 1 * H))).encode("utf-8"),
                headers={"Content-Type": "application/octet-stream"})
    check("one event as an octet-stream body with a BOM", r.status_code == 200 and r.json().get("logged") is True, (r.status_code, r.text))
    r = cl.post(url, content=one)
    check("sending the file again says ok and duplicate", r.json().get("ok") is True and r.json().get("duplicate") is True, r.text)
    r = cl.post(url, content=json.dumps([ev("park", NOW - 10 * 60, 45.7, -123.7), ev("drive", NOW - 5 * 60)]))
    check("a list is a batch", r.status_code == 200 and r.json()["received"] == 2 and r.json()["ok"], r.text)
    reset()                                         # the plain-line form, from a clean slate
    line = "park|%s|45.88|-123.88" % iso(NOW - 3 * H)
    r = cl.post(url, content=line + "\n", headers={"Content-Type": "text/plain"})
    check("one event as a plain line, trailing newline and all", r.status_code == 200 and r.json().get("ok") is True
          and r.json().get("tracking") is True, (r.status_code, r.text))
    r = cl.post(url, content="drive|%s\n" % iso(NOW - 1 * H))
    check("a drive line needs no place", r.status_code == 200 and r.json().get("logged") is True, (r.status_code, r.text))
    r = cl.post(url, content="park|%s|45.77|-123.77\n\ndrive|%s\n" % (iso(NOW - 50 * 60), iso(NOW - 20 * 60)))
    check("several lines are a batch", r.status_code == 200 and r.json()["received"] == 2 and r.json()["ok"], r.text)
    r = cl.post(url, content="park|%s|45.88|-123.88" % iso(NOW - 3 * H))
    check("a line sent again is a duplicate", r.json().get("duplicate") is True, r.text)
    r = cl.post(url, content="hello|world")
    check("a line that is not an event is refused (400)", r.status_code == 400, (r.status_code, r.text))
    r = cl.post(url, content="this is not json")
    check("a body that is not JSON is refused (400), so the phone keeps its file",
          r.status_code == 400 and "ok" not in r.json(), (r.status_code, r.text))
    check("...and the Tools page can show why", "not JSON" in (tv._stops_read()["last_error"] or {}).get("msg", ""), tv._stops_read())
    r = cl.post(url, content=json.dumps([{"event": "drive"}] * 501))
    check("more than 500 events at once is refused", r.status_code == 413, r.status_code)
    r = cl.post("/display/stop?event=drive&stoptoken=" + tok)
    check("the old query-only call still works", r.status_code == 200 and r.json().get("ok") is True, (r.status_code, r.text))
    r = cl.post("/display/stop?event=park&stoptoken=" + tok)
    check("park without a place is refused", r.status_code == 400, r.status_code)
    r = cl.post("/display/stop?stoptoken=" + tok)
    check("nothing at all is refused", r.status_code == 400, r.status_code)
    r = cl.post("/display/stop?stoptoken=wrong", content=one)
    check("a wrong token is still refused", r.status_code == 401, r.status_code)
    r = cl.post("/display/stop?event=drive&at=" + iso(NOW - 2 * H).replace("+", "%2B") + "&stoptoken=" + tok)
    check("at= in the query works too", r.status_code == 200 and r.json().get("ok"), (r.status_code, r.text))
    r = cl.get("/tools/stops/status", headers={"Authorization": "Bearer t"})
    j = r.json()
    check("the Tools page status", r.status_code == 200 and j["last_upload"] and "now" in j and j["stops"] >= 1, (r.status_code, r.text))
    r = cl.get("/tools/stops/status?stoptoken=" + tok)
    check("a --stops token cannot read the status", r.status_code in (401, 403), r.status_code)
except ImportError as e:
    print("skip endpoint checks (" + str(e) + ")")

# ---- the location agent keeps a history ------------------------------------------------------------------------------
agent = TMP / "agent"
(agent / "bin").mkdir(parents=True)
shutil.copy(ROOT / "locationagent.sh", agent / "locationagent.sh")
fake = agent / "bin" / "CoreLocationCLI"
fake.write_text("#!/bin/bash\necho '45.123|-123.456|60|Seaside|OR'\n")
fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
env = dict(os.environ, PATH=str(agent / "bin") + os.pathsep + os.environ["PATH"])
subprocess.run(["bash", str(agent / "locationagent.sh")], env=env, check=True)
hist = (agent / ".run" / "location_history.log").read_text().splitlines()
parts = hist[-1].split("|")
check("each fix is appended as epoch|lat|lon|accuracy", len(parts) == 4 and abs(float(parts[0]) - time.time()) < 30
      and parts[1:] == ["45.123", "-123.456", "60"], hist)
check("location.raw is still written as before", (agent / ".run" / "location.raw").read_text().strip() == "45.123|-123.456|60|Seaside|OR")
(agent / ".run" / "location_history.log").write_text("1|1|1|1\n" * 2600)
subprocess.run(["bash", str(agent / "locationagent.sh")], env=env, check=True)
n = len((agent / ".run" / "location_history.log").read_text().splitlines())
check("the history is trimmed past 2500 lines", 1900 <= n <= 2002, n)
tv.LOCATION_HISTORY_FILE = agent / ".run" / "location_history.log"
tv.LOCATION_HISTORY_FILE.write_text("%d|45.123|-123.456|60\n" % (NOW - 600))
got = tv._mac_fix_at(NOW - 500)
check("...and finds the nearest fix", got and abs(got["lat"] - 45.123) < 1e-9, got)

print()
print("FAILED: " + ", ".join(fails) if fails else "ALL OK")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if fails else 0)
