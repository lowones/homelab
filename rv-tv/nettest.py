#!/usr/bin/env python3
"""Network health test for video calls. Runs on the Mac mini (or anywhere with Python 3.9+).

    python3 nettest.py                 10-minute test, prints a summary, writes a report
    python3 nettest.py --duration 60   shorter test (seconds)
    python3 nettest.py report [DIR]    re-render report.html from a saved result (default: latest)
    python3 nettest.py latest          print the folder of the latest run

What it does for the whole test, all at once:
  * ping (1 per second) to every target       -> round-trip time, jitter, loss
  * TCP connect timing (1 per second)         -> same idea for hosts that ignore ping (Meet, Zoom, Teams)
  * iperf3 UDP, 1.5 Mbit/s both ways          -> jitter and loss of a call-like stream (needs an iperf3
                                                 server on the other end and `brew install iperf3` here)
Then it scores each target (good / marginal / poor), computes a call-quality score (MOS), and writes
result.json plus a self-contained report.html with a graph per target.

Config (flags win; otherwise tv.env or the environment):
  TV_NETTEST_IPERF    name=host[:port],...   iperf3 servers (also pinged). Port default 5201.
  TV_NETTEST_PING     name=host,...          ping-only targets (default cloudflare=1.1.1.1,google=8.8.8.8)
  TV_NETTEST_TCP      name=host:port,...     TCP-connect targets (default meet, zoom, teams on :443)
  TV_NETTEST_SECONDS  test length (default 600)
  TV_NETTEST_KBPS     UDP stream rate in kbit/s (default 1500)
  TV_NETTEST_DIR      where runs are saved (default .run/nettest next to this file)
  TV_NETTEST_PING_BIN / TV_NETTEST_IPERF_BIN   override the binaries (used by the tests)

Only the standard library is used.
"""
import argparse
import datetime
import html
import json
import math
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent


def load_env():
    """Same tv.env rules as tvserver.py: KEY=VALUE lines, matching quotes stripped, real env wins."""
    f = Path(os.environ.get("TV_ENV_FILE", str(BASE / "tv.env")))
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                v = v.strip()
                if len(v) > 1 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                os.environ.setdefault(k.strip(), v)


load_env()

RUN_DIR = Path(os.environ.get("TV_NETTEST_DIR") or (BASE / ".run" / "nettest"))
LOCATION_FILE = Path(os.environ.get("TV_LOCATION_FILE") or (BASE / ".run" / "location.raw"))
DEFAULT_SECONDS = 600
DEFAULT_PING = "cloudflare=1.1.1.1,google=8.8.8.8"
DEFAULT_TCP = "meet=meet.google.com:443,zoom=zoom.us:443,teams=teams.microsoft.com:443"
UDP_PAYLOAD = 1200           # bytes per datagram, about what a video call sends
TCP_TIMEOUT = 2.0            # seconds before a connect counts as lost
GRACE = 45                   # seconds after the nominal end to let ping and iperf3 wrap up

# ---- how the verdict is decided -------------------------------------------------------------------
# Judgment calls in the range of what Teams and Zoom publish for their network requirements; tune here.
# A target is only as good as its worst number.
GOOD = {"loss": 1.0, "jitter": 30.0, "rtt": 150.0, "mos": 4.0}        # all must hold for GOOD
MARGINAL = {"loss": 3.0, "jitter": 60.0, "rtt": 300.0, "mos": 3.6}    # all must hold for MARGINAL
OUTAGE_MARGINAL = 3          # a dropout this many seconds long makes calls freeze: at least MARGINAL
OUTAGE_POOR = 8              # this long: calls drop: POOR
WINDOW = 10                  # seconds, for the "worst 10 s" number
VERDICTS = ["GOOD", "MARGINAL", "POOR"]

# Categorical colours (reference palette, fixed order, light / dark). A target keeps its slot by its
# position in the configured list, so reorder the list rarely.
SLOTS_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
SLOTS_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]


# ====================================================================================================
# small helpers
# ====================================================================================================
def now_id():
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def parse_targets(spec, kind):
    """'a=host,b=host2:5201' -> [{'name','host','port'}]. kind: ping | iperf | tcp."""
    out = []
    for part in [p.strip() for p in (spec or "").split(",") if p.strip()]:
        if "=" in part:
            name, _, rest = part.partition("=")
        else:
            name, rest = part.split(":")[0].split(".")[0], part
        name, rest = name.strip(), rest.strip()
        host, port = rest, None
        head, sep, tail = rest.rpartition(":")
        if sep and tail.isdigit() and ":" not in head:      # not an IPv6 literal
            host, port = head, int(tail)
        if kind == "iperf" and port is None:
            port = 5201
        if kind == "tcp" and port is None:
            port = 443
        if kind == "ping":
            host, port = rest, None
        if name and host:
            out.append({"name": re.sub(r"[^A-Za-z0-9_.-]", "-", name), "host": host, "port": port})
    return out


def percentile(sorted_vals, p):
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p / 100.0
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def rtt_jitter(series):
    """Mean absolute difference between neighbouring replies (neighbours = consecutive seconds)."""
    diffs = [abs(b - a) for a, b in zip(series, series[1:]) if a is not None and b is not None]
    return sum(diffs) / len(diffs) if diffs else 0.0


def lost_runs(series):
    """Lengths of consecutive lost samples, in order."""
    runs, cur = [], 0
    for v in series:
        if v is None:
            cur += 1
        elif cur:
            runs.append(cur)
            cur = 0
    if cur:
        runs.append(cur)
    return runs


def mos_score(rtt_ms, jitter_ms, loss_pct):
    """Simplified E-model (the usual VoIP monitoring shortcut): one-way latency = rtt/2, jitter counts
    double, loss costs 2.5 points per percent. Treat it as a relative score, 4.3+ is excellent."""
    eff = (rtt_ms or 0) / 2.0 + 2 * (jitter_ms or 0) + 10
    r = 93.2 - eff / 40.0 if eff < 160 else 93.2 - (eff - 120) / 10.0
    r -= 2.5 * (loss_pct or 0)
    r = max(0.0, min(100.0, r))
    return max(1.0, min(4.5, 1 + 0.035 * r + 7e-6 * r * (r - 60) * (100 - r)))


def level(value, good, marginal, higher_is_better=False):
    """0 good, 1 marginal, 2 poor."""
    if value is None:
        return 0
    if higher_is_better:
        return 0 if value >= good else 1 if value >= marginal else 2
    return 0 if value <= good else 1 if value <= marginal else 2


