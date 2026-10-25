#!/usr/bin/env python3
"""Checks for Tools > Prep for move: the helper (prepmove.py) against stand-in osascript / launchctl / sync, and the
server side (endpoint, plan, launchd job). Nothing real is quit or stopped.
    python3 tests/test_prepmove.py           (needs the packages tvserver.py needs)
The stand-in apps are real throw-away processes: one quits when asked, one only dies to SIGTERM, one ignores both."""
import asyncio
import importlib.util
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="rvtv-prep-"))
HOME = TMP / "home"
STATE = TMP / "state"
BIN = TMP / "bin"
for d in (HOME / "Library" / "LaunchAgents", STATE, BIN):
    d.mkdir(parents=True)
for label in ("camtunnel", "tvlocation", "tvserver"):
    (HOME / "Library" / "LaunchAgents" / ("com.lowone.%s.plist" % label)).write_text("<plist/>")

fails = []


def check(name, ok, extra=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else "  " + str(extra)))
    if not ok:
        fails.append(name)


# ---- stand-in commands ---------------------------------------------------------------------------------------
def fake(name, body):
    p = BIN / name
    p.write_text("#!%s\n%s" % (sys.executable, body))
    p.chmod(p.stat().st_mode | stat.S_IEXEC)


fake("osascript", r'''
import json, os, signal, sys
st = os.environ["FAKE_STATE"]
args = sys.argv[1:]
def log(line):
    with open(st + "/calls.log", "a") as f:
        f.write(line + "\n")
if os.path.exists(st + "/no_osascript"):
    sys.stderr.write("not authorized\n"); sys.exit(1)
if len(args) == 2 and "System Events" in args[1]:
    log("list")
    sys.stdout.write(open(st + "/apps.txt").read()); sys.exit(0)
if len(args) >= 4 and "to quit" in args[3]:
    name = args[-1]
    log("quit " + name)
    behavior = json.load(open(st + "/behavior.json")).get(name, "quits")
    pids = {l.split("\t")[0]: int(l.split("\t")[1]) for l in open(st + "/apps.txt").read().splitlines() if l}
    if behavior == "quits" and name in pids:
        os.kill(pids[name], signal.SIGKILL)
    sys.exit(0)
if len(args) == 4:                                  # the iMessage send: -e SCRIPT to body
    log("text " + args[2])
    with open(st + "/texts.log", "a") as f:
        f.write(json.dumps({"to": args[2], "body": args[3]}) + "\n")
    sys.exit(0)
log("osascript? " + repr(args)); sys.exit(0)
''')
fake("launchctl", r'''
import os, sys
st = os.environ["FAKE_STATE"]
with open(st + "/calls.log", "a") as f:
    f.write("launchctl " + " ".join(sys.argv[1:]) + "\n")
sys.exit(int(open(st + "/launchctl_rc").read()) if os.path.exists(st + "/launchctl_rc") else 0)
''')
fake("sync", r'''
import os
with open(os.environ["FAKE_STATE"] + "/calls.log", "a") as f:
    f.write("sync\n")
''')
ENV = dict(os.environ, PATH=str(BIN) + os.pathsep + os.environ["PATH"], FAKE_STATE=str(STATE), HOME=str(HOME),
           TV_RUN_DIR=str(TMP / "run"), TV_PREP_QUIT_WAIT="3", TV_PREP_TERM_WAIT="2", TV_PREP_TEXT_WAIT="0")
os.environ.update(ENV)
(TMP / "run").mkdir()

procs = {}


