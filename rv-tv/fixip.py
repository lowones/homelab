#!/usr/bin/env python3
"""Finds the RV TV on the network by its MAC address and fixes TV_IP when its address has moved.

Why: the TV's IP is a Starlink DHCP lease, so it can change (2026-10-28, 2026-11-03). Wake-on-LAN goes by MAC, so
Power on keeps working, while status, volume and Power off connect by IP and time out. This does what was done by hand:
  1. Is the TV still where tv.env says? (port 3001 answers, or the ARP table shows the TV's MAC at that address)
  2. If not: ask every address on the Mac's own network who it is (ARP) and look for the TV's MAC.
  3. Write the new address into tv.env (only the TV_IP line, in place) and report it. tvserver switches to it at once.

Standard library only (Python 3.9 compatible). tvserver.py calls check_and_fix() from POST /tools/fixip; ./tv fixip is
the command-line way to the same endpoint. Nothing here wakes the TV or changes anything but the TV_IP line.
"""
import ipaddress
import os
import re
import shutil
import socket
import stat
import subprocess
import tempfile
import time
from pathlib import Path

PRIVATE_NETS = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
MAX_SWEEP = 1022       # at most a /22 is swept; a wider network is cut down to the /24 around the Mac
POKE_WAITS = (1.5, 2.5)  # seconds to let ARP answers arrive after each round of pokes


def norm_mac(s):
    """'EC:31:11:A2:E5:16', 'ec-31-11-a2-e5-16' and macOS's 'ec:31:11:a2:5:16' (no leading zeros) all become
    'ec:31:11:a2:05:16'. None if it is not a MAC address."""
    parts = re.split(r"[:\-]", str(s or "").strip().lower())
    if len(parts) != 6:
        return None
    out = []
    for p in parts:
        if not 1 <= len(p) <= 2:
            return None
        try:
            out.append("%02x" % int(p, 16))
        except ValueError:
            return None
    return ":".join(out)


ARP_RE = re.compile(r"\((\d{1,3}(?:\.\d{1,3}){3})\)\s+at\s+([0-9a-fA-F]{1,2}(?::[0-9a-fA-F]{1,2}){5})\b")


def parse_arp(text):
    """Output of `arp -an` (macOS and Linux) -> {ip: mac}. Incomplete entries have no MAC and are left out."""
    table = {}
    for m in ARP_RE.finditer(text or ""):
        mac = norm_mac(m.group(2))
        if mac and mac not in ("ff:ff:ff:ff:ff:ff", "00:00:00:00:00:00"):
            table[m.group(1)] = mac
    return table


IFCONFIG_RE = re.compile(r"\binet\s+(\d{1,3}(?:\.\d{1,3}){3})\s+netmask\s+(0x[0-9a-fA-F]+|\d{1,3}(?:\.\d{1,3}){3})")


def parse_ifconfig(text):
    """Output of `ifconfig -a` -> (networks, own addresses): the private IPv4 networks this machine is on.
    Loopback, link-local (169.254), Tailscale (100.64/10) and public addresses are not swept."""
    nets, own = [], set()
    for m in IFCONFIG_RE.finditer(text or ""):
        try:
            addr = ipaddress.IPv4Address(m.group(1))
            mask = m.group(2)
            if mask.lower().startswith("0x"):          # macOS prints the mask in hex
                mask = str(ipaddress.IPv4Address(int(mask, 16)))
            net = ipaddress.IPv4Network("%s/%s" % (addr, mask), strict=False)
        except ValueError:
            continue
        if not any(addr in p for p in PRIVATE_NETS) or net.prefixlen >= 31:
            continue
        if net.num_addresses - 2 > MAX_SWEEP:
            net = ipaddress.IPv4Network("%s/24" % addr, strict=False)
        own.add(str(addr))
        if net not in nets:
            nets.append(net)
    return nets, own


