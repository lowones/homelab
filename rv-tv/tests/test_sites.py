#!/usr/bin/env python3
"""Checks for Tools > Network test > Set location: saved sites, tagging runs, the guarded retag, and the car-park log.
Plain python, no network:   python3 tests/test_sites.py   (needs the same packages as tvserver.py)"""
import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="rvtv-sites-"))
os.environ.update(TV_TOKEN="t", TV_ENV_FILE=str(TMP / "none.env"), TV_LOCATION_FILE=str(TMP / "location.raw"),
                  TV_TOKENS_FILE=str(TMP / "tokens.json"), TV_ITINERARY="", TV_NETTEST_DIR=str(TMP / "nettest"),
                  TV_SITES_FILE=str(TMP / "sites.json"))
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("tvserver", ROOT / "tvserver.py")
tv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tv)
tv.RUN_DIR = TMP
tv.STOPS_FILE = TMP / "stops.json"
import nettest as nt

fails = []


def check(name, ok, extra=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else "  " + str(extra)))
    if not ok:
        fails.append(name)


NOW = time.time()
H = 3600
CLOV = (45.2296, -123.9684)      # Cloverdale
BEACH = (44.5473, -124.0702)     # South Beach, 76 km away
nt.RUN_DIR.mkdir(parents=True)


def add_run(rid, ago_h, lat, lon, place, fix_age=60, site=None, short=False):
    rec = {"v": 1, "id": rid, "started": NOW - ago_h * H, "place": place, "lat": lat, "lon": lon, "accuracy_m": 40,
           "fix_age_s": fix_age, "site": site, "short": short, "sent": 600, "bins": {}}
    with open(str(nt.history_path()), "a") as f:
        f.write(json.dumps(rec) + "\n")
    d = nt.RUN_DIR / rid
    d.mkdir()
    (d / "result.json").write_text(json.dumps({"id": rid, "site": site}))


add_run("20261025-050000", 30, CLOV[0], CLOV[1], "Cloverdale, OR")                  # before the move, really at Cloverdale
add_run("20261025-132100", 5, CLOV[0], CLOV[1], None, fix_age=18000)                 # after the move, on a stale Cloverdale fix
add_run("20261025-134700", 4.5, BEACH[0], BEACH[1], "South Beach, OR")               # after the move, good fix
add_run("20261023-185400", 6, None, None, "Cloverdale, OR")                          # no position saved

# ---- a fresh fix names the site only when it is on a saved site --------------------------------------------------
fix = {"lat": BEACH[0], "lon": BEACH[1], "age_s": 60}
check("no saved sites: a run has no site", nt.site_tag_for_fix(fix) is None)

# ---- which runs a Set location would tag ----------------------------------------------------------------------------
c = {x["id"]: x for x in nt.retag_candidates(BEACH[0], BEACH[1], NOW)}
check("a run at this spot is ticked", c["20261025-134700"]["suggest"] is True, c)
check("a run on a stale fix from 76 km away is ticked, with the reason", c["20261025-132100"]["suggest"] is True
      and "old" in c["20261025-132100"]["reason"], c["20261025-132100"])
check("a run with a good fix somewhere else is left alone", c["20261025-050000"]["suggest"] is False
      and "km" in c["20261025-050000"]["reason"], c["20261025-050000"])
check("a run with no position is left alone", c["20261023-185400"]["suggest"] is False, c["20261023-185400"])

# ---- Set location --------------------------------------------------------------------------------------------------
try:
    nt.set_site("   ", BEACH[0], BEACH[1], now=NOW)
    check("an empty name is refused", False)
except ValueError:
    check("an empty name is refused", True)
try:
    nt.set_site("x", BEACH[0], BEACH[1], ids=["nope"], now=NOW)
    check("run ids outside the window are refused", False)
except ValueError:
    check("run ids outside the window are refused", True)