def spawn_apps():
    """Real processes standing in for apps. The reaper threads stop dead ones lingering as zombies."""
    for p in procs.values():
        try:
            p.kill()
        except OSError:
            pass
    procs.clear()
    code = {"Safari": "import time; time.sleep(60)", "Slack": "import time; time.sleep(60)",
            "Messages": "import time; time.sleep(60)", "Finder": "import time; time.sleep(60)",
            "Tailscale": "import time; time.sleep(60)",
            "Stubborn": "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"}
    for name, c in code.items():
        p = subprocess.Popen([sys.executable, "-c", c], stdin=subprocess.DEVNULL)
        procs[name] = p
        threading.Thread(target=p.wait, daemon=True).start()
    time.sleep(0.3)
    (STATE / "apps.txt").write_text("\n".join("%s\t%d" % (n, p.pid) for n, p in procs.items()) + "\n")
    (STATE / "behavior.json").write_text(json.dumps({"Safari": "quits", "Messages": "quits", "Slack": "ignores", "Stubborn": "ignores"}))
    for f in ("calls.log", "texts.log", "launchctl_rc", "no_osascript"):
        if (STATE / f).exists():
            (STATE / f).unlink()


def calls():
    f = STATE / "calls.log"
    return f.read_text().splitlines() if f.exists() else []


def running(name):
    return procs[name].poll() is None


def helper(*args):
    return subprocess.run([sys.executable, str(ROOT / "prepmove.py")] + list(args), env=ENV, capture_output=True, text=True, timeout=60)


def report():
    return json.loads((TMP / "run" / "prepmove-report.json").read_text())


PLAN = {"self_to": ["+15555550100"], "destination_text": "Move day: heading to Cloverdale, OR\n1 Main St\nhttps://maps.apple.com/?dirflg=d&daddr=1%20Main%20St",
        "warnings": ["TV did not confirm power off (unreachable)."], "imessage_script": "on run argv\nend run", "keep_extra": ["Keepme"]}

# ---- dry run --------------------------------------------------------------------------------------------------
spawn_apps()
plan_f = TMP / "plan.json"
plan_f.write_text(json.dumps(dict(PLAN, dry_run=True)))
r = helper("--plan", str(plan_f))
rep = report()
check("dry run exits cleanly", r.returncode == 0, (r.returncode, r.stdout, r.stderr))
check("dry run lists what it would quit", set(rep["quit"]) == {"Safari", "Slack", "Stubborn", "Messages"} and rep["dry_run"], rep)
check("dry run keeps Finder and Tailscale", set(rep["kept"]) == {"Finder", "Tailscale"}, rep)
check("dry run changes nothing", not any(c.startswith(("quit", "text", "sync")) for c in calls())
      and all(running(n) for n in procs) and not (STATE / "texts.log").exists(), calls())
check("(a job that was started by tvserver removes its own launchd entry, even after a dry run)",
      any("bootout" in c and c.endswith("com.lowone.prepmove") for c in calls()), calls())
r = helper("--dry-run")
check("--dry-run by hand needs no plan", r.returncode == 0 and "DRY RUN" in r.stdout, (r.stdout, r.stderr))

# ---- the real run -----------------------------------------------------------------------------------------------
spawn_apps()
plan_f.write_text(json.dumps(PLAN))
t0 = time.time()
r = helper("--plan", str(plan_f))
rep = report()
c = calls()
check("real run exits cleanly", r.returncode == 0, (r.returncode, r.stdout, r.stderr))
check("Safari quit politely, Slack died to SIGTERM", not running("Safari") and not running("Slack"))
check("what ignores both is left alone and reported", running("Stubborn") and rep["refused"] == ["Stubborn"], rep)
check("Finder and Tailscale are never touched", running("Finder") and running("Tailscale") and "quit Finder" not in c and "quit Tailscale" not in c, c)
check("the three agents are stopped, in order",
      [x.rsplit("/", 1)[-1] for x in c if x.startswith("launchctl bootout") and "prepmove" not in x]
      == ["com.lowone.camtunnel", "com.lowone.tvlocation", "com.lowone.tvserver"], c)
first_quit = next(i for i, x in enumerate(c) if x.startswith("quit "))
check("agents are stopped before any app is quit", max(i for i, x in enumerate(c) if "tvserver" in x) < first_quit, c)
check("Messages stays up until the text is out", running("Messages") is False
      and c.index("text +15555550100") < c.index("quit Messages"), c)
