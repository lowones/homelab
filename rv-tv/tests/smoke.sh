#!/bin/bash
# Self-contained check of the access rules, guest page and dashboard against a fake TV,
# a fake weather service and a fake browser.
# Uses its own temp dir and ports; never touches the real TV or prod files.
#   bash tests/smoke.sh      (needs rv-tv/.venv from install-tvserver.sh)
set -uo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
PY="$HERE/.venv/bin/python"; [ -x "$PY" ] || PY=python3
T=$(mktemp -d); MOCK_PORT=3198; API_PORT=8798; STUB_PORT=8799
cleanup() { for p in ${P1:-} ${P2:-} ${P3:-} ${P4:-} ${P5:-} ${P6:-} ${P7:-}; do kill $p 2>/dev/null; done; rm -rf "$T"; }
trap cleanup EXIT
openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj /CN=mock -keyout $T/mock.key -out $T/mock.crt 2>/dev/null
echo mockkey123 > $T/key.txt   # the key the fake TV accepts
# fake weather + place lookup (plain files served over http), and a fake "browser" that
# records its arguments and then idles, standing in for Chrome in kiosk mode
mkdir -p $T/stub/v1
$PY - "$T/stub/v1" <<'EOF'
import json, sys
d = sys.argv[1]
json.dump({"current": {"temperature_2m": 54.3, "apparent_temperature": 50.8, "weather_code": 2, "wind_speed_10m": 8.4},
           "daily": {"time": ["2026-10-15", "2026-10-16", "2026-10-17"], "temperature_2m_max": [58.1, 61.0, 49.5],
                     "temperature_2m_min": [44.2, 46.9, 38.0], "weather_code": [2, 61, 71]}}, open(d + "/forecast", "w"))
json.dump({"results": [{"name": "Moab", "admin1": "Utah", "country": "United States", "country_code": "US", "latitude": 38.57, "longitude": -109.55},
                       {"name": "Moab", "admin1": "Elsewhere", "country": "X", "country_code": "XX", "latitude": 1, "longitude": 2}]}, open(d + "/search", "w"))
EOF
$PY -m http.server $STUB_PORT --bind 127.0.0.1 --directory $T/stub > $T/stub.log 2>&1 & P3=$!
printf '#!/bin/bash\nprintf "%%s\\n" "$@" > "%s/browser.args"\nexec sleep 300\n' "$T" > $T/fakebrowser
chmod +x $T/fakebrowser
MOCK_CERT_DIR=$T $PY "$HERE/tests/mocktv.py" $MOCK_PORT > $T/mock.log 2>&1 & P1=$!
export TV_ENV_FILE=$T/none.env TV_TOKENS_FILE=$T/tokens.json TV_KEYFILE=$T/key.txt \
       TV_IP=127.0.0.1 TV_WS_PORT=$MOCK_PORT TV_TOKEN=admintok TV_BIND=127.0.0.1 TV_SERVER_PORT=$API_PORT \
       TV_DISPLAY_FILE=$T/display.json TV_WEATHER_URL=http://127.0.0.1:$STUB_PORT/v1/forecast \
       TV_GEOCODE_URL=http://127.0.0.1:$STUB_PORT/v1/search TV_DISPLAY_BROWSER=$T/fakebrowser \
       TV_LOCATION_FILE=$T/none.loc   # never read the real .run/location.raw
