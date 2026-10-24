#!/usr/bin/env python3
"""Network health test for video calls. Runs on the Mac mini (or anywhere with Python 3.9+).

    python3 nettest.py                 10-minute test, prints a summary, writes a report
    python3 nettest.py --duration 60   shorter test (seconds)
    python3 nettest.py report [DIR]    re-render report.html from a saved result (default: latest)
    python3 nettest.py latest          print the folder of the latest run
    python3 nettest.py history         list every saved run (place, time, numbers)
    python3 nettest.py rescore         judge saved runs again under the current scoring rules
    python3 nettest.py compare         write compare.html: all saved runs on one graph, to compare places and times

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
HISTORY_NAME = "history.jsonl"
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
MIN_LOST = 3                 # fewer lost pings than this is not evidence of a bad path (1 in 60 reads as 1.7 %)
BIN_S = 10                   # seconds per point in the saved history (what the compare graph plots)
SHORT_S = 300                # a run shorter than this is "short": kept in the history, hidden in compare by default
MAX_COMPARE = 8              # runs drawn at once (one colour each; a 9th would have to repeat a colour)
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
    """Typical change between neighbouring replies (neighbours = consecutive seconds): the median of the absolute
    differences, so one stalled second does not count as a jittery link. Its size is the 95th percentile and
    the dropouts, which are reported separately."""
    diffs = sorted(abs(b - a) for a, b in zip(series, series[1:]) if a is not None and b is not None)
    return percentile(diffs, 50) if diffs else 0.0


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


def verdict_for(loss, jitter, rtt_p95, mos, longest_lost, jitter_trusted=True):
    """-> (rank 0..2, [reasons]). Reasons name every number that cost a level. Jitter that only comes from ping
    or TCP timing once a second (jitter_trusted False) can cost one level but never two."""
    checks = [
        ("loss", loss, "%.1f%% packet loss" % (loss or 0)),
        ("jitter", jitter, "%.0f ms jitter" % (jitter or 0)),
        ("rtt", rtt_p95, "%.0f ms round trip (95th percentile)" % (rtt_p95 or 0)),
    ]
    rank, reasons = 0, []
    for key, val, text in checks:
        lv = level(val, GOOD[key], MARGINAL[key])
        if key == "jitter" and not jitter_trusted:
            lv = min(lv, 1)
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


def location_fix():
    """The Mac's own location fix ('lat|lon|accuracy_m|city|state') if it is fresh (under 6 hours), else None.
    Returns {'place', 'lat', 'lon', 'accuracy_m', 'age_s'}; each part is None when it is missing or invalid."""
    try:
        age = time.time() - LOCATION_FILE.stat().st_mtime
        if age > 6 * 3600:
            return None
        parts = LOCATION_FILE.read_text().strip().split("|") + [""] * 5
    except OSError:
        return None

    def num(s, lo, hi):
        try:
            v = float(s)
        except ValueError:
            return None
        return v if lo <= v <= hi else None

    lat, lon = num(parts[0], -90, 90), num(parts[1], -180, 180)
    if lat is None or lon is None:
        lat = lon = None
    acc = num(parts[2], 0, 1e7)
    city, state = parts[3].strip(), parts[4].strip()
    place = None
    if city and city != "(null)":
        place = "%s, %s" % (city, state) if state and state != "(null)" else city
    if place is None and lat is None:
        return None
    return {"place": place, "lat": None if lat is None else round(lat, 4), "lon": None if lon is None else round(lon, 4),
            "accuracy_m": None if acc is None else int(round(acc)), "age_s": int(age)}


def place_label():
    """'Cloverdale, OR' from the Mac's own location fix if it is fresh, else None."""
    fix = location_fix()
    return fix["place"] if fix else None


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


def summarize_target(entry, probes, sent, link_jitter=None):
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
    return score_target(t, link_jitter)


def udp_jitter_of(t):
    """Worst-direction jitter of a target's UDP stream, or None."""
    vals = [t["udp"][d]["jitter_ms"] for d in ("up", "down") if t.get("udp") and t["udp"].get(d) and t["udp"][d]["jitter_ms"] is not None]
    return max(vals) if vals else None


def link_udp_jitter(targets):
    """The link's jitter as the UDP stream(s) measured it (median over targets that had one), or None."""
    vals = [udp_jitter_of(t) for t in targets]
    vals = [v for v in vals if v is not None]
    return median_of(vals) if vals else None


