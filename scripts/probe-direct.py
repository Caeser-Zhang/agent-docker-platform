#!/usr/bin/env python3
"""Test creating a session via httpx from the WSL host, targeting the agent container directly."""
import asyncio
import time
import httpx
import subprocess

# Get the container name and password
r = subprocess.run(
    ["docker", "ps", "--filter", "label=managed-by=agent-platform", "--format", "{{.Names}}"],
    capture_output=True, text=True
)
container = r.stdout.strip().split("\n")[0]
print(f"container: {container}")

r = subprocess.run(
    ["docker", "inspect", "--format", "{{json .Config.Env}}", container],
    capture_output=True, text=True
)
pw = ""
for line in r.stdout.split(","):
    if "OPENCODE_SERVER_PASSWORD=" in line:
        pw = line.split("=", 1)[1].strip(' "\n')
        break
print(f"password: {pw[:8]}...")

# Get the container IP on agent-net
r = subprocess.run(
    ["docker", "inspect", "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", container],
    capture_output=True, text=True
)
ip = r.stdout.strip()
print(f"ip: {ip}")
BASE = f"http://{ip}:4096"


async def test():
    auth = ("opencode", pw)
    async with httpx.AsyncClient(auth=auth, timeout=30) as c:
        # health
        r = await c.get(f"{BASE}/api/health")
        print(f"health: {r.status_code} {r.text[:100]}")

        # create session
        body = {
            "model": {"providerID": "GLM", "id": "glm-5.2"},
            "agent": "build",
            "location": {"directory": "/workspace"},
        }
        r = await c.post(f"{BASE}/api/session", json=body)
        print(f"session: {r.status_code}")
        if r.status_code != 200:
            print(f"  body: {r.text[:300]}")
            return
        sid = r.json()["data"]["id"]
        print(f"  id={sid}")

        # prompt
        r = await c.post(
            f"{BASE}/api/session/{sid}/prompt",
            json={"prompt": {"text": "Reply with exactly: PONG"}},
        )
        print(f"prompt: {r.status_code}")

        # poll
        for i in range(15):
            time.sleep(3)
            r = await c.get(f"{BASE}/api/session/{sid}/message")
            data = r.json().get("data", [])
            ast = [m for m in data if m.get("type") == "assistant"]
            if ast:
                texts = [b.get("text", "") for b in ast[0].get("content", []) if b.get("type") == "text"]
                print(f"ASSISTANT: {' '.join(texts)}")
                break
            print(f"  poll {i+1}: {len(data)} msgs")
        else:
            print("TIMEOUT")


asyncio.run(test())
