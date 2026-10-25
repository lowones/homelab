#!/bin/bash
# Refresh .run/location.raw ("lat|lon|accuracy_m|city|state") from CoreLocationCLI.
# Run every 10 minutes by launchd (com.lowone.tvlocation). On failure the old file is left
# alone, so its age is how tvserver knows the fix is stale.
DIR="$(cd "$(dirname "$0")" && pwd)"
CLI="$(command -v CoreLocationCLI || true)"
[ -n "$CLI" ] || CLI=/opt/homebrew/bin/CoreLocationCLI
mkdir -p "$DIR/.run"
if ! out=$("$CLI" --format '%latitude|%longitude|%h_accuracy|%locality|%administrativeArea') || [ -z "$out" ]; then
  # the reverse-geocoded name needs Apple's service; fall back to coordinates alone
  out=$("$CLI" --format '%latitude|%longitude|%h_accuracy||') || exit 1
fi
[ -n "$out" ] || exit 1
printf '%s\n' "$out" | head -n 1 > "$DIR/.run/location.raw.tmp" \
  && mv "$DIR/.run/location.raw.tmp" "$DIR/.run/location.raw"
# History for the stop log ("epoch|lat|lon|accuracy_m", newest last, about two weeks of 10-minute fixes):
# a stop the phone uploads days later is judged against where the Mac was THEN, not where it is now.
H="$DIR/.run/location_history.log"
printf '%s|%s\n' "$(date +%s)" "$(printf '%s' "$out" | head -n 1 | cut -d'|' -f1-3)" >> "$H"
if [ "$(( $(wc -l < "$H") ))" -gt 2500 ]; then
  tail -n 2000 "$H" > "$H.tmp" && mv "$H.tmp" "$H"
fi
