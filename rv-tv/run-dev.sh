#!/bin/bash
# Dev instance of the TV service: same code as prod, port 8781, its own token and
# guest list, never touches prod's files. Ctrl-C to stop.
#   bash run-dev.sh          dev against the REAL TV (uses prod's pairing key, read-only)
#   bash run-dev.sh mock     dev against a fake TV (nothing on the real TV changes)
set -euo pipefail
cd "$(dirname "$0")"
[ -d .venv ] || { echo "no .venv: run install-tvserver.sh first"; exit 1; }
PY=.venv/bin/python
mkdir -p .run
export TV_ENV_FILE="$PWD/tv.dev.env" TV_TOKENS_FILE="$PWD/tokens.dev.json"
export TV_PREPMOVE=off   # a dev copy must never quit apps or stop services (Tools > Prep for move)
if [ ! -f tv.dev.env ]; then
  echo "TV_TOKEN=devtoken-$(openssl rand -hex 8)" > tv.dev.env
  echo "TV_BIND=127.0.0.1" >> tv.dev.env
  echo "TV_SERVER_PORT=8781" >> tv.dev.env
  chmod 600 tv.dev.env
fi
if [ "${1:-}" = "mock" ]; then
  [ -f .run/mock.crt ] || openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj /CN=mock \
      -keyout .run/mock.key -out .run/mock.crt 2>/dev/null
  MOCK_CERT_DIR="$PWD/.run" $PY tests/mocktv.py 3099 > .run/mock.log 2>&1 &
  MOCK=$!
  trap 'kill $MOCK 2>/dev/null' EXIT
  [ -f .run/tv_key_mock.txt ] || echo mockkey123 > .run/tv_key_mock.txt
  export TV_IP=127.0.0.1 TV_WS_PORT=3099 TV_KEYFILE="$PWD/.run/tv_key_mock.txt"
  echo "Dev + MOCK TV on http://127.0.0.1:8781"
else
  export TV_KEYFILE="$PWD/tv_key.txt"
  echo "Dev + REAL TV on http://127.0.0.1:8781"
fi
echo "Dev token: $(grep ^TV_TOKEN tv.dev.env | cut -d= -f2)"
echo "Add a test guest: TV_ENV_FILE=$PWD/tv.dev.env TV_TOKENS_FILE=$PWD/tokens.dev.json $PY tvserver.py token add test"
$PY tvserver.py
