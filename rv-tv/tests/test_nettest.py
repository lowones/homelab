#!/usr/bin/env python3
"""Checks for nettest.py: parsers, scoring, report, and short end-to-end runs against stand-in ping and
iperf3 (plus a real iperf3 over loopback when one is installed). Plain python, no network:
    python3 tests/test_nettest.py            (about 40 seconds)
Set NETTEST_REAL_IPERF3=/path/to/iperf3 to use an iperf3 that is not on PATH."""
import importlib.util
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
TMP = Path(tempfile.mkdtemp(prefix="rvtv-nettest-"))
os.environ.update(TV_ENV_FILE=str(TMP / "none.env"), TV_NETTEST_DIR=str(TMP / "runs"),
                  TV_LOCATION_FILE=str(TMP / "location.raw"))
spec = importlib.util.spec_from_file_location("nettest", ROOT / "nettest.py")
nt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nt)
for stub in ("fakeping.py", "fakeiperf.py"):
    (TESTS / stub).chmod(0o755)
FAKEPING, FAKEIPERF = str(TESTS / "fakeping.py"), str(TESTS / "fakeiperf.py")

fails = []


def check(name, ok, extra=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else "  " + str(extra)))
    if not ok:
        fails.append(name)


def close(a, b, tol=0.01):
    return a is not None and abs(a - b) <= tol


def fixture(name):
    return json.loads((TESTS / "fixtures" / name).read_text())


# ---- small helpers --------------------------------------------------------------------------------
t = nt.parse_targets("vps=example.net:5300, oracle=oracle.ts.net,x=1.2.3.4", "iperf")
check("iperf targets: port parsed, default 5201", [(a["name"], a["host"], a["port"]) for a in t] ==
      [("vps", "example.net", 5300), ("oracle", "oracle.ts.net", 5201), ("x", "1.2.3.4", 5201)], t)
t = nt.parse_targets("meet=meet.google.com:443,zoom=zoom.us", "tcp")
check("tcp targets: default 443", [(a["host"], a["port"]) for a in t] == [("meet.google.com", 443), ("zoom.us", 443)], t)
t = nt.parse_targets("cf=1.1.1.1", "ping")
check("ping targets have no port", t == [{"name": "cf", "host": "1.1.1.1", "port": None}], t)
check("empty spec is empty", nt.parse_targets("", "ping") == [])

s = sorted([10.0, 20.0, 30.0, 40.0, 50.0])
check("percentile 50", close(nt.percentile(s, 50), 30.0))
check("percentile 95 interpolates", close(nt.percentile(s, 95), 48.0))
check("percentile of nothing", nt.percentile([], 50) is None)
check("jitter: median abs neighbour difference", close(nt.rtt_jitter([10, 14, 12, None, 20, 21]), 2.0))
check("lost runs", nt.lost_runs([1, None, None, 2, None, 3, None]) == [2, 1, 1], nt.lost_runs([1, None, None, 2, None, 3, None]))
check("lost run at the end counted", nt.lost_runs([1, None, None]) == [2])

m = nt.mos_score(40, 5, 0)
check("MOS of a clean path is excellent", 4.3 <= m <= 4.5, m)
check("MOS falls with loss", nt.mos_score(40, 5, 5) < nt.mos_score(40, 5, 1) < m)
check("MOS falls with jitter", nt.mos_score(40, 60, 0) < nt.mos_score(40, 10, 0))
check("MOS falls with latency", nt.mos_score(600, 5, 0) < nt.mos_score(100, 5, 0))
check("MOS never leaves 1..4.5", 1.0 <= nt.mos_score(5000, 500, 100) <= 4.5 and nt.mos_score(1, 0, 0) <= 4.5)

r, why = nt.verdict_for(0.0, 5, 40, 4.4, 0)
check("verdict good", r == 0 and not why, (r, why))
r, why = nt.verdict_for(2.0, 5, 40, 4.2, 0)
check("verdict marginal on loss", r == 1 and "2.0% packet loss" in why, (r, why))
r, why = nt.verdict_for(5.0, 5, 40, 4.0, 0)
check("verdict poor on loss", r == 2, (r, why))
r, why = nt.verdict_for(0.0, 45, 40, 4.2, 0)
check("verdict marginal on jitter", r == 1 and "45 ms jitter" in why, (r, why))
r, why = nt.verdict_for(0.0, 5, 400, 4.2, 0)
check("verdict poor on round trip", r == 2, (r, why))
r, why = nt.verdict_for(0.0, 5, 40, 4.4, 3)
check("3 s dropout is marginal", r == 1 and any("3 s dropout" in w for w in why), (r, why))
r, why = nt.verdict_for(0.0, 5, 40, 4.4, 8)
check("8 s dropout is poor", r == 2, (r, why))
r, why = nt.verdict_for(0.0, 5, 40, 3.7, 0)
check("low score alone gives marginal with a reason", r == 1 and why, (r, why))

