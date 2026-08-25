#!/usr/bin/env python3
"""web_search MCP server — Python stdlib only, no third-party dependencies.

Baked into the agent image at /opt/agent/builtin-mcp/web_search/web_search_mcp.py
and spawned by `opencode serve` as a local MCP server. The SearXNG base URL is
injected by the platform through the `environment` block of the mcp config, so
every user container gets working web search with zero user configuration:

    "mcp": {
      "web_search": {
        "type": "local",
        "command": ["python3", "/opt/agent/builtin-mcp/web_search/web_search_mcp.py"],
        "environment": {"SEARXNG_URL": "http://searxng:8080"},
        "enabled": true
      }
    }

opencode registers MCP tools with the server name as a prefix, so the agent
sees the tool as `web_search_search`. The tool queries SearXNG's JSON API
(format=json must be enabled in its settings — see searxng/settings.yml).

Protocol: MCP over stdio — newline-delimited JSON-RPC 2.0 messages.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

SERVER_NAME = "web-search-mcp"
SERVER_VERSION = "1.0.0"
SUPPORTED_PROTOCOL = "2024-11-05"

DEFAULT_SEARXNG_URL = "http://searxng:8080"
SEARCH_TIMEOUT = 30  # seconds — SearXNG fans out to many upstream engines
MAX_RESULTS = 10
USER_AGENT = f"{SERVER_NAME}/{SERVER_VERSION} (opencode agent container)"

SEARCH_TOOL = {
    "name": "search",
    "title": "Web search",
    "description": (
        "Search the public web using the platform's SearXNG meta-search "
        "engine. Returns up to 10 ranked results with title, URL and a "
        "content snippet. Use this whenever you need fresh information, "
        "current events, or facts you are not sure about."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query.",
            },
            "pageno": {
                "type": "integer",
                "description": "Result page number, 1-based. Default 1.",
                "minimum": 1,
            },
            "language": {
                "type": "string",
                "description": (
                    "Preferred result language, e.g. 'zh-CN', 'en-US' or "
                    "'all'. Optional."
                ),
            },
            "time_range": {
                "type": "string",
                "enum": ["day", "week", "month", "year"],
                "description": "Restrict results by age. Optional.",
            },
        },
        "required": ["query"],
    },
}


def searxng_url() -> str:
    return os.environ.get("SEARXNG_URL", DEFAULT_SEARXNG_URL).rstrip("/")


# ----------------------------------------------------------------------
# JSON-RPC plumbing
# ----------------------------------------------------------------------

def _send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _reply(request_id, result) -> None:
    _send({"jsonrpc": "2.0", "id": request_id, "result": result})


def _reply_error(request_id, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": request_id,
           "error": {"code": code, "message": message}})


# ----------------------------------------------------------------------
# Tool implementation
# ----------------------------------------------------------------------

def _one_line(value) -> str:
    return " ".join(str(value or "").split())


def _do_search(args: dict) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError("'query' is required and must not be empty")

    params = {"q": query, "format": "json"}
    try:
        pageno = max(1, int(args.get("pageno") or 1))
    except (TypeError, ValueError):
        pageno = 1
    params["pageno"] = pageno
    if args.get("language"):
        params["language"] = str(args["language"])
    if args.get("time_range"):
        params["time_range"] = str(args["time_range"])

    url = f"{searxng_url()}/search?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=SEARCH_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        hint = ""
        if exc.code == 403:
            hint = " (SearXNG rejected the request — is 'json' enabled in its search formats?)"
        elif exc.code == 429:
            hint = " (rate limited — retry in a moment)"
        raise RuntimeError(f"SearXNG returned HTTP {exc.code}{hint}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Cannot reach SearXNG at {searxng_url()}: {exc.reason}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("SearXNG returned invalid JSON") from exc

    lines: list[str] = []
    for answer in data.get("answers") or []:
        answer = _one_line(answer)
        if answer:
            lines.append(f"Answer: {answer}")
            lines.append("")

    shown = 0
    for result in data.get("results") or []:
        if not isinstance(result, dict):
            continue
        link = _one_line(result.get("url"))
        if not link:
            continue
        shown += 1
        lines.append(f"{shown}. {_one_line(result.get('title')) or link}")
        lines.append(f"   {link}")
        snippet = _one_line(result.get("content"))
        if snippet:
            lines.append(f"   {snippet}")
        lines.append("")
        if shown >= MAX_RESULTS:
            break

    if not shown and not lines:
        return f"No results found for: {query}"
    if pageno > 1:
        lines.append(f"(page {pageno})")
    return "\n".join(lines).strip()


def _call_tool(params: dict) -> dict:
    name = params.get("name")
    if name != "search":
        raise ValueError(f"Unknown tool: {name!r}")
    args = params.get("arguments") or {}
    if not isinstance(args, dict):
        raise ValueError("'arguments' must be an object")
    text = _do_search(args)
    return {"content": [{"type": "text", "text": text}]}


# ----------------------------------------------------------------------
# Request dispatch
# ----------------------------------------------------------------------

def _handle(request: dict) -> None:
    method = request.get("method")
    request_id = request.get("id")
    params = request.get("params") or {}

    if method == "initialize":
        # Echo the client's protocol version when possible — a minimal
        # tools-only server is compatible across revisions.
        _reply(request_id, {
            "protocolVersion": params.get("protocolVersion") or SUPPORTED_PROTOCOL,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    elif method == "ping":
        _reply(request_id, {})
    elif method == "tools/list":
        _reply(request_id, {"tools": [SEARCH_TOOL]})
    elif method == "tools/call":
        try:
            _reply(request_id, _call_tool(params))
        except Exception as exc:  # noqa: BLE001 - tool failure, not protocol failure
            sys.stderr.write(f"search failed: {exc}\n")
            _reply(request_id, {
                "content": [{"type": "text", "text": f"Search failed: {exc}"}],
                "isError": True,
            })
    elif request_id is None:
        pass  # notification (initialized, cancelled, ...) — nothing to send
    else:
        _reply_error(request_id, -32601, f"Method not found: {method}")


def main() -> int:
    while True:
        line = sys.stdin.readline()
        if not line:  # EOF — client went away
            return 0
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            sys.stderr.write(f"invalid JSON-RPC message: {exc}\n")
            continue
        if not isinstance(request, dict):
            continue
        try:
            _handle(request)
        except BrokenPipeError:
            return 0
        except Exception as exc:  # noqa: BLE001 - never crash the protocol loop
            sys.stderr.write(f"error handling {request.get('method')}: {exc}\n")
            if request.get("id") is not None:
                try:
                    _reply_error(request["id"], -32603, f"Internal error: {exc}")
                except BrokenPipeError:
                    return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())