tx = [json.loads(x) for x in (STATE / "texts.log").read_text().splitlines()]
check("exactly one text, to the number given", len(tx) == 1 and tx[0]["to"] == "+15555550100", tx)
body = tx[0]["body"]
check("the text names what would not quit", "Stubborn" in body and "would not quit" in body, body)
check("the text carries the Apple Maps link", "https://maps.apple.com/?dirflg=d&daddr=1%20Main%20St" in body, body)
check("the text carries the warnings from the server", "TV did not confirm power off" in body, body)
check("sync ran before and after", c.count("sync") >= 3, c)
check("the report says what happened", rep["text"] == "sent" and rep["done"] and not rep["dry_run"]
      and set(rep["quit"]) >= {"Safari", "Slack", "Messages"}, rep)
check("it removes its own launchd entry last", c[-1].endswith("bootout gui/%d/com.lowone.prepmove" % os.getuid()) or
      c[-1].startswith("list") and c[-2].endswith("com.lowone.prepmove"), c[-4:])
check("it finishes well inside the waits (no hanging)", time.time() - t0 < 25, time.time() - t0)

# ---- a clean prep says so ---------------------------------------------------------------------------------------
spawn_apps()
procs["Stubborn"].kill()
(STATE / "apps.txt").write_text("\n".join("%s\t%d" % (n, p.pid) for n, p in procs.items() if n != "Stubborn") + "\n")
time.sleep(0.2)
plan_f.write_text(json.dumps(PLAN))
helper("--plan", str(plan_f))
body = json.loads((STATE / "texts.log").read_text().splitlines()[0])["body"]
check("with nothing refusing, the text says safe to cut power", "Safe to cut power" in body and "would not quit" not in body, body)

# ---- extra keep names, no number, and a Mac that will not list apps ------------------------------------------------
spawn_apps()
(STATE / "apps.txt").write_text((STATE / "apps.txt").read_text() + "Keepme\t%d\n" % procs["Finder"].pid)
plan_f.write_text(json.dumps(dict(PLAN, self_to=[])))
helper("--plan", str(plan_f))
rep = report()
check("TV_PREP_KEEP names are left open", "Keepme" not in rep["quit"] and "quit Keepme" not in calls(), rep)
check("no TV_TEXT_SELF: no text, and the report says so", not (STATE / "texts.log").exists() and rep["text"].startswith("skipped"), rep)
spawn_apps()
(STATE / "no_osascript").write_text("1")
r = helper("--plan", str(plan_f))
rep = report()
check("if macOS refuses to list apps it says so and still stops the agents",
      r.returncode == 0 and any("cannot list apps" in e for e in rep["errors"]) and len(rep["stopped"]) == 3, (r.stdout, r.stderr, rep))

# ---- by hand ------------------------------------------------------------------------------------------------------
spawn_apps()
r = helper()
check("a real run by hand is refused", r.returncode == 2 and not any(x.startswith("quit") for x in calls()), (r.returncode, r.stderr))
spawn_apps()
r = helper("--resume")
c = calls()
boot = [x for x in c if x.startswith("launchctl bootstrap")]
check("--resume loads the agents again, tvserver first",
      len(boot) == 3 and boot[0].endswith("com.lowone.tvserver.plist"), boot)
r = subprocess.run(["bash", str(ROOT / "tv"), "prepmove", "--dry-run"], env=dict(ENV, TV_ENV=str(TMP / "none.env")),
                   capture_output=True, text=True, timeout=60)
check("`tv prepmove --dry-run` reaches the helper without a token", r.returncode == 0 and "DRY RUN" in r.stdout, (r.returncode, r.stdout, r.stderr))

