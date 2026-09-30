"""Per-user proxy in front of the shared OpenViking memory service.

Structurally the same control as :mod:`app.routers.kb_proxy`, with two
differences that follow from memory being read-write and from MCP being a
streaming protocol:

  * **Writes are allowed** — but only through an explicit tool allow-list. A
    memory service that cannot write is useless, so unlike the read-only fastk
    proxy this one forwards mutations. ``forget`` and ``cancel_watch`` are
    deliberately *not* on the list: the first destroys memories the platform
    cannot restore, the second cancels background work the platform cannot
    account for. Both are a one-line change here if that ever needs to flip.
  * **Streaming** — MCP Streamable HTTP answers with either JSON or an SSE
    stream, so the response is relayed chunk-by-chunk (see ``/llm-proxy``)
    rather than buffered like kb_proxy does.

Two upstream surfaces are exposed, because the agent talks to memory over two
channels at once:

  * ``/ov/mcp`` — the MCP tools the agent invokes deliberately, filtered by
    ``_ALLOWED_TOOLS``.
  * ``/ov/api/v1/*`` and ``/ov/health`` — the plain REST calls the OpenViking
    plugin makes *on the agent's behalf* (auto-recall, auto-capture, profile
    injection). Filtered by ``_ALLOWED_REST`` / ``_SESSION_PATH``, which list
    exactly the paths the pinned plugin version emits and nothing more.

Everything else — the Admin API, the console, the debug endpoints — is
unreachable from a container by construction, not by a deny-list that has to be
kept in sync.

One request is rewritten rather than relayed verbatim: context-mode searches
gain a fixed ``exclude_uris`` list (see ``_SCAFFOLD_URIS``). That is the only
place this module edits a body, and it exists because the plugin knob that
would normally carry it is inert in the pinned version.

The caller's own credential is an opaque ``ovproxy:<uid>`` token; the real
OpenViking User API Key is derived and injected here and never leaves the
backend.
"""
from __future__ import annotations

import json
import logging
import re

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ..config import settings
from ..services import ov_access

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ov", tags=["ov-proxy"])

# Hop-by-hop / transport headers, plus the caller's proxy token — replaced below
# with the user's real OpenViking key. accept-encoding is forced to identity so
# aiter_raw() yields bytes we can relay untouched.
_REQUEST_DROP_HEADERS = {
    "host",
    "content-length",
    "connection",
    "keep-alive",
    "te",
    "transfer-encoding",
    "upgrade",
    "proxy-authorization",
    "proxy-connection",
    "accept-encoding",
    "x-api-key",
    "authorization",
}
_RESPONSE_DROP_HEADERS = {
    "content-encoding",
    "content-length",
    "transfer-encoding",
    "connection",
    "keep-alive",
}

# The MCP tools a container may invoke. Everything else is refused with a
# JSON-RPC error so the agent sees a tool failure rather than a transport error.
_ALLOWED_TOOLS = frozenset({
    # retrieval / inspection
    "find", "search", "read", "list", "tree", "grep", "glob",
    "list_watches", "health",
    # mutation — a memory service has to be able to remember things
    "remember", "write", "edit", "add_resource", "add_skill",
})

# The REST paths @openviking/opencode-plugin@2026.9.25-2 emits, harvested from
# every path literal in the published tarball. Deliberately exact-match: the
# plugin builds these itself and never takes a path from the agent, so a wider
# prefix would only widen what a compromised container could reach.
#
# Bumping the plugin version means re-harvesting this set; a path the plugin
# needs but we do not list shows up as a logged 404, never as a silent failure.
_ALLOWED_REST = frozenset({
    "/health",                      # lib/runtime.mjs, memory-session.mjs liveness
    "/api/v1/system/status",        # profile-inject / recall-core feature probe
    "/api/v1/fs/ls",                # recall-core + profile-inject directory scan
    "/api/v1/content/read",         # recall body fetch, profile document read
    "/api/v1/skills",               # skill catalogue injection
    "/api/v1/search/search",        # recall via the context face (preferred)
    "/api/v1/search/find",          # raw retrieval fallback
    "/api/v1/search/recall",        # deprecated preset, last-resort fallback
})