def score_target(t, link_jitter=None):
    """Fill in metrics, rank, verdict and reasons of a target summary (also used to rescore saved runs).
    Jitter: the target's own UDP stream if it had one; else the link's UDP jitter (marked "link"); else the
    ping or TCP timing, which can only cost one level."""
    for k in ("metrics", "rank", "verdict", "reasons"):
        t.pop(k, None)
    t["no_data"] = False
    rtt = t["rtt"]
    have_rtt = bool(rtt and rtt["received"])
    if not have_rtt and not t["udp"]:
        t["no_data"] = True
        return t
    losses = [rtt["loss_pct"]] if rtt else []
    # a couple of lost pings is noise, not a bad path: the verdict only counts ping loss from MIN_LOST packets up
    verdict_losses = [rtt["loss_pct"] if rtt["lost_total"] >= MIN_LOST else 0.0] if rtt else []
    if t["udp"]:
        for d in ("up", "down"):
            if t["udp"][d]:
                losses.append(t["udp"][d]["loss_pct"])
                verdict_losses.append(t["udp"][d]["loss_pct"])
    own = udp_jitter_of(t)
    if own is not None:
        jitter, jitter_from = own, "udp"
    elif link_jitter is not None:
        jitter, jitter_from = link_jitter, "link"
    else:
        jitter, jitter_from = (rtt["jitter"] if rtt else 0.0), "rtt"
    loss = max(losses) if losses else 0.0
    avg = rtt["avg"] if have_rtt else 0.0
    p95 = rtt["p95"] if have_rtt else None
    longest = rtt["longest_lost"] if rtt else 0
    mos = mos_score(avg, jitter, loss)
    rank, reasons = verdict_for(max(verdict_losses) if verdict_losses else 0.0, jitter, p95, mos, longest,
                                jitter_trusted=jitter_from != "rtt")
    t["metrics"] = {"loss_pct": round(loss, 2), "jitter_ms": round(jitter, 1),
                    "rtt_avg": avg, "rtt_p95": p95, "mos": round(mos, 2), "longest_lost": longest,
                    "jitter_from": jitter_from}
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


DERIVED_NOTE = re.compile(r"hit most targets at the same moment|is weaker than the rest|^Most affected:")


def judge(targets, sent, base_notes):
    """-> (overall, notes): the verdict for the run and the sentences explaining it. A pure function of the
    per-target summaries, so saved runs can be judged again (rescore)."""
    notes = list(base_notes)
    usable = [t for t in targets if not t["no_data"]]
    drops = common_dropouts(targets, sent)
    longest_common = max(drops) if drops else 0
    if not usable:
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
    return overall, notes


def build_result(run_id, started, duration, elapsed, partial, plan, probes_by_target, kbps, iperf_note):
    sent = duration if not partial else max(1, min(duration, int(elapsed) - 1))
    # the link's UDP jitter first, so targets without their own UDP stream can use it
    first = [summarize_target(e, probes_by_target[e["name"]], sent) for e in plan]
    link = link_udp_jitter(first)
    targets = [score_target(t, link) for t in first] if link is not None else first
    base = []
    if iperf_note:
        base.append(iperf_note)
    for t in targets:
        if t["no_data"]:
            base.append("%s did not answer at all (%s)." % (t["name"], t["errors"][0] if t["errors"] else "no replies"))
        else:
            for e in t["errors"]:
                base.append("%s: %s" % (t["name"], e))
    fix = location_fix()
    if fix is None or fix["lat"] is None:
        base.append("No fresh location fix, so this run has no %s. It still counts in the history, just without a map point."
                    % ("place or coordinates" if fix is None else "coordinates"))
    overall, notes = judge(targets, sent, base)
    return {"version": 3, "id": run_id, "started": started, "duration_s": duration, "sent": sent,
            "elapsed_s": round(elapsed, 1), "partial": partial, "host": platform.node(),
            "place": fix["place"] if fix else None,
            "geo": {"lat": fix["lat"], "lon": fix["lon"], "accuracy_m": fix["accuracy_m"], "fix_age_s": fix["age_s"]}
                   if fix and fix["lat"] is not None else None,
            "udp_kbps": kbps, "targets": targets, "overall": overall, "notes": notes,
            "thresholds": {"good": GOOD, "marginal": MARGINAL, "outage_marginal_s": OUTAGE_MARGINAL,
                           "outage_poor_s": OUTAGE_POOR}}


def rescore(r):
    """Judge a saved run again under the current rules (jitter measure, UDP jitter, MIN_LOST). Changes the
    verdicts and notes only; the measurements stay as they were."""
    for t in r["targets"]:
        if t.get("rtt") and t["rtt"].get("series") is not None:
            t["rtt"]["jitter"] = round(rtt_jitter(t["rtt"]["series"]), 1)
    link = link_udp_jitter(r["targets"])
    for t in r["targets"]:
        score_target(t, link)
    base = [n for n in r.get("notes", []) if not DERIVED_NOTE.search(n)]
    r["overall"], r["notes"] = judge(r["targets"], r.get("sent") or r["duration_s"], base)
    r["version"] = max(r.get("version", 1), 3)
    return r


# ====================================================================================================
# history: one line per run in history.jsonl, so runs at different places and times can be compared
# ====================================================================================================
def history_path():
    return RUN_DIR / HISTORY_NAME


