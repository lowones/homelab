#!/bin/bash
# Self-contained check of the access rules and guest page against a fake TV.
# Uses its own temp dir and ports; never touches the real TV or prod files.
#   bash tests/smoke.sh      (needs rv-tv/.venv from install-tvserver.sh)
set -uo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
PY="$HERE/.venv/bin/python"; [ -x "$PY" ] || PY=python3
T=$(mktemp -d); MOCK_PORT=3198; API_PORT=8798
cleanup() { [ -n "${P1:-}" ] && kill $P1 2>/dev/null; [ -n "${P2:-}" ] && kill $P2 2>/dev/null; rm -rf "$T"; }
trap cleanup EXIT
openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj /CN=mock -keyout $T/mock.key -out $T/mock.crt 2>/dev/null
echo mockkey123 > $T/key.txt   # the key the fake TV accepts
MOCK_CERT_DIR=$T $PY "$HERE/tests/mocktv.py" $MOCK_PORT > $T/mock.log 2>&1 & P1=$!
export TV_ENV_FILE=$T/none.env TV_TOKENS_FILE=$T/tokens.json TV_KEYFILE=$T/key.txt \
       TV_IP=127.0.0.1 TV_WS_PORT=$MOCK_PORT TV_TOKEN=admintok TV_BIND=127.0.0.1 TV_SERVER_PORT=$API_PORT
$PY "$HERE/tvserver.py" > $T/server.log 2>&1 & P2=$!
for i in $(seq 1 30); do curl -fs http://127.0.0.1:$API_PORT/health >/dev/null 2>&1 && break; sleep 0.3; done
B=http://127.0.0.1:$API_PORT
G=$($PY "$HERE/tvserver.py" token add guest | tail -1 | sed 's/.*#//')
N=$($PY "$HERE/tvserver.py" token add nooff --no-off | tail -1 | sed 's/.*#//')
fail=0
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
$PY "$HERE/tvserver.py" token revoke guest >/dev/null
chk "revoked guest rejected"          401 -H "$A $G" $B/status
chk "other guest unaffected"          200 -H "$A $N" $B/status
[ $fail = 0 ] && echo "ALL PASSED" || { echo "SOME FAILED"; tail -20 $T/server.log; }
exit $fail