def verdict_for(loss, jitter, rtt_p95, mos, longest_lost):
    """-> (rank 0..2, [reasons]). Reasons name every number that cost a level."""
    checks = [
        ("loss", loss, "%.1f%% packet loss" % (loss or 0)),
        ("jitter", jitter, "%.0f ms jitter" % (jitter or 0)),
        ("rtt", rtt_p95, "%.0f ms round trip (95th percentile)" % (rtt_p95 or 0)),
    ]
    rank, reasons = 0, []
    for key, val, text in checks:
        lv = level(val, GOOD[key], MARGINAL[key])
        if lv:
            reasons.append(text)
        rank = max(rank, lv)
    lv = level(mos, GOOD["mos"], MARGINAL["mos"], higher_is_better=True)
    if lv and not reasons:
        reasons.append("call-quality score %.1f" % mos)
    rank = max(rank, lv)
    lv = 2 if longest_lost >= OUTAGE_POOR else 1 if longest_lost >= OUTAGE_MARGINAL else 0
    if lv:
        reasons.append("a %d s dropout" % longest_lost)
    return max(rank, lv), reasons


def summary_sentence(rank):
    return ["Video calls should work well.",
            "Video calls will mostly work, with occasional glitches or freezes.",
            "Video calls will struggle: expect freezing, robotic audio or dropped calls."][rank]


def place_label():
    """'Cloverdale, OR' from the Mac's own location fix if it is fresh, else None."""
    try:
        if time.time() - LOCATION_FILE.stat().st_mtime > 6 * 3600:
            return None
        parts = LOCATION_FILE.read_text().strip().split("|")
        city, state = parts[3].strip(), parts[4].strip()
        if city and city != "(null)":
            return "%s, %s" % (city, state) if state and state != "(null)" else city
    except (OSError, IndexError):
        pass
    return None


# ====================================================================================================
# parsing what the tools print
# ====================================================================================================
PING_RE = re.compile(r"icmp_seq[=: ]+(\d+).*?time[=<]\s*([\d.]+)\s*ms")
UDP_SERVER_LINE = re.compile(
    r"^\[\s*\d+\](?:\[RX-S\])?\s+(\d+\.\d+)-(\d+\.\d+)\s+sec\s+.*?(\d+\.\d+)\s+ms\s+(\d+)/(\d+)\s+\(([^)]+)\)\s*$")


def _pct(v):
    try:
        return float(str(v).strip().rstrip("%"))
    except ValueError:
        return None


def parse_iperf(doc):
    """Reads iperf3 -u -J (optionally --bidir, --get-server-output) output.
    Returns {'up': {...}|None, 'down': {...}|None, 'intervals': [...], 'error': str|None}.
    up = this machine -> server, measured by the server; down = server -> this machine."""
    res = {"up": None, "down": None, "intervals": [], "error": doc.get("error")}

    def summ(u):
        return {"jitter_ms": u.get("jitter_ms"), "loss_pct": _pct(u.get("lost_percent")) or 0.0,
                "lost": u.get("lost_packets", 0), "packets": u.get("packets", 0)}

    end = doc.get("end") or {}
    for s in end.get("streams", []):
        u = s.get("udp")
        if u and "jitter_ms" in u:
            res["up" if u.get("sender") else "down"] = summ(u)
    if res["up"] is None and res["down"] is None:          # older layout: only the sums
        ts = (doc.get("start") or {}).get("test_start") or {}
        if "jitter_ms" in (end.get("sum") or {}):
            res["down" if (ts.get("reverse") and not ts.get("bidir")) else "up"] = summ(end["sum"])
        if "jitter_ms" in (end.get("sum_bidir_reverse") or {}):
            res["down"] = summ(end["sum_bidir_reverse"])

    by_t = {}
    for iv in doc.get("intervals", []):                    # download, measured here, one per second
        jit, lost, pk = [], 0, 0
        t = None
        for s in iv.get("streams", []):
            if s.get("sender") is False and "jitter_ms" in s:
                jit.append(s["jitter_ms"])
                lost += s.get("lost_packets", 0)
                pk += s.get("packets", 0)
                t = int(round(s.get("start", 0)))
        if jit and t is not None:
            by_t.setdefault(t, {})["down"] = {"jitter_ms": sum(jit) / len(jit), "lost": lost, "packets": pk}
    for line in (doc.get("server_output_text") or "").splitlines():     # upload, measured by the server
        m = UDP_SERVER_LINE.match(line.strip())
        if m and abs((float(m.group(2)) - float(m.group(1))) - 1.0) < 0.2:
            by_t.setdefault(int(round(float(m.group(1)))), {})["up"] = {
                "jitter_ms": float(m.group(3)), "lost": int(m.group(4)), "packets": int(m.group(5))}
    for t in sorted(by_t):
        row = {"t": t}
        for d in ("up", "down"):
            if d in by_t[t]:
                row[d + "_jitter"] = round(by_t[t][d]["jitter_ms"], 3)
                row[d + "_lost"] = by_t[t][d]["lost"]
                row[d + "_packets"] = by_t[t][d]["packets"]
        res["intervals"].append(row)
    return res


# ====================================================================================================
# probes (one thread each; all run for the whole test)
# ====================================================================================================
class Probe(threading.Thread):
    def __init__(self, target, duration, stop):
        threading.Thread.__init__(self, daemon=True)
        self.target, self.duration, self.stop = target, duration, stop
        self.error = None
        self.raw = ""
        self.finished_cleanly = False
        self.proc = None

    def kill(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except OSError:
                pass


class PingProbe(Probe):
    kind = "ping"

    def __init__(self, target, duration, stop, binary):
        Probe.__init__(self, target, duration, stop)
        self.binary = binary
        self.replies = {}                       # second index -> ms
        self.base = 0 if sys.platform == "darwin" else 1      # icmp_seq starts at 0 on macOS, 1 on Linux

    def run(self):
        cmd = [self.binary, "-c", str(self.duration), "-i", "1", "-n", self.target["host"]]
        lines = []
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         universal_newlines=True, bufsize=1)
            for line in self.proc.stdout:
                lines.append(line)
                m = PING_RE.search(line)
                if m:
                    idx = int(m.group(1)) - self.base
                    if idx >= 0 and idx not in self.replies:
                        self.replies[idx] = float(m.group(2))
            self.proc.wait()
            self.finished_cleanly = not self.stop.is_set()
        except OSError as e:
            self.error = "could not run ping (%s)" % e
        self.raw = "".join(lines)
        if not self.replies and not self.error:
            tail = [l.strip() for l in lines if l.strip()]
            self.error = tail[-1][:160] if tail else "ping printed nothing"

    def series(self, sent):
        return [self.replies.get(i) for i in range(sent)]


