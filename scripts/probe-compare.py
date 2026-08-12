#!/usr/bin/env python3
"""Compare httpx vs curl for session creation on the same container."""
import asyncio
import httpx
import subprocess
import json

# Get container info
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

r = subprocess.run(
    ["docker", "inspect", "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", container],
    capture_output=True, text=True
)
ip = r.stdout.strip()

print(f"password: {pw[:8]}...")
print(f"ip: {ip}")

BODY = {
    "model": {"providerID": "GLM", "id": "glm-5.2"},
    "agent": "build",
    "location": {"directory": "/workspace"},
}


async def test_httpx(name, base):
    auth = ("opencode", pw)
    async with httpx.AsyncClient(auth=auth, timeout=30) as c:
        r = await c.post(f"{base}/api/session", json=BODY)
        sid = r.json().get("data", {}).get("id", "?") if r.status_code == 200 else "FAIL"
        print(f"{name}: {r.status_code} session={sid}")
        return sid


async def main():
    # Test 1: httpx via IP
    sid1 = await test_httpx("httpx-ip", f"http://{ip}:4096")

    # Test 2: httpx via container name (DNS)
    sid2 = await test_httpx("httpx-dns", f"http://{container}:4096")

    # Test 3: curl via docker exec (loopback)
    r = subprocess.run(
        ["docker", "exec", container, "curl", "-sS",
         "-u", f"opencode:{pw}",
         "-X", "POST", "http://127.0.0.1:4096/api/session",
         "-H", "Content-Type: application/json",
         "-d", json.dumps(BODY)],
        capture_output=True, text=True
    )
    resp = json.loads(r.stdout) if r.stdout else {}
    sid3 = resp.get("data", {}).get("id", "?") if resp.get("data") else "FAIL"
    print(f"curl-loopback: 200 session={sid3}")

    # Now send prompt to each session and check
    for name, sid in [("httpx-ip", sid1), ("httpx-dns", sid2), ("curl-loopback", sid3)]:
        if sid in ("?", "FAIL"):
            print(f"  {name}: SKIP (no session)")
            continue
        # Send prompt via httpx-ip
        async with httpx.AsyncClient(auth=("opencode", pw), timeout=30) as c:
            base = f"http://{ip}:4096"
            r = await c.post(f"{base}/api/session/{sid}/prompt",
                             json={"prompt": {"text": "Reply with exactly: PONG"}})
            print(f"  {name} prompt: {r.status_code}")

            # Wait and check
            import time
            time.sleep(8)
            r = await c.get(f"{base}/api/session/{sid}/message")
            data = r.json().get("data", [])
            ast = [m for m in data if m.get("type") == "assistant"]
            if ast:
                texts = [b.get("text", "") for b in ast[0].get("content", []) if b.get("type") == "text"]
                print(f"  {name} ASSISTANT: {' '.join(texts)}")
            else:
                print(f"  {name} NO ASSISTANT ({len(data)} msgs)")


asyncio.run(main())