def rtt_bins(targets, sent, size=BIN_S):
    """Per `size` seconds, all usable targets pooled: median and 95th round trip, jitter, % lost.
    None where nothing answered (or, for jitter, where fewer than two replies came back)."""
    series = [t["rtt"]["series"] for t in targets if t.get("rtt") and t["rtt"]["received"]]
    out = {"p50": [], "p95": [], "jitter": [], "loss": []}
    for s0 in range(0, max(sent, 0), size):
        pooled, lost, total, jit = [], 0, 0, []
        for s in series:
            w = s[s0:s0 + size]
            vals = [v for v in w if v is not None]
            pooled.extend(vals)
            lost += len(w) - len(vals)
            total += len(w)
            if len(vals) >= 2:
                jit.append(rtt_jitter(w))
        srt = sorted(pooled)
        out["p50"].append(round(percentile(srt, 50), 1) if srt else None)
        out["p95"].append(round(percentile(srt, 95), 1) if srt else None)
        mj = median_of(jit)
        out["jitter"].append(round(mj, 1) if mj is not None else None)
        out["loss"].append(round(100.0 * lost / total, 1) if total else None)
    return out


def history_record(r):
    """The compact line saved per run (everything compare needs; no per-target series)."""
    o = r.get("overall", {})
    geo = r.get("geo") or {}
    sent = r.get("sent") or r.get("duration_s") or 0
    return {"v": 1, "id": r["id"], "started": r["started"], "place": r.get("place"),
            "lat": geo.get("lat"), "lon": geo.get("lon"), "accuracy_m": geo.get("accuracy_m"),
            "duration_s": r.get("duration_s"), "sent": sent, "partial": bool(r.get("partial")),
            "short": (r.get("elapsed_s") or sent) < SHORT_S, "host": r.get("host"),
            "verdict": o.get("verdict"), "rank": o.get("rank"), "mos": o.get("mos"),
            "rtt_avg": o.get("rtt_avg"), "rtt_p95": o.get("rtt_p95"), "jitter_ms": o.get("jitter_ms"),
            "loss_pct": o.get("loss_pct"), "longest_lost": o.get("longest_lost"),
            "usable": o.get("usable"), "of": o.get("of"),
            "targets": sorted(t["name"] for t in r.get("targets", []) if not t.get("no_data")),
            "udp": any(t.get("udp") for t in r.get("targets", [])),
            "bin_s": BIN_S, "bins": rtt_bins(r.get("targets", []), sent)}


def read_history():
    """All saved history records, oldest first, one per run id (the last line for an id wins)."""
    seen = {}
    try:
        lines = history_path().read_text().splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            rec = json.loads(line)
            seen[rec["id"]] = rec
        except (ValueError, KeyError, TypeError):
            continue
    return sorted(seen.values(), key=lambda x: x.get("started") or 0)


def sync_history(rebuild=False):
    """Append a line for every saved run that is not in history.jsonl yet (this also fills in runs that
    were saved before the history existed). With rebuild, every run's line is made again from its result.json
    (after a rescore); lines of runs whose folder is gone are kept. Returns how many lines were added or redone."""
    existing = {rec["id"]: rec for rec in read_history()}
    try:
        dirs = sorted(d for d in RUN_DIR.iterdir() if d.is_dir() and (d / "result.json").exists())
    except OSError:
        return 0
    new = []
    for d in dirs:
        if d.name in existing and not rebuild:
            continue
        try:
            new.append(history_record(json.loads((d / "result.json").read_text())))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    if not new:
        return 0
    if rebuild:
        for rec in new:
            existing[rec["id"]] = rec
        lines = [json.dumps(rec, separators=(",", ":")) for rec in sorted(existing.values(), key=lambda x: x.get("started") or 0)]
        tmp = history_path().with_name(HISTORY_NAME + ".tmp")
        tmp.write_text("\n".join(lines) + "\n")
        os.replace(str(tmp), str(history_path()))
    else:
        with open(str(history_path()), "a") as f:
            for rec in new:
                f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    return len(new)


def rescore_runs(dirs, say=print):
    """Judge saved runs again under the current rules; keeps the first result.json as result.orig.json."""
    done = 0
    for d in dirs:
        d = Path(d)
        try:
            r = json.loads((d / "result.json").read_text())
            before = (r["overall"]["verdict"], r["overall"]["jitter_ms"] if "jitter_ms" in r["overall"] else None)
            if not (d / "result.orig.json").exists():
                (d / "result.orig.json").write_text(json.dumps(r))
            rescore(r)
            (d / "result.json").write_text(json.dumps(r))
            write_report(d)
        except (OSError, ValueError, KeyError, TypeError) as e:
            say("%s: skipped (%s)" % (d.name, e))
            continue
        after = (r["overall"]["verdict"], r["overall"].get("jitter_ms"))
        say("%s  %s -> %s%s" % (d.name, before[0].title() if before[0] else "-", after[0].title(),
                               "" if before == after else "   jitter %s -> %s ms" % (before[1], after[1])))
        done += 1
    if done:
        sync_history(rebuild=True)
    return done


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
    try:
        sync_history()
    except OSError as e:
        say("Could not update %s: %s" % (HISTORY_NAME, e))
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
    geo = r.get("geo")
    say("Logged: %s, %s%s" % (datetime.datetime.fromtimestamp(r["started"]).strftime("%a %b %d %H:%M"),
                              r.get("place") or "place unknown",
                              " (%.4f, %.4f)" % (geo["lat"], geo["lon"]) if geo else ""))
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
            fmt(rt["max"] if rt else None, "", 0), "%.1f" % m["jitter_ms"], {"udp": " (udp)", "link": " (link udp)"}.get(m["jitter_from"], ""), m["loss_pct"], m["longest_lost"],
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
               "needs the UDP stream to see it. Jitter comes from the UDP stream when there is one (a target without its own uses the link's, marked "
               "link udp); from ping or TCP timing alone it is the typical second-to-second change and can lower a verdict by one level only.</p>" % (g["loss"], g["jitter"], g["rtt"], mg["loss"], mg["jitter"], mg["rtt"], OUTAGE_MARGINAL, OUTAGE_POOR))
    out.append('<div id="tip"></div>')
    out.append('<script type="application/json" id="nt-data">%s</script>' % json.dumps({"panels": panels}).replace("<", "\\u003c"))
    out.append("<script>%s</script></main></body></html>" % JS)
    return "".join(out)