res = nt.set_site("South Beach, Newport KOA", BEACH[0], BEACH[1], source="car", now=NOW)
check("saved a new site and tagged the two suggested runs", res["created"] and res["retagged"] == 2 and res["site"]["id"] == "s1", res)
hist = {r["id"]: r for r in nt.read_history()}
check("history has the tag", hist["20261025-134700"]["site"] == {"id": "s1", "name": "South Beach, Newport KOA"}
      and hist["20261025-132100"]["site"]["id"] == "s1", hist["20261025-132100"])
check("the Cloverdale run was not touched", hist["20261025-050000"]["site"] is None)
check("each run's result.json carries it too", json.loads((nt.RUN_DIR / "20261025-132100" / "result.json").read_text())["site"]["id"] == "s1")
check("the sites file records the Set location", len(nt.read_sites()["anchors"]) == 1 and nt.read_sites()["anchors"][0]["retagged"] == 2)
check("what was left alone is reported", {x["id"] for x in res["left"]} == {"20261025-050000", "20261023-185400"}, res["left"])

# ---- later runs: a fresh fix on the site is tagged; a stale one or one elsewhere is not -----------------------------------
check("a fresh fix on the site gets it", nt.site_tag_for_fix({"lat": BEACH[0] + 0.001, "lon": BEACH[1], "age_s": 60}) == {"id": "s1", "name": "South Beach, Newport KOA"})
check("a stale fix names nothing", nt.site_tag_for_fix({"lat": BEACH[0], "lon": BEACH[1], "age_s": 7200}) is None)
check("a fix 76 km away names nothing", nt.site_tag_for_fix({"lat": CLOV[0], "lon": CLOV[1], "age_s": 60}) is None)
(TMP / "location.raw").write_text("%f|%f|40|South Beach|OR" % BEACH)
check("build_result tags a run from the Mac's fix", nt.location_fix()["lat"] is not None
      and nt.site_tag_for_fix(nt.location_fix())["id"] == "s1")

# ---- a second Set location only looks back to the first, and reuses a site it is already on ---------------------------
add_run("20261026-050000", -0.1, BEACH[0], BEACH[1], "South Beach, OR", site=None)          # started just after the first Set location
later = NOW + 0.5 * H
c2 = {x["id"] for x in nt.retag_candidates(BEACH[0], BEACH[1], later)}
check("only runs since the previous Set location are offered", c2 == {"20261026-050000"}, c2)
res2 = nt.set_site("Some other name", BEACH[0] + 0.0005, BEACH[1], now=later)
check("a point on a saved site reuses it and keeps its name", res2["created"] is False and res2["site"]["name"] == "South Beach, Newport KOA"
      and res2["retagged"] == 1, res2)
res3 = nt.set_site("Cloverdale RV Park", CLOV[0], CLOV[1], ids=[], now=later + 1)
check("a new place is a new site; no runs after the last Set location means nothing to tag", res3["created"] and res3["site"]["id"] == "s2" and res3["retagged"] == 0, res3)
check("two sites are saved", [x["id"] for x in nt.read_sites()["sites"]] == ["s1", "s2"])

# ---- backfill: old history can be tagged site by site, whatever order ----------------------------------------------------
add_run("20261022-100000", 80, CLOV[0], CLOV[1], "Cloverdale, OR")
resb = nt.set_site("Cloverdale RV Park", CLOV[0], CLOV[1], ids=["20261022-100000", "20261025-050000", "20261023-185400"],
                   now=later + 2, backfill=True)
check("backfill reaches runs from before the last Set location", resb["retagged"] == 3 and resb["created"] is False, resb)
hb = {r["id"]: r for r in nt.read_history()}
check("...including one with no saved position when it is named", hb["20261023-185400"]["site"]["id"] == "s2")
try:
    nt.set_site("Cloverdale RV Park", CLOV[0], CLOV[1], ids=["20261022-100000"], now=later + 3)
    check("without backfill an old run is refused", False)
except ValueError:
    check("without backfill an old run is refused", True)

