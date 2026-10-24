#!/usr/bin/env python3
"""Checks for reading the trip sheet as a Google service account (TV_GOOGLE_KEY). Plain python, no
network except localhost stubs; needs the `openssl` command:
    python3 tests/test_google_auth.py"""
import base64
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="rvtv-gauth-"))
KEY = TMP / "key.pem"
PUB = TMP / "pub.pem"
subprocess.run(["openssl", "genrsa", "-out", str(KEY), "2048"], check=True, capture_output=True)
subprocess.run(["openssl", "rsa", "-in", str(KEY), "-pubout", "-out", str(PUB)], check=True, capture_output=True)

tok_calls, sheet_calls, other_calls = [], [], []
STATE = {"expires_in": 3600, "token_status": 200, "sheet_status": 200, "n": 0}


def b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Stub(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, body, ctype="text/csv", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body.encode())

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        form = parse_qs(self.rfile.read(n).decode())
        tok_calls.append(form)
        if STATE["token_status"] != 200:
            return self.reply(STATE["token_status"], "{}", "application/json")
        STATE["n"] += 1
        self.reply(200, json.dumps({"access_token": "tok%d" % STATE["n"], "expires_in": STATE["expires_in"]}),
                   "application/json")

    def do_GET(self):
        auth = self.headers.get("Authorization")
        if self.path.startswith("/sheet"):
            sheet_calls.append(auth)
            if STATE["sheet_status"] != 200:
                return self.reply(STATE["sheet_status"], "no")
            return self.reply(200, "W,DATE\n1,10/24/2026\n")
        if self.path.startswith("/bounce"):          # redirects to the *other* host (127.0.0.2)
            return self.reply(302, "", extra={"Location": "http://127.0.0.2:%d/landed" % self.server.server_port})
        if self.path.startswith("/landed"):
            other_calls.append(auth)
            return self.reply(200, "W,DATE\n")
        self.reply(404, "")


srv = HTTPServer(("127.0.0.1", 0), Stub)
srv2 = None
threading.Thread(target=srv.serve_forever, daemon=True).start()
PORT = srv.server_port
try:                                               # a second loopback address, to stand in for "not Google"
    srv2 = HTTPServer(("127.0.0.2", PORT), Stub)
    threading.Thread(target=srv2.serve_forever, daemon=True).start()
except OSError:
    srv2 = None

SA = TMP / "sa.json"
SA.write_text(json.dumps({"client_email": "tv@proj.iam.gserviceaccount.com", "private_key": KEY.read_text(),
                          "token_uri": "http://127.0.0.1:%d/token" % PORT}))
os.environ.update(TV_TOKEN="t", TV_ENV_FILE=str(TMP / "none.env"), TV_LOCATION_FILE=str(TMP / "location.raw"),
                  TV_TOKENS_FILE=str(TMP / "tokens.json"), TV_ITINERARY="", TV_GOOGLE_KEY=str(SA))
spec = importlib.util.spec_from_file_location("tvserver", ROOT / "tvserver.py")
tv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tv)
tv.GOOGLE_HOST_SUFFIXES = (".127.0.0.1", "127.0.0.1")        # the stub stands in for Google

fails = []


def check(name, ok, extra=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else "  " + str(extra)))
    if not ok:
        fails.append(name)


tv_real = tv.GOOGLE_HOST_SUFFIXES
tv.GOOGLE_HOST_SUFFIXES = (".google.com", ".googleusercontent.com", ".googleapis.com")
check("docs.google.com is Google", tv.google_host("https://docs.google.com/spreadsheets/d/x/export?format=csv"))
check("a doc-xx.googleusercontent.com redirect is Google", tv.google_host("https://doc-0g.googleusercontent.com/x"))
check("www.googleapis.com is Google", tv.google_host("https://www.googleapis.com/drive/v3/files"))
check("evilgoogle.com is not Google", not tv.google_host("https://evilgoogle.com/x"))
check("google.com.evil.net is not Google", not tv.google_host("https://google.com.evil.net/x"))
check("overpass is not Google", not tv.google_host("https://overpass.openstreetmap.fr/api/interpreter"))
check("plain 127.0.0.1 is not Google", not tv.google_host("http://127.0.0.1:1/x"))
tv.GOOGLE_HOST_SUFFIXES = tv_real

