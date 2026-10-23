#!/usr/bin/env python3
"""Checks for the Overpass fallback list. Plain python, no network:
    python3 tests/test_overpass_mirrors.py          (needs the same packages as tvserver.py)"""
import importlib.util
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="rvtv-ovp-"))
os.environ.update(TV_TOKEN="t", TV_ENV_FILE=str(TMP / "none.env"), TV_LOCATION_FILE=str(TMP / "location.raw"),
                  TV_TOKENS_FILE=str(TMP / "tokens.json"), TV_ITINERARY="",
                  TV_OVERPASS_URL="https://a.example/api, https://b.example/api ,https://c.example/api")
spec = importlib.util.spec_from_file_location("tvserver", ROOT / "tvserver.py")
tv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tv)

fails = []


def check(name, ok, extra=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else "  " + str(extra)))
    if not ok:
        fails.append(name)


check("comma list is split and trimmed", tv.OVERPASS_URLS == ["https://a.example/api", "https://b.example/api", "https://c.example/api"], tv.OVERPASS_URLS)

calls = []
behaviour = {}                                       # host -> exception, or a dict to return


def fake_http_json(url, timeout=10):
    host = url.split("/")[2]
    calls.append((host, timeout))
    r = behaviour[host]
    if isinstance(r, Exception):
        raise r
    return r


tv.http_json = fake_http_json
good = {"elements": [{"type": "node", "lat": 1, "lon": 2}]}

behaviour.update({"a.example": RuntimeError("curl: (56) The requested URL returned error: 502"), "b.example": good, "c.example": good})
calls.clear()
check("first server fails, second answers", tv.overpass_json("q", 25) is good and [c[0] for c in calls] == ["a.example", "b.example"], calls)
check("each server gets the per-call timeout", all(c[1] == 25 for c in calls), calls)
calls.clear()
tv.overpass_json("q", 25)
check("the server that worked is tried first next time", [c[0] for c in calls] == ["b.example"], calls)

behaviour.update({"b.example": TimeoutError("timed out"), "c.example": {"elements": [], "remark": "runtime error: Query timed out in \"query\" at line 4"}})
calls.clear()
try:
    tv.overpass_json("q", 25)
    check("all failing raises", False)
except RuntimeError as e:
    msg = str(e)
    check("all failing raises, naming every server", "all 3 servers failed" in msg and all(h in msg for h in ("a.example", "b.example", "c.example")), msg)
    check("an empty 200 with a runtime-error remark counts as a failure", "runtime error" in msg, msg)
check("every server was tried once", sorted(c[0] for c in calls) == ["a.example", "b.example", "c.example"], calls)

behaviour.update({"a.example": {"nope": 1}, "b.example": {"elements": [], "remark": ""}, "c.example": RuntimeError("x")})
calls.clear()
res = tv.overpass_json("q", 25)
check("a genuinely empty area (no remark) is a good answer", res == {"elements": [], "remark": ""}, res)

# the cache keeps the last good value while a refresh fails, and says why
bg = tv.BgCache("t", ok_ttl=0, fail_ttl=0)
bg._items["k"] = {"value": None, "error": None, "at": None, "next": 0.0, "busy": True, "touched": 0}
bg._run("k", lambda: [1, 2, 3])
bg._items["k"]["busy"] = True
bg._run("k", lambda: (_ for _ in ()).throw(RuntimeError("overpass: all 2 servers failed (x)")))
e = bg._items["k"]
check("last good value survives a failed refresh", e["value"] == [1, 2, 3] and "all 2 servers" in e["error"], e)

shutil.rmtree(TMP, ignore_errors=True)
print("\nSOME FAILED" if fails else "\nALL OK")
sys.exit(1 if fails else 0)
