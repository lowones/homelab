#!/usr/bin/env python3
"""Talks to the smart plug (Shelly Plug US Gen4) that powers the RV TV, so the TV can be power cycled from the phone.

Why: when the TV stops answering (frozen webOS, a stuck standby), a hard power cut is the only fix, and nobody is
standing next to the plug. The plug has a local HTTP API (no cloud, no hub, works through a Starlink outage):
  GET /rpc/Switch.GetStatus?id=0                              -> {"output": true, ...}
  GET /rpc/Switch.Set?id=0&on=false&toggle_after=10           -> off now, and the PLUG turns itself on again after 10 s
The second form matters: the plug does the "on again" itself, so the cycle completes even if the connection drops.

Standard library only (Python 3.9 compatible). tvserver.py calls cycle() from POST /tools/powercycle (it does the waiting
for the TV, the wake packet and the input switch); ./tv powercycle is the command-line way to the same endpoint.
This module never touches the TV, only the plug. info() / describe() are read-only (Tools > Check TV plug).
"""
import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# A host name or IPv4 address with an optional port (the port is for the tests). Nothing else gets into the URL.
HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.\-]*[A-Za-z0-9])?(?::\d{1,5})?$")


class PlugError(Exception):
    """The plug could not be reached or did not behave like a Shelly switch. Nothing was changed when this is raised
    before the Switch.Set call."""


def _get(host, path, params=None, timeout=4.0):
    if not HOST_RE.match(str(host or "")):
        raise PlugError("SHELLY_PLUG_HOST is not a host name or address")
    url = "http://%s%s" % (host, path)
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read(65536)
    except urllib.error.HTTPError as e:
        raise PlugError("the plug answered HTTP %s" % e.code)
    except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError) as e:
        raise PlugError(str(getattr(e, "reason", None) or e) or type(e).__name__)
    try:
        return json.loads(body.decode("utf-8"))
    except ValueError:
        raise PlugError("the plug answered something that is not JSON")


def status(host, timeout=4.0):
    """Switch 0 status as a dict (it has 'output': True/False). Raises PlugError."""
    st = _get(host, "/rpc/Switch.GetStatus", {"id": 0}, timeout)
    if not isinstance(st, dict) or "output" not in st:
        raise PlugError("that address did not answer like a Shelly switch")
    return st


def cycle(host, off_seconds=10, timeout=4.0):
    """Cuts the TV's power for off_seconds (the plug turns itself back on). If the plug is already off, turns it on
    instead: an off-then-on-again request on an off plug would do nothing and then switch it ON, the opposite of a cut.
    Returns {"was_on": bool, "action": "cycled" | "turned_on"}. Raises PlugError."""
    st = status(host, timeout)
    if st.get("output"):
        _get(host, "/rpc/Switch.Set", {"id": 0, "on": "false", "toggle_after": int(off_seconds)}, timeout)
        return {"was_on": True, "action": "cycled"}
    _get(host, "/rpc/Switch.Set", {"id": 0, "on": "true"}, timeout)
    return {"was_on": False, "action": "turned_on"}


def log_cycle(path, who, result, seconds):
    """One line per power cycle in .run/powercycle.log (no secrets): when, who asked, how it ended, how long the TV
    took to answer again (None when it did not)."""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(str(p), "a") as f:
            f.write("%s by %s result %s tv_back_after %s\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S%z"), who, result, "-" if seconds is None else "%ds" % seconds))
    except OSError:
        pass