# ---- ping output ----------------------------------------------------------------------------------
M = nt.PING_RE
mac = M.search("64 bytes from 1.1.1.1: icmp_seq=0 ttl=57 time=22.123 ms")
lin = M.search("64 bytes from 1.1.1.1: icmp_seq=12 ttl=58 time=12.3 ms")
fast = M.search("64 bytes from 127.0.0.1: icmp_seq=3 ttl=64 time<1 ms")
check("ping regex: macOS line", mac and (mac.group(1), mac.group(2)) == ("0", "22.123"))
check("ping regex: Linux line", lin and (lin.group(1), lin.group(2)) == ("12", "12.3"))
check("ping regex: ignores timeouts", M.search("Request timeout for icmp_seq 4") is None)
check("ping regex: ignores summary", M.search("5 packets transmitted, 5 packets received, 0.0% packet loss") is None)

# ---- iperf3 JSON (captured from a real iperf3 3.22 over loopback) -----------------------------------
p = nt.parse_iperf(fixture("iperf-udp-bidir.json"))
check("iperf bidir: upload summary", p["up"] and p["up"]["packets"] == 626 and p["up"]["loss_pct"] == 0 and close(p["up"]["jitter_ms"], 0.0099, 0.001), p["up"])
check("iperf bidir: download summary", p["down"] and p["down"]["packets"] == 626 and close(p["down"]["jitter_ms"], 0.0148, 0.001), p["down"])
check("iperf bidir: four per-second rows with both directions",
      len(p["intervals"]) == 4 and all("up_jitter" in r and "down_jitter" in r for r in p["intervals"]), p["intervals"][:2])
check("iperf bidir: seconds numbered from 0", [r["t"] for r in p["intervals"]] == [0, 1, 2, 3], [r["t"] for r in p["intervals"]])
p = nt.parse_iperf(fixture("iperf-udp-reverse.json"))
check("iperf reverse: download only", p["down"] and p["up"] is None and all("up_jitter" not in r for r in p["intervals"]), p)
p = nt.parse_iperf(fixture("iperf-udp-forward.json"))
check("iperf forward: upload only", p["up"] and p["down"] is None, p)
check("iperf forward: upload per-second rows come from the server text", len(p["intervals"]) == 3 and all("up_jitter" in r for r in p["intervals"]), p["intervals"])
p = nt.parse_iperf(fixture("iperf-udp-bidir-lossy.json"))
check("iperf lossy: summary loss", close(p["down"]["loss_pct"], 4.952) and close(p["up"]["loss_pct"], 1.917), (p["up"], p["down"]))
check("iperf lossy: spike seconds found", close(p["intervals"][1]["down_jitter"], 41.0) and p["intervals"][1]["down_lost"] == 31
      and close(p["intervals"][2]["up_jitter"], 55.0) and p["intervals"][2]["up_lost"] == 12, p["intervals"])
p = nt.parse_iperf({"start": {}, "intervals": [], "end": {}, "error": "error - the server is busy running a test. try again later"})
check("iperf error text kept", p["error"] and "busy" in p["error"] and p["up"] is None and p["down"] is None, p)
p = nt.parse_iperf({"start": {"test_start": {"reverse": 1}}, "intervals": [], "end": {"sum": {"jitter_ms": 2.0, "lost_packets": 1, "packets": 100, "lost_percent": 1}}})
check("iperf old layout: sums only", p["down"] and close(p["down"]["jitter_ms"], 2.0), p)


# ---- scoring a whole run from fake probes ----------------------------------------------------------
class FakeRtt:
    def __init__(self, vals, kind="ping", error=None):
        self.vals, self.error, self.results, self.replies = vals, error, {i: v for i, v in enumerate(vals) if v is not None}, {}

    def series(self, sent):
        return (self.vals + [None] * sent)[:sent]


class FakeUdp:
    def __init__(self, parsed, error=None):
        self.parsed, self.error, self.mode, self.kbps = parsed, error, "bidir", 1500


def wobble(n, base=30.0):
    return [base + (i % 5) * 2.0 for i in range(n)]


def plan_of(*names):
    return [{"name": n, "host": n + ".test", "port": None, "kind": "ping", "slot": i} for i, n in enumerate(names)]


def run_of(plan, probes, duration=60, partial=False, elapsed=None):
    return nt.build_result("20260101-000000", time.time(), duration, duration if elapsed is None else elapsed, partial, plan, probes, 1500, None)