def write_report(run_dir):
    run_dir = Path(run_dir)
    r = json.loads((run_dir / "result.json").read_text())
    (run_dir / "report.html").write_text(render_report(r))
    return run_dir / "report.html"


# ====================================================================================================
# compare: every saved run on one graph, for comparing places and times
# ====================================================================================================
CMP_CSS = """
.ctl{display:flex;flex-wrap:wrap;gap:10px 18px;align-items:center;margin:0 0 10px}
.ctl label{font-size:14px;color:var(--ink2);display:inline-flex;align-items:center;gap:6px}
select{font:inherit;color:var(--ink);background:var(--bg);border:1px solid var(--grid);border-radius:8px;padding:5px 8px}
.legend{display:flex;flex-wrap:wrap;gap:6px 18px;margin:10px 0 0;padding:0;list-style:none;font-size:13px}
.legend li{display:inline-flex;align-items:center;gap:7px;color:var(--ink)}
.legend small{color:var(--ink3);font-size:12px}
.band{opacity:.14;stroke:none}
.cmpnote{color:var(--ink2);font-size:13px;margin:8px 0 0}
.endl{fill:var(--ink2);font-size:11px}
.sw{display:inline-block;width:16px;border-top:3px solid transparent;border-radius:2px;vertical-align:middle;margin-left:6px}
tr.short{display:none}
body.showshort tr.short,tr.short.sel{display:table-row}
th:nth-child(2),td:nth-child(2),th:nth-child(3),td:nth-child(3){text-align:left}
th[data-k]{cursor:pointer;user-select:none}
th[data-k]:hover,th[data-k]:focus{color:var(--ink);outline:none;text-decoration:underline}
td small{color:var(--ink3);display:block;font-size:12px}
td a{color:var(--ink2)}
.rowv{display:inline-flex;align-items:center;gap:6px}
details{margin-top:10px}
summary{cursor:pointer;color:var(--ink2);font-size:13px}
#tip div{display:flex;align-items:center;gap:7px;margin-top:2px}
#tip i{display:inline-block;width:14px;border-top:3px solid;border-radius:2px}
"""

