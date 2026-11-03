#!/usr/bin/env python3
"""Checks for Tools > Check TV address (fixip.py and POST /tools/fixip): reading arp / ifconfig output, finding the TV by
its MAC on a stand-in network, the tv.env rewrite, and the endpoint (permissions, hot swap of TV_IP, check-only, 409).
No real network, no real tv.env:   python3 tests/test_fixip.py   (needs the packages tvserver.py needs; node is optional)"""
import importlib.util
import ipaddress
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import logging
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="rvtv-fixip-"))
os.environ.update(TV_TOKEN="t", TV_ENV_FILE=str(TMP / "none.env"), TV_TOKENS_FILE=str(TMP / "tokens.json"),
                  TV_LOCATION_FILE=str(TMP / "location.raw"), TV_ITINERARY="")
sys.path.insert(0, str(ROOT))
import fixip as fx

fails = []


def check(name, ok, extra=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else "  " + str(extra)))
    if not ok:
        fails.append(name)


MAC = "ec:31:11:a2:e5:16"
PORT = 3001
fx.POKE_WAITS = (0, 0)              # no real waiting in the tests

# ---- reading the machine's own output -----------------------------------------------------------------------------
check("norm_mac pads macOS's short octets", fx.norm_mac("EC:31:11:A2:5:6") == "ec:31:11:a2:05:06")
check("norm_mac takes dashes", fx.norm_mac("ec-31-11-a2-e5-16") == MAC)
check("norm_mac refuses junk", fx.norm_mac("") is None and fx.norm_mac("ec:31:11") is None
      and fx.norm_mac("zz:31:11:a2:e5:16") is None and fx.norm_mac("ec:31:11:a2:e5:166") is None and fx.norm_mac(None) is None)

MAC_ARP = """? (192.168.1.1) at 4a:b2:11:0:9c:de on en0 ifscope [ethernet]
? (192.168.1.200) at ec:31:11:a2:e5:16 on en0 ifscope [ethernet]
? (192.168.1.77) at (incomplete) on en0 ifscope [ethernet]
? (192.168.1.255) at ff:ff:ff:ff:ff:ff on en0 ifscope [ethernet]
? (224.0.0.251) at 1:0:5e:0:0:fb on en0 ifscope permanent [ethernet]
"""
t = fx.parse_arp(MAC_ARP)
check("arp (macOS): the TV is found, zero-stripped octets are padded", t.get("192.168.1.200") == MAC and t.get("192.168.1.1") == "4a:b2:11:00:9c:de", t)
check("arp (macOS): incomplete and broadcast entries are left out", "192.168.1.77" not in t and "192.168.1.255" not in t, t)
LIN_ARP = "? (10.0.0.7) at aa:bb:cc:dd:ee:ff [ether] on eth0\ngateway (10.0.0.1) at 11:22:33:44:55:66 [ether] on eth0\n"
check("arp (Linux): both name styles read", fx.parse_arp(LIN_ARP) == {"10.0.0.7": "aa:bb:cc:dd:ee:ff", "10.0.0.1": "11:22:33:44:55:66"})
check("arp: empty or None gives nothing", fx.parse_arp("") == {} and fx.parse_arp(None) == {})

