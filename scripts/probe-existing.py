#!/usr/bin/env python3
"""Probe through platform tunnel using the e2e user's existing container."""
import json
import time
import urllib.error
import urllib.request

BASE = "http://localhost:9123"


def call(method, path, body=None, token=None, timeout=60):
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


# Use the e2e user that already has a running container
code, body = call("POST", "/api/auth/register", {"username": "e2etest", "password": "e2e-password-123"})
if code != 200:
    code, body = call("POST", "/api/auth/login", {"username": "e2etest", "password": "e2e-password-123"})
token = body["access_token"]
print(f"auth: {code}")

# Check agent status
code, body = call("GET", "/api/agent/runtime", token=token)
print(f"runtime: {code} running={body.get('running')} healthy={body.get('healthy')}")

# Create session via tunnel
code, sess = call("POST", "/api/tunnel/oc/api/session", {
    "model": {"providerID": "GLM", "id": "glm-5.2"},
    "agent": "build",
    "location": {"directory": "/workspace"},
}, token=token)
print(f"session: {code}")
if code != 200:
    print(f"  body: {sess}")
    exit(1)
sid = sess["data"]["id"]
print(f"  id={sid}")

# Send prompt
code, r = call("POST", f"/api/tunnel/oc/api/session/{sid}/prompt",
               {"prompt": {"text": "Reply with exactly: PONG"}}, token=token)
print(f"prompt: {code}")

# Poll for assistant reply
for i in range(30):
    time.sleep(2)
    code, msgs = call("GET", f"/api/tunnel/oc/api/session/{sid}/message", token=token)
    data = msgs.get("data", []) if isinstance(msgs, dict) else []
    assistant = [m for m in data if m.get("type") == "assistant"]
    if assistant:
        for m in assistant:
            texts = [b.get("text", "") for b in m.get("content", []) if b.get("type") == "text"]
            print(f"ASSISTANT: {' '.join(texts)}")
            if m.get("error"):
                print(f"  error: {m['error']}")
        break
    print(f"  poll {i+1}: {len(data)} msgs")
else:
    print("TIMEOUT")
    # Print whatever we have
    code, msgs = call("GET", f"/api/tunnel/oc/api/session/{sid}/message", token=token)
    print(json.dumps(msgs, indent=2, default=str)[:500])