class RealNet(object):
    """This machine's real network. The tests use a stand-in with the same four methods."""

    def tcp_open(self, ip, port, timeout=2.0):
        try:
            s = socket.create_connection((ip, int(port)), timeout=timeout)
        except OSError:
            return False
        s.close()
        return True

    def arp_table(self):
        exe = shutil.which("arp") or "/usr/sbin/arp"
        try:
            out = subprocess.run([exe, "-an"], capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            return {}
        return parse_arp(out)

    def local_net(self):
        exe = shutil.which("ifconfig") or "/sbin/ifconfig"
        try:
            out = subprocess.run([exe, "-a"], capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            return [], set()
        return parse_ifconfig(out)

    def poke(self, ips):
        """One tiny UDP packet to each address. Nothing has to answer: before the Mac can send, it must ask who has the
        address (ARP), and the answers land in the ARP table, which is what we read afterwards."""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setblocking(False)
            for ip in ips:
                try:
                    s.sendto(b"\0", (ip, 9))
                except OSError:
                    pass              # "host is down" and "no buffer space" only mean this one has not answered (yet)
        finally:
            s.close()


def _in_nets(addr, nets):
    if not nets:
        return True                   # the Mac's own networks are unknown: do not filter
    try:
        a = ipaddress.IPv4Address(addr)
    except ValueError:
        return False
    return any(a in n for n in nets)


def _hosts(nets, own):
    out = []
    for n in nets:
        out.extend(str(h) for h in n.hosts() if str(h) not in own)
    return out


def find_tv(mac, ip, port, net=None, sleep=time.sleep):
    """Looks for the TV. Returns a dict:
       result   ok (tv.env is right) | found (the TV is at another address) | not_found | ambiguous | error
       ip       the TV's address when known (the old one for ok / not_found)
       was      the address that was asked about
       reachable  True when port 3001 answers at `ip`
       candidates, subnets, message (one plain sentence for the phone)"""
    net = net or RealNet()
    out = {"result": "error", "ip": ip, "was": ip, "mac": None, "reachable": False, "candidates": [], "subnets": [],
           "message": ""}
    want = norm_mac(mac)
    if not want:
        out["message"] = "TV_MAC in tv.env is not a MAC address, so the TV cannot be recognised."
        return out
    out["mac"] = want

    # 1. Is it where tv.env says? The connect also makes the Mac ask for that address (ARP), so the table is fresh.
    answers = net.tcp_open(ip, port)
    table = net.arp_table()
    seen = table.get(ip)
    out["reachable"] = answers
    if (answers and seen in (None, want)) or (not answers and seen == want):
        out["result"] = "ok"
        out["message"] = ("The TV is at %s and answers. Nothing to change." % ip) if answers else (
            "The TV is at %s (its network card answers there) but is not taking connections right now. "
            "If it is off, press Power on." % ip)
        return out

    # 2. Not there. Maybe the Mac already knows the new address; otherwise ask every address on its own network.
    subnets, own = net.local_net()
    out["subnets"] = [str(n) for n in subnets]

    def candidates(tbl):
        found = [a for a, m in tbl.items() if m == want and a != ip and _in_nets(a, subnets)]
        return sorted(found, key=lambda a: ipaddress.IPv4Address(a))

    cands = candidates(table)
    if not cands:
        hosts = _hosts(subnets, own)
        for wait in POKE_WAITS if hosts else ():
            net.poke(hosts)
            sleep(wait)
            cands = candidates(net.arp_table())
            if cands:
                break
    out["candidates"] = cands
    if not cands:
        where = ", ".join(out["subnets"]) or "this network"
        out["result"] = "not_found"
        out["message"] = ("The TV is not on the network (looked at %s). It may be off, installing an update, or on "
                          "another Wi-Fi. Try Power on, wait a minute, and check again." % where)
        return out

    # 3. Which address is it? The MAC is the identity; port 3001 only tells which candidate is awake.
    live = [c for c in cands if net.tcp_open(c, port, 1.5)]
    pick = live or cands
    if len(pick) > 1:
        out["result"] = "ambiguous"
        out["message"] = ("The TV's network card shows up at more than one address (%s)%s. Turn the TV on and check again."
                          % (", ".join(pick), "" if live else " and none of them answers"))
        return out
    out.update(result="found", ip=pick[0], reachable=bool(live))
    return out


_IP_LINE = re.compile(r"""^(\s*(?:export\s+)?TV_IP\s*=\s*)(["']?)(.*?)(\2)(\s*(?:#.*)?)$""")


def set_env_ip(path, new_ip):
    """Changes the TV_IP line(s) of tv.env to new_ip, in place and atomically, keeping every other byte and the file mode.
    No copy of the file is made: it holds the tokens. Returns True when something was written; raises ValueError for
    anything that is not a plain IPv4 address and OSError when the file cannot be written."""
    new_ip = str(ipaddress.IPv4Address(str(new_ip)))
    p = Path(os.path.realpath(str(path)))
    text = p.read_bytes().decode("utf-8")           # bytes, not read_text(): that would turn CRLF into LF
    lines, hits, changed = text.split("\n"), 0, False
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith("#"):
            continue
        m = _IP_LINE.match(ln)
        if m:
            hits += 1
            if m.group(3) != new_ip:
                lines[i] = m.group(1) + m.group(2) + new_ip + m.group(4) + m.group(5)
                changed = True
    new_text = "\n".join(lines)
    if not hits:                                     # no TV_IP line at all: add one
        if new_text and not new_text.endswith("\n"):
            new_text += "\n"
        new_text += "TV_IP=%s\n" % new_ip
        changed = True
    if not changed:
        return False
    mode = stat.S_IMODE(p.stat().st_mode)
    fd, tmp = tempfile.mkstemp(prefix=".tv.env.", dir=str(p.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(new_text.encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, str(p))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True


def check_and_fix(mac, ip, port, env_file, apply=True, net=None, sleep=time.sleep):
    """find_tv(), and when the TV is at another address and apply is true: write it to tv.env. The caller (tvserver)
    switches its own TV_IP when the result is 'fixed'."""
    r = find_tv(mac, ip, port, net=net, sleep=sleep)
    if r["result"] != "found":
        return r
    new = r["ip"]
    r["saved"] = False
    nudge = "" if r["reachable"] else " It is not answering yet; if it is off, press Power on."
    if not apply:
        r["message"] = "The TV is at %s now, not %s as tv.env says. Nothing was changed (check only).%s" % (new, ip, nudge)
        return r
    try:
        r["saved"] = set_env_ip(env_file, new)
        r["message"] = "The TV moved from %s to %s. Fixed, no restart needed.%s" % (ip, new, nudge)
    except (OSError, ValueError) as e:
        r["message"] = ("The TV moved from %s to %s and is used from now on, but tv.env could not be saved (%s), so a "
                        "restart would go back to the old address.%s" % (ip, new, e, nudge))
    r["result"] = "fixed"
    return r


def log_change(path, old, new, mac, who):
    """One line per fix in .run/fixip.log (no secrets): when the lease moved, and who asked."""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(str(p), "a") as f:
            f.write("%s %s -> %s mac %s by %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S%z"), old, new, mac, who))
    except OSError:
        pass
