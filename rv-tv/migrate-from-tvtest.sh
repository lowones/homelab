#!/bin/bash
# One-time move of prod from ~/dev/tvtest to this repo checkout. Copies the pairing key,
# tv.env and guest tokens (so nobody re-pairs or gets a new link), then reinstalls the
# launchd agent pointing here. Safe to re-run. The old folder is left untouched.
set -euo pipefail
cd "$(dirname "$0")"
OLD="$HOME/dev/tvtest"
for f in tv_key.txt tv.env tokens.json; do
  if [ -f "$OLD/$f" ] && [ ! -f "$f" ]; then cp -p "$OLD/$f" "$f"; echo "copied $f"; fi
done
[ -f tv_key.txt ] || { echo "no tv_key.txt found; pair first"; exit 1; }
bash install-tvserver.sh