def info(host, timeout=4.0):
    """Everything Tools > Check TV plug shows, read-only. Switch status is required (PlugError without it); the rest
    (config, device info, Wi-Fi signal) is best effort and left as None when the plug does not answer it.
    Returns a dict: on, watts, volts, temp_c, source, initial_state, auto_on, auto_off, auth (True = a password is set),
    firmware, mac, rssi, warnings (list of sentences)."""
    st = status(host, timeout)
    out = {"on": bool(st.get("output")), "watts": st.get("apower"), "volts": st.get("voltage"),
           "temp_c": (st.get("temperature") or {}).get("tC"), "source": st.get("source"),
           "initial_state": None, "auto_on": None, "auto_off": None, "auth": None, "firmware": None, "mac": None,
           "rssi": None}
    try:
        cfg = _get(host, "/rpc/Switch.GetConfig", {"id": 0}, timeout)
        out["initial_state"] = cfg.get("initial_state")
        out["auto_on"] = cfg.get("auto_on")
        out["auto_off"] = cfg.get("auto_off")
    except (PlugError, AttributeError):
        pass
    try:
        dev = _get(host, "/rpc/Shelly.GetDeviceInfo", None, timeout)
        out["auth"] = bool(dev.get("auth_en"))
        out["firmware"] = dev.get("ver")
        out["mac"] = dev.get("mac")
    except (PlugError, AttributeError):
        pass
    try:
        whole = _get(host, "/rpc/Shelly.GetStatus", None, timeout)
        out["rssi"] = (whole.get("wifi") or {}).get("rssi")
    except (PlugError, AttributeError):
        pass
    w = []
    if not out["on"]:
        w.append("The plug is off, so the TV has no power. Switch it on in the Shelly app, or tap Power cycle TV (it turns an off plug on).")
    elif isinstance(out["watts"], (int, float)) and out["watts"] < 2:
        w.append("The plug is on but delivering under 2 W: the TV is off, or something else is plugged in here.")
    if out["initial_state"] is not None and out["initial_state"] != "on":
        w.append("After a power failure the plug will NOT switch itself on (initial_state is %s). Set it to on." % out["initial_state"])
    if out["auto_off"]:
        w.append("The auto-off timer is on: the plug would cut the TV's power by itself.")
    if isinstance(out["rssi"], (int, float)) and out["rssi"] < -75:
        w.append("Weak Wi-Fi signal at the plug (%d dBm): commands may be slow or fail." % out["rssi"])
    if isinstance(out["temp_c"], (int, float)) and out["temp_c"] > 60:
        w.append("The plug is hot (%.0f C)." % out["temp_c"])
    out["warnings"] = w
    return out


def last_cycle(path):
    """The last line of .run/powercycle.log as a short sentence, or None when there is none."""
    try:
        lines = [ln.strip() for ln in Path(path).read_text().splitlines() if ln.strip()]
    except OSError:
        return None
    if not lines:
        return None
    m = re.match(r"^(\S+) (\d\d:\d\d:\d\d)\S* by (\S+) result (\S+) tv_back_after (\S+)$", lines[-1])
    if not m:
        return lines[-1][:120]
    day, clock, who, result, back = m.groups()
    return "%s %s by %s: %s%s" % (day, clock, who, result.replace("_", " "), "" if back == "-" else ", TV back after " + back)


def describe(i, last, cooldown_left):
    """The lines the phone and ./tv plug print. i is info()'s dict, last is last_cycle()'s sentence or None."""
    def num(v, unit, nd=1):
        return "-" if not isinstance(v, (int, float)) else ("%." + str(nd) + "f %s") % (v, unit)
    lines = ["Plug: %s, %s, %s, %s" % ("on" if i["on"] else "OFF", num(i["watts"], "W"), num(i["volts"], "V", 0),
                                         num(i["temp_c"], "C", 0))]
    extra = []
    if isinstance(i.get("rssi"), (int, float)):
        r = i["rssi"]
        extra.append("Wi-Fi %d dBm (%s)" % (r, "good" if r >= -60 else "ok" if r >= -75 else "weak"))
    if i.get("firmware"):
        extra.append("firmware " + str(i["firmware"]))
    if extra:
        lines.append(", ".join(extra))
    st = i.get("initial_state")
    if st is not None:
        lines.append("After a power failure: %s. Auto-on timer: %s, auto-off timer: %s." % (
            "switches itself on" if st == "on" else "stays " + str(st), "on" if i.get("auto_on") else "off",
            "on" if i.get("auto_off") else "off"))
    if i.get("auth") is not None:
        lines.append("Password: " + ("set" if i["auth"] else "none (anyone on the RV Wi-Fi can switch the plug)"))
    lines.append("Last power cycle: " + (last or "none logged"))
    lines.append("Power cycle ready." if cooldown_left <= 0 else "Next power cycle allowed in %d s." % cooldown_left)
    lines.extend("\u25b2 " + w for w in i.get("warnings", []))
    return lines
