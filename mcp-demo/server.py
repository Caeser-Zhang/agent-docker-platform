#!/usr/bin/env python3
"""demo-mcp — a minimal Streamable HTTP MCP server (Python stdlib only).

Exposes three deterministic, side-effect-free tools (echo / add / get_time) so
the MCP's deployment, tool listing and tool invocation can be verified
end-to-end. It is meant to be added by a user as a *remote* MCP server:

    {
      "name": "demo_mcp",
      "type": "remote",
      "url": "http://demo-mcp:8080/mcp",
      "enabled": true
    }

Protocol: MCP Streamable HTTP — JSON-RPC 2.0 over HTTP POST at /mcp, returning
a JSON (non-streaming) response body. Stateless: no session header is used.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERVER_NAME = "demo-mcp"
SERVER_VERSION = "1.0.0"
SUPPORTED_PROTOCOL = "2025-03-26"
LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 8080

TOOLS = [
    {
        "name": "echo",
        "description": "Echo the provided message back verbatim.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "The message to echo."},
            },
            "required": ["message"],
        },
    },
    {
        "name": "add",
        "description": "Add two numbers and return the sum.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "a": {"type": "number", "description": "First operand."},
                "b": {"type": "number", "description": "Second operand."},
            },
            "required": ["a", "b"],
        },
    },
    {
        "name": "get_time",
        "description": "Return the current UTC time in ISO 8601 format.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def call_tool(name: str, args: dict) -> str:
    """Execute a single tool and return its text result."""
    if name == "echo":
        message = args.get("message")
        if message is None:
            raise ValueError("'message' is required")
        return str(message)
    if name == "add":
        try:
            return str(args["a"] + args["b"])
        except (KeyError, TypeError) as exc:
            raise ValueError("'a' and 'b' are required numbers") from exc
    if name == "get_time":
        return datetime.now(timezone.utc).isoformat()
    raise ValueError(f"Unknown tool: {name!r}")


def dispatch(request: dict):
    """Return a JSON-RPC result (or None for notifications) for a request."""
    method = request.get("method")
    params = request.get("params") or {}

    if method == "initialize":
        return {
            "protocolVersion": params.get("protocolVersion") or SUPPORTED_PROTOCOL,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            raise ValueError("'arguments' must be an object")
        try:
            return {"content": [{"type": "text", "text": call_tool(name, args)}]}
        except ValueError as exc:
            return {
                "content": [{"type": "text", "text": str(exc)}],
                "isError": True,
            }
    if isinstance(method, str) and method.startswith("notifications/"):
        return None  # notifications produce no response
    raise ValueError(f"Method not found: {method!r}")


class Handler(BaseHTTPRequestHandler):
    server_version = f"{SERVER_NAME}/{SERVER_VERSION}"

    def log_message(self, fmt: str, *args) -> None:  # noqa: D102
        # Keep logs concise; prefix with client address for debugging.
        print(f"[demo-mcp] {self.client_address[0]} {fmt % args}", flush=True)

    def do_GET(self) -> None:  # noqa: D102
        if self.path == "/health":
            self._send_json(200, {"status": "ok", "server": SERVER_NAME})
        else:
            self._send_json(405, {"error": "method not allowed"})

    def do_POST(self) -> None:  # noqa: D102
        if self.path != "/mcp":
            self._send_json(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            request = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(400, {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            })
            return

        if not isinstance(request, dict):
            self._send_json(400, {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Invalid Request"},
            })
            return

        request_id = request.get("id")
        try:
            result = dispatch(request)
        except ValueError as exc:
            self._send_json(200, {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": str(exc)},
            })
            return

        if result is None:
            # Notification (no id) — 202 Accepted with an empty body.
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        self._send_json(200, {"jsonrpc": "2.0", "id": request_id, "result": result})

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    print(f"[demo-mcp] listening on http://{LISTEN_HOST}:{LISTEN_PORT}/mcp", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())