N = 60
res = run_of(plan_of("a", "b", "c"), {n: {"ping": FakeRtt(wobble(N, base))} for n, base in (("a", 30.0), ("b", 40.0), ("c", 25.0))})
o = res["overall"]
check("clean link: good", o["verdict"] == "GOOD" and o["rank"] == 0 and o["mos"] >= 4.2, o)
check("clean link: numbers present", o["usable"] == 3 and close(o["loss_pct"], 0) and o["longest_lost"] == 0 and o["rtt_avg"] > 0, o)
check("clean link: no complaints (only the missing location fix)", [n for n in res["notes"] if "location fix" not in n] == [], res["notes"])
check("no location file: run is saved without a place and says so", res["place"] is None and res["geo"] is None and any("location fix" in n for n in res["notes"]), res["notes"])

# ---- one lost ping is noise, three are not --------------------------------------------------------
one = wobble(60, 30.0); one[20] = None
three = wobble(60, 30.0); three[20] = three[21] = three[40] = None
r_one = run_of(plan_of("a", "b", "c"), {"a": {"ping": FakeRtt(one)}, "b": {"ping": FakeRtt(wobble(60, 40.0))}, "c": {"ping": FakeRtt(wobble(60, 25.0))}})
check("1 lost ping in 60: not called a weak path", r_one["overall"]["rank"] == 0 and r_one["targets"][0]["verdict"] == "GOOD"
      and not any("weaker" in n for n in r_one["notes"]) and close(r_one["targets"][0]["metrics"]["loss_pct"], 1.67), (r_one["targets"][0]["verdict"], r_one["notes"]))
r_three = run_of(plan_of("a", "b", "c"), {"a": {"ping": FakeRtt(three)}, "b": {"ping": FakeRtt(wobble(60, 40.0))}, "c": {"ping": FakeRtt(wobble(60, 25.0))}})
check("3 lost pings in 60 (5%): still flagged", r_three["targets"][0]["verdict"] != "GOOD" and any("a is weaker" in n for n in r_three["notes"]), (r_three["targets"][0]["verdict"], r_three["notes"]))

# ---- the location fix -----------------------------------------------------------------------------
LOC = TMP / "location.raw"
nt.LOCATION_FILE = LOC
LOC.write_text("44.0521|-123.0868|35.4|Cloverdale|OR\n")
f = nt.location_fix()
check("location fix: place, coordinates (4 places), accuracy", f and f["place"] == "Cloverdale, OR" and f["lat"] == 44.0521 and f["lon"] == -123.0868 and f["accuracy_m"] == 35, f)
LOC.write_text("44.052149|-123.08684|10|(null)|(null)\n")
f = nt.location_fix()
check("location fix: coordinates without a city still count", f and f["place"] is None and f["lat"] == 44.0521 and f["lon"] == -123.0868, f)
LOC.write_text("95.0|-123.0|10|Nowhere|XX\n")
f = nt.location_fix()
check("location fix: impossible latitude is dropped, the place stays", f and f["lat"] is None and f["lon"] is None and f["place"] == "Nowhere, XX", f)
LOC.write_text("abc|def||(null)|(null)\n")
check("location fix: nothing usable -> None", nt.location_fix() is None)
LOC.write_text("44.0|-123.0|10|Cloverdale|OR\n")
old = time.time() - 7 * 3600
os.utime(str(LOC), (old, old))
check("location fix: older than 6 hours -> None", nt.location_fix() is None and nt.place_label() is None)
LOC.unlink()
check("location fix: no file -> None", nt.location_fix() is None)
check("targets keep their colour slot", [tg["slot"] for tg in res["targets"]] == [0, 1, 2])

drop = wobble(N)
for i in range(20, 24):
    drop[i] = None
res = run_of(plan_of("a", "b", "c"), {n: {"ping": FakeRtt(list(drop))} for n in "abc"})
o = res["overall"]
check("shared 4 s dropout: link verdict is at least marginal", o["rank"] >= 1 and o["dropouts"]["events"] == 1 and o["dropouts"]["longest_s"] == 4, o)
check("shared dropout: note blames the link", any("same moment" in n for n in res["notes"]), res["notes"])

lossy = wobble(N)
for i in range(0, N, 10):
    lossy[i + 5] = None                                    # 6 of 60 lost = 10 %
res = run_of(plan_of("a", "b", "c"), {"a": {"ping": FakeRtt(wobble(N))}, "b": {"ping": FakeRtt(wobble(N))}, "c": {"ping": FakeRtt(lossy)}})
check("one bad target: link still good", res["overall"]["rank"] == 0, res["overall"])
check("one bad target: that target is poor and named", [tg["verdict"] for tg in res["targets"]] == ["GOOD", "GOOD", "POOR"]
      and any("c is weaker" in n for n in res["notes"]), (res["targets"][2].get("verdict"), res["notes"]))
