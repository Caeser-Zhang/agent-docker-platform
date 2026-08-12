#!/usr/bin/env python3
"""Test through platform tunnel with long delays after start and session creation."""
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


code, body = call("POST", "/api/auth/register", {"username": "fresh2", "password": "pw123"})
if code != 200:
    code, body = call("POST", "/api/auth/login", {"username": "fresh2", "password": "pw123"})
token = body["access_token"]

code, body = call("POST", "/api/agent/start", {"workspace": None}, token=token, timeout=120)
print(f"start: {code}")
container = body.get("container_name", "")
print(f"container: {container}")

# Wait 15s for provider to fully initialize
print("waiting 15s for provider init...")
time.sleep(15)

# Create session
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

# Wait 10s for title generation to complete
print("waiting 10s for title gen...")
time.sleep(10)

# Send prompt
code, r = call("POST", f"/api/tunnel/oc/api/session/{sid}/prompt",
               {"prompt": {"text": "Reply with exactly: PONG"}}, token=token)
print(f"prompt: {code}")

# Poll for assistant reply
for i in range(20):
    time.sleep(3)
    code, msgs = call("GET", f"/api/tunnel/oc/api/session/{sid}/message", token=token)
    data = msgs.get("data", []) if isinstance(msgs, dict) else []
    ast = [m for m in data if m.get("type") == "assistant"]
    if ast:
        texts = [b.get("text", "") for b in ast[0].get("content", []) if b.get("type") == "text"]
        print(f"ASSISTANT: {' '.join(texts)}")
        break
    print(f"  poll {i+1}: {len(data)} msgs")
else:
    print("TIMEOUT")
    # Check container logs
    import subprocess
    r = subprocess.run(["docker", "logs", "--tail", "10", container], capture_output=True, text=True)
    print(r.stdout[-500:] if r.stdout else "")
