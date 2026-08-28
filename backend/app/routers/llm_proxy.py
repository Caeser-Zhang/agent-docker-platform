"""OpenAI-compatible LLM reverse proxy with SSE normalization.

Why this exists: some OpenAI-compatible gateways (observed on
deepseek-v4-pro behind the volces gateway) emit streaming tool-call
continuation chunks like::

    {"choices":[{"delta":{"tool_calls":[{
        "id": "", "index": 0,
        "function": {"name": "", "arguments": "{"}
    }]}}]}

opencode's @ai-sdk/openai-compatible runtime merges chunks with
``id ?? previous.id`` — an empty string is *not* nullish, so the fallback
never fires and the stream dies with::

    OpenAI Chat tool call delta is missing id or name

(Well-behaved models such as glm-5.3 simply omit id/name on continuation
chunks, which is why they work.)

Since the ai-sdk bundle is compiled inside the opencode binary, the fix
has to live on the platform side: agent containers point their provider
``baseURL`` at ``{llm_proxy_base}/{provider_id}`` (see
services/opencode_config.py) and this router forwards to the real
upstream while stripping empty-string ``id`` / ``function.name`` from
tool-call deltas — making the gateway's output equivalent to the
well-behaved format.

The upstream URL for each provider is resolved live from the mounted host
config (config/opencode.json), so config edits apply without a backend
restart. No platform auth: callers are per-user agent containers on
agent-net and must still present the upstream API key, which is forwarded
untouched.
"""
from __future__ import annotations

import json
import logging

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..database import get_db
from ..services import user_config
from ..services.opencode_config import _rewrite_loopback, load_source_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/llm-proxy", tags=["llm-proxy"])

# Hop-by-hop / transport headers we must not forward upstream. accept-encoding
# is forced to identity so aiter_raw() yields plain bytes we can rewrite.
REQUEST_DROP_HEADERS = {
    "host",
    "content-length",
    "connection",
    "transfer-encoding",
    "keep-alive",
    "te",
    "upgrade",
    "proxy-authorization",
    "proxy-connection",
    "accept-encoding",
}
# Response headers that describe the hop, not the payload. We re-chunk the
# body, so length/encoding headers from upstream would be wrong.
RESPONSE_DROP_HEADERS = {
    "content-encoding",
    "content-length",
    "transfer-encoding",
    "connection",
    "keep-alive",
}


def _upstream_base(provider_id: str) -> str | None:
    """Resolve a provider's real baseURL from the mounted host config.

    Loopback hosts are rewritten to container_host_alias so a proxy running
    on the Docker host stays reachable from the backend container.
    """
    source, _ = load_source_config()
    prov = (source.get("provider") or {}).get(provider_id)
    if not isinstance(prov, dict):
        return None
    base = (prov.get("options") or {}).get("baseURL")
    if not isinstance(base, str) or not base:
        return None
    return _rewrite_loopback(base, settings.container_host_alias)


def _normalize_tool_calls(tool_calls: list) -> bool:
    """Drop empty-string id / function.name from tool-call deltas.

    Returns True when something was removed (only then does the caller need
    to re-serialize the JSON).
    """
    changed = False
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        if tc.get("id") == "":
            tc.pop("id", None)
            changed = True
        fn = tc.get("function")
        if isinstance(fn, dict) and fn.get("name") == "":
            fn.pop("name", None)
            if not fn:
                tc.pop("function", None)
            changed = True
    return changed


def _rewrite_sse_line(line: bytes) -> bytes:
    """Normalize one SSE line; non-`data:` and non-JSON lines pass through."""
    stripped = line.strip()
    if not stripped.startswith(b"data:"):
        return line
    payload = stripped[5:].strip()
    if not payload or payload == b"[DONE]":
        return line
    try:
        obj = json.loads(payload)
    except ValueError:
        return line
    if not isinstance(obj, dict):
        return line
    changed = False
    for choice in obj.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta")
        if isinstance(delta, dict) and isinstance(delta.get("tool_calls"), list):
            changed |= _normalize_tool_calls(delta["tool_calls"])
        message = choice.get("message")
        if isinstance(message, dict) and isinstance(message.get("tool_calls"), list):
            changed |= _normalize_tool_calls(message["tool_calls"])
    if not changed:
        return line
    rewritten = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return b"data: " + rewritten.encode("utf-8")