check("one bad target: no shared dropout claimed", res["overall"]["dropouts"]["events"] == 0, res["overall"]["dropouts"])

res = run_of(plan_of("a", "b"), {"a": {"ping": FakeRtt([None] * N, error="ping: a.test: Name or service not known")}, "b": {"ping": FakeRtt([None] * N, error="no reply")}})
check("nothing answers: poor, no score", res["overall"]["verdict"] == "POOR" and res["overall"]["mos"] == 1.0 and any("Nothing answered" in n for n in res["notes"]), res["overall"])

res = run_of(plan_of("a", "b"), {"a": {"ping": FakeRtt(wobble(N))}, "b": {"ping": FakeRtt([None] * N, error="ping: b.test: Name or service not known")}})
check("a silent target is reported but does not sink the rest", res["overall"]["verdict"] == "GOOD" and res["targets"][1]["no_data"]
      and any("b did not answer" in n for n in res["notes"]), (res["overall"], res["notes"]))

udp = nt.parse_iperf(fixture("iperf-udp-bidir-lossy.json"))
res = run_of(plan_of("a"), {"a": {"ping": FakeRtt(wobble(N)), "udp": FakeUdp(udp)}})
tg = res["targets"][0]
check("UDP numbers drive jitter and loss", tg["metrics"]["jitter_from"] == "udp" and close(tg["metrics"]["loss_pct"], 4.95) and close(tg["metrics"]["jitter_ms"], 24.5), tg["metrics"])
check("UDP loss makes the target poor", tg["verdict"] == "POOR" and any("packet loss" in r for r in tg["reasons"]), tg.get("reasons"))
res = run_of(plan_of("a"), {"a": {"ping": FakeRtt(wobble(N)), "udp": FakeUdp(None, error="could not run iperf3")}})
check("a failed iperf3 falls back to ping and says so", res["targets"][0]["verdict"] == "GOOD" and any("iperf3: could not run" in n for n in res["notes"]), res["notes"])

# ---- jitter: one stalled second is not a jittery link; ping-only jitter alone cannot make a target poor ----
stall = wobble(60, 40.0)
stall[30] = 1300.0
check("jitter: one stalled second does not make a jittery link", nt.rtt_jitter(stall) <= 10, nt.rtt_jitter(stall))
alt = [30.0 if i % 2 else 91.0 for i in range(60)]
res = run_of(plan_of("a", "b", "c"), {"a": {"ping": FakeRtt(alt)}, "b": {"ping": FakeRtt(wobble(60, 40.0))}, "c": {"ping": FakeRtt(wobble(60, 25.0))}})
ta = res["targets"][0]
check("ping-only jitter of 61 ms: marginal, not poor", ta["metrics"]["jitter_from"] == "rtt" and ta["metrics"]["jitter_ms"] == 61.0 and ta["verdict"] == "MARGINAL"
      and any("61 ms jitter" in r for r in ta["reasons"]), (ta["metrics"], ta["verdict"]))
quiet = nt.parse_iperf(fixture("iperf-udp-bidir.json"))
res = run_of(plan_of("a", "b"), {"a": {"ping": FakeRtt(alt), "udp": FakeUdp(quiet)}, "b": {"ping": FakeRtt(alt)}})
tb = res["targets"][1]
check("a target without its own UDP uses the link's UDP jitter", tb["metrics"]["jitter_from"] == "link" and tb["metrics"]["jitter_ms"] < 1 and tb["verdict"] == "GOOD", tb["metrics"])
check("the target with UDP keeps its own", res["targets"][0]["metrics"]["jitter_from"] == "udp" and res["overall"]["verdict"] == "GOOD", res["targets"][0]["metrics"])
page = nt.render_report(res, history=[])
check("report marks borrowed jitter as link udp", "(link udp)" in page and "(udp)" in page)

import copy
old = copy.deepcopy(res)
for t in old["targets"]:
    t["metrics"]["jitter_ms"], t["verdict"], t["rank"] = 99.0, "POOR", 2
old["overall"].update(verdict="POOR", rank=2)
old["notes"] = ["iperf3: kept note", "b is weaker than the rest (99 ms jitter): that path is the problem, not the whole link."]
once = nt.rescore(copy.deepcopy(old))
check("rescore: verdicts follow the current rules", once["overall"]["verdict"] == "GOOD" and all(t["verdict"] == "GOOD" for t in once["targets"]), once["overall"])
check("rescore: other notes stay, explanatory ones are remade", "iperf3: kept note" in once["notes"] and not any("weaker" in n for n in once["notes"]), once["notes"])
check("rescore: twice gives the same", json.dumps(nt.rescore(copy.deepcopy(once)), sort_keys=True) == json.dumps(once, sort_keys=True))
check("rescore: measurements untouched", once["targets"][0]["rtt"]["series"] == res["targets"][0]["rtt"]["series"] and once["targets"][0]["udp"] == res["targets"][0]["udp"])