$PY "$HERE/tvserver.py" > $T/server.log 2>&1 & P2=$!
for i in $(seq 1 30); do curl -fs http://127.0.0.1:$API_PORT/health >/dev/null 2>&1 && break; sleep 0.3; done
B=http://127.0.0.1:$API_PORT
G=$($PY "$HERE/tvserver.py" token add guest | tail -1 | sed 's/.*#//')
N=$($PY "$HERE/tvserver.py" token add nooff --no-off | tail -1 | sed 's/.*#//')
D=$($PY "$HERE/tvserver.py" token add shower --display | tail -1 | sed 's/.*#//')
fail=0
body() { # name pattern curl-args...  (response body must contain the pattern)
  out=$(curl -s "${@:3}")
  case "$out" in *"$2"*) echo "ok   $1";; *) echo "FAIL $1 (no '$2' in: ${out:0:120})"; fail=1;; esac
}
chk() { # name expected curl-args...
  got=$(curl -s -o /dev/null -w '%{http_code}' "${@:3}")
  if [ "$got" = "$2" ]; then echo "ok   $1"; else echo "FAIL $1 (want $2, got $got)"; fail=1; fi
}
A="Authorization: Bearer"
chk "guest page needs no auth"        200 $B/guest
chk "no token rejected"               401 $B/status
chk "bad token rejected"              401 -H "$A nope" $B/status
chk "admin status"                    200 -H "$A admintok" $B/status
chk "guest status"                    200 -H "$A $G" $B/status
chk "guest cannot list inputs"        403 -H "$A $G" $B/inputs
chk "guest cannot list apps"          403 -H "$A $G" $B/apps
chk "guest input HDMI_1"              200 -X POST -H "$A $G" $B/input/HDMI_1
chk "guest input HDMI_2 denied"       403 -X POST -H "$A $G" $B/input/HDMI_2
chk "admin input HDMI_2"              200 -X POST -H "$A admintok" $B/input/HDMI_2
chk "guest netflix"                   200 -X POST -H "$A $G" $B/app/netflix
chk "guest other app denied"          403 -X POST -H "$A $G" $B/app/com.webos.app.settings
chk "guest volume"                    200 -X POST -H "$A $G" $B/volume/20
chk "guest mute"                      200 -X POST -H "$A $G" $B/mute/on
chk "nooff guest cannot power off"    403 -X POST -H "$A $N" $B/power/off
chk "guest can power off"             200 -X POST -H "$A $G" $B/power/off

echo "-- who am I, and what may I do"
body "whoami admin"                   '"admin":true'          -H "$A admintok" $B/whoami
body "whoami guest lacks display"     '"actions"'             -H "$A $G" $B/whoami
out=$(curl -s -H "$A $G" $B/whoami); case "$out" in *display*) echo "FAIL guest whoami lists display"; fail=1;; *) echo "ok   guest is not offered display";; esac
chk "whoami needs a token"            401 $B/whoami

echo "-- dashboard page, data and weather"
chk "display page needs no auth"      200 $B/display
chk "display static file"             200 $B/display/static/layout.json
chk "display data needs a token"      401 $B/display/data
body "display data has weather"       '"temp":54.3'           -H "$A admintok" $B/display/data
body "display data has tv state"      '"power":"on"'          -H "$A admintok" $B/display/data
chk "guest cannot read display data"  403 -H "$A $G" $B/display/data
body "default weather place"          'Lansing, MI'           -H "$A admintok" $B/display/data

echo "-- weather place"
chk "guest cannot set place"          403 -X POST -H "$A $G" "$B/display/location?place=Moab"
body "set place by name"              '"label":"Moab, Utah"'  -X POST -H "$A admintok" "$B/display/location?place=Moab,%20Utah"
body "place now used by weather"      'Moab, Utah'            -H "$A admintok" $B/display/data
body "set place by coordinates"       '"lat":10.5'            -X POST -H "$A admintok" "$B/display/location?lat=10.5&lon=20.25&label=Test"
chk "place needs a place or coords"   400 -X POST -H "$A admintok" "$B/display/location"
chk "bad latitude rejected"           422 -X POST -H "$A admintok" "$B/display/location?lat=999&lon=0"