# /api/v1/sessions/<sid>[/<tail>] — <sid> is percent-encoded by the plugin so it
# never contains a slash. The plugin only ever reads session state or appends to
# it; there is no DELETE or session-scoped write outside these four tails.
_SESSION_PATH = re.compile(
    r"^/api/v1/sessions/[^/]+(?:/(?:context|commit|messages(?:/batch)?))?$"
)

# Directory nodes the context assembler keeps ranking into the recall block
# ahead of actual memories. Each holds nothing but a synthesised English
# ``.overview.md`` — zero CJK characters in a deployment whose memories are
# Chinese — and with ``purpose="coding"`` the ``resources`` bucket carries the
# heaviest quota of all, so they arrive in force. Excluding all of them was
# measured on a Chinese probe space at 9 entries / 893 tokens -> 4 entries /
# 549 tokens (-38.5%) with not one real memory lost.
#
# Two properties of the server make this safe. Exclusion is *per node*, not per
# subtree (retrieve/context_assembler/gather.py), so dropping a directory's own
# overview leaves its children retrievable. And ``~`` is OpenViking's home
# alias, expanded to the caller's space at the request boundary
# (core/namespace.resolve_current_user_uri), so one list serves every user and
# this module never has to spell a user id into a URI.
#
# Excluding only some of them does not work: gather over-fetches to compensate
# for dropped nodes, so the next scaffold node simply takes the vacated slot.
# Measured — excluding the root alone left the block *larger* than excluding
# nothing, because ``~/peers`` (207 chars) moved up into ``~``'s (92 chars).
#
# The plugin's own ``recallExcludeUris`` knob would be the natural home for
# this, but in @openviking/opencode-plugin@2026.9.25-2 lib/memory-recall.mjs
# never passes ``excludeUris`` to lib/shared/recall-core.mjs, so configuring it
# is inert. Hence injecting here.
_SCAFFOLD_URIS = (
    "viking://~",
    "viking://~/peers",
    "viking://~/privacy",
    "viking://~/resources",
    "viking://~/skills",
    "viking://agent/skills",
    "viking://resources",
)

# ``exclude_uris`` is context-only (CONTEXT_ONLY_FIELDS in the server's
# routers/search.py) and a list-mode body carrying it is rejected with a 400,
# so the rewrite has to be narrow. /search/recall is a context preset with no
# ``mode`` field and accepts it unconditionally; /search/find does not have the
# field at all and is left alone.
_SCAFFOLD_PATHS = frozenset({"/api/v1/search/search", "/api/v1/search/recall"})

# MAX_EXCLUDE_URIS in the server's retrieve/context_assembler/params.py. Going
# over it is a 400, so the merged list is capped rather than trusted.
_MAX_EXCLUDE_URIS = 200

# read=None: an MCP SSE stream idles between server notifications indefinitely.
_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=60.0, pool=10.0)

# REST calls are finite but slow: recall can spend up to recallTimeoutMs (120s
# by default) waiting on server-side query expansion and rewriting. 180s stays
# above the plugin's own client timeout, so the plugin gives up first and the
# failure is attributed where the agent can see it.
_REST_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=60.0, pool=10.0)


def _error(status_code: int, message: str) -> JSONResponse:
    """Error shape the platform's other proxies use (kb_proxy._error)."""
    return JSONResponse(status_code=status_code, content={"error": {"message": message}})


def _iter_messages(body: bytes):
    """JSON-RPC messages in a request body (MCP allows batches)."""
    try:
        payload = json.loads(body)
    except ValueError:
        return
    for message in payload if isinstance(payload, list) else [payload]:
        if isinstance(message, dict):
            yield message