class TcpProbe(Probe):
    kind = "tcp"

    def __init__(self, target, duration, stop):
        Probe.__init__(self, target, duration, stop)
        self.results = {}

    def run(self):
        host, port = self.target["host"], self.target["port"]
        try:
            infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
            addr = infos[0][4]
        except OSError as e:
            self.error = "could not look up %s (%s)" % (host, e)
            return
        t0 = time.time()
        for i in range(self.duration):
            wait = t0 + i - time.time()
            if wait > 0 and self.stop.wait(wait):
                return
            start = time.perf_counter()
            try:
                s = socket.create_connection(addr, timeout=TCP_TIMEOUT)
                self.results[i] = (time.perf_counter() - start) * 1000.0
                s.close()
            except OSError as e:
                if not self.error:
                    self.error = "%s: %s" % (host, e)
        self.finished_cleanly = True
        if self.results:
            self.error = None

    def series(self, sent):
        return [self.results.get(i) for i in range(sent)]


def iperf_caps(binary):
    """Which options this iperf3 understands, read from its own help text."""
    try:
        p = subprocess.run([binary, "--help"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           universal_newlines=True, timeout=10)
        text = p.stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return {"bidir": "--bidir" in text, "server_output": "--get-server-output" in text,
            "connect_timeout": "--connect-timeout" in text}


class IperfProbe(Probe):
    kind = "udp"

    def __init__(self, target, duration, stop, binary, caps, kbps, raw_dir):
        Probe.__init__(self, target, duration, stop)
        self.binary, self.caps, self.kbps, self.raw_dir = binary, caps, kbps, raw_dir
        self.parsed = None
        self.mode = None

    def command(self, bidir):
        c = [self.binary, "-c", self.target["host"], "-p", str(self.target["port"]), "-u",
             "-b", "%dK" % self.kbps, "-l", str(UDP_PAYLOAD), "-t", str(self.duration), "-i", "1", "-J"]
        c.append("--bidir" if bidir else "-R")
        if self.caps.get("server_output"):
            c.append("--get-server-output")
        if self.caps.get("connect_timeout"):
            c += ["--connect-timeout", "8000"]
        return c

    def run(self):
        for bidir in ([True, False] if self.caps.get("bidir") else [False]):
            try:
                self.proc = subprocess.Popen(self.command(bidir), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                             universal_newlines=True)
                out, err = self.proc.communicate()
            except OSError as e:
                self.error = "could not run iperf3 (%s)" % e
                return
            self.raw = out or err or ""
            try:
                doc = json.loads(out)
            except ValueError:
                doc = None
            if doc is None:
                self.error = (err or out or "iperf3 gave no output").strip().splitlines()[-1][:160]
                if bidir and "unrecognized" in (err or "").lower():
                    continue
                return
            self.parsed = parse_iperf(doc)
            self.mode = "bidir" if bidir else "download only"
            if self.parsed["error"]:
                self.error = self.parsed["error"]
                if "busy" in self.error:
                    self.error += " (someone else is running a test on it)"
            elif self.parsed["up"] is None and self.parsed["down"] is None:
                self.error = "iperf3 finished but reported no UDP numbers (raw output is saved)"
            self.finished_cleanly = not self.stop.is_set()
            return


# ====================================================================================================
# turning samples into numbers
# ====================================================================================================
def rtt_stats(series, source):
    got = [v for v in series if v is not None]
    srt = sorted(got)
    sent = len(series)
    runs = lost_runs(series)
    worst = None
    if sent:
        w = min(WINDOW, sent)
        for s in range(0, sent - w + 1):
            win = series[s:s + w]
            lost = sum(1 for v in win if v is None)
            vals = [v for v in win if v is not None]
            key = (lost, sum(vals) / len(vals) if vals else 1e9)
            if worst is None or key > worst[0]:
                worst = (key, {"start_s": s, "seconds": w, "lost": lost,
                               "avg": round(sum(vals) / len(vals), 1) if vals else None,
                               "max": round(max(vals), 1) if vals else None})
    return {"source": source, "sent": sent, "received": len(got),
            "loss_pct": round(100.0 * (sent - len(got)) / sent, 2) if sent else 0.0,
            "min": round(srt[0], 1) if srt else None,
            "avg": round(sum(got) / len(got), 1) if got else None,
            "p50": round(percentile(srt, 50), 1) if srt else None,
            "p95": round(percentile(srt, 95), 1) if srt else None,
            "p99": round(percentile(srt, 99), 1) if srt else None,
            "max": round(srt[-1], 1) if srt else None,
            "jitter": round(rtt_jitter(series), 1),
            "lost_total": sent - len(got), "lost_events": len(runs), "longest_lost": max(runs) if runs else 0,
            "worst_window": worst[1] if worst else None,
            "series": [None if v is None else round(v, 1) for v in series]}


def summarize_target(entry, probes, sent):
    """entry: {'name','host','port','slot'}; probes: {'ping'|'tcp'|'udp': Probe}."""
    t = {"name": entry["name"], "host": entry["host"], "port": entry.get("port"), "slot": entry["slot"],
         "rtt": None, "udp": None, "errors": [], "no_data": False}
    src = "ping" if "ping" in probes else "tcp" if "tcp" in probes else None
    if src:
        p = probes[src]
        if p.error and not (src == "tcp" and p.results):
            t["errors"].append(p.error)
        t["rtt"] = rtt_stats(p.series(sent), src)
    u = probes.get("udp")
    if u is not None:
        if u.error:
            t["errors"].append("iperf3: " + u.error)
        if u.parsed and (u.parsed["up"] or u.parsed["down"]):
            t["udp"] = {"mode": u.mode, "up": u.parsed["up"], "down": u.parsed["down"],
                        "intervals": u.parsed["intervals"], "kbps": u.kbps}
    rtt = t["rtt"]
    have_rtt = bool(rtt and rtt["received"])
    if not have_rtt and not t["udp"]:
        t["no_data"] = True
        return t
    losses = [rtt["loss_pct"]] if rtt else []
    jitters = []
    if t["udp"]:
        for d in ("up", "down"):
            if t["udp"][d]:
                losses.append(t["udp"][d]["loss_pct"])
                if t["udp"][d]["jitter_ms"] is not None:
                    jitters.append(t["udp"][d]["jitter_ms"])
    loss = max(losses) if losses else 0.0
    jitter = max(jitters) if jitters else (rtt["jitter"] if rtt else 0.0)
    avg = rtt["avg"] if have_rtt else 0.0
    p95 = rtt["p95"] if have_rtt else None
    longest = rtt["longest_lost"] if rtt else 0
    mos = mos_score(avg, jitter, loss)
    rank, reasons = verdict_for(loss, jitter, p95, mos, longest)
    t["metrics"] = {"loss_pct": round(loss, 2), "jitter_ms": round(jitter, 1),
                    "rtt_avg": avg, "rtt_p95": p95, "mos": round(mos, 2), "longest_lost": longest,
                    "jitter_from": "udp" if jitters else "rtt"}
    t["rank"], t["verdict"], t["reasons"] = rank, VERDICTS[rank], reasons
    return t


def median_of(vals):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    n = len(vals)
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0


def common_dropouts(targets, sent):
    """Seconds when at least half of the targets (and at least two) lost the same sample."""
    series = [t["rtt"]["series"] for t in targets if t.get("rtt") and t["rtt"]["received"]]
    if len(series) < 2 or not sent:
        return []
    need = int(math.ceil(len(series) / 2.0))
    flags = [sum(1 for s in series if i < len(s) and s[i] is None) >= need for i in range(sent)]
    return lost_runs([None if f else 0 for f in flags])


def build_result(run_id, started, duration, elapsed, partial, plan, probes_by_target, kbps, iperf_note):
    sent = duration if not partial else max(1, min(duration, int(elapsed) - 1))
    targets = [summarize_target(e, probes_by_target[e["name"]], sent) for e in plan]
    usable = [t for t in targets if not t["no_data"]]
    notes = []
    if iperf_note:
        notes.append(iperf_note)
    for t in targets:
        if t["no_data"]:
            notes.append("%s did not answer at all (%s)." % (t["name"], t["errors"][0] if t["errors"] else "no replies"))
        else:
            for e in t["errors"]:
                notes.append("%s: %s" % (t["name"], e))
    drops = common_dropouts(targets, sent)
    longest_common = max(drops) if drops else 0
    if not usable:
        rank = 2
        notes.insert(0, "Nothing answered. The link may be down, or this machine has no route out.")
        overall = {"rank": 2, "verdict": "POOR", "summary": "No network: nothing answered.", "mos": 1.0}
    else:
        ranks = sorted(t["rank"] for t in usable)
        rank = ranks[len(ranks) // 2]                       # the typical target; the upper one on a tie
        if longest_common >= OUTAGE_POOR:
            rank = 2
        elif longest_common >= OUTAGE_MARGINAL:
            rank = max(rank, 1)
        mos = median_of(t["metrics"]["mos"] for t in usable)
        overall = {"rank": rank, "verdict": VERDICTS[rank], "summary": summary_sentence(rank), "mos": round(mos, 2)}
        overall.update({
            "rtt_avg": round(median_of(t["metrics"]["rtt_avg"] for t in usable if t["rtt"] and t["rtt"]["received"]) or 0, 1),
            "rtt_p95": round(median_of(t["metrics"]["rtt_p95"] for t in usable if t["metrics"]["rtt_p95"] is not None) or 0, 1),
            "jitter_ms": round(median_of(t["metrics"]["jitter_ms"] for t in usable) or 0, 1),
            "loss_pct": round(median_of(t["metrics"]["loss_pct"] for t in usable) or 0, 2),
            "longest_lost": max([t["metrics"]["longest_lost"] for t in usable] + [longest_common]),
            "usable": len(usable), "of": len(targets)})
        if drops:
            notes.insert(0, "%d dropout%s hit most targets at the same moment (longest %d s). That points at the link "
                            "itself (dish view, Wi-Fi), not at one server." % (len(drops), "" if len(drops) == 1 else "s", longest_common))
        weak = [t for t in usable if t["rank"] > rank]
        for t in weak:
            notes.append("%s is weaker than the rest (%s): that path is the problem, not the whole link."
                         % (t["name"], ", ".join(t["reasons"]) or t["verdict"].lower()))
        if rank and not drops:
            bad = [t for t in usable if t["rank"] == rank and t["reasons"]]
            if bad:
                notes.append("Most affected: %s." % "; ".join("%s (%s)" % (t["name"], ", ".join(t["reasons"])) for t in bad[:3]))
    overall["dropouts"] = {"events": len(drops), "longest_s": longest_common}
    return {"version": 1, "id": run_id, "started": started, "duration_s": duration, "sent": sent,
            "elapsed_s": round(elapsed, 1), "partial": partial, "host": platform.node(), "place": place_label(),
            "udp_kbps": kbps, "targets": targets, "overall": overall, "notes": notes,
            "thresholds": {"good": GOOD, "marginal": MARGINAL, "outage_marginal_s": OUTAGE_MARGINAL,
                           "outage_poor_s": OUTAGE_POOR}}


# ====================================================================================================
# running a test
# ====================================================================================================
def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def running_info():
    """The status of a test in progress, or None."""
    f = RUN_DIR / "running.json"
    try:
        d = json.loads(f.read_text())
    except (OSError, ValueError):
        return None
    if not pid_alive(int(d.get("pid", 0))):
        return None
    return d


def write_status(d):
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    tmp = RUN_DIR / "running.tmp"
    tmp.write_text(json.dumps(d))
    os.replace(str(tmp), str(RUN_DIR / "running.json"))


def build_plan(args):
    iperf = parse_targets(args.iperf if args.iperf is not None else os.environ.get("TV_NETTEST_IPERF", ""), "iperf")
    ping = parse_targets(args.ping if args.ping is not None else os.environ.get("TV_NETTEST_PING", DEFAULT_PING), "ping")
    tcp = [] if args.no_tcp else parse_targets(args.tcp if args.tcp is not None else os.environ.get("TV_NETTEST_TCP", DEFAULT_TCP), "tcp")
    plan, seen = [], set()
    for kind, items in (("iperf", iperf), ("ping", ping), ("tcp", tcp)):
        for it in items:
            name = it["name"]
            while name in seen:
                name += "2"
            seen.add(name)
            plan.append(dict(it, name=name, kind=kind, slot=len(plan) % len(SLOTS_LIGHT)))
    return plan


def run_test(args, say=print):
    duration = args.duration
    plan = build_plan(args)
    if not plan:
        say("No targets configured.")
        return 2
    live = running_info()
    if live:
        say("A test is already running (started %s, pid %s)." % (live.get("id"), live.get("pid")))
        return 4
    ping_bin = os.environ.get("TV_NETTEST_PING_BIN") or shutil.which("ping") or "ping"
    iperf_bin = os.environ.get("TV_NETTEST_IPERF_BIN") or shutil.which("iperf3")
    caps = iperf_caps(iperf_bin) if iperf_bin else None
    kbps = int(args.kbps)
    run_id = now_id()
    out_dir = RUN_DIR / run_id
    (out_dir / "raw").mkdir(parents=True, exist_ok=True)
    stop = threading.Event()

    def on_signal(signum, frame):
        stop.set()
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    iperf_note = None
    probes, by_target = [], {}
    for e in plan:
        mine = {}
        if e["kind"] in ("iperf", "ping"):
            mine["ping"] = PingProbe(e, duration, stop, ping_bin)
        if e["kind"] == "tcp":
            mine["tcp"] = TcpProbe(e, duration, stop)
        if e["kind"] == "iperf":
            if caps:
                mine["udp"] = IperfProbe(e, duration, stop, iperf_bin, caps, kbps, out_dir / "raw")
            else:
                iperf_note = ("iperf3 is not installed here, so jitter and loss come from ping only. "
                              "Run `brew install iperf3` for a call-like UDP test.")
        by_target[e["name"]] = mine
        probes.extend(mine.values())

    started = time.time()
    say("Testing %d target%s for %d s (%s). Ctrl-C stops early and still saves a report."
        % (len(plan), "" if len(plan) == 1 else "s", duration, ", ".join(e["name"] for e in plan)))
    write_status({"id": run_id, "pid": os.getpid(), "started": started, "duration_s": duration, "elapsed_s": 0})
    for p in probes:
        p.start()
    deadline = started + duration + GRACE
    last_say = started
    try:
        while any(p.is_alive() for p in probes) and not stop.is_set() and time.time() < deadline:
            time.sleep(0.5)
            el = time.time() - started
            if int(el) % 5 == 0:
                write_status({"id": run_id, "pid": os.getpid(), "started": started, "duration_s": duration,
                              "elapsed_s": round(el, 1)})
            if time.time() - last_say >= 30 and el < duration:
                last_say = time.time()
                say("  %3d / %d s" % (el, duration))
    finally:
        partial = stop.is_set()
        stop.set()
        for p in probes:
            p.kill()
        for p in probes:
            p.join(timeout=5)
        try:
            (RUN_DIR / "running.json").unlink()
        except OSError:
            pass
    elapsed = time.time() - started          # partial means interrupted (Ctrl-C, SIGTERM), not "probes ended early"
    for e in plan:
        for kind, p in by_target[e["name"]].items():
            if p.raw:
                (out_dir / "raw" / ("%s-%s.%s" % (kind, e["name"], "json" if kind == "udp" else "txt"))).write_text(p.raw)
    result = build_result(run_id, started, duration, elapsed, partial, plan, by_target, kbps, iperf_note)
    (out_dir / "result.json").write_text(json.dumps(result))
    write_report(out_dir)
    print_summary(result, out_dir, say)
    return 0 if any(not t["no_data"] for t in result["targets"]) else 3


# ====================================================================================================
# text summary
# ====================================================================================================
def fmt(v, unit="", nd=0):
    return "-" if v is None else ("%.*f%s" % (nd, v, unit))


def print_summary(r, out_dir, say=print):
    o = r["overall"]
    say("")
    say("%s  call quality %.1f / 5   %s%s" % (o["verdict"], o["mos"], o["summary"], "   (stopped early)" if r["partial"] else ""))
    if o.get("usable"):
        say("typical target: round trip %s (95th %s), jitter %s, loss %s, longest dropout %d s" % (
            fmt(o["rtt_avg"], " ms"), fmt(o["rtt_p95"], " ms"), fmt(o["jitter_ms"], " ms"),
            fmt(o["loss_pct"], "%", 1), o["longest_lost"]))
    say("")
    say("%-12s %-9s %6s %7s %7s %8s %7s %6s %9s" % ("target", "verdict", "MOS", "avg ms", "p95 ms", "jitter", "loss", "drop", "UDP j/l"))
    for t in r["targets"]:
        if t["no_data"]:
            say("%-12s %-9s no answer" % (t["name"], "-"))
            continue
        m = t["metrics"]
        udp = ""
        if t["udp"]:
            parts = []
            for d, arrow in (("up", "^"), ("down", "v")):
                if t["udp"][d]:
                    parts.append("%s%.1fms/%.1f%%" % (arrow, t["udp"][d]["jitter_ms"] or 0, t["udp"][d]["loss_pct"]))
            udp = " ".join(parts)
        say("%-12s %-9s %6.2f %7s %7s %6.1f ms %6.2f%% %4d s %s" % (
            t["name"], t["verdict"], m["mos"], fmt(m["rtt_avg"], "", 0), fmt(m["rtt_p95"], "", 0),
            m["jitter_ms"], m["loss_pct"], m["longest_lost"], udp))
    for n in r["notes"]:
        say("  - " + n)
    say("")
    say("Report: %s" % (Path(out_dir) / "report.html"))


# ====================================================================================================
# report: one self-contained HTML page with inline SVG
# ====================================================================================================
CSS = """
:root{color-scheme:light dark;--bg:#f4f4f2;--card:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--ink3:#77766f;--grid:#e6e5e1;
--good:#0ca30c;--warn:#fab219;--bad:#d03b3b;--lost:#d03b3b;%(light)s}
@media (prefers-color-scheme:dark){:root{--bg:#121211;--card:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--ink3:#8f8e86;--grid:#2c2c2a;%(dark)s}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,sans-serif}
main{max-width:940px;margin:0 auto;padding:20px 16px 40px}
h1{font-size:20px;margin:0 0 2px;font-weight:650}
h2{font-size:15px;margin:28px 0 10px;font-weight:650}
.sub{color:var(--ink2);font-size:14px}
.card{background:var(--card);border:1px solid var(--grid);border-radius:14px;padding:16px}
.hero{display:flex;flex-wrap:wrap;gap:20px 36px;align-items:center;margin-top:14px}
.mos{font-size:56px;line-height:1;font-weight:650;letter-spacing:-.02em}
.mos small{display:block;font-size:13px;font-weight:500;letter-spacing:0;color:var(--ink2);margin-top:6px}
.chip{display:inline-flex;align-items:center;gap:8px;font-weight:650;font-size:18px}
.chip svg{flex:none}
.say{color:var(--ink2);margin:4px 0 0;max-width:34em}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(150px,100%%),1fr));gap:10px;margin-top:12px}
.tile{background:var(--card);border:1px solid var(--grid);border-radius:12px;padding:12px 14px}
.tile .l{font-size:13px;color:var(--ink2)}
.tile .v{font-size:26px;font-weight:650;margin-top:2px}
.tile .v small{font-size:14px;font-weight:500;color:var(--ink2)}
.notes{margin:10px 0 0;padding-left:20px;color:var(--ink2)}
.notes li{margin:3px 0}
.panels{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(380px,100%%),1fr));gap:12px}
.ph{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.key{width:18px;height:0;border-top:2px solid;border-radius:1px;flex:none}
.ph b{font-weight:650}
.ph .host{color:var(--ink3);font-size:13px}
.ph .mini{margin-left:auto;display:inline-flex;align-items:center;gap:5px;font-size:13px;font-weight:600}
.stats{color:var(--ink2);font-size:13px;margin:4px 0 2px}
svg.plot{width:100%%;height:auto;display:block;touch-action:pan-y}
.gl{stroke:var(--grid);stroke-width:1;fill:none}
.ax{fill:var(--ink3);font-size:10px}
.ln{fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.lostt{stroke:var(--lost);stroke-width:2}
.thr{stroke:var(--ink3);stroke-width:1;fill:none}
.thrt{fill:var(--ink3);font-size:9px}
.cross{stroke:var(--ink3);stroke-width:1;display:none}
.dot{display:none}
#tip{position:fixed;display:none;pointer-events:none;background:var(--card);color:var(--ink);border:1px solid var(--grid);
border-radius:8px;padding:6px 9px;font-size:12px;box-shadow:0 2px 10px rgba(0,0,0,.18);z-index:5;white-space:nowrap}
#tip b{font-size:15px;display:block}
#tip span{color:var(--ink2)}
table{width:100%%;border-collapse:collapse;font-size:13px}
th,td{padding:7px 8px;text-align:right;border-bottom:1px solid var(--grid);white-space:nowrap}
th:first-child,td:first-child{text-align:left}
th{color:var(--ink2);font-weight:600}
td{font-variant-numeric:tabular-nums}
.scroll{overflow-x:auto}
.legendnote{color:var(--ink2);font-size:13px;margin:6px 0 0;display:flex;align-items:center;gap:6px}
.legendnote i{display:inline-block;width:2px;height:10px;background:var(--lost)}
.foot{color:var(--ink3);font-size:12px;margin-top:22px}
"""

JS = """
(function(){
var D=JSON.parse(document.getElementById('nt-data').textContent), tip=document.getElementById('tip');
function fmtT(s){var m=Math.floor(s/60),x=s%60;return m+':'+(x<10?'0':'')+x}
document.querySelectorAll('svg.plot').forEach(function(svg){
  var P=D.panels[svg.dataset.panel]; if(!P)return;
  var cross=svg.querySelector('.cross'),dot=svg.querySelector('.dot');
  function at(ev){
    var r=svg.getBoundingClientRect(),x=(ev.clientX-r.left)*(P.w/r.width);
    var i=Math.round((x-P.l)/P.pw*(P.n-1)); i=Math.max(0,Math.min(P.n-1,i));
    var px=P.l+P.pw*i/(Math.max(P.n-1,1)),v=P.vals[i];
    cross.setAttribute('x1',px);cross.setAttribute('x2',px);cross.style.display='block';
    if(v===null){dot.style.display='none'}else{
      var y=P.t+P.ph-P.ph*Math.min(v,P.ymax)/P.ymax;dot.setAttribute('cx',px);dot.setAttribute('cy',y);dot.style.display='block'}
    tip.textContent='';
    var b=document.createElement('b');b.textContent=(v===null?P.lostWord:v+' '+P.unit);tip.appendChild(b);
    var s=document.createElement('span');s.textContent=P.name+' at '+fmtT(P.t0+i*P.step);tip.appendChild(s);
    tip.style.display='block';
    tip.style.left=Math.min(ev.clientX+14,window.innerWidth-tip.offsetWidth-8)+'px';
    tip.style.top=Math.max(ev.clientY-tip.offsetHeight-12,6)+'px';
  }
  svg.addEventListener('pointermove',at);svg.addEventListener('pointerdown',at);
  svg.addEventListener('pointerleave',function(){cross.style.display='none';dot.style.display='none';tip.style.display='none'});
});
})();
"""


def esc(s):
    return html.escape(str(s), quote=True)


def nice_ceil(v, floor):
    v = max(v, floor)
    mag = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 2.5, 5, 10):
        if v <= m * mag:
            return m * mag
    return 10 * mag


def icon(rank):
    """Status icon + colour: shape carries the meaning, colour backs it up."""
    if rank == 0:
        return ('<svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="9" fill="var(--good)"/>'
                '<path d="M5.6 10.4l3 3 5.8-6.4" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>')
    if rank == 1:
        return ('<svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true"><path d="M10 1.8l9 15.4H1z" fill="var(--warn)" stroke="var(--warn)" stroke-width="1.5" stroke-linejoin="round"/>'
                '<path d="M10 7.6v4.4" stroke="#1a1a19" stroke-width="2" stroke-linecap="round"/><circle cx="10" cy="14.3" r="1.1" fill="#1a1a19"/></svg>')
    return ('<svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="9" fill="var(--bad)"/>'
            '<path d="M6.4 6.4l7.2 7.2M13.6 6.4l-7.2 7.2" stroke="#fff" stroke-width="2" stroke-linecap="round"/></svg>')


def x_ticks(n):
    step = 120 if n > 300 else 60 if n > 120 else 30 if n > 60 else 10
    return list(range(0, n, step))


def panel_svg(pid, vals, unit, color_var, ymax, name, lost_word, panels, threshold=None, step=1, t0=0, ticks=None):
    """Line chart of one series with a lane of red ticks for lost samples (ticks: one bool per sample, default
    "no value"). Registers the hover data in panels."""
    W, H, L, R, T, B = 440, 152, 38, 8, 8, 40
    PW, PH = W - L - R, H - T - B
    n = len(vals)
    span = max(n - 1, 1)
    xs = lambda i: L + PW * i / float(span)
    ys = lambda v: T + PH - PH * min(v, ymax) / float(ymax)
    parts = ['<svg class="plot" viewBox="0 0 %d %d" data-panel="%s" role="img" aria-label="%s over time">' % (W, H, esc(pid), esc(name))]
    for frac in (0, 0.5, 1):
        y = T + PH - PH * frac
        parts.append('<line class="gl" x1="%d" x2="%d" y1="%.1f" y2="%.1f"/>' % (L, W - R, y, y))
        parts.append('<text class="ax" x="%d" y="%.1f" text-anchor="end">%s</text>' % (L - 5, y + 3, ("%g" % (ymax * frac))))
    for tk in x_ticks(n * step):
        i = tk / float(step)
        lab = ("%d min" % (tk // 60)) if tk and tk % 60 == 0 else ("%d s" % tk if tk else "0")
        parts.append('<text class="ax" x="%.1f" y="%d" text-anchor="middle">%s</text>' % (xs(i), H - 4, lab))
    if threshold is not None and threshold < ymax:
        y = ys(threshold)
        parts.append('<line class="thr" x1="%d" x2="%d" y1="%.1f" y2="%.1f"/>' % (L, W - R, y, y))
        parts.append('<text class="thrt" x="%d" y="%.1f" text-anchor="end">%g %s limit</text>' % (W - R - 2, y - 3, threshold, esc(unit)))
    d, seg = [], []
    for i, v in enumerate(vals):
        if v is None:
            if seg:
                d.append(seg)
                seg = []
        else:
            seg.append((xs(i), ys(v)))
    if seg:
        d.append(seg)
    path = []
    for s in d:
        path.append("M%.1f %.1f" % s[0] + ("".join(" L%.1f %.1f" % p for p in s[1:]) if len(s) > 1 else " l0.01 0"))
    parts.append('<path class="ln" style="stroke:var(%s)" d="%s"/>' % (color_var, " ".join(path)))
    lane_y = T + PH + 8
    for i, v in enumerate(vals):
        if (ticks[i] if ticks is not None else v is None):
            parts.append('<line class="lostt" x1="%.1f" x2="%.1f" y1="%d" y2="%d"/>' % (xs(i), xs(i), lane_y, lane_y + 8))
    parts.append('<line class="cross" x1="0" x2="0" y1="%d" y2="%d"/>' % (T, T + PH))
    parts.append('<circle class="dot" r="4" cx="0" cy="0" style="fill:var(%s);stroke:var(--card);stroke-width:2"/>' % color_var)
    parts.append('<rect x="%d" y="%d" width="%d" height="%d" fill="transparent"/>' % (L, T, PW, PH + 18))
    parts.append("</svg>")
    panels[pid] = {"w": W, "l": L, "pw": PW, "t": T, "ph": PH, "n": n, "ymax": ymax, "unit": unit, "name": name,
                   "lostWord": lost_word, "step": step, "t0": t0,
                   "vals": [None if v is None else round(v, 1) for v in vals]}
    return "".join(parts)


def stat_line(items):
    return " · ".join(items)


def history_rows(current_id, limit=8):
    rows = []
    try:
        dirs = sorted([d for d in RUN_DIR.iterdir() if d.is_dir() and (d / "result.json").exists() and d.name != current_id],
                      reverse=True)[:limit]
    except OSError:
        return rows
    for d in dirs:
        try:
            r = json.loads((d / "result.json").read_text())
            o = r["overall"]
            rows.append({"id": r["id"], "place": r.get("place"), "verdict": o["verdict"], "rank": o["rank"], "mos": o["mos"],
                         "p95": o.get("rtt_p95"), "jitter": o.get("jitter_ms"), "loss": o.get("loss_pct"),
                         "duration": r.get("duration_s"), "partial": r.get("partial")})
        except (OSError, ValueError, KeyError):
            continue
    return rows


def render_report(r, history=None):
    o = r["overall"]
    targets = r["targets"]
    panels = {}
    usable = [t for t in targets if not t["no_data"]]
    when = datetime.datetime.fromtimestamp(r["started"]).strftime("%a %b %-d, %-I:%M %p") if os.name != "nt" else str(r["started"])
    mins = r["duration_s"] / 60.0
    sub = "%s · %s · %s test%s" % (when, esc(r.get("place") or r.get("host") or ""), ("%g min" % round(mins, 1)) if mins >= 1 else ("%d s" % r["duration_s"]),
                                   " (stopped early)" if r["partial"] else "")
    out = ['<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">',
           "<title>Call network test</title><style>", CSS % {
               "light": ";".join("--s%d:%s" % (i + 1, c) for i, c in enumerate(SLOTS_LIGHT)),
               "dark": ";".join("--s%d:%s" % (i + 1, c) for i, c in enumerate(SLOTS_DARK))},
           "</style></head><body><main>", "<h1>Video call network test</h1>", '<div class="sub">%s</div>' % sub]
    # hero: score + verdict
    out.append('<div class="card hero"><div class="mos">%.1f<small>Call quality score (1 to 5)</small></div>' % o["mos"])
    out.append('<div><div class="chip">%s%s</div><p class="say">%s</p></div></div>' % (icon(o["rank"]), esc(o["verdict"].title()), esc(o["summary"])))
    if o.get("usable"):
        tiles = [("Round trip", fmt(o["rtt_avg"], "", 0), " ms avg"), ("Slowest 5%", fmt(o["rtt_p95"], "", 0), " ms"),
                 ("Jitter", fmt(o["jitter_ms"], "", 1), " ms"), ("Packet loss", fmt(o["loss_pct"], "", 1), " %"),
                 ("Longest dropout", "%d" % o["longest_lost"], " s")]
        out.append('<div class="tiles">%s</div>' % "".join(
            '<div class="tile"><div class="l">%s</div><div class="v">%s<small>%s</small></div></div>' % (esc(a), esc(b), esc(c)) for a, b, c in tiles))
        out.append('<div class="sub" style="margin-top:6px">Typical of %d of %d targets.</div>' % (o["usable"], o["of"]))
    if r["notes"]:
        out.append('<ul class="notes">%s</ul>' % "".join("<li>%s</li>" % esc(n) for n in r["notes"]))

    # round-trip panels, one shared scale so they compare
    rtt_t = [t for t in usable if t["rtt"] and t["rtt"]["received"]]
    if rtt_t:
        ymax = nice_ceil(max((t["rtt"]["p99"] or 0) for t in rtt_t) * 1.2, 100)
        ymax = min(ymax, 1000)
        out.append("<h2>Round trip time</h2>")
        out.append('<div class="panels">')
        for t in rtt_t:
            rt, m = t["rtt"], t["metrics"]
            out.append('<div class="card"><div class="ph"><span class="key" style="border-color:var(--s%d)"></span><b>%s</b><span class="host">%s</span>'
                       '<span class="mini">%s%s</span></div>' % (t["slot"] + 1, esc(t["name"]), esc(t["host"] + (":%s" % t["port"] if t["port"] and rt["source"] == "tcp" else "")),
                                                                     icon(t["rank"]).replace('width="20" height="20"', 'width="14" height="14"'), esc(t["verdict"].title())))
            out.append('<div class="stats">%s</div>' % esc(stat_line([
                "avg %s ms" % fmt(rt["avg"], "", 0), "95th %s ms" % fmt(rt["p95"], "", 0), "max %s ms" % fmt(rt["max"], "", 0),
                "%s lost of %d" % (rt["lost_total"], rt["sent"])] + (["TCP connect time"] if rt["source"] == "tcp" else []))))
            out.append(panel_svg("rtt-" + t["name"], rt["series"], "ms", "--s%d" % (t["slot"] + 1), ymax, t["name"], "lost", panels))
            out.append("</div>")
        out.append("</div>")
        out.append('<div class="legendnote"><i></i> red tick = a probe that got no answer. Every panel uses the same scale.</div>')

    # call-like UDP stream panels
    udp_t = [t for t in usable if t["udp"] and t["udp"]["intervals"]]
    if udp_t:
        out.append("<h2>Call-like stream (UDP, %d kbit/s each way)</h2>" % (r["udp_kbps"]))
        out.append('<div class="panels">')
        for t in udp_t:
            for d, label in (("down", "Download"), ("up", "Upload")):
                iv = t["udp"]["intervals"]
                jit = [row.get(d + "_jitter") for row in iv]
                if not any(v is not None for v in jit):
                    continue
                lost = [row.get(d + "_lost", 0) for row in iv]
                vals = [None if (jit[i] is None) else jit[i] for i in range(len(iv))]
                ymax = nice_ceil(max([v for v in vals if v is not None] + [0]) * 1.2, 40)
                summ = t["udp"][d] or {}
                out.append('<div class="card"><div class="ph"><span class="key" style="border-color:var(--s%d)"></span><b>%s</b><span class="host">%s jitter</span></div>'
                           % (t["slot"] + 1, esc(t["name"]), label))
                out.append('<div class="stats">%s</div>' % esc(stat_line([
                    "jitter %s ms (whole test)" % fmt(summ.get("jitter_ms"), "", 1),
                    "%s of %s datagrams lost (%.2f%%)" % (summ.get("lost", 0), summ.get("packets", 0), summ.get("loss_pct", 0.0))])))
                pid = "udp-%s-%s" % (t["name"], d)
                svg = panel_svg(pid, vals, "ms", "--s%d" % (t["slot"] + 1), ymax, "%s %s jitter" % (t["name"], d), "no data", panels,
                                threshold=GOOD["jitter"], ticks=[bool(lost[i]) or vals[i] is None for i in range(len(vals))])
                out.append(svg + "</div>")
        out.append("</div>")

    # table view
    out.append("<h2>All numbers</h2>")
    out.append('<div class="card scroll"><table><tr><th>Target</th><th>Verdict</th><th>Score</th><th>Avg ms</th><th>95th ms</th><th>Max ms</th>'
               "<th>Jitter ms</th><th>Loss %</th><th>Longest dropout</th><th>Worst 10 s</th><th>UDP up (jitter / loss)</th><th>UDP down (jitter / loss)</th></tr>")
    for t in targets:
        if t["no_data"]:
            out.append('<tr><td>%s</td><td colspan="11" style="text-align:left">no answer: %s</td></tr>' % (
                esc(t["name"]), esc(t["errors"][0] if t["errors"] else "no replies")))
            continue
        rt, m = t["rtt"], t["metrics"]
        ww = rt["worst_window"] if rt else None
        udp = []
        for d in ("up", "down"):
            u = t["udp"][d] if t["udp"] else None
            udp.append("%.1f ms / %.2f%%" % (u["jitter_ms"] or 0, u["loss_pct"]) if u else "-")
        out.append("<tr><td>%s</td><td>%s</td><td>%.2f</td><td>%s</td><td>%s</td><td>%s</td><td>%s%s</td><td>%.2f</td><td>%d s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            esc(t["name"]), esc(t["verdict"].title()), m["mos"], fmt(rt["avg"] if rt else None, "", 0), fmt(rt["p95"] if rt else None, "", 0),
            fmt(rt["max"] if rt else None, "", 0), "%.1f" % m["jitter_ms"], " (udp)" if m["jitter_from"] == "udp" else "", m["loss_pct"], m["longest_lost"],
            esc("%d lost, avg %s ms at %d:%02d" % (ww["lost"], fmt(ww["avg"], "", 0), ww["start_s"] // 60, ww["start_s"] % 60) if ww else "-"), esc(udp[0]), esc(udp[1])))
    out.append("</table></div>")

    # earlier runs
    hist = history_rows(r["id"]) if history is None else history
    if hist:
        out.append("<h2>Earlier runs</h2>")
        out.append('<div class="card scroll"><table><tr><th>When</th><th>Where</th><th>Verdict</th><th>Score</th><th>95th ms</th><th>Jitter ms</th><th>Loss %</th></tr>')
        for h in hist:
            try:
                d = datetime.datetime.strptime(h["id"], "%Y%m%d-%H%M%S").strftime("%b %-d, %-I:%M %p")
            except ValueError:
                d = h["id"]
            out.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%.2f</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                esc(d + (" (short)" if h.get("partial") else "")), esc(h.get("place") or "-"), esc(h["verdict"].title()), h["mos"],
                fmt(h.get("p95"), "", 0), fmt(h.get("jitter"), "", 1), fmt(h.get("loss"), "", 2)))
        out.append("</table></div>")

    g, mg = GOOD, MARGINAL
    out.append('<p class="foot">How to read this: a target is Good when loss is at most %g%%, jitter at most %g ms and the slowest 5%% of round trips '
               "under %g ms; Marginal up to %g%% / %g ms / %g ms; worse is Poor. A dropout of %d s or more drops it a level (%d s or more: Poor). "
               "The score is a simplified E-model estimate and is best used to compare runs. Ping and TCP probes are one per second, so loss under about 1%% "
               "needs the UDP stream to see it.</p>" % (g["loss"], g["jitter"], g["rtt"], mg["loss"], mg["jitter"], mg["rtt"], OUTAGE_MARGINAL, OUTAGE_POOR))
    out.append('<div id="tip"></div>')
    out.append('<script type="application/json" id="nt-data">%s</script>' % json.dumps({"panels": panels}).replace("<", "\\u003c"))
    out.append("<script>%s</script></main></body></html>" % JS)
    return "".join(out)


def write_report(run_dir):
    run_dir = Path(run_dir)
    r = json.loads((run_dir / "result.json").read_text())
    (run_dir / "report.html").write_text(render_report(r))
    return run_dir / "report.html"


def latest_dir():
    try:
        dirs = sorted(d for d in RUN_DIR.iterdir() if d.is_dir() and (d / "result.json").exists())
    except OSError:
        return None
    return dirs[-1] if dirs else None


# ====================================================================================================
def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("run", "report", "latest", "-h", "--help"):
        argv.insert(0, "run")
    ap = argparse.ArgumentParser(description="Network health test for video calls")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run", help="run a test (default)")
    r.add_argument("--duration", "-d", type=int, default=int(os.environ.get("TV_NETTEST_SECONDS", DEFAULT_SECONDS)),
                   help="seconds (default 600)")
    r.add_argument("--iperf", help="name=host[:port],...  iperf3 servers (also pinged)")
    r.add_argument("--ping", help="name=host,...  ping-only targets")
    r.add_argument("--tcp", help="name=host:port,...  TCP connect targets")
    r.add_argument("--no-tcp", action="store_true", help="skip the TCP connect targets")
    r.add_argument("--kbps", default=os.environ.get("TV_NETTEST_KBPS", "1500"), help="UDP stream rate in kbit/s")
    rp = sub.add_parser("report", help="re-render report.html from a saved result")
    rp.add_argument("dir", nargs="?")
    sub.add_parser("latest", help="print the latest run folder")
    args = ap.parse_args(argv)
    if args.cmd == "report":
        d = Path(args.dir) if args.dir else latest_dir()
        if not d or not (d / "result.json").exists():
            print("No result found.")
            return 2
        print(write_report(d))
        return 0
    if args.cmd == "latest":
        d = latest_dir()
        print(d if d else "")
        return 0 if d else 2
    if args.duration < 5:
        print("Duration must be at least 5 seconds.")
        return 2
    return run_test(args)


if __name__ == "__main__":
    sys.exit(main())