res = run_of(plan_of("a"), {"a": {"ping": FakeRtt(wobble(25))}}, duration=600, partial=True, elapsed=26)
check("stopped early: sample count follows the elapsed time", res["partial"] and 20 <= res["sent"] <= 25, res["sent"])

# ---- the report -----------------------------------------------------------------------------------
udp = nt.parse_iperf(fixture("iperf-udp-bidir-lossy.json"))
plan = [{"name": "vps<b>", "host": "x.test", "port": None, "kind": "iperf", "slot": 0},
        {"name": "meet", "host": "meet.test", "port": 443, "kind": "tcp", "slot": 1}]
res = run_of(plan, {"vps<b>": {"ping": FakeRtt(wobble(N)), "udp": FakeUdp(udp)}, "meet": {"ping": FakeRtt(drop)}})
page = nt.render_report(res, history=[{"id": "20260102-030405", "place": "Seaside, OR", "verdict": "GOOD", "rank": 0, "mos": 4.3, "p95": 55.0, "jitter": 4.0, "loss": 0.0, "duration": 600, "partial": False}])
check("report: self-contained page", page.startswith("<!doctype html>") and "http://" not in page.replace("http://www.w3.org", "") and "https://" not in page)
check("report: score, verdict and chart present", "Call quality score" in page and "<svg class=\"plot\"" in page and page.count('class="plot"') >= 4, page.count('class="plot"'))
check("report: a name with markup is escaped", "vps<b>" not in page.replace("\\u003c", "<").split('id="nt-data"')[0] and "vps&lt;b&gt;" in page)
check("report: no stray None or nan", "None" not in page and "nan" not in page.lower().replace("nanosecond", ""), [w for w in ("None", "nan") if w in page])
data = json.loads(page.split('id="nt-data">')[1].split("</script>")[0].replace("\\u003c", "<"))
check("report: hover data for every panel", len(data["panels"]) >= 4 and all(len(v["vals"]) == v["n"] for v in data["panels"].values()), list(data["panels"]))
check("report: shows earlier runs", "Earlier runs" in page and "Seaside, OR" in page)
check("report: dark mode and loss ticks", "prefers-color-scheme:dark" in page and 'class="lostt"' in page)
check("report: no unformatted template tokens", "%(" not in page and "%s" not in page and "%d" not in page)

# ---- end to end: stand-in ping and iperf3, real TCP to a local port ---------------------------------
srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", 0))
srv.listen(16)
tcp_port = srv.getsockname()[1]


def accept_loop():
    while True:
        try:
            c, _ = srv.accept()
            c.close()
        except OSError:
            return


threading.Thread(target=accept_loop, daemon=True).start()


def env_for(runs, extra=None):
    e = dict(os.environ, TV_ENV_FILE=str(TMP / "none.env"), TV_NETTEST_DIR=str(runs), TV_LOCATION_FILE=str(TMP / "location.raw"),
             TV_NETTEST_PING_BIN=FAKEPING, TV_NETTEST_IPERF_BIN=FAKEIPERF)
    e.update(extra or {})
    return e


def run_tool(args, runs, extra=None, timeout=120):
    return subprocess.run([sys.executable, str(ROOT / "nettest.py")] + args, env=env_for(runs, extra), stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, universal_newlines=True, timeout=timeout)


runs1 = TMP / "e2e1"
(TMP / "location.raw").write_text("45.0|-123.9|60|Cloverdale|OR\n")
t0 = time.time()
cp = run_tool(["--duration", "12", "--iperf", "vps=127.0.0.1:5201,refused=127.0.0.1:1", "--ping", "ok=ok.test,loss=loss.test,dead=dead.test",
               "--tcp", "local=127.0.0.1:%d" % tcp_port], runs1)