def _denied_tool(body: bytes) -> str | None:
    """The name of a ``tools/call`` this caller may not make, else None."""
    for message in _iter_messages(body):
        if message.get("method") != "tools/call":
            continue
        params = message.get("params")
        name = params.get("name") if isinstance(params, dict) else None
        if not isinstance(name, str) or name not in _ALLOWED_TOOLS:
            return name if isinstance(name, str) else "<unknown>"
    return None


def _deny_tool_call(body: bytes, tool: str) -> Response:
    """Refuse in JSON-RPC so the agent reads it as a failed tool call."""
    request_id = next((m["id"] for m in _iter_messages(body) if "id" in m), None)
    if request_id is None:
        return _error(403, f"记忆服务不允许调用 {tool}。")
    return JSONResponse(
        status_code=200,
        content={
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": -32601,
                "message": f"平台未开放记忆工具 {tool}。",
            },
        },
    )


async def _relay(upstream: httpx.Response, client: httpx.AsyncClient):
    """Pass the upstream body through untouched, closing both at the end."""
    try:
        async for chunk in upstream.aiter_raw():
            yield chunk
    finally:
        await upstream.aclose()
        await client.aclose()


async def _open(client: httpx.AsyncClient, method: str, url: str, headers: dict, body: bytes):
    return await client.send(
        client.build_request(method, url, headers=headers, content=body), stream=True
    )


def _rest_allowed(path: str) -> bool:
    """Is this an upstream path the pinned plugin is known to call?"""
    return path in _ALLOWED_REST or _SESSION_PATH.match(path) is not None


def _without_scaffold(path: str, body: bytes) -> bytes:
    """Add the scaffold nodes to a context-mode search body.

    Anything this does not positively recognise — another path, a body that is
    not JSON, a search that is not in context mode — is returned byte-for-byte,
    so the rewrite can only ever narrow a recall block and never reject a call.
    """
    if path not in _SCAFFOLD_PATHS:
        return body
    try:
        payload = json.loads(body)
    except ValueError:
        return body
    if not isinstance(payload, dict):
        return body
    if path == "/api/v1/search/search" and payload.get("mode") != "context":
        return body

    requested = payload.get("exclude_uris")
    merged = [uri for uri in requested if isinstance(uri, str)] if isinstance(requested, list) else []
    merged += [uri for uri in _SCAFFOLD_URIS if uri not in merged]
    # Caller's own entries first: if the cap bites, what is lost is this
    # optimisation, not an exclusion somebody asked for on purpose.
    payload["exclude_uris"] = merged[:_MAX_EXCLUDE_URIS]
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _caller_user_id(request: Request) -> str | None:
    """The user behind the caller's proxy token, or None.

    Two spellings because two different clients hold the same token: opencode's
    built-in MCP client sends the ``X-API-Key`` header we stamp into
    opencode.json, while the plugin's own REST calls go through its
    ``buildOvHeaders()``, which deliberately emits ``Authorization: Bearer``
    (lib/shared/ov-http.mjs) and never ``X-API-Key``. Accepting both here is
    what lets one derived token serve both channels.
    """
    token = request.headers.get("x-api-key")
    if not token:
        authorization = request.headers.get("authorization") or ""
        if authorization[:7].lower() == "bearer ":
            token = authorization[7:].strip()
    return ov_access.verify_proxy_token(token)


def _upstream_headers(request: Request, api_key: str) -> dict:
    """Caller headers minus the transport/proxy-token ones, plus the real key."""
    headers = {
        k: v for k, v in request.headers.items() if k.lower() not in _REQUEST_DROP_HEADERS
    }
    headers["accept-encoding"] = "identity"
    headers["X-API-Key"] = api_key
    return headers


