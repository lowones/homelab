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

# ---- reminder rules: column G hides when it is only a past date; column L (INFO) also fires -----
D = datetime.date
past = tv.note_is_past_date
check("full past date is past", past("6/25/2026", today) and past("10/21/2026", today))
check("today's date is not past", not past("10/22/2026", today))
check("future date is not past", not past("10/23/2026", today) and not past("2/24/2027", today))
check("2-digit year and ISO dates work", past("6/25/26", today) and past("2026-06-25", today)
      and not past("2/24/27", today) and not past("2026-11-01", today))
check("surrounding spaces do not matter", past("  6/25/2026 ", today))
check("a bare m/d (no year) is not decided: shows", not past("6/25", today))
check("a date plus text shows", not past("6/25/2026 call KOA", today) and not past("call KOA 6/25/2026", today))
check("plain text shows", not past("Book Bandon", today) and not past("", today))
check("an impossible date shows", not past("13/45/2026", today) and not past("2/30/2026", today))

check("INFO column is L", tv.INFO_COL == 11 and tv.REMINDER_COL == 6)
infotext = (
    "W,DATE,DAY,CITY,CAMP,DIST,REMINDER,CONF,$,TT,ADDR,INFO\n"
    "1,10/22/2026,Thu,,,,,,,,,Dentist at 9\n"                      # INFO in column L (index 11)
    "1,10/23/2026,Fri,,,,6/25/2026,,,,,\n"
    "1,10/24/2026,Sat,,,,,,,,,\"Turkey Trot,\nCloverdale\"\n"       # multi-line INFO cell
    "1,10/25/2026,Sun,,,,Text\n")                                    # short row: no L at all
i = tv.parse_infos(infotext)
check("parse_infos reads column L of dated rows only", set(i) == {D(2026, 10, 22), D(2026, 10, 24)}, i)
check("INFO line breaks collapse", i[D(2026, 10, 24)] == "Turkey Trot, Cloverdale", i.get(D(2026, 10, 24)))
check("parse_reminders still reads column G only", tv.parse_reminders(infotext)[D(2026, 10, 23)] == "6/25/2026")

def put(g=None, l=None, src="fake-a", d=today):
    tv._itin["reminders"][src] = {d: g} if g is not None else {}
    tv._itin["infos"][src] = {d: l} if l is not None else {}

tv.ITIN_SOURCES = ["fake-a", "fake-b"]
tv._itin["next"] = {"fake-a": 9e12, "fake-b": 9e12}
tv._itin["rows"] = {"fake-a": [], "fake-b": []}
tv._itin["reminders"] = {}
tv._itin["infos"] = {}
put(g="6/25/2026")
check("past date alone: no card", tv.reminder_today(today) is None)
put(g="6/25/2026", l="")
check("past date and empty INFO: no card", tv.reminder_today(today) is None)
put(g="10/22/2026")
check("today's date alone still shows", tv.reminder_today(today)["text"] == "10/22/2026")
put(g="2/24/2027")
check("future date alone shows", tv.reminder_today(today)["text"] == "2/24/2027")
put(g="Book Bandon")
check("plain text alone shows", tv.reminder_today(today)["text"] == "Book Bandon")
put(l="Dentist at 9")
check("INFO alone shows", tv.reminder_today(today)["text"] == "Dentist at 9")
put(g="6/25/2026", l="Dentist at 9")
check("INFO shows even when G is a past date (and only INFO)", tv.reminder_today(today)["text"] == "Dentist at 9")
put(g="Book Bandon", l="Dentist at 9")
check("both: G first, INFO on its own line", tv.reminder_today(today)["text"] == "Book Bandon\nDentist at 9")
put(g="2/24/2027", l="Dentist at 9")
check("future date and INFO both show", tv.reminder_today(today)["text"] == "2/24/2027\nDentist at 9")
check("whitespace-only INFO is blank (dropped when the sheet is read)",
      tv.parse_infos("W,DATE,DAY,CITY,CAMP,D,G,H,I,J,K,L\n1,10/22/2026,Thu,,,,,,,,,   \n") == {})
put(g="Book Bandon", l="Dentist at 9", src="fake-a")
put(g="Book Bandon", l="Dentist at 9", src="fake-b")
check("the same G and INFO on two tabs show once", tv.reminder_today(today)["text"] == "Book Bandon\nDentist at 9")
put(g="Book Bandon", l="Dentist at 9", src="fake-a")
put(g="Call Newport", l="Vet at 3", src="fake-b")
check("different text on two tabs: joined per line", tv.reminder_today(today)["text"] ==
      "Book Bandon / Call Newport\nDentist at 9 / Vet at 3")
put(g="6/25/2026", l="Dentist at 9", src="fake-a")
put(g="Call Newport", l=None, src="fake-b")
check("a hidden past date on one tab does not hide the other tab", tv.reminder_today(today)["text"] ==
      "Call Newport\nDentist at 9")
put(g="Book", l="x" * 1000, src="fake-a")
put(g=None, l=None, src="fake-b")
t = tv.reminder_today(today)["text"]
check("combined text is cut to the limit and keeps the first line", len(t) == tv.REMINDER_MAX and t.endswith("…")
      and t.startswith("Book\nxxx"), len(t))
put(g="6/25/2026", l="Dentist", src="fake-a", d=today + datetime.timedelta(days=1))
check("only today's row counts for G and INFO", tv.reminder_today(today) is None)

shutil.rmtree(TMP, ignore_errors=True)
print("\nSOME FAILED" if fails else "\nALL OK")
sys.exit(1 if fails else 0)