print("     (took %.0f s)" % (time.time() - t0))
check("e2e: finishes with exit 0", cp.returncode == 0, cp.stdout[-600:])
found = sorted(runs1.glob("*/result.json"))
check("e2e: wrote result.json and report.html", len(found) == 1 and (found[0].parent / "report.html").exists())
if found:
    res = json.loads(found[0].read_text())
    byname = {x["name"]: x for x in res["targets"]}
    check("e2e: all targets present", set(byname) == {"vps", "refused", "ok", "loss", "dead", "local"}, set(byname))
    check("e2e: ok target answered every probe", byname["ok"]["rtt"]["received"] == 12 and byname["ok"]["verdict"] == "GOOD", byname["ok"].get("rtt", {}).get("received"))
    check("e2e: dropout found in the loss target", byname["loss"]["rtt"]["longest_lost"] == 4 and byname["loss"]["rtt"]["lost_total"] == 4, byname["loss"]["rtt"]["lost_total"])
    check("e2e: dead target reported, not fatal", byname["dead"]["no_data"] and any("Name or service" in e for e in byname["dead"]["errors"]), byname["dead"]["errors"])
    check("e2e: TCP probe timed local connects", byname["local"]["rtt"]["source"] == "tcp" and byname["local"]["rtt"]["received"] >= 11, byname["local"]["rtt"]["received"])
    check("e2e: iperf3 numbers attached to the vps target", byname["vps"]["udp"] and byname["vps"]["udp"]["up"] and byname["vps"]["udp"]["down"], byname["vps"]["udp"])
    check("e2e: iperf3 connection refusal is kept as a note", byname["refused"]["udp"] is None and any("Connection refused" in e for e in byname["refused"]["errors"]), byname["refused"]["errors"])
    check("e2e: place picked up from the location fix", res["place"] == "Cloverdale, OR", res["place"])
    check("e2e: raw tool output saved", (found[0].parent / "raw" / "udp-vps.json").exists() and (found[0].parent / "raw" / "ping-ok.txt").exists())
    check("e2e: the shared dropout is not claimed when only one target dropped", res["overall"]["dropouts"]["events"] == 0, res["overall"]["dropouts"])
check("e2e: summary printed", "call quality" in cp.stdout and "Report:" in cp.stdout, cp.stdout[-300:])

time.sleep(1.1)                                                   # run ids are timestamps
cp = run_tool(["--duration", "6", "--iperf", "", "--ping", "ok=ok.test", "--tcp", "local=127.0.0.1:%d" % tcp_port], runs1)
found = sorted(runs1.glob("*/report.html"))
check("e2e: second run lists the first as an earlier run", len(found) == 2 and "Earlier runs" in found[-1].read_text(), len(found))
cp = run_tool(["latest"], runs1)
check("latest prints the newest folder", cp.stdout.strip() == str(found[-1].parent), cp.stdout)
cp = run_tool(["report"], runs1)
check("report subcommand re-renders", cp.returncode == 0 and cp.stdout.strip().endswith("report.html"), cp.stdout)