echo "-- open and close the dashboard"
chk "guest cannot open dashboard"     403 -X POST -H "$A $G" $B/display/open
chk "guest cannot close dashboard"    403 -X POST -H "$A $G" $B/display/close
body "state starts closed"            '"open":false'          -H "$A admintok" $B/display/state
n0=$(grep -c 'tv/switchInput' $T/mock.log)
body "admin opens dashboard"          '"input":"HDMI_1"'      -X POST -H "$A admintok" $B/display/open
n1=$(grep -c 'tv/switchInput' $T/mock.log)
if [ "$n1" -gt "$n0" ]; then echo "ok   open switched the tv to the mac input"; else echo "FAIL open did not switch the input"; fail=1; fi
body "state is open"                  '"open":true'           -H "$A admintok" $B/display/state
URL=$(tail -1 $T/browser.args); KT=${URL#*#}
case "$URL" in http://127.0.0.1:$API_PORT/display#*) echo "ok   browser got the dashboard url";; *) echo "FAIL browser url: $URL"; fail=1;; esac
chk "kiosk token reads display data"  200 -H "$A $KT" $B/display/data
chk "kiosk token cannot control tv"   403 -X POST -H "$A $KT" $B/power/off
chk "kiosk token cannot read status"  403 -H "$A $KT" $B/status
body "display guest may open"         '"display":"open"'      -X POST -H "$A $D" "$B/display/open?tv=false"
body "admin closes dashboard"         '"display":"closed"'    -X POST -H "$A admintok" $B/display/close
body "state is closed again"          '"open":false'          -H "$A admintok" $B/display/state
chk "kiosk token dies on close"       401 -H "$A $KT" $B/display/data
body "close when closed is fine"      '"was_open":false'      -X POST -H "$A admintok" $B/display/close

echo "-- weather follows the trip schedule; sheet button"
$PY - "$T/stub" <<'EOF2'
import datetime, sys
t = datetime.date.today(); f = lambda d: f"{d.month}/{d.day}/{d.year}"
D = datetime.timedelta
open(sys.argv[1] + "/trip.csv", "w").write(
    "W,DATE,DAY,CITY,CAMPGROUND\n"
    f"1,{f(t - D(3))},x,\"Moab, UT\",Site A\n1,{f(t - D(2))},x,,\n1,{f(t)},x,,\n"
    f"1,{f(t + D(5))},x,\"Page, AZ\",Site B\n")
EOF2
T2=$T/second; mkdir -p $T2
TV_SERVER_PORT=8797 TV_DISPLAY_FILE=$T2/display.json TV_ITINERARY="http://127.0.0.1:$STUB_PORT/trip.csv?gid=555" \
  TV_SHEET_URL=http://127.0.0.1:$STUB_PORT/the-sheet $PY "$HERE/tvserver.py" > $T/server2.log 2>&1 & P4=$!
for i in $(seq 1 30); do curl -fs http://127.0.0.1:8797/health >/dev/null 2>&1 && break; sleep 0.3; done
B2=http://127.0.0.1:8797
body "weather uses today's scheduled city" '"location":"Moab, UT"' -H "$A admintok" $B2/display/data
body "state says itinerary source"     '"source":"itinerary"'   -H "$A admintok" $B2/display/state
body "pin a place by hand"             '"label":"Moab, Utah"'   -X POST -H "$A admintok" "$B2/display/location?place=Moab,%20Utah"
body "pinned place wins"               '"source":"manual"'      -H "$A admintok" $B2/display/state
body "follow schedule again"           '"source":"itinerary"'   -X POST -H "$A admintok" "$B2/display/location?auto=true"
chk "no itinerary -> auto refused"     400 -X POST -H "$A admintok" "$B/display/location?auto=true"
chk "no sheet -> 404 on first server"  404 -X POST -H "$A admintok" "$B/display/open?tv=false&page=sheet"
chk "bad page name rejected"           422 -X POST -H "$A admintok" "$B2/display/open?tv=false&page=evil"
chk "guest cannot open sheet"          403 -X POST -H "$A $G" "$B2/display/open?page=sheet"
body "sheet opens"                     '"page":"sheet"'         -X POST -H "$A admintok" "$B2/display/open?tv=false&page=sheet"
if [ "$(tail -1 $T/browser.args)" = "http://127.0.0.1:$STUB_PORT/the-sheet?gid=555#gid=555&range=A4" ]; then echo "ok   browser got the sheet url aimed at today's row (tab 555, row 4)"; else echo "FAIL sheet url: $(tail -1 $T/browser.args)"; fail=1; fi
body "state shows sheet page"          '"page":"sheet"'         -H "$A admintok" $B2/display/state
body "close the sheet"                 '"was_open":true'        -X POST -H "$A admintok" $B2/display/close

echo "-- weather follows the Mac's own location (device fix)"
T3=$T/third; mkdir -p $T3; LOC=$T/loc.raw; rm -f $LOC
TV_SERVER_PORT=8796 TV_DISPLAY_FILE=$T3/display.json TV_LOCATION_FILE=$LOC \
  TV_ITINERARY="http://127.0.0.1:$STUB_PORT/trip.csv?gid=555" $PY "$HERE/tvserver.py" > $T/server3.log 2>&1 & P5=$!
for i in $(seq 1 30); do curl -fs http://127.0.0.1:8796/health >/dev/null 2>&1 && break; sleep 0.3; done
B3=http://127.0.0.1:8796
age() { $PY -c "import os,sys,time; t=time.time()-float(sys.argv[2]); os.utime(sys.argv[1],(t,t))" "$LOC" "$1"; }
body "no device file -> schedule"          '"source":"itinerary"'     -H "$A admintok" $B3/display/data
printf '46.004026|-123.903557|59|Seaside|OR\n' > $LOC
body "fresh fix beats the schedule"        '"source":"device"'        -H "$A admintok" $B3/display/data
body "device label is City, ST"            '"location":"Seaside, OR"' -H "$A admintok" $B3/display/data
body "fix far from schedule: note it"      '"schedule":"Moab, UT"'    -H "$A admintok" $B3/display/data
printf '38.57|-109.55|40|Moab|Utah\n' > $LOC
body "state name becomes abbreviation"     '"location":"Moab, UT"'    -H "$A admintok" $B3/display/data
body "fix agrees with schedule: no note"   '"schedule":null'          -H "$A admintok" $B3/display/data
printf '46.004026|-123.903557|59|(null)|(null)\n' > $LOC
body "(null) city falls back to coords"    '"location":"46.00, -123.90"' -H "$A admintok" $B3/display/data
printf '46.0|-123.9|9000|Seaside|OR\n' > $LOC
body "coarse fix (9 km) is ignored"        '"source":"itinerary"'     -H "$A admintok" $B3/display/data
printf '46.0|-123.9|59|Seaside|OR\n' > $LOC; age 60
body "1 minute old fix still used"         '"source":"device"'        -H "$A admintok" $B3/display/data
age 7200
body "2 hour old fix is ignored"           '"source":"itinerary"'     -H "$A admintok" $B3/display/data
echo "this is not a fix" > $LOC
body "garbage file is ignored"             '"source":"itinerary"'     -H "$A admintok" $B3/display/data
printf '46.0|-123.9|59|Seaside|OR\n' > $LOC
body "pin a place by hand (device on)"     '"label":"Moab, Utah"'     -X POST -H "$A admintok" "$B3/display/location?place=Moab,%20Utah"
body "pin beats the device fix"            '"source":"manual"'        -H "$A admintok" $B3/display/data
body "auto resumes the device fix"         '"source":"device"'        -X POST -H "$A admintok" "$B3/display/location?auto=true"
T4=$T/fourth; mkdir -p $T4
TV_SERVER_PORT=8795 TV_DISPLAY_FILE=$T4/display.json TV_LOCATION_FILE=$LOC $PY "$HERE/tvserver.py" > $T/server4.log 2>&1 & P6=$!
for i in $(seq 1 30); do curl -fs http://127.0.0.1:8795/health >/dev/null 2>&1 && break; sleep 0.3; done
chk "auto allowed with only a device fix" 200 -X POST -H "$A admintok" "http://127.0.0.1:8795/display/location?auto=true"

echo "-- Canny cameras (fake osascript stands in for Safari)"
mkdir -p $T/bin $T/fifth
printf '#!/bin/bash\necho "$@" >> "%s/osa.log"\necho 123\n' "$T" > $T/bin/osascript
chmod +x $T/bin/osascript
B7=http://127.0.0.1:8794
PATH="$T/bin:$PATH" TV_SERVER_PORT=8794 TV_DISPLAY_FILE=$T/fifth/display.json TV_LOCATION_FILE=$T/none.loc \
  TV_CAMS_LIVE=driveway,coop TV_GO2RTC_URL=http://localhost:1984 $PY "$HERE/tvserver.py" > $T/server7.log 2>&1 & P7=$!
for i in $(seq 1 30); do curl -fs $B7/health >/dev/null 2>&1 && break; sleep 0.3; done
chk "camera page needs no auth"            200 "$B7/display/cams?src=driveway"
body "camera page carries the go2rtc url"  'http://localhost:1984' "$B7/display/cams?src=all"
body "camera page lists a placeholder"     '"id": "batcave", "label": "BatCave", "live": false' "$B7/display/cams?src=all"
chk "unknown camera name rejected"         422 "$B7/display/cams?src=evil"
chk "guest cannot open a camera"           403 -X POST -H "$A $G" "$B7/display/cams?cam=driveway&tv=false"
chk "no token cannot open a camera"        401 -X POST "$B7/display/cams?cam=driveway&tv=false"
chk "unknown camera rejected on open"      422 -X POST -H "$A admintok" "$B7/display/cams?cam=evil&tv=false"
chk "BatCave placeholder is not openable"  409 -X POST -H "$A admintok" "$B7/display/cams?cam=batcave&tv=false"
body "placeholder says not connected"      'not connected yet' -X POST -H "$A admintok" "$B7/display/cams?cam=batcave&tv=false"
body "admin opens the driveway camera"     '"cam":"driveway"' -X POST -H "$A admintok" "$B7/display/cams?cam=driveway&tv=false"
body "display guest opens the grid"        '"cam":"all"' -X POST -H "$A $D" "$B7/display/cams?cam=all&tv=false"
if grep -q 'display/cams?src=all' $T/osa.log; then echo "ok   Safari was asked for the grid page"; else echo "FAIL Safari never got the grid url"; fail=1; fi
body "state lists the cameras"             '"label":"Coop","live":true' -H "$A admintok" $B7/display/state
body "state shows the camera window open"  '"batcam":true' -H "$A admintok" $B7/display/state
body "close screen removes it"             '"batcam_closed":true' -X POST -H "$A admintok" $B7/display/close
body "default live camera is the driveway" '"id":"driveway","label":"Driveway","live":true' -H "$A admintok" $B/display/state

echo "-- map pins: post offices, libraries, Costco"
cat > $T/poi_test.py <<'PYEOF'
import json, sys, threading, http.server
sys.path.insert(0, sys.argv[1])
fake = {"elements": [
  {"type": "node", "lat": 42.730, "lon": -84.550, "tags": {"amenity": "post_office"}},
  {"type": "way", "center": {"lat": 42.73001, "lon": -84.55001}, "tags": {"amenity": "post_office"}},   # same place: merged
  {"type": "node", "lat": 42.800, "lon": -84.600, "tags": {"amenity": "post_office"}},
  {"type": "node", "lat": 42.740, "lon": -84.560, "tags": {"amenity": "library"}},
  {"type": "node", "lat": 42.741, "lon": -84.561, "tags": {"amenity": "library", "access": "private"}},  # skipped
  {"type": "way", "center": {"lat": 42.780, "lon": -84.480}, "tags": {"shop": "wholesale", "name": "Costco Wholesale"}},
  {"type": "node", "lat": 42.781, "lon": -84.481, "tags": {"amenity": "fuel", "brand": "Costco", "name": "Costco Gas"}},  # gas: skipped
  {"type": "node", "lat": 42.9, "lon": -84.4, "tags": {"shop": "supermarket", "name": "Meijer"}}]}   # not ours
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.last = self.path
        b = json.dumps(fake).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def log_message(self, *a): pass
srv = http.server.HTTPServer(("127.0.0.1", int(sys.argv[2])), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
import os
os.environ["TV_OVERPASS_URL"] = "http://127.0.0.1:%s/api" % sys.argv[2]
import tvserver as t
items = t.poi_job(42.73, -84.55, 50)
kinds = sorted(i["t"] for i in items)
assert kinds == ["costco", "library", "post", "post"], kinds
assert [i["t"] for i in items][:2] == ["post", "post"] and items[0]["lat"] == 42.73, items   # nearest first
assert "post_office" in srv.last and "Costco" in srv.last and "Q715583" in srv.last, srv.last
q = t.poi_query(42.73, -84.55, 50)
assert q.startswith("[out:json]") and q.count("(") >= 6
print("poi backend ok")
PYEOF
if $PY $T/poi_test.py "$HERE" 8793 > $T/poi.out 2>&1; then echo "ok   Overpass answer parsed (kinds, merged duplicates, skipped gas and private)"; else echo "FAIL pins backend"; cat $T/poi.out; fail=1; fi
cat > $T/poi_test.mjs <<'JSEOF'
import { pickPoi } from './map.mjs';
const items = [];
for (let i = 1; i <= 8; i++) items.push({ t: 'post', lat: 42.73 + i * 0.01, lon: -84.55 });    // 8 post offices going north
items.push({ t: 'costco', lat: 42.60, lon: -84.55 }, { t: 'library', lat: 42.74, lon: -84.56 });
const bounds = { contains: ([la]) => la > 42.70 && la < 42.80 };                                  // costco (42.60) is outside the picture
const got = pickPoi(items, bounds, [42.73, -84.55], 3);
const posts = got.filter(p => p.t === 'post').map(p => p.lat);
const ok = posts.length === 3 && Math.abs(posts[0] - 42.74) < 1e-9 && !got.some(p => p.t === 'costco') && got.some(p => p.t === 'library');
console.log(ok ? 'pick ok' : 'pick BAD ' + JSON.stringify(got)); process.exit(ok ? 0 : 1);
JSEOF
cp "$HERE/display/widgets/map.js" $T/map.mjs
if node $T/poi_test.mjs > $T/poipick.out 2>&1; then echo "ok   map keeps only the nearest few of each kind inside the view"; else echo "FAIL pin picking (needs node)"; cat $T/poipick.out; fail=1; fi

echo "-- camera tunnel agent"
if bash "$HERE/install-cams-tunnel.sh" --print | $PY -c "
import sys, plistlib
d = plistlib.loads(sys.stdin.buffer.read())
a = d['ProgramArguments']
assert d['Label'] == 'com.lowone.camtunnel' and d['KeepAlive'] is True and d['RunAtLoad'] is True
assert a[0] == '/usr/bin/ssh' and a[-1] == 'oracle-r' and '127.0.0.1:1984:127.0.0.1:1984' in a and 'ExitOnForwardFailure=yes' in a, a
" 2>$T/tun.err; then echo "ok   tunnel agent plist is valid (ssh -L 1984 to oracle-r, KeepAlive)"; else echo "FAIL tunnel agent plist"; cat $T/tun.err; fail=1; fi
if TV_CAMS_SSH_HOST=cam-host TV_CAMS_PORT=1999 bash "$HERE/install-cams-tunnel.sh" --print | grep -q '<string>cam-host</string>'; then echo "ok   tunnel host is configurable"; else echo "FAIL tunnel host override"; fail=1; fi

echo "-- concurrent requests (guest page and dashboard polling at once)"
pids=""; : > $T/par.txt
for i in 1 2 3 4 5 6 7 8; do
  curl -s -o /dev/null -w '%{http_code}\n' -H "$A admintok" $B/status >> $T/par.txt & pids="$pids $!"
done
wait $pids
if [ "$(grep -c '^200$' $T/par.txt)" = "8" ]; then echo "ok   8 parallel status calls all answered"; else echo "FAIL parallel calls: $(sort $T/par.txt | uniq -c | tr '\n' ' ')"; fail=1; fi

$PY "$HERE/tvserver.py" token revoke guest >/dev/null
chk "revoked guest rejected"          401 -H "$A $G" $B/status
chk "other guest unaffected"          200 -H "$A $N" $B/status
[ $fail = 0 ] && echo "ALL PASSED" || { echo "SOME FAILED"; tail -20 $T/server.log; }
exit $fail
