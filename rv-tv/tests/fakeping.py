#!/usr/bin/env python3
"""Stand-in for ping, used by test_nettest.py. Understands: fakeping.py -c N -i 1 -n HOST
Prints the same reply lines as the real ping (macOS style on macOS, Linux style elsewhere), instantly.
The host name picks the behaviour:
  dead*   prints an error and exits 2 (nothing answers)
  loss*   a 4 second dropout at seconds 3-6
  flaky*  every 10th probe lost (10 %)
  slow*   180 ms round trips
  else    25-33 ms round trips with a small wobble
"""
import sys

args = sys.argv[1:]
count = int(args[args.index("-c") + 1])
host = args[-1]
mac = sys.platform == "darwin"
base = 0 if mac else 1
if host.startswith("dead"):
    sys.stderr.write("ping: %s: Name or service not known\n" % host)
    sys.exit(2)
print("PING %s (192.0.2.7): 56 data bytes" % host)
got = 0
for i in range(count):
    seq = i + base
    lost = (host.startswith("loss") and 3 <= i <= 6) or (host.startswith("flaky") and i % 10 == 9)
    if lost:
        if mac:
            print("Request timeout for icmp_seq %d" % seq)
        sys.stdout.flush()
        continue
    rtt = 180.0 + (i % 3) if host.startswith("slow") else 25.0 + (i % 5) * 2
    got += 1
    if mac:
        print("64 bytes from 192.0.2.7: icmp_seq=%d ttl=57 time=%.3f ms" % (seq, rtt))
    else:
        print("64 bytes from 192.0.2.7: icmp_seq=%d ttl=57 time=%.1f ms" % (seq, rtt))
    sys.stdout.flush()
print("--- %s ping statistics ---" % host)
print("%d packets transmitted, %d packets received, %.1f%% packet loss" % (count, got, 100.0 * (count - got) / count))
sys.exit(0 if got else 2)
