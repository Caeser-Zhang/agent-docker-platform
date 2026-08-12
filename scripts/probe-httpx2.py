#!/usr/bin/env python3
"""Test creating a session via httpx from the backend container, targeting a freshly started agent."""
import asyncio
import time
import httpx
import json

# Target the e2e user's container
BASE = "http://agent-c3931c77-f897-49df-854c-5aea6f4f233a:4096"
PW = "AQvqMb3sfeH8qJ-9NfWQkGUdNG1mI7m8kkaSXslnmQ4"  # wrong - need to get from env


async def get_pw():
    """Get the password from the container's env via Docker SDK."""
    import subprocess
    r = subprocess.run(
        ["docker", "inspect", "--format", "{{json .Config.Env}}",
         "agent-c3931c77-f897-49df-854c-5aea6f4f233a"],
        capture_output=True, text=True
    )
    for line in r.stdout.split(","):
        if "OPENCODE_SERVER_PASSWORD=" in line:
            return line.split("=", 1)[1].strip(' "\n')
    return ""


async def test():
    pw = await get_pw()
    print(f"password: {pw[:8]}...")
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
