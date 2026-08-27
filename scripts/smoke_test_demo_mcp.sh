#!/usr/bin/env bash
# Smoke-test the demo-mcp Streamable HTTP MCP server.
#
#   bash scripts/smoke_test_demo_mcp.sh
#
# Verifies:
#   1. Component protocol function (initialize / tools/list / tools/call)
#      from inside the demo-mcp container itself.
#   2. Cross-container reachability from the backend container over agent-net
#      (the same network every user agent container joins).
set -euo pipefail

DEMO_C="agent-docker-demo-demo-mcp-1"
BACKEND_C="agent-docker-demo-backend-1"

echo "===== 1) protocol smoke test (inside demo-mcp) ====="
docker exec -i "$DEMO_C" python - <<'PY'
import json, urllib.request

def rpc(method, params=None, rid=1):
    payload = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        payload["params"] = params
    req = urllib.request.Request(
        "http://127.0.0.1:8080/mcp",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())

print("initialize:", rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "smoke", "version": "1"}}))
print("tools/list:", rpc("tools/list", rid=2))
print("tools/call add(2,3):", rpc("tools/call", {"name": "add", "arguments": {"a": 2, "b": 3}}, rid=3))
print("tools/call echo:", rpc("tools/call", {"name": "echo", "arguments": {"message": "hello-demo-mcp"}}, rid=4))
print("tools/call get_time:", rpc("tools/call", {"name": "get_time", "arguments": {}}, rid=5))
PY

echo
echo "===== 2) cross-container reachability (backend -> demo-mcp over agent-net) ====="
docker exec -i "$BACKEND_C" python - <<'PY'
import json, urllib.request
payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
req = urllib.request.Request(
    "http://demo-mcp:8080/mcp",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(req, timeout=5) as resp:
    body = json.loads(resp.read().decode())
print("reachable via http://demo-mcp:8080/mcp ->", body["result"]["tools"][0]["name"], "...")
PY

echo
echo "===== smoke test passed ====="