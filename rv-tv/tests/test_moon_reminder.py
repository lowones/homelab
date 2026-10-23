#!/usr/bin/env python3
"""Checks for the moon phase and the column G reminder. Plain python, no network:
    python3 tests/test_moon_reminder.py          (needs the same packages as tvserver.py)"""
import calendar
import datetime
import importlib.util
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="rvtv-moon-"))
os.environ.update(TV_TOKEN="t", TV_ENV_FILE=str(TMP / "none.env"), TV_LOCATION_FILE=str(TMP / "location.raw"),
                  TV_TOKENS_FILE=str(TMP / "tokens.json"), TV_ITINERARY="")
spec = importlib.util.spec_from_file_location("tvserver", ROOT / "tvserver.py")
tv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tv)

fails = []


def check(name, ok, extra=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else "  " + str(extra)))
    if not ok:
        fails.append(name)


utc = lambda *a: calendar.timegm(datetime.datetime(*a).timetuple())

# ---- moon: published new and full moons (UTC), and the shape of the answer -----------------------
m = tv.moon_info(utc(2026, 10, 10, 15, 50))
check("new moon 2026-10-10", m["name"] == "New Moon" and m["percent"] <= 1, m)
m = tv.moon_info(utc(2026, 10, 26, 4, 12))
check("full moon 2026-10-26", m["name"] == "Full Moon" and m["percent"] >= 99, m)
m = tv.moon_info(utc(2026, 1, 3, 10, 3))
check("full moon 2026-01-03", m["name"] == "Full Moon" and m["percent"] >= 99, m)
m = tv.moon_info(utc(2026, 10, 22, 20, 50))
check("Oct 22 2026 evening is a waxing gibbous", m["name"] == "Waxing Gibbous" and m["waxing"] and 80 <= m["percent"] <= 92, m)
names = set()
for h in range(0, 30 * 24, 6):                       # a whole lunar month in 6-hour steps
    x = tv.moon_info(utc(2026, 10, 1) + h * 3600)
    names.add(x["name"])
    assert 0 <= x["percent"] <= 100 and x["icon"] == tv.MOON_ICONS[x["name"]]
check("all eight phase names turn up in a month", names == set(tv.MOON_ICONS), names)
check("waning after full", not tv.moon_info(utc(2026, 11, 2))["waxing"])

# ---- reminder: column G of the dated rows --------------------------------------------------------
today = datetime.date(2026, 10, 22)
csvtext = (
    "W,DATE,DAY,CITY,CAMP,H,REMINDER\n"
    "1,10/21/2026,Wed,\"Newport, OR\",Site A,,Yesterday note\n"
    "1,10/22/2026,Thu,,,,\"Book Bandon\nfor June 12,   call by Fri\"\n"      # multi-line cell, extra spaces
    "1,10/23/2026,Fri,,,,\n"                                                  # empty G
    "1,10/24/2026,Sat,\"Coos Bay, OR\"\n"                                     # short row: no column G at all
    "1,10/25/2026,Sun,,,,Second one\n1,10/25/2026,Sun,,,,Third one\n"         # same date twice
    "not a date row,,,,,,ignored\n")
r = tv.parse_reminders(csvtext)
check("header and junk rows are skipped", set(r) == {datetime.date(2026, 10, 21), today, datetime.date(2026, 10, 25)}, r)
check("line breaks and runs of spaces collapse", r[today] == "Book Bandon for June 12, call by Fri", r.get(today))
check("two rows with the same date are both kept", r[datetime.date(2026, 10, 25)] == "Second one / Third one")
check("parse_itinerary still reads the same rows", [d for d, _c, _n in tv.parse_itinerary(csvtext)][:2] ==
      [datetime.date(2026, 10, 21), today])

tv.ITIN_SOURCES = ["fake-a", "fake-b"]
tv._itin["next"] = {"fake-a": 9e12, "fake-b": 9e12}                  # pretend both were just fetched
tv._itin["rows"] = {"fake-a": [], "fake-b": []}
tv._itin["reminders"] = {"fake-a": {today: "Book Bandon"}, "fake-b": {today: "Book Bandon"}}
check("the same text on two tabs shows once", tv.reminder_today(today)["text"] == "Book Bandon")
tv._itin["reminders"] = {"fake-a": {today: "Book Bandon"}, "fake-b": {today: "Call Newport"}}
check("different text on two tabs shows both", tv.reminder_today(today)["text"] == "Book Bandon / Call Newport")
tv._itin["reminders"] = {"fake-a": {today + datetime.timedelta(days=1): "tomorrow"}}
check("nothing for today -> None", tv.reminder_today(today) is None)
tv._itin["reminders"] = {"fake-a": {today: "x" * 1000}}
t = tv.reminder_today(today)["text"]
check("long text is cut to the limit with an ellipsis", len(t) == tv.REMINDER_MAX and t.endswith("…"), len(t))

# a failed refresh keeps the last good copy of the sheet
tv._itin["reminders"] = {"fake-a": {today: "kept"}}
tv._itin["rows"] = {"fake-a": []}
tv._itin["next"] = {"fake-a": 0}
tv.read_source = lambda src: (_ for _ in ()).throw(OSError("offline"))
check("failed refresh still serves the last good reminder", tv.reminder_today(today)["text"] == "kept")
check("and the error is recorded", tv._itin["error"] == "offline", tv._itin["error"])

shutil.rmtree(TMP, ignore_errors=True)
print("\nSOME FAILED" if fails else "\nALL OK")
sys.exit(1 if fails else 0)
