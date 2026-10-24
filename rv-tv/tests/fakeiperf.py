#!/usr/bin/env python3
"""Stand-in for iperf3, used by test_nettest.py when a real iperf3 is not installed (and for the failure
cases either way). It never touches the network; it prints captured real iperf3 output:
  --help      the option list (so nettest sees --bidir, --get-server-output, --connect-timeout)
  -p 1        "unable to connect to server" as iperf3 -J prints it
  -c ...      the bidir fixture, or the reverse fixture when --bidir is missing; FAKE_IPERF_LOSSY=1 picks
              the variant with 4.95 % download loss, 1.9 % upload loss and jitter spikes
"""
import os
import sys
from pathlib import Path

args = sys.argv[1:]
fx = Path(__file__).resolve().parent / "fixtures"
if "--help" in args or "-h" in args:
    print("Usage: iperf3 [-s|-c host] [options]\n  --bidir\n  --get-server-output\n  --connect-timeout #")
    sys.exit(0)
if "-p" in args and args[args.index("-p") + 1] == "1":
    print('{"start": {}, "intervals": [], "end": {}, "error": "error - unable to connect to server: Connection refused"}')
    sys.exit(1)
if "--bidir" in args:
    name = "iperf-udp-bidir-lossy.json" if os.environ.get("FAKE_IPERF_LOSSY") else "iperf-udp-bidir.json"
else:
    name = "iperf-udp-reverse.json"
sys.stdout.write((fx / name).read_text())
