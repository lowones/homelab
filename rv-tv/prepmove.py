#!/usr/bin/env python3
"""Get the Mac mini ready for a power cut (Tools > Prep for move on the phone menu).

The power will be cut with the Mac still running, so the goal is: nothing left that writes to disk.
In order:
  1. stop the RV TV launchd agents (tvserver, the location agent, the camera tunnel). `launchctl bootout`
     lasts for this session only; every one of them loads again at the next login.
  2. quit every app with a window or menu-bar icon except a keep list (Finder, Tailscale, system UI):
     a polite quit first, then SIGTERM; whatever still lives is reported, never force-killed.
  3. text Devon "ready", with the apps that would not quit and the driving link for the next stop.
  4. quit Messages (it had to stay up for the text), flush the disks with sync.
The reverse tunnel daemon (a system LaunchDaemon), Tailscale and sshd are left running on purpose.

tvserver starts this as its own one-shot launchd job (it outlives the server it stops):
    prepmove.py --plan .run/prepmove-plan.json [--dry-run]
By hand:
    prepmove.py --dry-run     list what it would quit and stop; changes nothing
    prepmove.py --resume      load the stopped agents again, for when you change your mind
Standard library only, Python 3.9-compatible. The report goes to .run/prepmove-report.json.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
RUN_DIR = Path(os.environ.get("TV_RUN_DIR") or BASE / ".run")
REPORT = RUN_DIR / "prepmove-report.json"
JOB_LABEL = "com.lowone.prepmove"
# stopped in this order; every plist lives in ~/Library/LaunchAgents
AGENTS = ["com.lowone.camtunnel", "com.lowone.tvlocation", "com.lowone.tvserver"]
# never quit: the desktop itself, and Tailscale (the way back in). Messages is handled separately.
KEEP = {"Finder", "Tailscale", "Dock", "SystemUIServer", "ControlCenter", "loginwindow",
        "Notification Center", "WindowManager", "Spotlight", "Control Center"}
QUIT_WAIT = float(os.environ.get("TV_PREP_QUIT_WAIT", "25"))      # seconds for polite quits
TERM_WAIT = float(os.environ.get("TV_PREP_TERM_WAIT", "8"))       # seconds after SIGTERM
TEXT_WAIT = float(os.environ.get("TV_PREP_TEXT_WAIT", "15"))      # let iMessage leave before Messages quits

LIST_APPS = '''tell application "System Events"
  set out to {}
  repeat with p in (application processes whose background only is false)
    set end of out to (name of p) & tab & (unix id of p)
  end repeat
end tell
set AppleScript's text item delimiters to linefeed
return out as text'''


def log(msg):
    print("%s  %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def sh(args, timeout=20):
    """Runs a command, never raises. (returncode, stdout, stderr); returncode -1 = could not run / timed out."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, "", str(e)


def list_apps():
    """[(name, pid)] of the apps the person can see. Raises RuntimeError when macOS will not tell us."""
    rc, out, err = sh(["osascript", "-e", LIST_APPS], timeout=30)
    if rc != 0:
        raise RuntimeError((err or "osascript failed").strip()[:200])
    apps = []
    for ln in out.splitlines():
        name, _, pid = ln.partition("\t")
        if name.strip() and pid.strip().isdigit():
            apps.append((name.strip(), int(pid)))
    return apps


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def quit_app(name):
    sh(["osascript", "-e", "on run argv", "-e", "tell application (item 1 of argv) to quit", "-e", "end run", name],
       timeout=12)


def quit_all(targets):
    """Polite quit for every target at once, then SIGTERM for stragglers. Returns (quit, refused) as name lists."""
    threads = [threading.Thread(target=quit_app, args=(n,), daemon=True) for n, _ in targets]
    for t in threads:
        t.start()
    deadline = time.time() + QUIT_WAIT
    while time.time() < deadline and any(alive(p) for _, p in targets):
        time.sleep(0.5)
    for n, p in targets:
        if alive(p):
            log("%s did not quit, sending SIGTERM" % n)
            try:
                os.kill(p, signal.SIGTERM)
            except OSError:
                pass
    deadline = time.time() + TERM_WAIT
    while time.time() < deadline and any(alive(p) for _, p in targets):
        time.sleep(0.5)
    refused = [n for n, p in targets if alive(p)]
    return [n for n, _ in targets if n not in refused], refused


def uid():
    return os.getuid()


def stop_agents():
    """bootout each agent; returns (stopped labels, notes)."""
    stopped, notes = [], []
    for label in AGENTS:
        rc, _, err = sh(["launchctl", "bootout", "gui/%d/%s" % (uid(), label)])
        if rc == 0:
            stopped.append(label)
        else:
            notes.append("%s: %s" % (label, (err or "not loaded").strip()[:80]))
    return stopped, notes


def resume_agents():
    done = []
    for label in reversed(AGENTS):        # tvserver first
        plist = Path.home() / "Library" / "LaunchAgents" / (label + ".plist")
        if not plist.exists():
            log("%s: no plist, skipped" % label)
            continue
        rc, _, err = sh(["launchctl", "bootstrap", "gui/%d" % uid(), str(plist)])
        log("%s: %s" % (label, "loaded" if rc == 0 else (err.strip()[:100] or "already loaded?")))
        done.append(label)
    return done


