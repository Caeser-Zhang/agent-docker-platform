"""End-to-end check of the backend fastk proxy routes (run inside backend)."""
import json
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://localhost:8000"


def call(method, path, body=None, token=None):
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(req, data, timeout=15) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


s, b, _ = call("POST", "/api/auth/register", {"username": "e2etest", "password": "e2etest123"})
print("register:", s)
s, b, _ = call("POST", "/api/auth/login", {"username": "e2etest", "password": "e2etest123"})
tok = json.loads(b).get("access_token") if s == 200 else None
print("login:", s, "token:", bool(tok))
if not tok:
    raise SystemExit(1)

s, b, _ = call("GET", "/api/fastk/chunk?db=fastdb&chunk_id=fb62184133c0c818", token=tok)
print("chunk:", s, b[:300].decode("utf-8", "replace"))

s, b, h = call("GET", "/api/fastk/chunk-image?db=vl_test&chunk_id=4b632f199a038522", token=tok)
print("image:", s, h.get("Content-Type"), len(b), "bytes")

s, b, _ = call("GET", "/api/fastk/chunk?db=fastdb&chunk_id=nonexistent0000", token=tok)
print("chunk-404:", s, b[:120].decode("utf-8", "replace"))

s, b, _ = call("GET", "/api/fastk/chunk?db=" + urllib.parse.quote("bad name!") + "&chunk_id=x", token=tok)
print("bad-db:", s, b[:120].decode("utf-8", "replace"))
