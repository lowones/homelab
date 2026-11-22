#!/usr/bin/env python3
"""Talks to the Shelly relays (Shelly 1 Gen4) that switch the RV lights, so they can be switched from the phone.

Same local HTTP API as the TV plug (powercycle.py), switch 0 on each relay:
  GET /rpc/Switch.GetStatus?id=0                 -> {"output": true, ...}
  GET /rpc/Switch.Set?id=0&on=true               -> on (the reply says what it was before)
  GET /rpc/Input.GetStatus?id=0                  -> {"state": true}  (the wall switch, wired to the relay's SW input)
The wall switch stays in charge too: the relay is set to "flip" (any flip of the wall switch toggles the light), so a
light switched from the phone can still be switched at the wall, and the two never fight.

Lights come from RV_LIGHTS in tv.env, in the order they show on the phone: "bedroom=shelly1g4-e83dc1a4c394.local,..."
A name is lower case letters, digits and _; the label is the name with each word capitalised ("Living Room").
Standard library only (Python 3.9 compatible). tvserver.py calls these from /lights and /tools/lights.
"""
import re

from powercycle import PlugError, _get, status

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}$")


def parse(spec):
    """RV_LIGHTS -> [{"name", "label", "host"}] in order. Bad or repeated entries are skipped and returned as problems:
    (lights, problems)."""
    lights, problems, seen = [], [], set()
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        name, _, host = part.partition("=")
        name, host = name.strip().lower(), host.strip()
        if not NAME_RE.match(name) or not host or name in seen:
            problems.append("RV_LIGHTS entry %r is not name=host (or the name is used twice)" % part)
            continue
        seen.add(name)
        lights.append({"name": name, "label": " ".join(w.capitalize() for w in name.split("_") if w), "host": host})
    return lights, problems


def is_on(host, timeout=4.0):
    """True / False. Raises PlugError."""
    return bool(status(host, timeout).get("output"))


def set_on(host, on, timeout=4.0):
    """Switches the light on or off and returns the new state (read back, not assumed). Raises PlugError."""
    status(host, timeout)                     # refuses an address that is not a Shelly switch before changing anything
    _get(host, "/rpc/Switch.Set", {"id": 0, "on": "true" if on else "false"}, timeout)
    return is_on(host, timeout)


def info(host, timeout=4.0):
    """Everything Check/Debug > Check lights shows, read-only. Switch status is required (PlugError without it); the
    rest is best effort and left as None. Returns a dict: on, source, temp_c, wall (the wall switch input, True = closed),
    in_mode, initial_state, auto_on, auto_off, auth, firmware, rssi, warnings (list of sentences)."""
    st = status(host, timeout)
    out = {"on": bool(st.get("output")), "source": st.get("source"), "temp_c": (st.get("temperature") or {}).get("tC"),
           "wall": None, "in_mode": None, "initial_state": None, "auto_on": None, "auto_off": None, "auth": None,
           "firmware": None, "rssi": None}
    try:
        out["wall"] = _get(host, "/rpc/Input.GetStatus", {"id": 0}, timeout).get("state")
    except (PlugError, AttributeError):
        pass
    try:
        cfg = _get(host, "/rpc/Switch.GetConfig", {"id": 0}, timeout)
        for k in ("in_mode", "initial_state", "auto_on", "auto_off"):
            out[k] = cfg.get(k)
    except (PlugError, AttributeError):
        pass
    try:
        dev = _get(host, "/rpc/Shelly.GetDeviceInfo", None, timeout)
        out["auth"] = bool(dev.get("auth_en"))
        out["firmware"] = dev.get("ver")
    except (PlugError, AttributeError):
        pass
    try:
        out["rssi"] = (_get(host, "/rpc/Shelly.GetStatus", None, timeout).get("wifi") or {}).get("rssi")
    except (PlugError, AttributeError):
        pass
    w = []
    if out["in_mode"] == "follow" and out["wall"] is not None and bool(out["wall"]) != out["on"]:
        w.append("The wall switch is in follow mode and disagrees with the light: the next flip at the wall may not "
                 "do what you expect. Set the input to flip in the Shelly app.")
    elif out["in_mode"] == "detached":
        w.append("The wall switch is detached: it does nothing to the light.")
    if out["auto_on"] or out["auto_off"]:
        w.append("An auto-on or auto-off timer is set: the light will switch by itself.")
    if isinstance(out["rssi"], (int, float)) and out["rssi"] < -75:
        w.append("Weak Wi-Fi signal at the relay (%d dBm): taps may be slow or fail." % out["rssi"])
    if isinstance(out["temp_c"], (int, float)) and out["temp_c"] > 70:
        w.append("The relay is hot (%.0f C)." % out["temp_c"])
    out["warnings"] = w
    return out


def describe(label, i):
    """The lines the phone shows for one light. i is info()'s dict."""
    lines = ["%s: %s%s" % (label, "ON" if i["on"] else "off",
                           ", %.0f C" % i["temp_c"] if isinstance(i.get("temp_c"), (int, float)) else "")]
    wall = []
    if i.get("in_mode"):
        wall.append("wall switch mode %s" % i["in_mode"])
    if i.get("wall") is not None:
        wall.append("switch contact %s" % ("closed" if i["wall"] else "open"))
    if i.get("source"):
        wall.append("last switched by %s" % {"SW_input": "the wall", "HTTP_in": "the server", "WS_in": "the Shelly app",
                                             "init": "power-up"}.get(i["source"], i["source"]))
    if wall:
        lines.append(", ".join(wall).capitalize())
    extra = []
    if isinstance(i.get("rssi"), (int, float)):
        r = i["rssi"]
        extra.append("Wi-Fi %d dBm (%s)" % (r, "good" if r >= -60 else "ok" if r >= -75 else "weak"))
    if i.get("firmware"):
        extra.append("firmware " + str(i["firmware"]))
    if i.get("initial_state"):
        extra.append("after a power failure: " + str(i["initial_state"]).replace("_", " "))
    if extra:
        lines.append(", ".join(extra))
    if i.get("auth") is not None:
        lines.append("Password: " + ("set" if i["auth"] else "none (anyone on the RV Wi-Fi can switch it)"))
    lines.extend("▲ " + w for w in i.get("warnings", []))
    return lines