# ---- the server side ---------------------------------------------------------------------------------------------------
os.environ.update(TV_TOKEN="t", TV_ENV_FILE=str(TMP / "none.env"), TV_LOCATION_FILE=str(TMP / "location.raw"),
                  TV_TOKENS_FILE=str(TMP / "tokens.json"), TV_ITINERARY="", TV_TEXT_SELF="+15555550100",
                  TV_TEXT_TO="+15555550199", TV_PREP_KEEP="Keepme, Other")
spec = importlib.util.spec_from_file_location("tvserver", ROOT / "tvserver.py")
tv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tv)
tv.RUN_DIR = TMP / "run"
check("TV_TEXT_SELF and TV_PREP_KEEP are read", tv.TEXT_SELF == ["+15555550100"] and tv.PREP_KEEP == ["Keepme", "Other"])

NAV = {"move_day": True, "target": {"label": "Cloverdale, OR", "address": "1 Main St, Cloverdale, OR", "date": "2026-10-23"},
       "route": {"miles": 75, "minutes": 141}, "here": {"label": "Seaside, OR"}}
cap = {"friend": 0, "off": 0, "kiosk": 0, "plan": None}


async def fake_friend(force=False):
    cap["friend"] += 1
    return {"sent": ["+15555550199"], "text": "x"}


async def fake_nav():
    return NAV


async def fake_call(uri, payload=None, expect_close=False):
    cap["off"] += 1
    return {}


def fake_spawn(plan):
    cap["plan"] = plan
    return "launchd"


tv.text_destination, tv.get_nav, tv.call = fake_friend, fake_nav, fake_call
tv.kiosk_stop = lambda: cap.__setitem__("kiosk", cap["kiosk"] + 1)
tv.prep_spawn = fake_spawn
_sleep = asyncio.sleep


async def quick_sleep(s, *a, **k):
    return await _sleep(0)
tv.asyncio.sleep = quick_sleep


def wait_done():
    for _ in range(100):
        if not tv._prep["running"]:
            return True
        time.sleep(0.05)
    return False


