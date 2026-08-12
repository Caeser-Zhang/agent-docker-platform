#!/usr/bin/env python3
"""Final end-to-end probe through the platform tunnel."""
import json
import time
import urllib.error
import urllib.request

BASE = "http://localhost:9123"


def call(method, path, body=None, token=None, timeout=120):
    url = f"{BASE}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            return r.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


# 1. Auth
code, body = call("POST", "/api/auth/register", {"username": "finaltest", "password": "pw123"})
if code != 200:
    code, body = call("POST", "/api/auth/login", {"username": "finaltest", "password": "pw123"})
token = body["access_token"]
print(f"auth: {code}")

# 2. Start agent
code, body = call("POST", "/api/agent/start", {}, token=token, timeout=120)
print(f"start: {code}")
if isinstance(body, dict):
    print(f"  running={body.get('running')} msg={body.get('message')}")
else:
    print(f"  body: {body}")
if code == 422:
    print(f"  detail: {body}")
    # Try with explicit workspace
    code, body = call("POST", "/api/agent/start", {"workspace": None}, token=token, timeout=120)
    print(f"  retry: {code} {body if not isinstance(body, dict) else body.get('message')}")

# 3. Create session via tunnel
code, sess = call("POST", "/api/tunnel/oc/api/session", {
    "model": {"providerID": "GLM", "id": "glm-5.2"},
    "agent": "build",
    "location": {"directory": "/workspace"},
}, token=token)
print(f"session: {code}")
if code != 200:
    print(f"  err: {sess}")
    exit(1)
sid = sess["data"]["id"]

# 4. Send prompt
code, r = call("POST", f"/api/tunnel/oc/api/session/{sid}/prompt",
               {"prompt": {"text": "Reply with exactly: PONG"}}, token=token)
print(f"prompt: {code}")

# 5. Poll for assistant reply
for i in range(30):
    time.sleep(2)
    code, msgs = call("GET", f"/api/tunnel/oc/api/session/{sid}/message", token=token)
    data = msgs.get("data", []) if isinstance(msgs, dict) else []
    ast = [m for m in data if m.get("type") == "assistant"]
    if ast:
        texts = [b.get("text", "") for b in ast[0].get("content", []) if b.get("type") == "text"]
        print(f"ASSISTANT: {' '.join(texts)}")
        if ast[0].get("error"):
            print(f"  error: {ast[0]['error']}")
        break
    print(f"  poll {i+1}: {len(data)} msgs")
else:
    print("TIMEOUT")
    # Print whatever we have
    code, msgs = call("GET", f"/api/tunnel/oc/api/session/{sid}/message", token=token)
    print(json.dumps(msgs, indent=2, default=str)[:500])