CMP_JS = """
(function(){
var D=JSON.parse(document.getElementById('cmp-data').textContent),tip=document.getElementById('tip');
var R={};D.runs.forEach(function(r){R[r.id]=r});
var DASH=['','7 4','2 3','9 3 2 3','','7 4','2 3','9 3 2 3'],MAXN=D.max;
var sel=[],slot={};
var W=900,H=360,L=48,RM=160,T=12,B=34,PW,PH,NS='http://www.w3.org/2000/svg';
var svg=document.getElementById('cmp'),meas=document.getElementById('measure'),legend=document.getElementById('legend');
var note=document.getElementById('cnote'),maxnote=document.getElementById('maxnote'),tb=document.getElementById('runs');
var dtab=document.getElementById('dtab'),boxes=[].slice.call(tb.querySelectorAll('input.pick'));
var M={rtt:{key:'p50',unit:'ms',floor:100,cap:1000,limit:D.limits.rtt,ll:'limit for the 95th'},
 jitter:{key:'jitter',unit:'ms',floor:40,cap:500,limit:D.limits.jitter,ll:'limit'},
 loss:{key:'loss',unit:'%',floor:5,cap:100,limit:D.limits.loss,ll:'limit'}};
function el(n,a,txt){var e=document.createElementNS(NS,n);for(var k in a)e.setAttribute(k,a[k]);if(txt!=null)e.textContent=txt;return e}
function niceCeil(v,f){v=Math.max(v,f);var mag=Math.pow(10,Math.floor(Math.log10(v))),m=[1,2,2.5,5,10];
 for(var i=0;i<m.length;i++){if(v<=m[i]*mag)return m[i]*mag}return 10*mag}
function fmtT(s){var m=Math.floor(s/60),x=Math.round(s%60);return m+':'+(x<10?'0':'')+x}
function stroke(id){return 'var(--s'+(slot[id]+1)+')'}
function style(id){return 'stroke:'+stroke(id)+(DASH[slot[id]]?';stroke-dasharray:'+DASH[slot[id]]:'')}
function short(r){return r.tag.length>22?r.tag.slice(0,21)+'\\u2026':r.tag}
var view={};
function draw(){
  var mk=meas.value,m=M[mk],key=m.key,ids=sel.slice(),band=mk==='rtt'&&ids.length<=3;
  W=Math.max(300,Math.round(svg.getBoundingClientRect().width||900));H=Math.round(Math.max(230,Math.min(360,W*0.42)));
  RM=(ids.length<=4&&W>=640)?160:14;PW=W-L-RM;PH=H-T-B;
  svg.setAttribute('viewBox','0 0 '+W+' '+H);
  while(svg.firstChild)svg.removeChild(svg.firstChild);
  legend.textContent='';
  if(!ids.length){svg.appendChild(el('text',{x:W/2,y:H/2,'text-anchor':'middle',class:'ax'},'Tick one or more runs below to draw them.'));
    note.textContent='';dtab.textContent='';view={};return}
  var xmax=0,vmax=0;
  ids.forEach(function(id){var r=R[id];xmax=Math.max(xmax,r.sent);
    r[key].forEach(function(v){if(v!==null)vmax=Math.max(vmax,v)});
    if(band)r.p95.forEach(function(v){if(v!==null)vmax=Math.max(vmax,v)})});
  xmax=Math.max(xmax,10);
  var ymax=Math.min(niceCeil(vmax*1.1,m.floor),m.cap);
  var xs=function(t){return L+PW*t/xmax},ys=function(v){return T+PH-PH*Math.min(v,ymax)/ymax};
  view={xmax:xmax,ymax:ymax,key:key,m:m,band:band};
  [0,0.5,1].forEach(function(f){var y=T+PH-PH*f;
    svg.appendChild(el('line',{class:'gl',x1:L,x2:L+PW,y1:y,y2:y}));
    svg.appendChild(el('text',{class:'ax',x:L-6,y:y+3,'text-anchor':'end'},String(ymax*f)))});
  var step=60,cand=[10,30,60,120,300,600,1800,3600];
  for(var q=0;q<cand.length;q++){step=cand[q];if(PW/(xmax/step)>=54)break}
  for(var t=0;t<=xmax;t+=step){svg.appendChild(el('text',{class:'ax',x:xs(t),y:H-12,'text-anchor':'middle'},
    t===0?'0':(t%60===0?(t/60)+' min':t+' s')))}
  if(m.limit<ymax){var yl=ys(m.limit);
    svg.appendChild(el('line',{class:'thr',x1:L,x2:L+PW,y1:yl,y2:yl}));
    svg.appendChild(el('text',{class:'thrt',x:L+PW-2,y:yl-3,'text-anchor':'end'},m.limit+' '+m.unit+' '+m.ll))}
  var ends=[];
  ids.forEach(function(id){
    var r=R[id],a=r[key],b=r.p95,segs=[],cur=[];
    for(var i=0;i<a.length;i++){
      if(a[i]===null){if(cur.length)segs.push(cur);cur=[]}
      else cur.push([xs((i+0.5)*r.bin),ys(a[i]),b[i]===null?null:ys(b[i])])}
    if(cur.length)segs.push(cur);
    if(band)segs.forEach(function(s){if(s.length<2)return;
      var up=s.map(function(p){return p[0]+' '+(p[2]===null?p[1]:p[2])}),dn=s.slice().reverse().map(function(p){return p[0]+' '+p[1]});
      svg.appendChild(el('polygon',{class:'band',points:up.concat(dn).join(' '),style:'fill:'+stroke(id)}))});
    var d=segs.map(function(s){return 'M'+s[0][0].toFixed(1)+' '+s[0][1].toFixed(1)+(s.length>1?s.slice(1).map(function(p){return ' L'+p[0].toFixed(1)+' '+p[1].toFixed(1)}).join(''):' l0.01 0')}).join(' ');
    svg.appendChild(el('path',{class:'ln',d:d,style:style(id)}));
    for(var j=0;j<r.p50.length;j++){if(r.p50[j]===null){var x=xs((j+0.5)*r.bin);
      svg.appendChild(el('line',{class:'lostt',x1:x,x2:x,y1:T+PH+4,y2:T+PH+12}))}}
    if(segs.length){var last=segs[segs.length-1],p=last[last.length-1];ends.push({id:id,x:p[0],y:p[1]})}
    var li=document.createElement('li'),sw=el('svg',{width:26,height:10,viewBox:'0 0 26 10','aria-hidden':'true'});
    sw.appendChild(el('line',{x1:1,x2:25,y1:5,y2:5,class:'ln',style:style(id)}));li.appendChild(sw);
    var lab=document.createElement('span');lab.textContent=r.label;li.appendChild(lab);
    var sm=document.createElement('small');sm.textContent=r.verdict||'';li.appendChild(sm);legend.appendChild(li)});
  if(RM>100){
    ends.sort(function(p,q){return p.y-q.y});
    for(var k=1;k<ends.length;k++){if(ends[k].y-ends[k-1].y<13)ends[k].y=ends[k-1].y+13}
    ends.forEach(function(e){var x0=L+PW+6;
      svg.appendChild(el('line',{class:'ln',x1:x0,x2:x0+12,y1:e.y,y2:e.y,style:style(e.id)}));
      svg.appendChild(el('text',{class:'endl',x:x0+16,y:e.y+4},short(R[e.id])))})}
  svg.appendChild(el('line',{class:'cross',x1:0,x2:0,y1:T,y2:T+PH,id:'xh'}));
  svg.appendChild(el('rect',{x:L,y:T,width:PW,height:PH+16,fill:'transparent',id:'hit'}));
  var tg={},shorts=false;ids.forEach(function(id){tg[R[id].tg]=1;if(R[id].short)shorts=true});
  var msg=[];
  if(Object.keys(tg).length>1)msg.push('These runs did not test the same targets (or not all had the UDP stream), so part of a difference may be the test and not the place.');
  if(shorts)msg.push('Runs under 5 minutes are noisy: a few lost pings move the lines a lot.');
  note.textContent=msg.join(' ');
  drawTable(ids,key,m)}
function drawTable(ids,key,m){
  dtab.textContent='';var t=document.createElement('table'),h=t.insertRow(),th=document.createElement('th');
  th.textContent='Minute';h.appendChild(th);
  ids.forEach(function(id){var c=document.createElement('th');c.textContent=R[id].tag;h.appendChild(c)});
  var n=0;ids.forEach(function(id){n=Math.max(n,R[id][key].length)});
  for(var i=0;i<n;i++){var row=t.insertRow(),c0=row.insertCell();
    c0.textContent=fmtT(i*R[ids[0]].bin);
    ids.forEach(function(id){var c=row.insertCell(),v=R[id][key][i];c.textContent=v===undefined?'':(v===null?(key==='loss'?'no data':'lost'):v+' '+m.unit)})}
  dtab.appendChild(t)}
function tipAt(ev){
  if(!view.xmax)return;
  var r0=svg.getBoundingClientRect(),x=(ev.clientX-r0.left)*(W/r0.width),t=(x-L)/PW*view.xmax;
  t=Math.max(0,Math.min(view.xmax,t));var px=L+PW*t/view.xmax,xh=document.getElementById('xh');
  xh.setAttribute('x1',px);xh.setAttribute('x2',px);xh.style.display='block';
  tip.textContent='';var b=document.createElement('b');b.textContent='at '+fmtT(t);tip.appendChild(b);
  sel.forEach(function(id){var r=R[id],i=Math.floor(t/r.bin),row=document.createElement('div'),sw=document.createElement('i');
    sw.style.borderColor=stroke(id);row.appendChild(sw);var s=document.createElement('span'),v=r[view.key][i],txt;
    if(t>r.sent||v===undefined)txt='run ended';
    else if(v===null)txt=view.key==='loss'?'no data':'lost';
    else{txt=v+' '+view.m.unit;if(meas.value==='rtt'&&r.p95[i]!==null)txt+=' (95th '+r.p95[i]+')'}
    s.textContent=r.tag+': '+txt;row.appendChild(s);tip.appendChild(row)});
  tip.style.display='block';
  tip.style.left=Math.min(ev.clientX+14,window.innerWidth-tip.offsetWidth-8)+'px';
  tip.style.top=Math.max(ev.clientY-tip.offsetHeight-12,6)+'px'}
svg.addEventListener('pointermove',tipAt);svg.addEventListener('pointerdown',tipAt);
svg.addEventListener('pointerleave',function(){var xh=document.getElementById('xh');if(xh)xh.style.display='none';tip.style.display='none'});
function setSel(id,on){
  if(on){if(sel.indexOf(id)>=0)return;if(sel.length>=MAXN)return;
    var used={};sel.forEach(function(i){used[slot[i]]=1});var s=0;while(used[s])s++;slot[id]=s;sel.push(id)}
  else{sel=sel.filter(function(i){return i!==id});delete slot[id]}}
function sync(){
  boxes.forEach(function(b){var id=b.dataset.id,on=sel.indexOf(id)>=0,tr=b.closest('tr'),sw=tr.querySelector('.sw');
    b.checked=on;b.disabled=!on&&sel.length>=MAXN;tr.classList.toggle('sel',on);
    sw.style.borderTopColor=on?stroke(id):'transparent';sw.style.borderTopStyle=on&&DASH[slot[id]]?'dashed':'solid'});
  maxnote.hidden=sel.length<MAXN}
tb.addEventListener('change',function(ev){var b=ev.target;if(!b.classList.contains('pick'))return;
  setSel(b.dataset.id,b.checked);sync();draw()});
meas.addEventListener('change',draw);
document.getElementById('showshort').addEventListener('change',function(ev){document.body.classList.toggle('showshort',ev.target.checked)});
[].forEach.call(document.querySelectorAll('th[data-k]'),function(th){
  function go(){var k=th.dataset.k,asc=th.dataset.dir!=='asc';th.dataset.dir=asc?'asc':'desc';
    var rows=[].slice.call(tb.rows);
    rows.sort(function(a,b){var x=a.dataset[k]||'',y=b.dataset[k]||'',nx=parseFloat(x),ny=parseFloat(y);
      var c=(isNaN(nx)||isNaN(ny))?x.localeCompare(y):nx-ny;return asc?c:-c});
    rows.forEach(function(r){tb.appendChild(r)})}
  th.addEventListener('click',go);th.addEventListener('keydown',function(ev){if(ev.key==='Enter'||ev.key===' '){ev.preventDefault();go()}})});
D.init.forEach(function(id){setSel(id,true)});sync();draw();
var rz=null;window.addEventListener('resize',function(){if(rz)cancelAnimationFrame(rz);rz=requestAnimationFrame(draw)});
})();
"""


