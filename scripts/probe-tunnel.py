#!/usr/bin/env python3
"""Quick probe through the platform tunnel to verify end-to-end."""
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
        return e.code, json.loads(raw) if raw else raw


# 1. Auth
code, body = call("POST", "/api/auth/register", {"username": "probe1", "password": "pw123"})
if code != 200:
    code, body = call("POST", "/api/auth/login", {"username": "probe1", "password": "pw123"})
token = body["access_token"]
print(f"auth: {code}")

# 2. Start agent (422 if already running is fine)
code, body = call("POST", "/api/agent/start", {}, token=token, timeout=60)
print(f"start: {code} running={body.get('running') if isinstance(body, dict) else '?'}")
if code not in (200, 422):
    print(f"start failed: {body}")
    exit(1)

# 3. Create session via tunnel
code, sess = call("POST", "/api/tunnel/oc/api/session", {
    "model": {"providerID": "GLM", "id": "glm-5.2"},
    "agent": "build",
    "location": {"directory": "/workspace"},
}, token=token)
sid = sess.get("data", {}).get("id")
print(f"session: {code} id={sid} model={sess.get('data', {}).get('model')}")

# 4. Send prompt
code, r = call("POST", f"/api/tunnel/oc/api/session/{sid}/prompt",
               {"prompt": {"text": "Reply with exactly: PONG"}}, token=token)
print(f"prompt: {code}")

# 5. Poll for assistant reply
for i in range(20):
    time.sleep(3)
    code, msgs = call("GET", f"/api/tunnel/oc/api/session/{sid}/message", token=token)
    data = msgs.get("data", [])
    assistant = [m for m in data if m.get("type") == "assistant"]
    if assistant:
        for m in assistant:
            texts = [b.get("text", "") for b in m.get("content", []) if b.get("type") == "text"]
            reasoning = [b.get("text", "") for b in m.get("content", []) if b.get("type") == "reasoning"]
            print(f"ASSISTANT model={m.get('model', {}).get('id')}: {' '.join(texts)}")
            if reasoning:
                print(f"  reasoning: {reasoning[0][:200]}")
            if m.get("error"):
                print(f"  error: {m['error']}")
        break
    print(f"  poll {i+1}: {len(data)} messages, no assistant yet")
else:
    print("TIMEOUT: no assistant reply after 60s")