MAC_IF = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 192.168.1.50 netmask 0xffffff00 broadcast 192.168.1.255
en1: flags=8863<UP> mtu 1500
\tinet 169.254.5.5 netmask 0xffff0000 broadcast 169.254.255.255
utun4: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1280
\tinet 100.74.199.18 --> 100.74.199.18 netmask 0xffffffff
bridge100: flags=8863<UP>
\tinet 10.0.5.9 netmask 0xffff0000 broadcast 10.0.255.255
"""
nets, own = fx.parse_ifconfig(MAC_IF)
check("ifconfig (macOS): the LAN is found from a hex mask", ipaddress.ip_network("192.168.1.0/24") in nets, nets)
check("ifconfig: loopback, link-local and Tailscale are not swept", all(str(n).split(".")[0] in ("192", "10") for n in nets) and len(nets) == 2, nets)
check("ifconfig: a /16 is cut down to the /24 around the Mac", ipaddress.ip_network("10.0.5.0/24") in nets, nets)
check("ifconfig: the Mac's own addresses are returned so they are skipped", own == {"192.168.1.50", "10.0.5.9"}, own)
LIN_IF = "eth0: flags=4163<UP,BROADCAST,RUNNING,MULTICAST>  mtu 1500\n        inet 172.20.3.4  netmask 255.255.254.0  broadcast 172.20.3.255\n"
check("ifconfig (Linux): a dotted mask reads too", fx.parse_ifconfig(LIN_IF)[0] == [ipaddress.ip_network("172.20.2.0/23")])
check("ifconfig: nothing readable gives nothing", fx.parse_ifconfig("") == ([], set()))


# ---- a stand-in network -------------------------------------------------------------------------------------------
class FakeNet(object):
    def __init__(self, open_ips=(), arp_text="", appears=None, appear_on=1, subnets=("192.168.1.0/24",), own=("192.168.1.50",)):
        self.open, self.arp, self.appears, self.appear_on = set(open_ips), fx.parse_arp(arp_text), appears or {}, appear_on
        self.nets = [ipaddress.ip_network(s) for s in subnets]
        self.own, self.pokes, self.connects = set(own), [], []
        self.gate = self.entered = None

    def tcp_open(self, ip, port, timeout=2.0):
        self.connects.append((ip, port))
        if self.entered:
            self.entered.set()
            self.gate.wait(10)
        return ip in self.open

    def arp_table(self):
        return dict(self.arp)

    def local_net(self):
        return list(self.nets), set(self.own)

    def poke(self, ips):
        self.pokes.append(list(ips))
        if len(self.pokes) >= self.appear_on:
            self.arp.update(self.appears)


def find(net, ip="192.168.1.201", mac=MAC):
    return fx.find_tv(mac, ip, PORT, net=net, sleep=lambda s: None)


r = find(FakeNet(open_ips=["192.168.1.201"], arp_text="? (192.168.1.201) at ec:31:11:a2:e5:16 on en0 ifscope [ethernet]"))
check("right address, TV answering: ok, nothing sent", r["result"] == "ok" and r["reachable"] and r["ip"] == "192.168.1.201", r)
n = FakeNet(open_ips=["192.168.1.201"])
r = find(n)
check("right address, answering, no ARP entry to compare: ok", r["result"] == "ok" and not n.pokes, r)
r = find(FakeNet(arp_text="? (192.168.1.201) at ec:31:11:a2:e5:16 on en0 ifscope [ethernet]"))
check("TV's network card is there but not taking connections (standby): ok, not reachable, says so",
      r["result"] == "ok" and not r["reachable"] and "not taking connections" in r["message"], r)
n = FakeNet(open_ips=["192.168.1.200"], arp_text="? (192.168.1.200) at ec:31:11:a2:e5:16 on en0 ifscope [ethernet]")
r = find(n)
check("moved, and the Mac already knows the new address: found without any sweep",
      r["result"] == "found" and r["ip"] == "192.168.1.200" and r["reachable"] and not n.pokes, (r, n.pokes))
check("... and port 3001 was the port tried", all(p == PORT for _, p in n.connects), n.connects)

n = FakeNet(open_ips=["192.168.1.200"], appears={"192.168.1.200": MAC})
r = find(n)
check("moved: one sweep finds it", r["result"] == "found" and r["ip"] == "192.168.1.200" and len(n.pokes) == 1, (r, len(n.pokes)))
check("the sweep covers the Mac's own network, not its own address, not .0 or .255",
      len(n.pokes[0]) == 253 and "192.168.1.50" not in n.pokes[0] and "192.168.1.0" not in n.pokes[0]
      and "192.168.1.255" not in n.pokes[0] and "192.168.1.1" in n.pokes[0], len(n.pokes[0]))

n = FakeNet(open_ips=["192.168.1.200"], appears={"192.168.1.200": MAC}, appear_on=2)
r = find(n)
check("a slow answer is caught by the second sweep", r["result"] == "found" and len(n.pokes) == 2, (r, len(n.pokes)))

n = FakeNet()
r = find(n)
check("TV not on the network: not_found, address unchanged, says where it looked",
      r["result"] == "not_found" and r["ip"] == "192.168.1.201" and len(n.pokes) == 2 and "192.168.1.0/24" in r["message"], r)

n = FakeNet(open_ips=["192.168.1.201", "192.168.1.200"], arp_text="? (192.168.1.201) at 11:22:33:44:55:66 on en0 ifscope [ethernet]",
            appears={"192.168.1.200": MAC})
r = find(n)
check("another device took the old address (and answers on 3001): the TV is still found elsewhere",
      r["result"] == "found" and r["ip"] == "192.168.1.200", r)

n = FakeNet(open_ips=["192.168.1.200"], arp_text="? (192.168.1.199) at ec:31:11:a2:e5:16 on en0 ifscope [ethernet]\n"
                                                  "? (192.168.1.200) at ec:31:11:a2:e5:16 on en0 ifscope [ethernet]")
r = find(n)
check("two addresses for the MAC, only one awake: the awake one is used", r["result"] == "found" and r["ip"] == "192.168.1.200", r)
n = FakeNet(open_ips=["192.168.1.199", "192.168.1.200"], arp_text="? (192.168.1.199) at ec:31:11:a2:e5:16 on en0 ifscope [ethernet]\n"
                                                                  "? (192.168.1.200) at ec:31:11:a2:e5:16 on en0 ifscope [ethernet]")
check("two addresses, both awake: ambiguous, nothing chosen", find(n)["result"] == "ambiguous")
n = FakeNet(arp_text="? (192.168.1.199) at ec:31:11:a2:e5:16 on en0 ifscope [ethernet]\n"
                     "? (192.168.1.200) at ec:31:11:a2:e5:16 on en0 ifscope [ethernet]")
r = find(n)
check("two addresses, none answering: ambiguous, and the message says so", r["result"] == "ambiguous" and "none of them answers" in r["message"], r)
r = find(FakeNet(arp_text="? (192.168.1.200) at ec:31:11:a2:e5:16 on en0 ifscope [ethernet]"))
check("one address, TV in standby there: found, not reachable yet", r["result"] == "found" and r["ip"] == "192.168.1.200" and not r["reachable"], r)

n = FakeNet(open_ips=["192.168.1.201"], arp_text="? (192.168.1.201) at EC:31:11:A2:E5:16 on en0 ifscope [ethernet]")
check("capital letters in tv.env or in ARP do not matter", find(n, mac="EC:31:11:A2:E5:16")["result"] == "ok")
n = FakeNet(open_ips=["192.168.1.200"], arp_text="? (192.168.1.200) at ec:31:11:a2:5:6 on en0 ifscope [ethernet]")
check("macOS's short octets match a full MAC in tv.env", find(n, mac="ec:31:11:a2:05:06")["result"] == "found")

n = FakeNet(arp_text="? (10.9.9.9) at ec:31:11:a2:e5:16 on en1 ifscope [ethernet]")
check("an ARP entry on some other network is ignored", find(n)["result"] == "not_found")
n = FakeNet(subnets=())
r = find(n)
check("the Mac's network unreadable and the TV unknown: not_found, no sweep of nothing", r["result"] == "not_found" and not n.pokes and "this network" in r["message"], r)
n = FakeNet()
r = find(n, mac="not a mac")
check("a bad TV_MAC is an error and touches nothing", r["result"] == "error" and not n.connects and not n.pokes, r)

# ---- tv.env -------------------------------------------------------------------------------------------------------
ENV = ("TV_IP=192.168.1.201\nTV_MAC=ec:31:11:a2:e5:16\nTV_TOKEN=abc123\nTV_SHEET_URL=\"https://x/y?a=1&b=2\"\n"
       "# TV_IP=10.0.0.9\nTV_BIND=100.74.199.18\n")


def envfile(text, mode=0o600, name="tv.env"):
    d = Path(tempfile.mkdtemp(dir=str(TMP)))
    p = d / name
    p.write_text(text)
    p.chmod(mode)
    return p


p = envfile(ENV)
changed = fx.set_env_ip(p, "192.168.1.200")
want = ENV.replace("TV_IP=192.168.1.201\n", "TV_IP=192.168.1.200\n", 1)
check("only the TV_IP line changes; every other byte stays (token, quoted URL, commented TV_IP)", changed and p.read_text() == want, p.read_text())
check("the file keeps its mode 600", (p.stat().st_mode & 0o777) == 0o600, oct(p.stat().st_mode))
check("no copy of the file and no temp file is left behind (it holds the tokens)", [x.name for x in p.parent.iterdir()] == ["tv.env"], list(p.parent.iterdir()))
check("the same address again writes nothing", fx.set_env_ip(p, "192.168.1.200") is False and p.read_text() == want)
p = envfile('export TV_IP="192.168.1.201"  # lan\nTV_TOKEN=x\n')
fx.set_env_ip(p, "10.1.2.3")
check("export, quotes and a trailing comment are kept", p.read_text() == 'export TV_IP="10.1.2.3"  # lan\nTV_TOKEN=x\n', p.read_text())
p = envfile("TV_IP=1.1.1.1\nTV_TOKEN=x\nTV_IP=2.2.2.2\n")
fx.set_env_ip(p, "192.168.1.200")
check("a duplicated TV_IP is changed everywhere (the loader takes the first, the shell the last)", p.read_text() == "TV_IP=192.168.1.200\nTV_TOKEN=x\nTV_IP=192.168.1.200\n", p.read_text())
p = envfile("TV_TOKEN=x")
fx.set_env_ip(p, "192.168.1.200")
check("no TV_IP line: one is added, after a missing final newline too", p.read_text() == "TV_TOKEN=x\nTV_IP=192.168.1.200\n", repr(p.read_text()))
p = envfile("TV_IP=192.168.1.201\r\nTV_TOKEN=x\r\n")
fx.set_env_ip(p, "192.168.1.200")
check("Windows line endings survive", p.read_bytes() == b"TV_IP=192.168.1.200\r\nTV_TOKEN=x\r\n", p.read_bytes())
p = envfile(ENV)
for bad in ("1.2.3.4\nTV_TOKEN=evil", "999.1.1.1", "", "tv.local", "192.168.1.200 ; rm"):
    try:
        fx.set_env_ip(p, bad)
        ok = False
    except ValueError:
        ok = True
    check("refuses %r and leaves the file alone" % bad, ok and p.read_text() == ENV)

# ---- check_and_fix ------------------------------------------------------------------------------------------------
p = envfile(ENV)
n = FakeNet(open_ips=["192.168.1.200"], appears={"192.168.1.200": MAC})
r = fx.check_and_fix(MAC, "192.168.1.201", PORT, p, apply=True, net=n, sleep=lambda s: None)
check("apply: fixed, saved, says what moved", r["result"] == "fixed" and r["saved"] and "192.168.1.201 to 192.168.1.200" in r["message"]
      and "TV_IP=192.168.1.200" in p.read_text(), r)
p = envfile(ENV)
n = FakeNet(open_ips=["192.168.1.200"], appears={"192.168.1.200": MAC})
r = fx.check_and_fix(MAC, "192.168.1.201", PORT, p, apply=False, net=n, sleep=lambda s: None)
check("check only: reports found, changes nothing", r["result"] == "found" and not r["saved"] and p.read_text() == ENV
      and "Nothing was changed" in r["message"], r)
n = FakeNet(arp_text="? (192.168.1.200) at ec:31:11:a2:e5:16 on en0 ifscope [ethernet]")
r = fx.check_and_fix(MAC, "192.168.1.201", PORT, envfile(ENV), apply=True, net=n, sleep=lambda s: None)
check("fixed while the TV is in standby: tells you to press Power on", r["result"] == "fixed" and "press Power on" in r["message"], r)
r = fx.check_and_fix(MAC, "192.168.1.201", PORT, TMP / "missing" / "tv.env", apply=True,
                     net=FakeNet(open_ips=["192.168.1.200"], appears={"192.168.1.200": MAC}), sleep=lambda s: None)
check("tv.env cannot be written: still fixed in memory, and says the file was not saved",
      r["result"] == "fixed" and r["saved"] is False and "could not be saved" in r["message"], r)
p = envfile(ENV)
r = fx.check_and_fix(MAC, "192.168.1.201", PORT, p, net=FakeNet(open_ips=["192.168.1.201"]), sleep=lambda s: None)
check("nothing moved: tv.env is not touched", r["result"] == "ok" and p.read_text() == ENV, r)
lg = TMP / "fixip.log"
fx.log_change(lg, "192.168.1.201", "192.168.1.200", MAC, "devon")
check("the change is logged without secrets", "192.168.1.201 -> 192.168.1.200 mac %s by devon" % MAC in lg.read_text())

# ---- the endpoint -------------------------------------------------------------------------------------------------
spec = importlib.util.spec_from_file_location("tvserver", ROOT / "tvserver.py")
tv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tv)
logging.getLogger("uvicorn.error").setLevel(logging.ERROR)
tv.RUN_DIR = TMP
tv.TV_MAC = MAC
(TMP / "tokens.json").write_text(json.dumps({
    "guest": {"token": "g"},
    "carphone": {"token": "c", "actions": ["stops"]},
    "shower": {"token": "s", "actions": tv.GUEST_ACTIONS + ["display"]}}))

page = tv.GUEST_PAGE
check("the phone page has the button, gated on the status permission, and the result line",
      'id="fx-go"' in page and 'data-act="status"' in page and "onclick=\"fixIp()\"" in page and 'id="tl-fix"' in page)
check("the page script calls the new endpoint", "api('POST','/tools/fixip')" in page)
node = shutil.which("node")
if node:
    js = page[page.index("<script>") + 8:page.index("</script>")]
    f = TMP / "page.js"
    f.write_text(js)
    r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
    check("the whole page script still parses (node --check)", r.returncode == 0, r.stderr[:300])
else:
    print("skip page script syntax check (node not installed)")

envp = envfile(ENV)
os.environ["TV_ENV_FILE"] = str(envp)
fxm = tv.fixip_mod()
state = {}


def use(net):
    state["net"] = net
    fxm.RealNet = lambda: net


from fastapi.testclient import TestClient
with TestClient(tv.app) as cl:
    H = lambda tok: {"Authorization": "Bearer " + tok}
    moved = lambda: FakeNet(open_ips=["192.168.1.200"], appears={"192.168.1.200": MAC})
    tv.TV_IP = "192.168.1.201"
    use(moved())
    check("no token: 401", cl.post("/tools/fixip").status_code == 401)
    check("a stops-only token (the car phone): 403", cl.post("/tools/fixip", headers=H("c")).status_code == 403)
    r = cl.post("/tools/fixip?apply=0", headers=H("g"))
    check("a default guest token may check: 200, found, nothing changed",
          r.status_code == 200 and r.json()["result"] == "found" and tv.TV_IP == "192.168.1.201" and envp.read_text() == ENV, (r.status_code, r.text))
    use(moved())
    r = cl.post("/tools/fixip", headers=H("s"))
    j = r.json()
    check("a display token fixes it: 200, fixed, saved", r.status_code == 200 and j["result"] == "fixed" and j["saved"] and j["ip"] == "192.168.1.200", (r.status_code, r.text))
    check("tvserver uses the new address at once (no restart)", tv.TV_IP == "192.168.1.200" and os.environ["TV_IP"] == "192.168.1.200", tv.TV_IP)
    check("tv.env has the new address and nothing else changed", envp.read_text() == ENV.replace("TV_IP=192.168.1.201", "TV_IP=192.168.1.200", 1), envp.read_text())
    check("the fix is logged in .run/fixip.log with who asked", "192.168.1.201 -> 192.168.1.200" in (TMP / "fixip.log").read_text()
          and "by shower" in (TMP / "fixip.log").read_text())
    use(FakeNet(open_ips=["192.168.1.200"], arp_text="? (192.168.1.200) at ec:31:11:a2:e5:16 on en0 ifscope [ethernet]"))
    r = cl.post("/tools/fixip", headers=H("t"))
    check("asked again, the admin is told it is fine", r.status_code == 200 and r.json()["result"] == "ok", (r.status_code, r.text))
    use(FakeNet())
    r = cl.post("/tools/fixip", headers=H("t"))
    check("TV off the network: 200 with not_found, TV_IP unchanged", r.status_code == 200 and r.json()["result"] == "not_found" and tv.TV_IP == "192.168.1.200", (r.status_code, r.text))
    # a second request while one is running is refused, not queued
    slow = FakeNet(open_ips=["192.168.1.200"], arp_text="? (192.168.1.200) at ec:31:11:a2:e5:16 on en0 ifscope [ethernet]")
    slow.gate, slow.entered = threading.Event(), threading.Event()
    use(slow)
    first = {}
    th = threading.Thread(target=lambda: first.update(r=cl.post("/tools/fixip", headers=H("t"))))
    th.start()
    entered = slow.entered.wait(10)
    r2 = cl.post("/tools/fixip", headers=H("t"))
    slow.gate.set()
    th.join(15)
    check("a second tap while one is looking: 409", entered and r2.status_code == 409, (entered, r2.status_code))
    check("... and the first one still finishes normally", first.get("r") is not None and first["r"].status_code == 200 and first["r"].json()["result"] == "ok", first)

shutil.rmtree(str(TMP), ignore_errors=True)
print()
print("all passed" if not fails else "%d FAILED: %s" % (len(fails), "; ".join(fails)))
sys.exit(1 if fails else 0)