def send_text(script, to, body):
    """iMessage through the Messages app (the AppleScript comes from tvserver, so there is one copy of it)."""
    rc, _, err = sh(["osascript", "-e", script, to, body], timeout=45)
    return rc == 0, (err or "").strip()[:160]


def build_text(plan, refused, notes):
    now = time.strftime("%-I:%M %p")
    if refused:
        head = "Mini prepped at %s, but these would not quit: %s." % (now, ", ".join(refused))
    else:
        head = "Mini ready at %s: apps closed, services stopped. Safe to cut power." % now
    lines = [head]
    lines += [n for n in (plan.get("warnings") or []) if n]
    lines += [n for n in notes if n]
    if plan.get("destination_text"):
        lines += ["", plan["destination_text"]]
    return "\n".join(lines)


def write_report(rep):
    try:
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        tmp = REPORT.with_suffix(".tmp")
        tmp.write_text(json.dumps(rep))
        os.replace(str(tmp), str(REPORT))
    except OSError as e:
        log("could not write the report: %s" % e)


def run(plan, dry_run):
    rep = {"at": time.time(), "dry_run": dry_run, "quit": [], "refused": [], "kept": [], "stopped": [],
           "text": "not sent", "errors": [], "done": False}
    keep = KEEP | set(plan.get("keep_extra") or [])
    try:
        apps = list_apps()
    except RuntimeError as e:
        log("cannot list apps: %s" % e)
        rep["errors"].append("cannot list apps: %s" % e)
        apps = []
    targets = [(n, p) for n, p in apps if n not in keep and n != "Messages"]
    messages = [(n, p) for n, p in apps if n == "Messages"]
    rep["kept"] = sorted(n for n, _ in apps if n in keep)
    if dry_run:
        rep["quit"] = [n for n, _ in targets + messages]
        rep["stopped"] = list(AGENTS)
        rep["text"] = "would send to %s" % (", ".join(plan.get("self_to") or []) or "nobody (TV_TEXT_SELF is not set)")
        rep["done"] = True
        log("DRY RUN: would stop %s" % ", ".join(AGENTS))
        log("DRY RUN: would quit %s" % (", ".join(rep["quit"]) or "nothing"))
        log("DRY RUN: keeps %s" % (", ".join(rep["kept"]) or "nothing"))
        write_report(rep)
        return rep

    stopped, notes = stop_agents()
    rep["stopped"] = stopped
    log("stopped %s" % ", ".join(stopped))
    sh(["sync"], timeout=60)

    quit_ok, refused = quit_all(targets)
    rep["quit"], rep["refused"] = quit_ok, refused
    log("quit %d app(s), %d refused" % (len(quit_ok), len(refused)))

    to = plan.get("self_to") or []
    if to and plan.get("imessage_script"):
        body = build_text(plan, refused, notes)
        sent = []
        for t in to:
            ok, err = send_text(plan["imessage_script"], t, body)
            sent.append(ok)
            if not ok:
                rep["errors"].append("text to %s failed: %s" % (t, err))
        rep["text"] = "sent" if all(sent) else "failed"
        if any(sent):
            time.sleep(TEXT_WAIT)
    else:
        rep["text"] = "skipped (TV_TEXT_SELF is not set)" if not to else "skipped (no iMessage script)"

    if messages:
        q, r = quit_all(messages)
        rep["quit"] += q
        rep["refused"] += r
    sh(["sync"], timeout=60)
    time.sleep(1)
    sh(["sync"], timeout=60)
    try:                      # whatever came back or never left, for the record (the text is already out)
        late = [n for n, _ in list_apps() if n not in keep]
        if late:
            rep["errors"].append("still running at the end: %s" % ", ".join(late))
    except RuntimeError:
        pass
    rep["done"] = True
    write_report(rep)
    log("done")
    return rep


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--plan", help="JSON written by tvserver (texts, warnings); without it nothing is texted")
    ap.add_argument("--dry-run", action="store_true", help="list what would be quit and stopped; change nothing")
    ap.add_argument("--resume", action="store_true", help="load the stopped agents again")
    a = ap.parse_args()
    if a.resume:
        resume_agents()
        return 0
    plan = {}
    if a.plan:
        try:
            plan = json.loads(Path(a.plan).read_text())
        except (OSError, ValueError) as e:
            log("cannot read the plan %s: %s" % (a.plan, e))
            return 2
    elif not a.dry_run:
        print("A real run quits every app, including the terminal you are typing in, so it is started from the\n"
              "phone menu (Tools > Prep for move). Here you can use --dry-run or --resume.", file=sys.stderr)
        return 2
    rep = run(plan, a.dry_run or bool(plan.get("dry_run")))
    if a.plan:                # started by tvserver as a one-shot launchd job: remove its entry; the last thing we do
        sh(["launchctl", "bootout", "gui/%d/%s" % (uid(), JOB_LABEL)])
    return 0 if rep.get("done") else 1


if __name__ == "__main__":
    sys.exit(main())
