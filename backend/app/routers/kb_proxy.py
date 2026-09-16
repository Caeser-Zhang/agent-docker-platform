"""fastk proxy — the whitelist enforcement point for agent containers.

Agent containers hold no real API key. They get ``FASTK_API_KEY`` = an opaque
Fernet token encoding their own user id (see :mod:`app.services.kb_access`) and
``FASTDB_BASE_URL`` = this backend, so the built-in read-only fastk CLI talks
here instead of the fastk server directly. Every request is then:

  1. authenticated by token → user_id
  2. authorised against the knowledge-domain model for the target database
     (the catalog branch instead FILTERS the server's listing down to the
     databases the user's domains cover)
  3. forwarded with a platform credential injected — the owning domain's key,
     or the catalog key (``AGENT_KB_CATALOG_KEY``) for the listing

Two invariants matter more than anything else in this file:

  * The caller's ``X-API-Key`` is **always dropped** before forwarding. It is a
    platform-internal token; leaking it upstream is meaningless today and would
    be rejected outright once the server turns on its own key validation.
  * Only reads pass. GET is open (every server GET is a read); POST is limited
    to the three search endpoints the CLI uses. Anything that could mutate a
    knowledge base is refused here rather than left to the container's goodwill.
"""
from __future__ import annotations

import logging
import re

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..database import get_db
from ..services import kb_access

logger = logging.getLogger(__name__)

router = APIRouter(tags=["kb-proxy"])

# Hop-by-hop headers plus the caller's credential — never forwarded upstream.
_DROP_HEADERS = {
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
}
_RESPONSE_DROP_HEADERS = {"content-encoding", "content-length", "transfer-encoding", "connection", "keep-alive"}

# The only POST bodies the read-only CLI ever sends (see
# agent-image/builtin-tools/fastk-cli/fastk). Anything else that accepts POST
# on the server mutates an index.
_READ_POST_PATHS = ("/search", "/query", "/grep")

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=60.0, pool=10.0)


def _error(status_code: int, message: str) -> JSONResponse:
    """Error shape the CLI understands — it prints ``error.message`` verbatim."""
    return JSONResponse(status_code=status_code, content={"error": {"message": message}})


def _upstream_url(request: Request) -> str:
    """Rebuild the upstream URL for this request.

    The path portion is already validated before forwarding (a known endpoint
    word plus a database name from ``_NAME_RE``), so nothing in it needs
    escaping. The query string — where filter expressions live — is taken from
    ``request.url.query`` verbatim, byte for byte, rather than re-encoded.
    """
    raw = request.scope["path"]
    prefix = "/fastk/api"
    path = raw[len(prefix):] if raw.startswith(prefix) else raw
    url = f"{settings.fastk_server_url.rstrip('/')}{prefix}{path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    return url


def _target_db(path: str) -> str | None:
    """Physical database name a decoded proxy path addresses, if any."""
    segments = [s for s in path.split("/") if s]
    if len(segments) >= 2 and segments[0] == "databases":
        return segments[1]
    return None


def _is_catalog(path: str) -> bool:
    return path.rstrip("/") in ("databases", "")


async def _filtered_catalog(client: httpx.AsyncClient, allowed: set[str]) -> Response:
    """Fetch the server's database listing and keep only the granted entries.

    Filtering (rather than rebuilding the list) preserves the server's own
    metadata — description, model, dimension — which is exactly what the agent
    needs to pick the right database. ``uri`` is dropped: it is a host-side
    storage path with no meaning inside a container and no business leaking.

    The listing is read with the platform-wide catalog key
    (``kb_access.catalog_headers``), never with a per-database key and never
    with the caller's proxy token. Filtering stays a discovery convenience
    rather than a confidentiality boundary — database names are enumerable
    from the server by anyone who can reach it.
    """
    resp = await client.get(
        f"{settings.fastk_server_url.rstrip('/')}/fastk/api/databases/",
        headers=kb_access.catalog_headers(),
    )
    if resp.status_code != 200:
        # Passed through verbatim (the CLI prints it), but logged too: a 401
        # here means AGENT_KB_CATALOG_KEY is missing or stale, which would
        # otherwise look exactly like "this user has no databases".
        logger.warning("kb-proxy: upstream catalog listing returned %d", resp.status_code)
        return Response(content=resp.content, status_code=resp.status_code,
                        media_type=resp.headers.get("content-type", "application/json"))
    entries = resp.json()
    kept = [
        {k: v for k, v in entry.items() if k != "uri"}
        for entry in entries
        if isinstance(entry, dict) and entry.get("name") in allowed
    ]
    return JSONResponse(status_code=200, content=kept)


@router.api_route("/fastk/api/{path:path}", methods=["GET", "POST"])
async def kb_proxy(path: str, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = kb_access.verify_proxy_token(request.headers.get("x-api-key"))
    if user_id is None:
        return _error(401, "无效的知识库访问凭据，请联系管理员重建容器。")

    if request.method == "POST" and not path.rstrip("/").endswith(_READ_POST_PATHS):
        return _error(405, "知识库为只读访问，不支持该操作。")

    headers = {k: v for k, v in request.headers.items() if k.lower() not in _DROP_HEADERS}
    body = await request.body()

    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
        try:
            if _is_catalog(path):
                allowed = set(await kb_access.granted_kbs(db, user_id))
                response = await _filtered_catalog(client, allowed)
                logger.info("kb-proxy user=%s catalog granted=%d", user_id, len(allowed))
                return response

            kb_name = _target_db(path)
            if kb_name is None or not _NAME_RE.fullmatch(kb_name):
                return _error(404, f"未知的知识库端点：/{path}")

            access = await kb_access.resolve_access(db, kb_name, user_id)
            if not access.granted:
                logger.info("kb-proxy user=%s db=%s %s -> 403", user_id, kb_name, path)
                return _error(403, kb_access.denial_message(kb_name))
            if access.api_key is None:
                logger.error("kb-proxy: database '%s' has no usable credential", kb_name)
                return _error(500, f"知识库 '{kb_name}' 缺少可用凭据，请联系管理员{settings.kb_admin_contact}。")

            headers["X-API-Key"] = access.api_key
            upstream = await client.request(
                request.method, _upstream_url(request), headers=headers, content=body
            )
        except httpx.HTTPError as exc:
            logger.warning("kb-proxy upstream failed for user=%s path=%s: %s", user_id, path, exc)
            return _error(502, "fastk 服务不可达")

    logger.info(
        "kb-proxy user=%s db=%s %s %s -> %d",
        user_id, kb_name, request.method, path, upstream.status_code,
    )
    passthrough = {
        k: v for k, v in upstream.headers.items() if k.lower() not in _RESPONSE_DROP_HEADERS
    }
    return Response(content=upstream.content, status_code=upstream.status_code, headers=passthrough)