# ---- token: signed JWT, trade, cache -------------------------------------------------------------
t = tv.google_token()
check("a token comes back", t == "tok1", t)
form = tok_calls[0]
check("grant type is the JWT bearer one", form["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"])
head, claims, sig = form["assertion"][0].split(".")
check("header is RS256", json.loads(b64d(head)) == {"alg": "RS256", "typ": "JWT"})
c = json.loads(b64d(claims))
check("claims: issuer, scope, audience, one hour", c["iss"] == "tv@proj.iam.gserviceaccount.com"
      and c["scope"] == tv.GOOGLE_SCOPE and c["aud"] == "http://127.0.0.1:%d/token" % PORT and c["exp"] - c["iat"] == 3600, c)
(TMP / "sig.bin").write_bytes(b64d(sig))
(TMP / "data.txt").write_bytes((head + "." + claims).encode())
v = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(PUB), "-signature", str(TMP / "sig.bin"), str(TMP / "data.txt")],
                   capture_output=True)
check("the signature verifies with the public key", v.returncode == 0, v.stdout + v.stderr)
check("a second call reuses the cached token", tv.google_token() == "tok1" and len(tok_calls) == 1)
tv._gtoken["exp"] = time.time() + 100                       # inside the 5-minute margin
check("a token about to expire is replaced", tv.google_token() == "tok2" and len(tok_calls) == 2)

# ---- header goes only to Google ------------------------------------------------------------------
tv.GOOGLE_HOST_SUFFIXES = ("127.0.0.1",)
text = tv.read_source("http://127.0.0.1:%d/sheet" % PORT)
check("sheet read works and sends the Bearer token", text.startswith("W,DATE") and sheet_calls[-1] == "Bearer tok2", sheet_calls)
if srv2:
    other_calls.clear()
    text = tv.read_source("http://127.0.0.1:%d/bounce" % PORT)
    check("a redirect to a non-Google host drops the token", text.startswith("W,DATE") and other_calls == [None], other_calls)
    tv.GOOGLE_HOST_SUFFIXES = ("127.0.0.1", "127.0.0.2")
    other_calls.clear()
    tv.read_source("http://127.0.0.1:%d/bounce" % PORT)
    check("a redirect that stays on Google keeps it", other_calls == ["Bearer tok2"], other_calls)
    tv.GOOGLE_HOST_SUFFIXES = ("127.0.0.1",)
else:
    print("skip redirect checks (no 127.0.0.2)")
sheet_calls.clear()
tv.GOOGLE_HOST_SUFFIXES = (".google.com",)
tv.read_source("http://127.0.0.1:%d/sheet" % PORT)
check("a non-Google host never gets the token", sheet_calls == [None], sheet_calls)

# ---- errors --------------------------------------------------------------------------------------
tv.GOOGLE_HOST_SUFFIXES = ("127.0.0.1",)
STATE["sheet_status"] = 403
try:
    tv.read_source("http://127.0.0.1:%d/sheet" % PORT)
    check("403 raises", False)
except ValueError as e:
    check("403 says to share the sheet with the account", "tv@proj.iam.gserviceaccount.com" in str(e) and "Viewer" in str(e), e)
STATE["sheet_status"] = 200
tv._gtoken.update(token=None, exp=0.0)
STATE["token_status"] = 400
try:
    tv.google_token()
    check("a refused login raises", False)
except ValueError as e:
    check("a refused login says so, without secrets", "refused" in str(e) and "BEGIN" not in str(e), e)
STATE["token_status"] = 200
tv.GOOGLE_KEY_FILE = str(TMP / "missing.json")
try:
    tv.google_token()
    check("a missing key file raises", False)
except ValueError as e:
    check("a missing key file names the file", "missing.json" in str(e), e)

# ---- off by default ------------------------------------------------------------------------------
tv.GOOGLE_KEY_FILE = ""
tok_calls.clear()
sheet_calls.clear()
check("no key set: no token", tv.google_token() is None and not tok_calls)
tv.read_source("http://127.0.0.1:%d/sheet" % PORT)
check("no key set: no Authorization header", sheet_calls == [None] and not tok_calls, sheet_calls)
f = TMP / "local.csv"
f.write_text("a,b\n")
check("local files still read", tv.read_source(str(f)) == "a,b\n")

shutil.rmtree(TMP, ignore_errors=True)
print("\nSOME FAILED" if fails else "\nALL OK")
sys.exit(1 if fails else 0)