# ---- Ctrl-C saves a partial result, and a second test cannot start meanwhile -------------------------
runs2 = TMP / "e2e2"
proc = subprocess.Popen([sys.executable, str(ROOT / "nettest.py"), "--duration", "120", "--iperf", "", "--ping", "ok=ok.test", "--tcp", "local=127.0.0.1:%d" % tcp_port],
                        env=env_for(runs2), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
for _ in range(50):
    if (runs2 / "running.json").exists():
        break
    time.sleep(0.2)
check("running.json appears while a test runs", (runs2 / "running.json").exists())
cp = run_tool(["--duration", "10", "--iperf", "", "--ping", "ok=ok.test", "--no-tcp"], runs2)
check("a second test is refused while one runs", cp.returncode == 4 and "already running" in cp.stdout, (cp.returncode, cp.stdout))
time.sleep(4)
proc.send_signal(signal.SIGINT)
try:
    out, _ = proc.communicate(timeout=30)
except subprocess.TimeoutExpired:
    proc.kill()
    out, _ = proc.communicate()
check("Ctrl-C ends the run promptly with a saved report", proc.returncode == 0 and "stopped early" in out, (proc.returncode, out[-300:]))
f = sorted(runs2.glob("*/result.json"))
if f:
    res = json.loads(f[0].read_text())
    check("partial result is marked and short", res["partial"] and res["sent"] < 20, (res["partial"], res["sent"]))
else:
    check("partial result saved", False)
check("running.json removed afterwards", not (runs2 / "running.json").exists())

# ---- a real iperf3 over loopback, if there is one --------------------------------------------------
real = os.environ.get("NETTEST_REAL_IPERF3") or shutil.which("iperf3")
if real:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = subprocess.Popen([real, "-s", "-p", str(port), "--one-off"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.6)
    runs3 = TMP / "e2e3"
    cp = run_tool(["--duration", "8", "--iperf", "loop=127.0.0.1:%d" % port, "--ping", "", "--no-tcp"], runs3,
                  extra={"TV_NETTEST_IPERF_BIN": real, "TV_NETTEST_PING_BIN": FAKEPING})
    server.terminate()
    found = sorted(runs3.glob("*/result.json"))
    if found:
        res = json.loads(found[0].read_text())
        u = res["targets"][0]["udp"]
        check("real iperf3: both directions measured", u and u["up"] and u["down"] and u["mode"] == "bidir", (u, cp.stdout[-400:]))
        if u and u["up"] and u["down"]:
            check("real iperf3: loopback is clean", u["up"]["loss_pct"] < 1 and u["down"]["loss_pct"] < 1 and (u["up"]["jitter_ms"] or 0) < 5, (u["up"], u["down"]))
            check("real iperf3: one row per second, both directions", 6 <= len(u["intervals"]) <= 9 and all("up_jitter" in r and "down_jitter" in r for r in u["intervals"]), u["intervals"])
        check("real iperf3: verdict good", res["targets"][0]["verdict"] == "GOOD", res["targets"][0].get("reasons"))
    else:
        check("real iperf3: run produced a result", False, cp.stdout[-500:])
    cp = run_tool(["--duration", "6", "--iperf", "gone=127.0.0.1:%d" % port, "--ping", "", "--no-tcp"], TMP / "e2e4",
                  extra={"TV_NETTEST_IPERF_BIN": real, "TV_NETTEST_PING_BIN": FAKEPING})
    f = sorted((TMP / "e2e4").glob("*/result.json"))
    r4 = json.loads(f[0].read_text()) if f else None
    check("real iperf3: no server listening is a note, not a crash", r4 and any("iperf3:" in e for e in r4["targets"][0]["errors"]) and r4["targets"][0]["rtt"]["received"] == 6,
          (r4 and r4["targets"][0]["errors"], cp.stdout[-300:]))
else:
    print("skip real iperf3 (not installed)")

# ---- history log and the compare page -------------------------------------------------------------
hist1 = runs1 / "history.jsonl"
lines = hist1.read_text().splitlines() if hist1.exists() else []
check("history: one line per run (2 runs -> 2 lines)", len(lines) == 2, lines[:1])
if lines:
    h0 = json.loads(lines[0])
    first = json.loads(sorted(runs1.glob("*/result.json"))[0].read_text())
    check("history: place, coordinates and accuracy are logged", h0["place"] == "Cloverdale, OR" and h0["lat"] == 45.0 and h0["lon"] == -123.9 and h0["accuracy_m"] == 60, h0)
    check("history: result.json carries the geo tag", first["geo"]["lat"] == 45.0 and first["geo"]["accuracy_m"] == 60 and first["version"] == 3, first.get("geo"))
    check("history: 12 s run -> two 10 s points, four series", all(len(h0["bins"][k]) == 2 for k in ("p50", "p95", "jitter", "loss")) and h0["bin_s"] == 10, h0["bins"])
    check("history: marked short, with the verdict numbers", h0["short"] and h0["verdict"] and h0["mos"] and h0["targets"] and h0["id"] == first["id"], h0)
nt.RUN_DIR = runs1
check("history: syncing again adds nothing", nt.sync_history() == 0)
hist1.unlink()
cp = run_tool(["history"], runs1)
check("history: rebuilt from the saved runs (backfill)", len(hist1.read_text().splitlines()) == 2 and "Cloverdale, OR" in cp.stdout and "*" in cp.stdout, cp.stdout)
with open(str(hist1), "a") as fh:
    fh.write("this is not json\n")
check("history: a damaged line is skipped, not fatal", len(nt.read_history()) == 2)

cp = run_tool(["rescore"], runs1)
check("rescore subcommand: every run, originals kept", cp.returncode == 0 and "2 runs rescored" in cp.stdout and len(list(runs1.glob("*/result.orig.json"))) == 2, cp.stdout)
check("rescore: history rebuilt clean with both runs", len(nt.read_history()) == 2 and "not json" not in hist1.read_text(), hist1.read_text()[:200])
check("rescore: reports redrawn", all((d / "report.html").exists() for d in runs1.glob("2*")))
cp = run_tool(["rescore"], TMP / "empty-runs2")
check("rescore with no runs says so", cp.returncode == 2 and "No runs" in cp.stdout, (cp.returncode, cp.stdout))

b = nt.rtt_bins([{"rtt": {"received": 1, "series": [10.0, 20.0, None, 40.0] + [None] * 6 + [100.0] * 10}}], 20)
check("bins: median, 95th, jitter and loss per 10 s", b["p50"] == [20.0, 100.0] and b["loss"] == [70.0, 0.0] and b["jitter"] == [10.0, 0.0] and b["p95"][1] == 100.0, b)
b = nt.rtt_bins([{"rtt": {"received": 1, "series": [None] * 10 + [5.0, None, None, None, None, None, None, None, None, None]}}], 20)
check("bins: a bin where nothing answered is None, not zero", b["p50"][0] is None and b["jitter"][0] is None and b["loss"][0] == 100.0 and b["jitter"][1] is None, b)

recs = [
    {"v": 1, "id": "20261001-100000", "started": 1790000000, "place": "Cloverdale, OR", "lat": 45.0, "lon": -123.9, "accuracy_m": 60, "sent": 600, "partial": False,
     "short": False, "verdict": "GOOD", "rank": 0, "mos": 4.4, "rtt_avg": 38, "rtt_p95": 83, "jitter_ms": 14, "loss_pct": 0, "longest_lost": 1, "usable": 5, "of": 5,
     "targets": ["a", "b"], "udp": False, "bin_s": 10, "bins": {"p50": [30.0] * 60, "p95": [80.0] * 60, "jitter": [3.0] * 60, "loss": [0.0] * 60}},
    {"v": 1, "id": "20261002-110000", "started": 1790090000, "place": "Seaside <script>alert(1)</script>, OR", "lat": None, "lon": None, "accuracy_m": None, "sent": 600,
     "partial": False, "short": False, "verdict": "POOR", "rank": 2, "mos": 2.9, "rtt_avg": 120, "rtt_p95": 400, "jitter_ms": 50, "loss_pct": 6, "longest_lost": 9, "usable": 5, "of": 5,
     "targets": ["a", "b"], "udp": True, "bin_s": 10, "bins": {"p50": [100.0] * 30 + [None] * 2 + [90.0] * 28, "p95": [400.0] * 30 + [None] * 2 + [300.0] * 28, "jitter": [40.0] * 60, "loss": [5.0] * 60}},
    {"v": 1, "id": "20261003-120000", "started": 1790100000, "place": "Newport, OR", "lat": 44.6, "lon": -124.05, "accuracy_m": 20, "sent": 60, "partial": False,
     "short": True, "verdict": "MARGINAL", "rank": 1, "mos": 4.0, "rtt_avg": 50, "rtt_p95": 120, "jitter_ms": 20, "loss_pct": 1.7, "longest_lost": 1, "usable": 5, "of": 5,
     "targets": ["a", "b"], "udp": False, "bin_s": 10, "bins": {"p50": [50.0] * 6, "p95": [120.0] * 6, "jitter": [9.0] * 6, "loss": [1.0] * 6}}]
page = nt.render_compare(recs)
check("compare: self-contained (no external scripts, styles or fonts)", "<script src" not in page and "<link" not in page and "@import" not in page)
check("compare: place names are escaped in the table", "<script>alert(1)</script>" not in page and "Seaside &lt;script&gt;" in page)
cd = json.loads(page.split('id="cmp-data">')[1].split("</script>")[0].replace("\\u003c", "<"))
check("compare: embedded data is valid, three runs, 8 at most", len(cd["runs"]) == 3 and cd["max"] == 8, cd["max"])
check("compare: short run is not drawn by default", cd["init"] == ["20261001-100000", "20261002-110000"], cd["init"])
check("compare: a run with no coordinates still has a row and no map link", page.count("openstreetmap.org") == 2 and "mlat=45.00000" in page, page.count("openstreetmap.org"))
check("compare: short rows are flagged for hiding", page.count('class="short"') == 1)
check("compare: coordinates and accuracy shown", "45.0000, -123.9000 ±60 m" in page)
check("compare: sorted newest first", page.index("20261003-120000") < page.index("20261002-110000") < page.index("20261001-100000"))
only_short = nt.render_compare(recs[2:])
check("compare: only short runs -> they are drawn anyway", json.loads(only_short.split('id="cmp-data">')[1].split("</script>")[0])["init"] == ["20261003-120000"])
many = [dict(recs[0], id="2026100%d-100000" % i, started=1790000000 + i) for i in range(1, 10)]
cdm = json.loads(nt.render_compare(many).split('id="cmp-data">')[1].split("</script>")[0])
check("compare: nine runs -> the eight newest are drawn", len(cdm["init"]) == 8 and cdm["init"][-1] == "20261009-100000", cdm["init"])
check("compare: empty history is a message, not a crash", "No runs saved" in nt.render_compare([]))
js = page.split("<script>")[-1].split("</script>")[0]
if shutil.which("node"):
    (TMP / "cmp.js").write_text(js)
    cp = subprocess.run(["node", "--check", str(TMP / "cmp.js")], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    check("compare: page script parses", cp.returncode == 0, cp.stdout)
cp = run_tool(["compare"], runs1)
check("compare subcommand writes compare.html next to the runs", cp.returncode == 0 and (runs1 / "compare.html").exists() and "2 runs" in cp.stdout, cp.stdout)
cp = run_tool(["compare", "--out", str(TMP / "x" / "c.html")], runs1)
check("compare --out", (TMP / "x" / "c.html").exists(), cp.stdout)
cp = run_tool(["compare"], TMP / "empty-runs")
check("compare with no runs says so and exits 2", cp.returncode == 2, (cp.returncode, cp.stdout))

srv.close()
shutil.rmtree(TMP, ignore_errors=True)
print()
if fails:
    print("%d FAILED: %s" % (len(fails), ", ".join(fails)))
    sys.exit(1)
print("ALL PASSED")