try:
    from fastapi.testclient import TestClient
    A = {"Authorization": "Bearer t"}
    with TestClient(tv.app) as cl:
        tv.IS_MAC = False
        r = cl.post("/tools/prepmove/start", headers=A)
        check("not on a Mac: refused (501)", r.status_code == 501, (r.status_code, r.text))
        tv.IS_MAC = True
        tv.PREP_ON = False
        r = cl.post("/tools/prepmove/start", headers=A)
        check("TV_PREPMOVE=off: refused (501)", r.status_code == 501, (r.status_code, r.text))
        tv.PREP_ON = True
        r = cl.post("/tools/prepmove/start?dry_run=1", headers=A)
        ok = wait_done()
        p = cap["plan"]
        check("dry run: replies at once and runs the helper in dry-run mode", r.status_code == 200 and r.json()["dry_run"] and ok
              and p and p["dry_run"] is True, (r.status_code, r.text, p))
        check("dry run: no friend text, no TV off, kiosk left alone", cap["friend"] == 0 and cap["off"] == 0 and cap["kiosk"] == 0, cap)
        cap["plan"] = None
        r = cl.post("/tools/prepmove/start", headers=A)
        ok = wait_done()
        p = cap["plan"]
        check("real run: starts and finishes the server part", r.status_code == 200 and ok and p, (r.status_code, r.text, p))
        check("real run: friend text, TV off, dashboard closed", cap["friend"] == 1 and cap["off"] == 1 and cap["kiosk"] == 1, cap)
        check("the plan texts you, with the Apple Maps link and the route", p["self_to"] == ["+15555550100"]
              and "https://maps.apple.com/?dirflg=d&daddr=" in p["destination_text"] and "75 mi" in p["destination_text"], p)
        check("the plan carries the iMessage script and the keep list", "Messages" in p["imessage_script"] and p["keep_extra"] == ["Keepme", "Other"], p)
        check("the reply says who gets texted", r.json()["text_to_you"] is True and r.json()["text_to_friend"] is True, r.json())
        tv._prep.update(running=True, started=time.time())
        r = cl.post("/tools/prepmove/start", headers=A)
        check("a second tap while one runs: 409", r.status_code == 409, r.status_code)
        tv._prep["running"] = False
        # failures on the way do not stop the prep; they ride along in the text
        async def boom_friend(force=False):
            raise RuntimeError("Messages did not answer")

        async def boom_call(*a, **k):
            raise tv.TVUnreachable("off")
        tv.text_destination, tv.call = boom_friend, boom_call
        cap["plan"] = None
        cl.post("/tools/prepmove/start", headers=A)
        wait_done()
        w = " ".join((cap["plan"] or {}).get("warnings", []))
        check("a failed friend text and an unreachable TV become warnings, and the prep still runs",
              cap["plan"] and "text to your friend failed" in w and "TV did not confirm" in w, cap["plan"])
        # no destination at all
        async def no_nav():
            return {}
        tv.get_nav = no_nav
        cap["plan"] = None
        cl.post("/tools/prepmove/start", headers=A)
        wait_done()
        check("no destination: the text just has no link", cap["plan"] and cap["plan"]["destination_text"] is None, cap["plan"])
        # status
        (TMP / "run").mkdir(exist_ok=True)
        (TMP / "run" / "prepmove-report.json").write_text(json.dumps({"at": time.time(), "dry_run": True, "quit": ["Safari"], "refused": []}))
        r = cl.get("/tools/prepmove/status", headers=A)
        check("status shows the last report and whether you will be texted", r.status_code == 200 and r.json()["report"]["quit"] == ["Safari"]
              and r.json()["text_to_you"] is True and r.json()["enabled"] is True, r.text)
        # a guest without the display permission cannot start it
        subprocess.run([sys.executable, str(ROOT / "tvserver.py"), "token", "add", "guest"], env=dict(os.environ), capture_output=True, text=True)
        gt = json.loads((TMP / "tokens.json").read_text())["guest"]["token"]
        r = cl.post("/tools/prepmove/start", headers={"Authorization": "Bearer " + gt})
        check("a plain guest link cannot start it", r.status_code == 403, r.status_code)
        tv.asyncio.sleep = _sleep
except ImportError as e:
    print("skip endpoint checks (" + str(e) + ")")

# ---- the launchd job ------------------------------------------------------------------------------------------------------
spec2 = importlib.util.spec_from_file_location("tvserver2", ROOT / "tvserver.py")
tv2 = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(tv2)
tv2.RUN_DIR = TMP / "run2"
how = tv2.prep_spawn(dict(PLAN, dry_run=True))
pl = (TMP / "run2" / "com.lowone.prepmove.plist").read_text()
c = calls()
check("prep_spawn hands the helper to launchd as a one-shot job", how == "launchd" and "RunAtLoad" in pl and "KeepAlive" not in pl
      and "prepmove.py" in pl and "--plan" in pl, (how, pl))
check("it clears any old entry first, then bootstraps", [x.split()[1] for x in c if x.startswith("launchctl") and "prepmove" in x][-2:] == ["bootout", "bootstrap"], c)
check("the plan file is private", oct((TMP / "run2" / "prepmove-plan.json").stat().st_mode & 0o777) == "0o600")
(STATE / "launchctl_rc").write_text("5")
os.environ["TV_RUN_DIR"] = str(TMP / "run2")
how = tv2.prep_spawn(dict(PLAN, dry_run=True))
check("if launchd refuses, a detached process does the same work", how == "detached", how)
time.sleep(2)
check("...and that process wrote its report", (TMP / "run2" / "prepmove-report.json").exists())

for p in procs.values():
    try:
        p.kill()
    except OSError:
        pass
print()
print("FAILED: " + ", ".join(fails) if fails else "ALL OK")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if fails else 0)