# ---- per-site averages in Compare ----------------------------------------------------------------------------------------
def full_run(rid, ago_days, site, loss, lost, rtt, mos, verdict, bins_loss, short=False):
    return {"v": 1, "id": rid, "started": NOW - ago_days * 86400, "place": "X", "lat": 1.0, "lon": 2.0, "accuracy_m": 40, "fix_age_s": 60,
            "site": site, "short": short, "sent": 600, "verdict": verdict, "rank": 1, "mos": mos, "rtt_avg": rtt, "rtt_p95": rtt * 2,
            "jitter_ms": 5.0, "loss_pct": loss, "longest_lost": lost, "udp": True, "targets": ["cloudflare", "oracle"], "bin_s": 10,
            "bins": {"p50": [30.0, 40.0], "p95": [60.0, 80.0], "jitter": [5.0, 5.0], "loss": bins_loss}}
S1, S2 = {"id": "s1", "name": "South Beach 71"}, {"id": "s2", "name": "Cloverdale 38"}
recs = [full_run("r1", 3, S1, 2.0, 3, 50.0, 4.2, "MARGINAL", [0.0, 10.0]),
        full_run("r2", 2, S1, 4.0, 5, 60.0, 4.0, "POOR", [0.0, 20.0]),
        full_run("r3", 1, S1, 6.0, 10, 70.0, 3.8, "POOR", [0.0, None]),
        full_run("r4", 1, S1, 90.0, 99, 999.0, 1.0, "POOR", [90.0, 90.0], short=True),            # short: left out
        full_run("r5", 40, S1, 50.0, 50, 500.0, 2.0, "POOR", [50.0, 50.0]),                        # outside the 30-day window
        full_run("r6", 5, S2, 1.0, 2, 40.0, 4.3, "MARGINAL", [1.0, 1.0]),
        full_run("r7", 1, None, 7.0, 7, 70.0, 3.9, "POOR", [7.0, 7.0])]                              # no site yet
sites, untagged = nt.site_summary(recs, NOW, 30)
check("two sites, newest first, short and old runs left out, the untagged run counted",
      [x["id"] for x in sites] == ["s1", "s2"] and sites[0]["n"] == 3 and untagged == 1, (sites[0]["n"], untagged))
st = sites[0]["stats"]
check("mean and range of loss", abs(st["loss"]["mean"] - 4.0) < 1e-9 and st["loss"]["min"] == 2.0 and st["loss"]["max"] == 6.0, st["loss"])
check("spread (standard deviation) of loss", abs(st["loss"]["sd"] - 1.633) < 0.01, st["loss"])
check("longest dropout keeps its worst", st["lost"]["max"] == 10 and abs(st["lost"]["mean"] - 6.0) < 1e-9, st["lost"])
check("a site with one run has no spread", sites[1]["stats"]["loss"]["sd"] is None)
check("verdicts are counted", sites[0]["verdicts"] == {"Marginal": 1, "Poor": 2}, sites[0]["verdicts"])
check("bins are averaged by position, ignoring lost bins", sites[0]["bins"]["loss"] == [0.0, 15.0], sites[0]["bins"]["loss"])
check("a 7-day window drops the 40-day run only", [x["n"] for x in nt.site_summary(recs, NOW, 7)[0]] == [3, 1])
html = nt.render_compare(recs, 30, NOW)
check("Compare has a Sites section with both sites", "<h2>Sites</h2>" in html and "South Beach 71" in html and "Cloverdale 38" in html)
check("the average is a line you can tick, and it is what the graph starts with",
      'data-id="site:s1"' in html and '"init":["site:s1","site:s2"]' in html and "average of 3 runs" in html)
check("the untagged runs are mentioned", "no site yet" in html)
check("with no sites tagged Compare looks as before", "<h2>Sites</h2>" not in nt.render_compare([full_run("q", 1, None, 1.0, 1, 40.0, 4.0, "GOOD", [0.0, 0.0])], 30, NOW))

# ---- the compare table shows the site name -----------------------------------------------------------------------------
html = nt.render_compare(nt.read_history())
check("compare shows the site name", "South Beach, Newport KOA" in html)

# ---- the CLI --------------------------------------------------------------------------------------------------------------
import io, contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = nt.main(["site", "list"])
check("site list prints the saved sites", rc == 0 and "South Beach, Newport KOA" in buf.getvalue() and "s2" in buf.getvalue(), buf.getvalue())

