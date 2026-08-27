#!/usr/bin/env python3
"""End-to-end verification: a user manually adds the `demo_mcp` remote MCP.

Covers the full "user perspective" flow and the required test dimensions:

  1. 组件已部署  (demo-mcp container running; protocol reachable on agent-net)
  2. 添加与标注  (POST /api/config/mcp/demo_mcp  ->  GET /api/config/mcp shows
                   source:"user" / builtin:false, distinct from built-in MCPs)
  3. 部署到容器  (config is injected into the user's agent container at
                   /data/config/opencode/opencode.json, and the internal
                   `builtin_mcp` override key does NOT leak into the container)
  4. 正常运行    (tools/list / tools/call succeed and return expected values)

Stdlib only. Run from the repo root via the bash docker wrapper:
    bash -lc "python3 scripts/verify_user_mcp.py"
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid
import urllib.error
import urllib.request

BASE = "http://localhost:9123"
DEMO_MCP_NAME = "demo-mcp"
MCP_HOST = "http://demo-mcp:8080"
MCP_URL = MCP_HOST + "/mcp"
BACKEND_CONTAINER = "agent-docker-demo-backend-1"

PASSED = 0
FAILED = 0


def check(cond: bool, msg: str) -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  PASS  {msg}")
    else:
        FAILED += 1
        print(f"  FAIL  {msg}")


def http(method: str, path: str, body=None, token: str | None = None, timeout: int = 300):
    url = BASE + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        return exc.code, (json.loads(raw) if raw else {})


def docker(args, stdin=None, timeout=120):
    out = subprocess.run(
        ["docker"] + args,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return out.stdout, out.stderr, out.returncode


def main() -> int:
    username = f"mcpuser_{uuid.uuid4().hex[:8]}"
    password = "verify-pass-123"

    print(f"\n==> Register test user `{username}`")
    code, reg = http("POST", "/api/auth/register",
                     {"username": username, "password": password})
    check(code == 200 and reg.get("access_token"), f"register ok (role={reg.get('role')})")
    token = reg.get("access_token", "")
    user_id = reg.get("user_id", "")
    if not token:
        print("     register response:", reg)
        return 1

    print("\n==> 1. Component deployed: demo-mcp container + health")
    out, _, rc = docker(["compose", "ps", "--status", "running", "-q", "demo-mcp"],
                        timeout=60)
    check(rc == 0 and out.strip() != "", "demo-mcp container is running")

    # Reachability + protocol from a container on agent-net (backend).
    reach_src = (
        "import json,urllib.request\n"
        f"u='{MCP_HOST}/health'\n"
        "r=urllib.request.urlopen(u)\n"
        "print(json.loads(r.read().decode())['status'])\n"
    )
    out, err, rc = docker(["exec", "-i", BACKEND_CONTAINER, "python3", "-"],
                          stdin=reach_src, timeout=60)
    check(rc == 0 and out.strip() == "ok",
          f"demo-mcp /health reachable from agent-net (got '{out.strip()}')")

    print("\n==> 2. Baseline: built-in MCP is marked as built-in")
    code, before = http("GET", "/api/config/mcp", token=token)
    mcp_before = (before.get("mcp") or {})
    web = mcp_before.get("web_search", {})
    check(web.get("builtin") is True and web.get("source") == "builtin",
          f"built-in `web_search` marked source=builtin ({web.get('source')})")
    check(DEMO_MCP_NAME not in mcp_before, "demo_mcp not present before adding")

    print("\n==> 3. User manually adds demo_mcp (remote MCP)")
    code, added = http("POST", f"/api/config/mcp/{DEMO_MCP_NAME}",
                       {"type": "remote", "url": MCP_URL, "enabled": True},
                       token=token)
    check(code == 200 and added.get("status") == "ok", "POST /api/config/mcp/demo-mcp ok")
    if code != 200:
        print("     POST response:", code, added)

    code, after = http("GET", "/api/config/mcp", token=token)
    mcp_after = (after.get("mcp") or {})
    entry = mcp_after.get(DEMO_MCP_NAME, {})
    check(entry.get("source") == "user", f"demo_mcp marked source=user ('{entry.get('source')}')")
    check(entry.get("builtin") is False, f"demo_mcp builtin=false")
    check(entry.get("enabled") is True and entry.get("url") == MCP_URL,
          f"demo_mcp enabled with url {entry.get('url')}")

    print("\n==> 4. Deploy into the user's agent container")
    print("     starting agent container (wait=true, can take ~1-2 min)...")
    code, start = http("POST", "/api/agent/start", {"wait": True}, token=token, timeout=600)
    check(start.get("running") is True, f"agent started (status={start.get('status')})")
    container_name = start.get("container_name") or f"agent-{user_id}"

    # Reload explicitly so the just-added MCP is re-injected.
    code, reloaded = http("POST", "/api/config/reload", token=token)
    check(code == 200 and reloaded.get("reloaded") is True, "config reloaded into container")

    out, err, rc = docker(["exec", container_name, "cat",
                           "/data/config/opencode/opencode.json"], timeout=60)
    injected_ok = False
    leak_free = False
    if rc == 0:
        try:
            cfg = json.loads(out)
            injected = (cfg.get("mcp") or {}).get(DEMO_MCP_NAME, {})
            injected_ok = injected.get("url") == MCP_URL and injected.get("type") == "remote"
            leak_free = "builtin_mcp" not in cfg
        except json.JSONDecodeError:
            pass
    check(injected_ok, f"demo_mcp injected into {container_name}:/data/config/opencode/opencode.json")
    check(leak_free, "internal `builtin_mcp` key does NOT leak into container config")

    print("\n==> 5. Functional test: tools/list + tools/call (echo/add/get_time)")
    func_src = (
        "import json,urllib.request\n"
        f"BASE='{MCP_URL}'\n"
        "def rpc(m,p=None,i=1):\n"
        " b=json.dumps({'jsonrpc':'2.0','id':i,'method':m,'params':p or {}}).encode()\n"
        " req=urllib.request.Request(BASE,data=b,headers={'Content-Type':'application/json'})\n"
        " return json.loads(urllib.request.urlopen(req,timeout=15).read().decode())\n"
        "names=sorted(t['name'] for t in rpc('tools/list')['result']['tools'])\n"
        "assert names==['add','echo','get_time'], names\n"
        "add=rpc('tools/call',{'name':'add','arguments':{'a':2,'b':3}})\n"
        "assert add['result']['content'][0]['text'].strip()=='5', add\n"
        "echo=rpc('tools/call',{'name':'echo','arguments':{'message':'hello-user-mcp'}})\n"
        "assert echo['result']['content'][0]['text'].strip()=='hello-user-mcp', echo\n"
        "tm=rpc('tools/call',{'name':'get_time','arguments':{}})['result']['content'][0]['text']\n"
        "print('TOOLS='+','.join(names),'ADD=5','ECHO=hello-user-mcp','TIME='+tm)\n"
    )
    out, err, rc = docker(["exec", "-i", BACKEND_CONTAINER, "python3", "-"],
                          stdin=func_src, timeout=60)
    ok = rc == 0 and "ADD=5" in out and "ECHO=hello-user-mcp" in out and "TOOLS=add,echo,get_time" in out
    check(ok, "tools/list + tools/call return expected values")
    if rc != 0:
        print("     stderr:", err.strip())
    if out.strip():
        print("     " + out.strip().replace("\n", "\n     "))

    print("\n==> Cleanup: stop/remove throwaway agent container")
    docker(["rm", "-f", container_name], timeout=60)
    out, _, _ = docker(["ps", "-q", "-f", f"name={container_name}"], timeout=30)
    check(out.strip() == "", f"test container {container_name} removed")

    print(f"\n==== RESULT: {PASSED} passed, {FAILED} failed ====")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())