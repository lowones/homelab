#!/usr/bin/env python3
"""Talks to the smart plug (Shelly Plug US Gen4) that powers the RV TV, so the TV can be power cycled from the phone.

Why: when the TV stops answering (frozen webOS, a stuck standby), a hard power cut is the only fix, and nobody is
standing next to the plug. The plug has a local HTTP API (no cloud, no hub, works through a Starlink outage):
  GET /rpc/Switch.GetStatus?id=0                              -> {"output": true, ...}
  GET /rpc/Switch.Set?id=0&on=false&toggle_after=10           -> off now, and the PLUG turns itself on again after 10 s
The second form matters: the plug does the "on again" itself, so the cycle completes even if the connection drops.

Standard library only (Python 3.9 compatible). tvserver.py calls cycle() from POST /tools/powercycle (it does the waiting
for the TV, the wake packet and the input switch); ./tv powercycle is the command-line way to the same endpoint.
This module never touches the TV, only the plug.
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