class _SSERewriter:
    """Incremental SSE rewriter — chunks may split lines at arbitrary bytes."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> bytes:
        self._buf.extend(chunk)
        out: list[bytes] = []
        while True:
            nl = self._buf.find(b"\n")
            if nl < 0:
                break
            line = bytes(self._buf[:nl])
            del self._buf[: nl + 1]
            out.append(_rewrite_sse_line(line) + b"\n")
        return b"".join(out)

    def flush(self) -> bytes:
        if not self._buf:
            return b""
        line = bytes(self._buf)
        self._buf.clear()
        return _rewrite_sse_line(line)


async def _stream(upstream: httpx.Response, client: httpx.AsyncClient, rewrite: bool):
    try:
        if rewrite:
            rewriter = _SSERewriter()
            async for chunk in upstream.aiter_raw():
                out = rewriter.feed(chunk)
                if out:
                    yield out
            tail = rewriter.flush()
            if tail:
                yield tail
        else:
            async for chunk in upstream.aiter_raw():
                yield chunk
    finally:
        await upstream.aclose()
        await client.aclose()


async def _forward(request: Request, upstream_base: str, path: str):
    """Forward one request to ``upstream_base/{path}`` with SSE normalization."""
    url = f"{upstream_base.rstrip('/')}/{path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    headers = {
        k: v for k, v in request.headers.items() if k.lower() not in REQUEST_DROP_HEADERS
    }
    headers["accept-encoding"] = "identity"
    body = await request.body()

    # read=None: an LLM stream may idle between chunks for minutes.
    timeout = httpx.Timeout(connect=15.0, read=None, write=60.0, pool=15.0)
    client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
    try:
        req = client.build_request(request.method, url, headers=headers, content=body)
        upstream = await client.send(req, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        logger.warning("LLM proxy upstream %s failed: %s", url, exc)
        return JSONResponse(
            status_code=502,
            content={"error": {"message": f"Upstream request failed: {exc}", "type": "proxy_error"}},
        )

    passthrough = {
        k: v for k, v in upstream.headers.items() if k.lower() not in RESPONSE_DROP_HEADERS
    }
    is_sse = "text/event-stream" in (upstream.headers.get("content-type") or "")
    return StreamingResponse(
        _stream(upstream, client, rewrite=is_sse),
        status_code=upstream.status_code,
        headers=passthrough,
    )


# IMPORTANT: the ``_user`` route must be declared before the generic
# ``/{provider_id}/{path:path}`` route. Starlette matches routes in declaration
# order, and ``/{provider_id}/{path:path}`` would otherwise swallow a request
# to ``/_user/u1/p1/v1/chat`` as ``provider_id="_user"`` /
# ``path="u1/p1/v1/chat"`` — making the user-scoped route unreachable.
@router.api_route("/_user/{user_id}/{provider_id}/{path:path}", methods=["GET", "POST"])
async def proxy_user(
    user_id: str,
    provider_id: str,
    path: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Forward a user-scoped provider's requests to its DB-stored upstream.

    User providers are not part of the host config; their base URL is stored
    (encrypted) on the ``user_llm_providers`` row. ``build_container_config``
    routes them to ``/_user/{user_id}/{provider_id}`` precisely so the proxy
    can resolve that upstream here, scoped to the owning user.
    """
    base = await user_config.resolve_user_provider_base(db, user_id, provider_id)
    if not base:
        return JSONResponse(
            status_code=404,
            content={"error": {"message": f"Unknown user provider '{provider_id}' for user '{user_id}'"}},
        )
    upstream_base = _rewrite_loopback(base, settings.container_host_alias)
    return await _forward(request, upstream_base, path)


@router.api_route("/{provider_id}/{path:path}", methods=["GET", "POST"])
async def proxy(provider_id: str, path: str, request: Request):
    upstream_base = _upstream_base(provider_id)
    if not upstream_base:
        return JSONResponse(
            status_code=404,
            content={"error": {"message": f"Unknown provider '{provider_id}' — no baseURL in host config"}},
        )
    return await _forward(request, upstream_base, path)