# ---- the car's park log and the Set location endpoints -------------------------------------------------------------------
(TMP / "location.raw").write_text("%f|%f|40|South Beach|OR" % BEACH)
r0 = tv.stop_event("park", BEACH[0] + 0.0004, BEACH[1] - 0.0003)        # parked at the RV: ignored for stops
check("a park at the RV is still not tracked as a stop", r0["tracking"] is False and tv._stops_read()["pending"] is None, r0)
tv.stop_event("drive", None, None)
tv.stop_event("park", BEACH[0] + 0.0004, BEACH[1] - 0.0003)             # the same spot again
tv.stop_event("park", BEACH[0] + 0.0080, BEACH[1] + 0.0100)             # a spot ~1.4 km away (a store)
tv.stop_event("park", 45.5, -123.0)                                      # far away
d = tv._stops_read()
check("every park is logged, including ones ignored at the RV", len(d["parks"]) == 4 and d["parks"][0]["rv"] is True, d["parks"])

try:
    from fastapi.testclient import TestClient
    cl = TestClient(tv.app)
    hdr = {"Authorization": "Bearer t"}
    r = cl.get("/tools/site/candidates", headers=hdr)
    j = r.json() if r.status_code == 200 else {}
    check("candidates answers", r.status_code == 200, (r.status_code, r.text[:200]))
    check("the Mac's fix and tonight's site come back", j.get("mac", {}).get("lat") == round(BEACH[0], 4) and (j.get("current") or {}).get("id") == "s1", j)
    check("car spots near the Mac, one per place, nearest first, far ones left out",
          [p["dist_m"] < 200 for p in j.get("parks", [])][:1] == [True] and len(j.get("parks", [])) == 2 and j["parks"][0]["dist_m"] <= j["parks"][1]["dist_m"], j.get("parks"))
    check("saved sites near the Mac are listed", [x["id"] for x in j.get("sites", [])] == ["s1"], j.get("sites"))
    r = cl.get("/tools/site/preview?lat=%f&lon=%f" % BEACH, headers=hdr)
    check("preview lists runs with a default tick and a reason", r.status_code == 200 and all("suggest" in x and "reason" in x for x in r.json()["runs"]), r.text[:200])
    r = cl.post("/tools/site/set", headers=hdr, json={"name": "Lot B", "lat": 46.0, "lon": -123.0, "source": "car"})
    check("set saves a new site", r.status_code == 200 and r.json()["created"] is True, (r.status_code, r.text[:200]))
    r = cl.post("/tools/site/set", headers=hdr, json={"lat": 1})
    check("a bad body is a 400", r.status_code == 400, r.status_code)
    r = cl.post("/tools/site/set", headers=hdr, json={"name": "x", "lat": 46.0, "lon": -123.0, "ids": ["nope"]})
    check("unknown run ids are a 400", r.status_code == 400, (r.status_code, r.text[:200]))
    r = cl.get("/tools/site/candidates")
    check("no token, no access", r.status_code in (401, 403), r.status_code)
except ImportError:
    print("skip  endpoint checks (fastapi testclient not installed)")

# ---- the phone page: the Set location view exists and its script is valid JavaScript ----------------------------------------
import re, shutil, subprocess
page = tv.GUEST_PAGE
check("the network test view has a Set location button and a map view",
      "siteOpen()" in page and 'id="v-site"' in page and 'id="site-map"' in page)
scripts = re.findall(r"<script>(.*?)</script>", page, re.S)
if shutil.which("node") and scripts:
    (TMP / "page.js").write_text(scripts[-1])
    chk = subprocess.run(["node", "--check", str(TMP / "page.js")], capture_output=True, text=True)
    check("the page script parses", chk.returncode == 0, chk.stderr[:300])
else:
    print("skip  page script syntax check (node not installed)")

print("\nALL OK" if not fails else "\nFAILED: %s" % ", ".join(fails))
sys.exit(1 if fails else 0)