async def _forward(client, method, url, headers, body, user_id):
    """Open the upstream request, realigning the user's key once on 401."""
    upstream = await _open(client, method, url, headers, body)
    if upstream.status_code != 401:
        return upstream
    # Our derived key was rejected: the keystore volume was reset, or
    # AGENT_SECRET_KEY changed since registration. Realign once — the body is
    # untouched and nothing has been streamed yet.
    await upstream.aclose()
    headers["X-API-Key"] = await ov_access.reauthorize(user_id)
    return await _open(client, method, url, headers, body)


async def _dispatch(request: Request, upstream_path: str, timeout: httpx.Timeout):
    """Authenticate, swap in the real key, forward, stream the answer back."""
    if not ov_access.enabled():
        return _error(503, "记忆服务未启用。")

    user_id = _caller_user_id(request)
    if user_id is None:
        return _error(401, "无效的记忆服务访问凭据，请联系管理员重建容器。")

    try:
        api_key = await ov_access.user_api_key(user_id)
    except ov_access.OvUnavailable as exc:
        logger.warning("OpenViking unavailable while resolving user %s: %s", user_id, exc)
        return _error(503, "记忆服务当前不可用，请稍后重试。")
    except ov_access.OvAdminError as exc:
        logger.error("OpenViking admin API rejected user %s: %s", user_id, exc)
        return _error(503, "记忆服务初始化失败，请联系管理员。")

    headers = _upstream_headers(request, api_key)
    url = f"{settings.openviking_url.rstrip('/')}{upstream_path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    body = await request.body()
    body = _without_scaffold(upstream_path, body)
    client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
    try:
        upstream = await _forward(client, request.method, url, headers, body, user_id)
    except httpx.HTTPError as exc:
        await client.aclose()
        logger.warning("OpenViking upstream %s failed: %s", url, exc)
        return _error(502, "记忆服务不可达，请稍后重试。")
    except (ov_access.OvUnavailable, ov_access.OvAdminError) as exc:
        await client.aclose()
        logger.error("OpenViking re-auth failed for user %s: %s", user_id, exc)
        return _error(503, "记忆服务凭据重建失败，请联系管理员。")

    passthrough = {
        k: v for k, v in upstream.headers.items() if k.lower() not in _RESPONSE_DROP_HEADERS
    }
    return StreamingResponse(
        _relay(upstream, client),
        status_code=upstream.status_code,
        headers=passthrough,
    )


@router.api_route("/mcp", methods=["GET", "POST", "DELETE"])
async def ov_mcp_proxy(request: Request):
    """Forward one MCP request to OpenViking as the authenticated user."""
    if request.method == "POST":
        body = await request.body()
        denied = _denied_tool(body)
        if denied is not None:
            logger.info("Blocked OpenViking tool %r for user %s", denied, _caller_user_id(request))
            return _deny_tool_call(body, denied)
    return await _dispatch(request, "/mcp", _TIMEOUT)


@router.get("/health")
async def ov_health_proxy(request: Request):
    """Liveness probe the plugin uses to decide whether memory is on at all."""
    return await _dispatch(request, "/health", _REST_TIMEOUT)


@router.api_route("/api/v1/{rest:path}", methods=["GET", "POST"])
async def ov_rest_proxy(request: Request, rest: str):
    """Forward one REST call the plugin makes on the agent's behalf.

    Only ``GET`` and ``POST`` are routed at all, because those are the only two
    verbs the plugin emits — a ``DELETE`` never reaches this function, FastAPI
    answers 405 first.
    """
    path = f"/api/v1/{rest}"
    if not _rest_allowed(path):
        # 404, not 403: an unlisted path should look absent, matching the
        # "unreachable by construction" promise in the module docstring. The log
        # is the loud half — it is how a plugin upgrade that adds a path gets
        # noticed instead of quietly degrading recall.
        logger.warning("Blocked OpenViking REST path %s; _ALLOWED_REST may be stale", path)
        return _error(404, "记忆服务不提供该接口。")
    return await _dispatch(request, path, _REST_TIMEOUT)