def when_label(ts):
    d = datetime.datetime.fromtimestamp(ts)
    return "%s %s %d, %d:%02d %s" % (d.strftime("%a"), d.strftime("%b"), d.day, d.hour % 12 or 12, d.minute, "am" if d.hour < 12 else "pm")


def length_label(sent):
    return ("%g min" % round(sent / 60.0, 1)) if sent >= 60 else ("%d s" % sent)


def render_compare(recs):
    """One self-contained page: every saved run as a line on one graph (x = time into the run), a table of the runs
    with tick boxes, and the same numbers as a table under the graph."""
    out = ['<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">',
           "<title>Compare network runs</title><style>", CSS % {
               "light": ";".join("--s%d:%s" % (i + 1, c) for i, c in enumerate(SLOTS_LIGHT)),
               "dark": ";".join("--s%d:%s" % (i + 1, c) for i, c in enumerate(SLOTS_DARK))}, CMP_CSS,
           "</style></head><body><main>", "<h1>Compare network runs</h1>"]
    if not recs:
        out.append('<div class="sub">No runs saved yet. Run <code>./tv nettest</code> first.</div></main></body></html>')
        return "".join(out)
    full = [r for r in recs if not r.get("short")]
    shown = (full or recs)[-MAX_COMPARE:]
    data = {"max": MAX_COMPARE, "limits": {"rtt": GOOD["rtt"], "jitter": GOOD["jitter"], "loss": GOOD["loss"]},
            "init": [r["id"] for r in shown], "runs": []}
    for r in recs:
        place = r.get("place") or "Unknown place"
        when = when_label(r["started"])
        bins = r.get("bins") or {}
        data["runs"].append({
            "id": r["id"], "label": "%s, %s%s" % (place, when, " (%s)" % length_label(r.get("sent") or 0) if r.get("short") else ""),
            "tag": "%s %s" % (place, when.split(", ")[-1]), "sent": r.get("sent") or 0, "bin": r.get("bin_s") or BIN_S,
            "p50": bins.get("p50", []), "p95": bins.get("p95", []), "jitter": bins.get("jitter", []), "loss": bins.get("loss", []),
            "verdict": (r.get("verdict") or "").title(), "short": bool(r.get("short")),
            "tg": ",".join(r.get("targets") or []) + ("+udp" if r.get("udp") else "")})
    nfull = len(full)
    out.append('<div class="sub">%d run%s saved%s. Each line is one run; the x axis is time since that run started.</div>'
               % (len(recs), "" if len(recs) == 1 else "s", (", %d of them full length" % nfull) if nfull != len(recs) else ""))
    out.append('<div class="card" style="margin-top:14px"><div class="ctl"><label>Show <select id="measure">'
               '<option value="rtt">Round trip time</option><option value="jitter">Jitter</option><option value="loss">Packets lost</option></select></label>'
               '<span class="sub" id="maxnote" hidden>Up to %d runs at once. Untick one to add another.</span></div>' % MAX_COMPARE)
    out.append('<svg class="plot" id="cmp" viewBox="0 0 900 360" role="img" aria-label="Selected runs on one graph"></svg>'
               '<ul class="legend" id="legend"></ul><p class="cmpnote" id="cnote"></p>'
               '<details><summary>Show this graph as a table</summary><div class="scroll" id="dtab"></div></details></div>')
    out.append("<h2>Runs</h2>")
    out.append('<div class="ctl"><label><input type="checkbox" id="showshort"> Show short runs (under %d min)</label></div>' % (SHORT_S // 60))
    out.append('<div class="card scroll"><table><thead><tr><th></th><th data-k="when" tabindex="0">When</th><th data-k="where" tabindex="0">Where</th>'
               '<th>Length</th><th>Verdict</th><th data-k="mos" tabindex="0">Score</th><th>Avg ms</th><th data-k="p95" tabindex="0">95th ms</th>'
               '<th data-k="jit" tabindex="0">Jitter ms</th><th data-k="loss" tabindex="0">Loss %</th><th>Longest dropout</th><th>Tests</th></tr></thead><tbody id="runs">')
    for r in reversed(recs):
        rank = r.get("rank")
        verdict = (r.get("verdict") or "-").title()
        place = r.get("place")
        where = esc(place or "unknown")
        if r.get("lat") is not None and r.get("lon") is not None:
            where += ('<small>%.4f, %.4f%s · <a href="https://www.openstreetmap.org/?mlat=%.5f&amp;mlon=%.5f#map=13/%.5f/%.5f" target="_blank" '
                      'rel="noopener noreferrer">map</a></small>' % (r["lat"], r["lon"], (" ±%d m" % r["accuracy_m"]) if r.get("accuracy_m") is not None else "",
                                                                      r["lat"], r["lon"], r["lat"], r["lon"]))
        out.append('<tr class="%s" data-id="%s" data-when="%s" data-where="%s" data-mos="%s" data-p95="%s" data-jit="%s" data-loss="%s">'
                   % ("short" if r.get("short") else "", esc(r["id"]), r["started"], esc(place or ""), r.get("mos") if r.get("mos") is not None else "",
                      r.get("rtt_p95") if r.get("rtt_p95") is not None else "", r.get("jitter_ms") if r.get("jitter_ms") is not None else "",
                      r.get("loss_pct") if r.get("loss_pct") is not None else ""))
        out.append('<td><input type="checkbox" class="pick" data-id="%s" aria-label="Show this run on the graph"><span class="sw"></span></td>' % esc(r["id"]))
        out.append("<td>%s</td><td>%s</td><td>%s%s</td>" % (esc(when_label(r["started"])), where, esc(length_label(r.get("sent") or 0)),
                                                              " (stopped early)" if r.get("partial") else ""))
        out.append('<td><span class="rowv">%s%s</span></td>' % (icon(rank).replace('width="20" height="20"', 'width="14" height="14"') if rank in (0, 1, 2) else "", esc(verdict)))
        out.append("<td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            "-" if r.get("mos") is None else "%.2f" % r["mos"], fmt(r.get("rtt_avg"), "", 0), fmt(r.get("rtt_p95"), "", 0),
            fmt(r.get("jitter_ms"), "", 1), fmt(r.get("loss_pct"), "", 2), "-" if r.get("longest_lost") is None else "%d s" % r["longest_lost"],
            esc("%s of %s targets%s" % (r.get("usable", "-"), r.get("of", "-"), " + UDP" if r.get("udp") else ""))))
    out.append("</tbody></table></div>")
    out.append('<p class="foot">Round trip, jitter and loss here come from the ping and TCP probes of every target together, in %d second steps; '
               "the verdict and score are from the full run. Different times of day on the same link differ as much as different places do, "
               "so the time is part of every label.</p>" % BIN_S)
    out.append('<div id="tip"></div>')
    out.append('<script type="application/json" id="cmp-data">%s</script>' % json.dumps(data, separators=(",", ":")).replace("<", "\\u003c"))
    out.append("<script>%s</script></main></body></html>" % CMP_JS)
    return "".join(out)


def write_compare(out_path=None):
    sync_history()
    recs = read_history()
    path = Path(out_path) if out_path else RUN_DIR / "compare.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_compare(recs))
    return path, len(recs)


def print_history(say=print):
    recs = read_history()
    if not recs:
        say("No runs saved yet.")
        return
    say("%-19s %-22s %-8s %6s %6s %6s %6s %8s" % ("when", "where", "verdict", "score", "95th", "jit", "loss", "length"))
    for r in recs:
        say("%-19s %-22s %-8s %6s %6s %6s %6s %8s" % (
            datetime.datetime.fromtimestamp(r["started"]).strftime("%Y-%m-%d %H:%M"), (r.get("place") or "unknown")[:22],
            (r.get("verdict") or "-").title(), "-" if r.get("mos") is None else "%.2f" % r["mos"], fmt(r.get("rtt_p95"), "", 0),
            fmt(r.get("jitter_ms"), "", 1), fmt(r.get("loss_pct"), "", 2), length_label(r.get("sent") or 0) + (" *" if r.get("short") else "")))
    say("(* = short run, hidden in the compare page by default)")


def latest_dir():
    try:
        dirs = sorted(d for d in RUN_DIR.iterdir() if d.is_dir() and (d / "result.json").exists())
    except OSError:
        return None
    return dirs[-1] if dirs else None


# ====================================================================================================
def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("run", "report", "latest", "compare", "history", "rescore", "-h", "--help"):
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
    cp = sub.add_parser("compare", help="write compare.html: every saved run on one graph")
    cp.add_argument("--out", help="where to write it (default: next to the runs)")
    sub.add_parser("history", help="list the saved runs")
    rs = sub.add_parser("rescore", help="judge saved runs again under the current scoring rules")
    rs.add_argument("dir", nargs="*", help="run folders (default: all)")
    args = ap.parse_args(argv)
    if args.cmd == "report":
        d = Path(args.dir) if args.dir else latest_dir()
        if not d or not (d / "result.json").exists():
            print("No result found.")
            return 2
        print(write_report(d))
        return 0
    if args.cmd == "compare":
        path, n = write_compare(args.out)
        print("%s  (%d run%s)" % (path, n, "" if n == 1 else "s"))
        return 0 if n else 2
    if args.cmd == "rescore":
        dirs = [Path(x) for x in args.dir] if args.dir else sorted(d for d in RUN_DIR.iterdir() if d.is_dir() and (d / "result.json").exists()) if RUN_DIR.is_dir() else []
        if not dirs:
            print("No runs saved yet.")
            return 2
        n = rescore_runs(dirs)
        print("%d run%s rescored. Originals are kept as result.orig.json." % (n, "" if n == 1 else "s"))
        return 0 if n else 2
    if args.cmd == "history":
        sync_history()
        print_history()
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
