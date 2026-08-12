#!/usr/bin/env python3
"""Test the httpx proxy path from inside the backend container."""
import asyncio
import time
import httpx


async def test():
    base = "http://agent-bf4b4934-908c-4e68-857b-f4b59fbe58b7:4096"
    pw = "zE_z1JbteMSv2fvr1p25FduyhziYIZXVTh55b6fgwCg"
    auth = ("opencode", pw)

    async with httpx.AsyncClient(auth=auth, timeout=30) as c:
        # health
        r = await c.get(f"{base}/api/health")
        print(f"health: {r.status_code} {r.text[:100]}")

        # create session
        r = await c.post(f"{base}/api/session", json={
            "model": {"providerID": "GLM", "id": "glm-5.2"},
            "agent": "build",
            "location": {"directory": "/workspace"},
        })
        print(f"session: {r.status_code}")
        if r.status_code != 200:
            print(f"  body: {r.text[:300]}")
            return
        sid = r.json()["data"]["id"]

        # prompt
        r = await c.post(
            f"{base}/api/session/{sid}/prompt",
            json={"prompt": {"text": "Reply with exactly: PONG"}},
        )
        print(f"prompt: {r.status_code}")

        # wait and get messages
        time.sleep(10)
        r = await c.get(f"{base}/api/session/{sid}/message")
        print(f"message: {r.status_code}")
        for m in r.json().get("data", []):
            if m.get("type") == "assistant":
                texts = [b.get("text", "") for b in m.get("content", []) if b.get("type") == "text"]
                print(f"ASSISTANT: {' '.join(texts)}")
            else:
                print(f"  {m.get('type')}: {str(m.get('text', ''))[:80]}")


asyncio.run(test())
