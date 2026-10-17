#!/usr/bin/env python3
"""Build display/pins.json (the red hike pins on the dashboard map).

    python3 tools/make_pins.py alltrails_activities.csv      # same CSV hike_map.py reads
    python3 tools/make_pins.py hike_map.html                 # or a page hike_map.py already built

Output is compact: [[lat, lon, "name", "YYYY-MM-DD"], ...]. For a CSV the script imports
hike_map.py, so keep hike_map.py next to this script, in the folder above it, or in the
current directory. Standard library only (Python 3.9+).
"""
import argparse
import json
import re
import sys
from pathlib import Path


def from_html(path):
    text = Path(path).read_text(encoding="utf-8")
    m = re.search(r"var DATA = (\[.*?\]);\n", text, re.S)
    if not m:
        sys.exit("no 'var DATA = [...]' found in that HTML file")
    return json.loads(m.group(1))


def from_csv(path, max_miles):
    here = Path(__file__).resolve().parent
    for p in (here, here.parent, Path.cwd()):
        if (p / "hike_map.py").exists():
            sys.path.insert(0, str(p))
            break
    try:
        import hike_map
    except ImportError:
        sys.exit("hike_map.py not found: put it next to this script or in the current folder")
    hikes, _dropped, _excluded = hike_map.load_hikes(path, max_miles)
    return hikes


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="AllTrails activities CSV, or a hike_map.html")
    ap.add_argument("-o", "--output", default=str(Path(__file__).resolve().parent.parent / "display" / "pins.json"))
    ap.add_argument("--max-miles", type=float, default=40.0, help="CSV only: drop longer activities (GPS glitches)")
    args = ap.parse_args()

    rows = from_html(args.source) if args.source.lower().endswith((".html", ".htm")) else from_csv(args.source, args.max_miles)
    pins = [[round(h["lat"], 4), round(h["lon"], 4), h["name"], h["date"]] for h in rows]
    Path(args.output).write_text(json.dumps(pins, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {args.output}: {len(pins)} pins")


if __name__ == "__main__":
    main()
